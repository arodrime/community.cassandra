from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config takes care of a node whose config files only need another owner,
# group or mode (e.g. written root:cassandra 0640 by a run whose inventory was
# not loaded, while Cassandra runs as dbsvc:dbgrp): the node is done, without a
# restart when Cassandra runs (it read its config at start), and started once
# written when it does not run (it could not read its config).

import os
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def find(tasks, name):
    return next(t for t in walk(tasks) if t.get("name") == name)


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def render(template, variables):
    return Templar(loader=DataLoader(), variables=trust(variables)).template(trust_as_template(template))


def with_task_vars(task, variables):
    """The variables plus the task's own vars, rendered in order."""
    variables = dict(variables)
    for name, value in (task.get("vars") or {}).items():
        variables[name] = render(value, variables) if isinstance(value, str) else value
    return variables


ROLE = load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "main.yml"))
FIND = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Find what each node needs"][0]
NOTE = find(FIND["tasks"], "Note what this node needs")


def stat(name, owner, group, mode):
    return {"cassandra_config_file": name,
            "stat": {"exists": True, "path": "/etc/cassandra/conf/" + name, "pw_name": owner, "gr_name": group,
                     "mode": mode, "uid": 0, "gid": 990}}


def perm_changes(live, **inventory):
    """The role's _cassandra_config_perm_changes for live stat results and the inventory's variables."""
    settings = load("roles", "cassandra_config", "vars", "main.yml")["_cassandra_config_perm_settings"]
    variables = dict({"cassandra_config_user": "root", "cassandra_config_group": "cassandra", "cassandra_config_mode": "0640",
                      "cassandra_config_public_mode": "0644", "cassandra_config_file_permissions": {},
                      "cassandra_user": "cassandra", "cassandra_group": "cassandra"}, **inventory)
    variables["_cassandra_config_perm_settings"] = dict((k, render(v, variables)) for k, v in settings.items())
    variables["cassandra_config_live_stat"] = {"results": live}
    listed = find(ROLE, "List the owner, group and mode changes")["ansible.builtin.set_fact"]
    return render(listed["_cassandra_config_perm_changes"], variables)


def note(changes=(), perms=(), pending=False, running=True, unit="inactive", jvm=None, result="success", status="0"):
    variables = {"_cassandra_config_changes": list(changes), "_cassandra_config_perm_changes": list(perms),
                 "cassandra_config_restart_pending": pending, "cassandra_config_newer_files": ["cassandra.yaml"],
                 "_cassandra_preflight_running": running, "cassandra_jvm": jvm or {},
                 "_cassandra_config_dir_notes": [],
                 "cassandra_apply_config_unit": {"skipped": True} if running else {
                     "status": {"ActiveState": unit, "Result": result, "ExecMainStatus": status}}}
    variables = with_task_vars(NOTE, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    return (render(facts["cassandra_apply_config_todo"], variables) in (True, "True"),
            render(facts["cassandra_apply_config_then"], variables),
            render(facts["cassandra_apply_config_notes"], variables))


# Written by a run without the inventory: root:cassandra; the inventory has
# Cassandra's account through the former name of cassandra_config_user
LIVE = [stat("cassandra.yaml", "root", "cassandra", "0640"), stat("cassandra-env.sh", "root", "cassandra", "0644")]
INVENTORY = {"cassandra_config_owner": "dbsvc", "cassandra_config_group": "dbgrp"}


def test_owner_and_mode_changes_are_listed_under_the_former_name_too():
    changes = perm_changes(LIVE, **INVENTORY)
    assert changes == [
        {"item": "/etc/cassandra/conf/cassandra.yaml (owner:group mode)", "before": "root:cassandra 0640", "after": "dbsvc:dbgrp 0640"},
        {"item": "/etc/cassandra/conf/cassandra-env.sh (owner:group mode)", "before": "root:cassandra 0644",
         "after": "dbsvc:dbgrp 0644"}]
    # the new name, and the former one winning over it
    assert perm_changes(LIVE, cassandra_config_user="dbsvc", cassandra_config_group="dbgrp") == changes
    assert perm_changes(LIVE, cassandra_config_user="root", **INVENTORY) == changes
    assert perm_changes(LIVE) == []


def test_a_node_with_only_owner_and_mode_to_change_is_done_without_a_restart():
    perms = perm_changes(LIVE, **INVENTORY)
    # it was left out (nothing to do) before: the files kept root:cassandra
    assert note(perms=perms) == (True, "none", [])


@pytest.mark.parametrize("unit", ["failed", "activating"])
def test_a_node_that_failed_to_start_is_started_once_written(unit):
    perms = perm_changes(LIVE, **INVENTORY)
    assert note(perms=perms, running=False, unit=unit) == (True, "start", [])
    assert note(changes=["cassandra.yaml"], running=False, unit=unit)[:2] == (True, "start")
    # not running, nothing to change: left as it is
    assert note(running=False, unit=unit)[0] is False


def test_a_node_stopped_on_purpose_is_written_and_left_stopped():
    perms = perm_changes(LIVE, **INVENTORY)
    assert note(perms=perms, running=False, unit="inactive")[:2] == (True, "write")
    assert note(changes=["cassandra.yaml"], running=False, unit="inactive")[:2] == (True, "write")
    # a stop that ended failed: SIGTERM's exit 143 (a kept unit without SuccessExitStatus=143), a stop timeout
    assert note(perms=perms, running=False, unit="failed", result="exit-code", status="143")[:2] == (True, "write")
    assert note(perms=perms, running=False, unit="failed", result="timeout", status="0")[:2] == (True, "write")
    # a failed start, a crash
    assert note(perms=perms, running=False, unit="failed", result="exit-code", status="1")[:2] == (True, "start")
    assert note(perms=perms, running=False, unit="failed", result="signal", status="9")[:2] == (True, "start")


def test_a_jvm_the_unit_did_not_start_counts_as_running():
    # e.g. started by hand: starting the unit would start a second one
    assert note(changes=["cassandra.yaml"], running=False, unit="failed", jvm={"pid": "123"})[:2] == (True, "restart")
    assert note(perms=perm_changes(LIVE, **INVENTORY), running=False, unit="inactive", jvm={"pid": "123"})[:2] == (True, "none")
    look = find(FIND["tasks"], "Look for a Cassandra JVM the unit did not start")
    assert look["ansible.builtin.include_role"]["tasks_from"] == "jvm_started.yml"
    names = [t.get("name") for t in FIND["tasks"]]
    assert names.index("Look for a Cassandra JVM the unit did not start") < names.index("Note what this node needs")
    unit = find(FIND["tasks"], "Read the state of its unit")
    assert unit["ansible.builtin.systemd_service"] == {"name": "cassandra"} and unit["register"] == "cassandra_apply_config_unit"
    assert names.index("Read the state of its unit") < names.index("Note what this node needs")


@pytest.mark.parametrize("changes, pending, notes", [
    (["cassandra.yaml"], False, []),
    ([], True, ["restart pending (cassandra.yaml changed since the running Cassandra started)"]),
])
def test_new_settings_restart_a_running_node(changes, pending, notes):
    # the settings are said by the view of what differs; a restart pending, under the node's outcome
    assert note(changes=changes, pending=pending) == (True, "restart", notes)
    assert note(changes=changes, perms=perm_changes(LIVE, **INVENTORY), pending=pending) == (True, "restart", notes)


def test_nothing_to_do():
    assert note() == (False, "none", [])


def test_the_action_restarts_starts_or_only_writes():
    tasks = load("roles", "cassandra_service", "tasks", "action_apply_config.yml")
    names = [t["name"] for t in tasks]
    # a restart loop is stopped before the write: it would not come up on the former config
    assert names == ["Clear the unit's failed starts", "Check the JVM a file is written back for still runs", "Write the config",
                     "Check the files written back", "Report what would be written",
                     "Say the restart of {{ inventory_hostname }}", "Drain and restart the node", "Start the time sync before Cassandra starts",
                     "Start the node and wait until it has joined", "Forget the restart in progress"]
    # (a file written back with no restart: test_apply_config_written_back.py)
    tasks = [t for t in tasks if t["name"] not in ("Check the JVM a file is written back for still runs", "Check the files written back")]
    tasks.pop()  # the mark of a stop in its restart window (os_window.yml), removed once up again
    tasks.pop(5)  # its time sync before a start (test_apply_config_os.py)
    phase = tasks.pop(3)  # a phase line, only before a restart
    assert "cassandra_apply_config_then | default('restart') == 'restart'" in phase["when"]
    clear, write, report, restart, start = tasks
    # --check: the plan's comparison (the same role run in check mode, just before) reported, nothing rendered again
    assert write["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_config"
    assert report["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_change_report"
    assert report["vars"]["cassandra_change_report_items"] == "{{ _cassandra_config_items | default([]) }}"
    for check in (True, False):
        assert [render("{{ %s }}" % t["when"], {"ansible_check_mode": check}) for t in (write, report)] == [not check, check]
    for then, expected in (("restart", [False, True, False]), ("start", [True, False, True]), ("none", [False, False, False]),
                           ("write", [False, False, False])):
        assert [render("{{ %s }}" % t["when"], {"cassandra_apply_config_then": then}) for t in (clear, restart, start)] == expected
    assert restart["ansible.builtin.include_tasks"] == "action_restart.yml"
    steps = clear["block"]
    assert steps[0]["ansible.builtin.systemd_service"] == {"name": "cassandra"} and steps[0]["failed_when"] is False
    # read again just before: one failed or stopped at the plan and running now (started by hand, on its former
    # config) stops the run; one restarting in a loop at the plan is stopped, even caught up for a moment
    came_up = "{{ %s }}" % steps[1]["ansible.builtin.assert"]["that"]
    assert steps[2]["ansible.builtin.systemd_service"] == {"name": "cassandra", "state": "stopped"}
    for plan, now, ok, stop in (("failed", ("active", "running"), False, False),
                                ("inactive", ("active", "running"), False, False),
                                ("failed", ("active", "exited"), True, False),        # SysV script, no JVM: not up
                                ("failed", ("failed", "failed"), True, False),
                                ("failed", ("activating", "auto-restart"), True, True),
                                ("activating", ("active", "running"), True, True),    # the loop's JVM, up for a moment
                                ("activating", ("activating", "auto-restart"), True, True),
                                ("failed", None, True, False)):                       # no unit
        values = {"cassandra_apply_config_unit": {"status": {"ActiveState": plan}},
                  "cassandra_apply_config_unit_now": {"status": {"ActiveState": now[0], "SubState": now[1]}} if now
                  else {"status": {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}}}
        values = with_task_vars(clear, values)
        assert render(came_up, values) is ok
        assert render("{{ %s }}" % steps[2]["when"], values) is stop
    assert steps[3]["ansible.builtin.command"] == "systemctl reset-failed cassandra"
    assert start["ansible.builtin.include_tasks"] == "main.yml" and start["vars"]["cassandra_service_state"] == "started"


def test_the_role_reports_them_and_records_what_cassandra_started_with():
    names = [t.get("name") for t in walk(ROLE)]
    assert names.index("Read the node's config files and directories") < names.index("List the owner, group and mode changes") \
        < names.index("Show the owner, group and mode changes") < names.index("Record the changes for the report")
    record = find(ROLE, "Record the changes for the report")["ansible.builtin.set_fact"]["_cassandra_config_items"]
    assert "_cassandra_config_perm_changes" in record
    shown = find(ROLE, "Show the owner, group and mode changes")
    assert render(shown["ansible.builtin.debug"]["msg"], {"_cassandra_config_perm_changes": perm_changes(LIVE, **INVENTORY),
                                                          "_cassandra_config_dir_changes": [], "_cassandra_config_dir_notes": []}) == [
        "/etc/cassandra/conf/cassandra.yaml (owner:group mode): root:cassandra 0640 -> dbsvc:dbgrp 0640",
        "/etc/cassandra/conf/cassandra-env.sh (owner:group mode): root:cassandra 0644 -> dbsvc:dbgrp 0644"]
    # recorded before an owner or mode change too: later runs then tell it needs no restart (same checksums)
    seed = find(ROLE, "Record the config the running Cassandra started with")
    condition = "{{ %s }}" % seed["when"][1]
    assert render(condition, {"_cassandra_config_changes": [], "_cassandra_config_perm_changes": [{"item": "x"}]}) is True
    assert render(condition, {"_cassandra_config_changes": [], "_cassandra_config_perm_changes": []}) is False


@pytest.mark.parametrize("resume, down_ok, expected", [(False, False, False), (True, False, True), (False, True, True)])
def test_apply_config_passes_a_member_seed_down(resume, down_ok, expected):
    play = load("playbooks", "preflight.yml")[1]
    check = [t for t in play["tasks"] if t.get("name") == "Check the running cluster"][0]
    seeds = [t for t in check["block"] if t["name"] == "Every seed is up"][0]
    variables = {"_addr": "10.100.100.2", "_up": ["10.100.100.1"], "_all": ["10.100.100.1", "10.100.100.2"],
                 "_host": ["10.100.100.2"], "_stopped": ["10.100.100.2"], "cassandra_rolling_resume": resume,
                 "_cassandra_preflight_seeds_down_ok": down_ok, "_seed_added": [], "_joining": []}
    condition = "{{ %s }}" % seeds["ansible.builtin.assert"]["that"].strip()
    assert render(condition, variables) is expected
    # apply_config: only a seed of this run that is not running (one it writes), not one down elsewhere
    assert render(condition, dict(variables, _stopped=[])) is resume
    # never a seed that is not a member of the ring
    variables["_all"] = ["10.100.100.1"]
    assert render(condition, variables) is False
    stopped = seeds["vars"]["_stopped"]
    hostvars = {"n1": {"_cassandra_preflight_running": True, "_cassandra_preflight": {"address": "10.100.100.1"}},
                "n2": {"_cassandra_preflight_running": False, "_cassandra_preflight": {"address": "10.100.100.2"}}}
    assert render(stopped, {"ansible_play_hosts": ["n1", "n2"], "hostvars": hostvars}) == ["10.100.100.2"]
    imported = load("playbooks", "apply_config.yml")[0]
    assert imported["ansible.builtin.import_playbook"] == "community.cassandra.preflight"
    assert imported["vars"]["_cassandra_preflight_seeds_down_ok"] is True


def test_the_other_nodes_not_running_may_stay_down_unless_a_node_is_restarted():
    play = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Apply the config, one node at a time"][0]
    names = [t["name"] for t in play["tasks"]]
    assert names == ["Give each node its own facts", "Note the other nodes that may stay down", "Apply the config on this node"]
    # computed once per node, read by the health checks
    include = dict(play["tasks"][2]["vars"], cassandra_service_health_down_ok=play["tasks"][1]["ansible.builtin.set_fact"][
        "_cassandra_apply_config_down_ok"])
    assert play["tasks"][2]["vars"]["cassandra_service_health_down_ok"] == "{{ _cassandra_apply_config_down_ok }}"
    # n5: stopped with nothing to do (not in this play); n6: outside --limit (no fact)
    then = {"n1": "none", "n2": "start", "n3": "write", "n4": "restart", "n5": "write"}
    hostvars = dict((h, {"_cassandra_preflight": {"ring_address": "10.100.100.%s" % h[1]}}) for h in list(then) + ["n6"])
    for h, t in then.items():
        hostvars[h].update(cassandra_apply_config_then=t, cassandra_apply_config_todo=h != "n5")
    variables = {"groups": {"all": list(hostvars), "cassandra": list(hostvars), "cassandra_dc1": list(hostvars)}, "hostvars": hostvars}
    for check, host, expected in ((False, "n1", ["10.100.100.2", "10.100.100.3", "10.100.100.5"]),
                                  (False, "n2", ["10.100.100.3", "10.100.100.5"]),
                                  # n2, started before n3's turn, must be up again
                                  (False, "n3", ["10.100.100.5"]), (False, "n4", []),
                                  # --check started nothing: a start node before the restart is still down
                                  (True, "n4", ["10.100.100.2"])):
        values = dict(variables, inventory_hostname=host, cassandra_apply_config_then=then[host], ansible_check_mode=check)
        down_ok = render(include["cassandra_service_health_down_ok"], values)
        assert down_ok == expected
        # no wait for them: they will not come back
        no_wait = render(include["_cassandra_health_no_wait"], dict(values, cassandra_service_health_down_ok=down_ok))
        assert no_wait in (bool(expected), str(bool(expected)))
    block = [t for t in load("roles", "cassandra_service", "tasks", "cluster_health.yml") if "block" in t][0]
    wait_on = block["vars"]["_cassandra_health_wait_on"]
    for no_wait, check, waits in ((True, False, False), (False, False, True), (False, True, False)):  # --check waits for nothing
        assert render(wait_on, {"_cassandra_health_no_wait": no_wait, "cassandra_service_health_timeout": 600,
                                "ansible_check_mode": check}) in (waits, str(waits))


def test_the_nodes_written_at_once_let_down_the_nodes_started_later():
    plays = load("playbooks", "apply_config.yml")
    batch = [p for p in plays if p.get("name") == "Apply the config where nothing is restarted, on these nodes at once"][0]
    assert [t["name"] for t in batch["tasks"]] == ["Give each node its own facts", "Note the other nodes that may stay down",
                                                   "Apply the config on these nodes"]
    include = dict(batch["tasks"][2]["vars"], cassandra_service_health_down_ok=batch["tasks"][1]["ansible.builtin.set_fact"][
        "_cassandra_apply_config_down_ok"])
    assert include["cassandra_service_node_batch"] is True and batch["any_errors_fatal"] is True and "serial" not in batch
    then = {"n1": "none", "n2": "start", "n3": "write", "n4": "restart", "n5": "write"}
    hostvars = dict((h, {"_cassandra_preflight": {"ring_address": "10.100.100.%s" % h[1]}}) for h in list(then) + ["n6"])
    for h, t in then.items():
        hostvars[h].update(cassandra_apply_config_then=t, cassandra_apply_config_todo=h != "n5")
    variables = {"groups": {"all": list(hostvars), "cassandra": list(hostvars), "cassandra_dc1": list(hostvars)}, "hostvars": hostvars}
    # written before any start or restart: every start node is still down; --check also checks the restart node, which
    # needs every other node up but the ones it starts and, a real run refusing it (said in the recap), the ones left
    # stopped
    for host, expected in (("n1", ["10.100.100.2", "10.100.100.3", "10.100.100.5"]), ("n3", ["10.100.100.2", "10.100.100.5"]),
                           ("n4", ["10.100.100.2", "10.100.100.3", "10.100.100.5"])):
        values = dict(variables, inventory_hostname=host, cassandra_apply_config_then=then[host], ansible_check_mode=host == "n4")
        assert render(include["cassandra_service_health_down_ok"], values) == expected
    # done at once: the ones no restart or start takes down, every one under --check; the others one at a time
    group = [t for t in plays[1]["tasks"] if t.get("name") == "Group the nodes to do at once"][0]
    key = group["ansible.builtin.group_by"]["key"]
    for todo, t, check, at_once in ((True, "none", False, True), (True, "write", False, True), (True, "restart", False, False),
                                    (True, "start", False, False), (True, "restart", True, True), (False, "none", True, False)):
        values = dict(cassandra_apply_config_todo=todo, cassandra_apply_config_then=t, ansible_check_mode=check)
        assert render(key, with_task_vars(group, values)) == "cassandra_apply_config_batch_%s" % at_once


def plan(nodes):
    """The hosts the restart refusal names (nodes: [(host, then, todo)] in inventory order)."""
    refusal = find(FIND["tasks"], "Note whether restarts are refused while a node is not running")
    hostvars = dict((h, {"cassandra_apply_config_then": t, "cassandra_apply_config_todo": d}) for h, t, d in nodes)
    variables = {"ansible_play_hosts": [h for h, t, d in nodes], "hostvars": hostvars}
    variables = with_task_vars(refusal, variables)
    return render(refusal["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables) == [], variables["_down"]


@pytest.mark.parametrize("nodes, refused", [
    ([("n1", "none", True), ("n2", "start", True), ("n3", "write", True)], []),     # nothing taken down
    ([("n1", "restart", True), ("n2", "none", True)], []),
    ([("n1", "write", True), ("n2", "restart", True)], ["n1"]),                     # left down
    ([("n1", "restart", True), ("n2", "start", True)], ["n2"]),                     # started after the restart
    ([("n1", "start", True), ("n2", "restart", True)], []),                         # started first (e.g. a resumed run)
    ([("n1", "start", False), ("n2", "restart", True)], ["n1"]),                    # failed, nothing to do: left down
    ([("n1", "restart", True), ("n2", "write", False)], ["n2"]),
])
def test_a_restart_is_refused_while_a_node_stays_down(nodes, refused):
    ok, down = plan(nodes)
    assert down == refused and ok is (not refused)


def test_a_node_not_running_has_no_restart_pending_unless_a_resumed_run_stopped_it():
    for resume, expected in ((False, (False, "write", [])), (True, (True, "start", [
            "restart pending (cassandra.yaml changed since the running Cassandra started)"]))):
        variables = {"_cassandra_config_changes": [], "_cassandra_config_perm_changes": [], "cassandra_config_restart_pending": True,
                     "cassandra_config_newer_files": ["cassandra.yaml"], "_cassandra_preflight_running": False, "cassandra_jvm": {},
                     "_cassandra_config_dir_notes": [],
                     "cassandra_apply_config_unit": {"status": {"ActiveState": "inactive"}}, "cassandra_rolling_resume": resume}
        variables = with_task_vars(NOTE, variables)
        facts = NOTE["ansible.builtin.set_fact"]
        assert (render(facts["cassandra_apply_config_todo"], variables) in (True, "True"),
                render(facts["cassandra_apply_config_then"], variables),
                render(facts["cassandra_apply_config_notes"], variables)) == expected


def test_the_second_node_checked_from_is_not_one_expected_down():
    work = load("roles", "cassandra_service", "tasks", "cluster_health.yml")[0]
    hosts = ["n1", "n2", "n3"]
    hostvars = dict((h, {"_cassandra_preflight": {"ring_address": "10.100.100.%s" % h[1]}}) for h in hosts)
    variables = {"groups": {"all": hosts, "cassandra": hosts, "cassandra_dc1": hosts}, "hostvars": hostvars, "inventory_hostname": "n2"}
    peer = work["ansible.builtin.set_fact"]["_cassandra_health_peer"]
    assert render(peer, with_task_vars(work, variables)) == "n1"
    # n1 stopped (apply_config writes it, left stopped): the ring is read from n3
    variables["cassandra_service_health_down_ok"] = ["10.100.100.1"]
    assert with_task_vars(work, variables)["_down_hosts"] == ["n1"]
    assert render(peer, with_task_vars(work, variables)) == "n3"


def test_a_batch_reads_the_ring_and_schema_once_from_a_node_outside_it():
    work = load("roles", "cassandra_service", "tasks", "cluster_health.yml")[0]
    hosts = ["n1", "n2", "n3"]
    hostvars = dict((h, {"_cassandra_preflight": {"ring_address": "10.100.100.%s" % h[1]}}) for h in hosts)
    peer = work["ansible.builtin.set_fact"]["_cassandra_health_peer"]
    for batch, play, me, expected in ((True, ["n1", "n2"], "n1", "n3"), (True, ["n1", "n2"], "n2", "n3"),
                                      (True, hosts, "n2", "n1"),  # all in the batch: the same node for all
                                      (False, ["n1", "n2"], "n1", "n2")):  # one node at a time: another node
        variables = {"groups": {"all": hosts, "cassandra": hosts, "cassandra_dc1": hosts}, "hostvars": hostvars, "inventory_hostname": me,
                     "ansible_play_hosts": play, "cassandra_service_health_batch": batch}
        assert render(peer, with_task_vars(work, variables)) == expected
    block = load("roles", "cassandra_service", "tasks", "cluster_health_read.yml")[0]
    shared = [t for t in block["block"] if t.get("delegate_to") == "{{ _cassandra_health_peer }}" and "async" in t]
    assert [t["name"] for t in shared] == ["Read the ring from {{ _cassandra_health_peer }}",
                                           "Check schema agreement (same answer from any node)"]
    for t in shared:  # a batch: once, both at once; else one after the other there (every node of a parallel play reads there)
        assert t["run_once"] == "{{ _batch }}" and t["async"] == "{{ _async if _batch else 0 }}" and t["poll"] == 0
    note = [t for t in block["block"] if str(t.get("name", "")).startswith("Note what was read from")][0]["ansible.builtin.set_fact"]
    jobs = {"cassandra_health_peer_job": {"finished": 1, "cluster_status": {"a": 1}}, "cassandra_health_peer_read": {"ansible_job_id": "1"},
            "cassandra_health_schema_job": {"finished": 1}, "cassandra_health_schema_read": {"ansible_job_id": "2"}}
    assert render(note["cassandra_health_peer"], dict(jobs, _batch=True)) == jobs["cassandra_health_peer_job"]
    assert render(note["cassandra_health_peer"], dict(jobs, _batch=False)) == jobs["cassandra_health_peer_read"]
    # this node's reads run at once: each launched, then collected, then their files removed
    launched = [t["register"] for t in block["block"] if "async" in t and t not in shared]
    collected = [t["ansible.builtin.async_status"]["jid"] for t in block["block"]
                 if "ansible.builtin.async_status" in t and "delegate_to" not in t]
    assert collected == ["{{ %s.ansible_job_id | default('') }}" % r for r in launched] and len(launched) == 4


def test_a_batch_says_its_nodes_once_and_writes_the_progress_file_a_line_at_a_time():
    tasks = load("roles", "cassandra_service", "tasks", "node_operation.yml")
    block = [t for t in tasks if "block" in t][0]["block"]
    say = [t for t in block if str(t.get("name", "")).startswith("Say what is done to")][0]
    assert say["run_once"] == "{{ cassandra_service_node_batch | default(false) | bool }}"
    names = say["vars"]["_names"]
    assert render(names, {"cassandra_service_node_batch": True, "ansible_play_hosts": ["n3", "n4"],
                          "cassandra_progress_done": ["n4"], "inventory_hostname": "n3"}) == ["n3"]
    assert render(names, {"ansible_play_hosts": ["n3", "n4"], "inventory_hostname": "n4"}) == "n4"
    progress = [t for t in block if t.get("name") == "Add the node to the progress file"][0]
    assert progress["throttle"] == 1


def written(preview, live, links=(), target=None, alternative=""):
    """The files the role's template task writes."""
    t = find(ROLE, "List the owner, group and mode changes")
    settings = load("roles", "cassandra_config", "vars", "main.yml")["_cassandra_config_perm_settings"]
    variables = {"cassandra_config_user": "root", "cassandra_config_group": "cassandra", "cassandra_config_mode": "0640",
                 "cassandra_config_public_mode": "0644", "cassandra_config_file_permissions": {}, "cassandra_user": "cassandra",
                 "cassandra_group": "cassandra", "cassandra_rpm_conf_alternative": alternative, "_cassandra_config_links": list(links),
                 "cassandra_config_preview": {"results": [{"cassandra_config_file": f, "rc": rc} for f, rc in preview]},
                 "cassandra_config_live_stat": {"results": live},
                 "cassandra_config_conf_target": {"skipped": True} if target is None else {"stat": {"exists": True, "lnk_source": target}}}
    variables["_cassandra_config_perm_settings"] = dict((k, render(v, variables)) for k, v in settings.items())
    return render(t["ansible.builtin.set_fact"]["_cassandra_config_write"], variables)


def test_the_files_the_same_on_the_node_are_not_written_again():
    live = [stat("cassandra.yaml", "root", "cassandra", "0640"), stat("cassandra-env.sh", "root", "cassandra", "0644"),
            stat("logback.xml", "root", "cassandra", "0600")]
    preview = [("cassandra.yaml", 0), ("cassandra-env.sh", 1), ("logback.xml", 0)]
    # a content change (or a live file the diff could not read, rc 1 too), an owner or mode change
    assert written(preview, live) == ["cassandra-env.sh", "logback.xml"]
    # a link: the template task replaces it with a file
    assert written(preview, live, links=["cassandra.yaml"]) == ["cassandra.yaml", "cassandra-env.sh", "logback.xml"]
    # RHEL: /etc/cassandra/conf about to point to the role's dir: every file written there, as before
    assert written(preview, live, target="/etc/cassandra/conf.default", alternative="/etc/cassandra/ansible.conf") == [
        "cassandra.yaml", "cassandra-env.sh", "logback.xml"]
    assert written(preview, live, target="/etc/cassandra/ansible.conf", alternative="/etc/cassandra/ansible.conf/") == [
        "cassandra-env.sh", "logback.xml"]


def test_a_batch_node_stopped_by_another_says_so():
    # any_errors_fatal stops the nodes of the batch with the one that failed (ansible-core 2.17+ rescues them too):
    # only that one is said FAILED; the others are not finished (2.16 lets them finish first: done)
    tasks = load("roles", "cassandra_service", "tasks", "node_operation.yml")
    op = [t for t in tasks if "block" in t][0]
    note = [t for t in op["block"] if t.get("name") == "Note a node of a batch as not finished"][0]
    record, dummy, stop = op["rescue"]
    base = {"cassandra_service_node_action": "apply_config", "cassandra_service_node_batch": True, "inventory_hostname": "n4"}
    assert render("{{ %s }}" % note["when"], base) is True
    assert render(note["ansible.builtin.set_fact"]["cassandra_op_result"], base) == (
        "apply_config not finished: the run stopped at another node done at the same time")
    assert render("{{ %s }}" % record["when"], base) is False
    assert render(stop["ansible.builtin.fail"]["msg"], base) == "Stopped with the node that failed."
    failed = dict(base, ansible_failed_task={"name": "Check gossip on this node"}, cassandra_progress_file="/p")
    assert render("{{ %s }}" % record["when"], failed) is True
    # (ansible-core 2.16 finishes the others first: they say done)
    assert render(stop["ansible.builtin.fail"]["msg"], failed).startswith(
        "Stopped at n4 (Check gossip on this node): no node was restarted; the nodes done at the same time: as said above.")
    # one node at a time: as before
    alone = dict(base, cassandra_service_node_batch=False, ansible_failed_task={"name": "x"})
    assert render("{{ %s }}" % note["when"], alone) is False and render("{{ %s }}" % record["when"], alone) is True
    assert render(stop["ansible.builtin.fail"]["msg"], alone).startswith("Stopped at n4 (x): no other node was touched after it.")


def test_the_nodes_to_do_are_not_reached_until_their_play():
    # a run stopped in the play of the nodes written at once: the summary says the others not reached (not --limit)
    plays = load("playbooks", "apply_config.yml")
    progress = [p for p in plays if p.get("name") == "Set up the progress file"][0]
    note = [t for t in progress["tasks"] if t.get("name") == "Note the nodes not reached yet"][0]
    assert note["ansible.builtin.set_fact"] == {"cassandra_op_result": "not reached"}
    names = [p.get("name") for p in plays]
    assert names.index("Set up the progress file") < names.index("Apply the config where nothing is restarted, on these nodes at once")
