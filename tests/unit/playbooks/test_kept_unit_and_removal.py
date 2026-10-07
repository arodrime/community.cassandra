from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A node's own unit kept as found (cassandra_service_unit_manage: false) may
# not drain on stop: stop_rack and the restarts drain it with nodetool first.
# remove_dead_node asks every node whether it is removing the dead one.
# The expressions are read from the playbooks and tasks, rendered by Ansible.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
TASKS = os.path.join(TOP, "roles", "cassandra_service", "tasks")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def true(conditions, **variables):
    conditions = conditions if isinstance(conditions, list) else [conditions]
    return all(render("{{ %s }}" % c, **variables) for c in conditions)


STOP_PLAY = next(p for p in load("playbooks", "stop_rack.yml") if p.get("name") == "Stop the rack")
DRAIN = next(t for t in STOP_PLAY["tasks"] if t.get("name") == "Drain the node")


def test_stop_rack_drains_before_stopping():
    names = [t["name"] for t in STOP_PLAY["tasks"]]
    assert names.index("Drain the node") < names.index("Stop Cassandra")
    assert "drained by its unit" not in open(os.path.join(TOP, "playbooks", "stop_rack.yml"), encoding="utf-8").read()


@pytest.mark.parametrize("operation_drain, unit_manage, drained", [
    (True, True, True),
    (False, False, True),   # a kept unit may not drain: drained anyway
    (False, True, False),   # left to the role's unit
])
def test_stop_rack_drain(operation_drain, unit_manage, drained):
    assert true(DRAIN["when"], cassandra_operation_drain=operation_drain, cassandra_service_unit_manage=unit_manage) is drained


def test_stop_rack_drain_never_stops_the_stop():
    # a node that does not answer (down, JMX): stopped all the same, not left drained and running
    assert DRAIN["failed_when"] is False and DRAIN["ignore_errors"] is True


@pytest.mark.parametrize("file", ["action_restart.yml", "action_reboot.yml"])
@pytest.mark.parametrize("operation_drain, unit_manage, drained", [
    (True, True, True), (False, False, True), (False, True, False)])
def test_restart_drain(file, operation_drain, unit_manage, drained):
    task = next(t for t in load("roles", "cassandra_service", "tasks", file) if t["name"] == "Drain the node")
    assert true(task["when"], cassandra_operation_drain=operation_drain, cassandra_service_unit_manage=unit_manage) is drained


@pytest.mark.parametrize("host_vars, drained", [({}, False), ({"cassandra_service_unit_manage": False}, True)])
def test_rack_restart_drains_a_kept_unit(host_vars, drained):
    block = load("roles", "cassandra_service", "tasks", "restart_batch.yml")[0]["block"]
    task = next(t for t in block if t["name"] == "Drain the rack's nodes")
    assert true(task["when"], cassandra_operation_drain=False, item="node1", hostvars={"node1": host_vars}) is drained


def test_service_stops_before_the_boot_setting():
    names = [t["name"] for t in load("roles", "cassandra_service", "tasks", "main.yml")]
    assert names.index("Stop cassandra") < names.index("Enable cassandra at boot")


REMOVE = next(p for p in load("playbooks", "remove_dead_node.yml") if p.get("name") == "Remove the dead node")
KEEP = next(t for t in REMOVE["tasks"] if t["name"] == "Keep where the removal stands")
READ = next(t for t in REMOVE["tasks"] if t["name"] == "Read the removals in progress on every node")
REMOVING = "RemovalStatus: Removing token (-42). Waiting for replication confirmation from [/10.0.0.2]."
DL_RING = {"cluster_status": {"dc1": {"nodes": [
    {"address": "10.0.0.1", "status": "U", "state": "N"}, {"address": "10.0.0.2", "status": "U", "state": "N"},
    {"address": "10.0.0.4", "status": "D", "state": "L"}]}}}
DN_RING = {"cluster_status": {"dc1": {"nodes": [
    {"address": "10.0.0.1", "status": "U", "state": "N"}, {"address": "10.0.0.2", "status": "U", "state": "N"},
    {"address": "10.0.0.4", "status": "D", "state": "N"}]}}}
TOKENS = {"stdout": "10.0.0.1  r1  Up  Normal  1 MiB  ?  7\n10.0.0.4  r1  Down  Leaving  1 MiB  ?  -42\n"}


def test_removal_status_read_on_every_node():
    assert "run_once" not in READ


def keep(hostvars):
    return render(KEEP["ansible.builtin.set_fact"]["cassandra_dead_removal"], ansible_play_hosts=["n1", "n2"],
                  hostvars=hostvars, cassandra_dead_ring=DL_RING, cassandra_dead_node_address="10.0.0.4")


def test_removal_found_on_the_second_node():
    hostvars = {"n1": {"cassandra_dead_removal_status": {"stdout": "RemovalStatus: No removals in process."},
                       "cassandra_dead_tokens": TOKENS},
                "n2": {"cassandra_dead_removal_status": {"stdout": REMOVING}, "cassandra_dead_tokens": {"skipped": True}}}
    assert keep(hostvars) == {"state": "resume", "on": "n2", "reason": ""}
    # a removal of another node on n2 (a token 10.0.0.4 does not have): not waited for as this one's
    hostvars["n2"]["cassandra_dead_removal_status"]["stdout"] = REMOVING.replace("-42", "7")
    assert keep(hostvars)["state"] == "busy"


def test_tokens_also_read_on_the_coordinator():
    # nodetool ring failed on n1: n2, which removes the node, lists its tokens
    task = next(t for t in REMOVE["tasks"] if t["name"] == "Read the tokens of the ring")
    assert "run_once" not in task
    when = task["when"][1]
    assert true(when, inventory_hostname="n2", ansible_play_hosts=["n1", "n2"],
                cassandra_dead_removal_status={"stdout": REMOVING})
    assert not true(when, inventory_hostname="n2", ansible_play_hosts=["n1", "n2"],
                    cassandra_dead_removal_status={"stdout": "RemovalStatus: No token removals in process."})
    assert true(when, inventory_hostname="n1", ansible_play_hosts=["n1", "n2"], cassandra_dead_removal_status={})
    hostvars = {"n1": {"cassandra_dead_removal_status": {"stdout": ""}, "cassandra_dead_tokens": {"rc": 1, "stdout": ""}},
                "n2": {"cassandra_dead_removal_status": {"stdout": REMOVING}, "cassandra_dead_tokens": TOKENS}}
    assert keep(hostvars) == {"state": "resume", "on": "n2", "reason": ""}


def test_removal_status_skipped_or_unanswered():
    hostvars = {"n1": {"cassandra_dead_removal_status": {"skipped": True}, "cassandra_dead_tokens": {"skipped": True}},
                "n2": {"cassandra_dead_removal_status": {"rc": 1, "stdout": ""}, "cassandra_dead_tokens": {"skipped": True}}}
    assert keep(hostvars) == {"state": "orphan", "on": "", "reason": ""}


def test_force_result_not_checked_in_check_mode():
    task = next(t for t in REMOVE["tasks"] if t["name"] == "It is out of the ring (removenode_force)")
    assert "not ansible_check_mode" in " ".join(task["when"])


def test_removal_checked_with_the_coordinator_jmx():
    # the removal found on n2 is followed there with n2's own JMX port and login
    task = next(t for t in load("roles", "cassandra_service", "tasks", "stream_check.yml")[0]["block"]
                if t["name"] == "Check the removal")
    variables = dict(task["vars"], _cassandra_stream_removal_on="n2", inventory_hostname="n1",
                     _cassandra_service_jmx={"port": 7199}, cassandra_jmx_username="ops", cassandra_jmx_password_file="",
                     cassandra_jmx_password="",
                     hostvars={"n2": {"cassandra_jmx_port": 7299, "cassandra_jmx_password_file": "/etc/cassandra/jmx.pw"}})
    cmd = render(task["ansible.builtin.command"], **variables)
    assert cmd == "nodetool -p 7299 -u ops -pwf /etc/cassandra/jmx.pw removenode status"


def test_orphan_removal_refused_unless_asked():
    task = next(t for t in REMOVE["tasks"] if t["name"] == "Its removal is not coordinated elsewhere")
    that = task["ansible.builtin.assert"]["that"]
    assert not true(that, cassandra_dead_removal={"state": "orphan", "on": ""})
    assert true(that, cassandra_dead_removal={"state": "orphan", "on": ""}, cassandra_dead_node_new_removal=True)
    assert true(that, cassandra_dead_removal={"state": "start", "on": ""})


def test_new_node_check_passes_the_import_marker():
    # without it the refusal of a blank host under an imported name would silently never happen
    block = load("roles", "cassandra_service", "tasks", "new_node_checks.yml")[1]["block"]
    gather = next(t for t in block if t.get("name") == "Gather what the checks need")
    task = next(t for t in gather["block"] if t.get("name") == "Put the findings together")
    kept = [e for e in task["vars"]["_all"] if "cassandra_new_node_kept_setup" in e][0]
    assert "imported=cassandra_imported_host | default(false)" in kept
    assert "allow=cassandra_new_node_allow_kept_setup" in kept


def test_force_runs_on_the_coordinator_not_the_first_node():
    # n2 coordinates the removal: n1 shows the node DN (forcing there does nothing), n2 shows it DL
    choose = next(t for t in REMOVE["tasks"] if t["name"] == "Choose the node that forces it")
    hostvars = {"n1": {"cassandra_dead_force_ring": DN_RING}, "n2": {"cassandra_dead_force_ring": DL_RING}}
    target = render(choose["ansible.builtin.set_fact"]["cassandra_dead_force"], ansible_play_hosts=["n1", "n2"],
                    hostvars=hostvars, cassandra_dead_node_address="10.0.0.4",
                    cassandra_dead_removal={"state": "resume", "on": "n2", "reason": ""})
    assert target == {"on": "n2", "reason": ""}
    for name in ("Finish a stuck removal (removenode force, on the node chosen above)", "Read the ring after removenode force"):
        task = next(t for t in REMOVE["tasks"] if t["name"] == name)
        assert "run_once" not in task
        assert "inventory_hostname == cassandra_dead_force.on" in task["when"]
    for method in ("removenode", "removenode_force"):
        assert true(READ["when"], _method=method)


def test_streams_left_by_removenode_force_are_not_a_failure():
    # e2e: after removenode force, the removal's streams still ran and the final check failed on them
    check = next(t for t in REMOVE["tasks"] if t["name"] == "Check the cluster without it")
    assert render(check["vars"]["cassandra_service_health_report_only"], _method="removenode_force") is True
    assert render(check["vars"]["cassandra_service_health_report_only"], _method="removenode") is False
    stop = next(t for t in REMOVE["tasks"] if t["name"] == "Stop on an unhealthy cluster (removenode_force)")
    variables = dict(stop["vars"], _method="removenode_force", cassandra_service_health_force=False, ansible_check_mode=False)
    streams = ["streams in progress on n1 (nodetool netstats)"]
    assert not true(stop["when"], cassandra_health_problems=streams, **variables)
    assert true(stop["when"], cassandra_health_problems=streams + ["10.0.0.2 (r1) is DN, seen from n1"], **variables)
    # --check: removenode force did not run, the node is still there
    assert not true(stop["when"], cassandra_health_problems=["10.0.0.4 (r3) is DL, seen from n1"],
                    **dict(variables, ansible_check_mode=True))
    warn = next(t for t in REMOVE["tasks"] if t["name"] == "Go on despite the problems (removenode_force, cassandra_service_health_force)")
    assert true(warn["when"], cassandra_health_problems=["10.0.0.2 (r1) is DN, seen from n1"],
                **dict(variables, cassandra_service_health_force=True))


def test_progress_report_knows_the_status_before_the_block_ends():
    # the block runs while _cassandra_stream_status is 'going': it is set after the progress is printed,
    # and the report gets the new status (a single done line, no "next check" once it has ended)
    for name, status, now in (("stream_check.yml", "_cassandra_stream_status", "_cassandra_stream_now"),
                              ("cleanup_check.yml", "_cassandra_cleanup_status", "_cassandra_cleanup_now")):
        block = load("roles", "cassandra_service", "tasks", name)[0]["block"]
        names = [t["name"] for t in block]
        where, write, keep = (block[names.index(n)] for n in ("Where it stands", "Write the progress", "Keep where it stands"))
        progress = next(t for t in block if t["name"].startswith("Progress of the"))
        assert names.index("Where it stands") < names.index("Write the progress") < block.index(progress) < names.index("Keep where it stands")
        assert status not in where["ansible.builtin.set_fact"] and now in where["ansible.builtin.set_fact"]
        assert keep["ansible.builtin.set_fact"][status] == "{{ %s }}" % now
        assert "status=%s" % now in write["ansible.builtin.set_fact"]["_cassandra_stream_report"]
        assert progress["ansible.builtin.debug"]["msg"] == "{{ _cassandra_stream_report }}"
        # the first waits are shorter (cassandra_stream_progress's wait), and the progress knows the interval
        wait = block[names.index("Wait before the next check")]
        assert wait["ansible.builtin.pause"]["seconds"] == "{{ _cassandra_stream_state.wait }}"
        assert "interval=cassandra_stream_check_interval | int" in block[names.index("Work out the progress")][
            "ansible.builtin.set_fact"]["_cassandra_stream_state"]


@pytest.mark.parametrize("job, mode, expected", [
    ({"finished": 1, "changed": True}, "LEAVING", "done"),  # nodetool decommission returned
    # the module changed nothing (the node was LEAVING already): its streams go on until DECOMMISSIONED
    ({"finished": 1, "changed": False}, "LEAVING", "going"),
    ({"finished": 1, "changed": False}, "DECOMMISSIONED", "done"),
    ({"finished": 1, "failed": True}, "LEAVING", "job_failed"),
])
def test_decommission_job_that_changed_nothing_is_not_the_end(job, mode, expected):
    block = load("roles", "cassandra_service", "tasks", "stream_check.yml")[0]["block"]
    where = next(t for t in block if t["name"] == "Where it stands")
    variables = dict(where["vars"], _cassandra_stream_leave=True, _cassandra_stream_job={"jid": "1"},
                     cassandra_stream_job_status=job, _cassandra_stream_self={"mode": mode},
                     _cassandra_stream_state={"stalled": False, "start": 0}, cassandra_stream_max_time=0)
    assert render(where["ansible.builtin.set_fact"]["_cassandra_stream_now"], **variables) == expected
