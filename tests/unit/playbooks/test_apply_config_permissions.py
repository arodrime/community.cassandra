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


ROLE = load("roles", "cassandra_config", "tasks", "main.yml")
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
    assert names == ["Clear the unit's failed starts", "Write the config", "Drain and restart the node",
                     "Start the node and wait until it has joined"]
    clear, write, restart, start = tasks
    assert write["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_config" and "when" not in write
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
    assert names.index("Read the owner and mode of the live files") < names.index("List the owner, group and mode changes") \
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
    include = play["tasks"][0]["vars"]
    # n5: stopped with nothing to do (not in this play); n6: outside --limit (no fact)
    then = {"n1": "none", "n2": "start", "n3": "write", "n4": "restart", "n5": "write"}
    hostvars = dict((h, {"_cassandra_preflight": {"ring_address": "10.100.100.%s" % h[1]}}) for h in list(then) + ["n6"])
    for h, t in then.items():
        hostvars[h].update(cassandra_apply_config_then=t, cassandra_apply_config_todo=h != "n5")
    variables = {"groups": {"all": list(hostvars), "cassandra": list(hostvars)}, "hostvars": hostvars}
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
    poll = block["vars"]["_poll"]
    assert int(render(poll, {"_cassandra_health_no_wait": True, "cassandra_service_health_timeout": 600})) == 1
    assert int(render(poll, {"_cassandra_health_no_wait": False, "cassandra_service_health_timeout": 600})) == 60


def plan(nodes):
    """The hosts the restart refusal names (nodes: [(host, then, todo)] in inventory order)."""
    refusal = find(FIND["tasks"], "Refuse restarts while a node is not running")
    hostvars = dict((h, {"cassandra_apply_config_then": t, "cassandra_apply_config_todo": d}) for h, t, d in nodes)
    variables = {"ansible_play_hosts": [h for h, t, d in nodes], "hostvars": hostvars}
    variables = with_task_vars(refusal, variables)
    return render("{{ %s }}" % refusal["ansible.builtin.assert"]["that"], variables), variables["_down"]


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
    variables = {"groups": {"all": hosts, "cassandra": hosts}, "hostvars": hostvars, "inventory_hostname": "n2"}
    peer = work["ansible.builtin.set_fact"]["_cassandra_health_peer"]
    assert render(peer, with_task_vars(work, variables)) == "n1"
    # n1 stopped (apply_config writes it, left stopped): the ring is read from n3
    variables["cassandra_service_health_down_ok"] = ["10.100.100.1"]
    assert with_task_vars(work, variables)["_down_hosts"] == ["n1"]
    assert render(peer, with_task_vars(work, variables)) == "n3"
