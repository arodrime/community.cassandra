from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config with a node not running and restarts to do: a real run refuses before any change; --check shows the
# whole view (every node, the settings) and says the refusal last, with the way out. The expressions are read from
# the playbook and rendered by Ansible with neutral data.

import os
import warnings

import pytest
import yaml

from ansible.plugins.loader import init_plugin_loader

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_outcomes
from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_recap
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks
from ansible_collections.community.cassandra.tests.unit.helpers import render_as_given, trusted, with_apply_config_view

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def render(template, variables):
    """template rendered over variables, with the facts apply_config notes of every node once (its view)."""
    if "hostvars" in variables and "ansible_play_hosts" in variables:
        variables = with_apply_config_view(variables)
    return render_as_given(template, variables)


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    """The views laid out at the terminal's width (width='terminal'): a wide one here, whatever runs the tests."""
    from ansible_collections.community.cassandra.plugins.filter import cassandra_apply_config
    monkeypatch.setattr(cassandra_apply_config, "terminal_width", lambda: 200)


def task(name, play_name=None):
    for play in PLAYS:
        if play_name and play.get("name") != play_name:
            continue
        for t in play.get("tasks") or []:
            if t.get("name") == name:
                return t
    raise AssertionError("no task %s" % name)


# node5 stopped on purpose (left stopped: write), node1 and node2 to restart
HOSTVARS = {
    "node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
              "_cassandra_preflight": {"ring_address": "10.0.0.1"}},
    "node2": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
              "_cassandra_preflight": {"ring_address": "10.0.0.2"}},
    "node3": {"cassandra_apply_config_todo": False, "cassandra_apply_config_then": "none",
              "_cassandra_preflight": {"ring_address": "10.0.0.3"}},
    "node5": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "write",
              "_cassandra_preflight": {"ring_address": "10.0.0.5", "cassandra_dc": "dc1", "cassandra_rack": "r1"}}}
HOSTS = sorted(HOSTVARS)
REASON = ("node5: Cassandra not running, and node1, node2 would be restarted, which needs every other node up.")
# the way out: a fact per line, each command on a line of its own (to paste)
WAY = ["stopped on purpose: apply the config there first (--limit node5), then start it with start_rack, which checks"
       " the start first:",
       "    ansible-playbook community.cassandra.start_rack --limit node5 -e cassandra_target_dc=dc1"
       " -e cassandra_target_rack=r1"]
AGAIN = "then run again."


def refused(hostvars):
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trusted(t["vars"]), hostvars=hostvars, ansible_play_hosts=HOSTS)
    return render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables)


def test_the_refusal_reason_and_its_way_out():
    assert refused(HOSTVARS) == [REASON] + WAY + [AGAIN]
    # node5 started by the run before the restarts (its unit failed, it is first): nothing refused
    started = dict(HOSTVARS, node0={"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "start"})
    started.pop("node5")
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trusted(t["vars"]), hostvars=started, ansible_play_hosts=sorted(started))
    assert render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables) == []


def test_the_refusal_names_a_failed_unit_this_run_does_not_start():
    # node7's unit failed and nothing is to apply there: in no play (then write, not to do), so --limit node7
    # would do nothing: the way out is on the node
    failed = {"cassandra_apply_config_todo": False, "cassandra_apply_config_then": "write", "cassandra_apply_config_unit_failed": True,
              "_cassandra_preflight": {"ring_address": "10.0.0.7", "cassandra_dc": "dc1", "cassandra_rack": "r2"}}
    hostvars = dict(HOSTVARS, node7=failed)
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trusted(t["vars"]), hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    reason, *way = render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables)
    assert reason == "node5, node7: Cassandra not running, and node1, node2 would be restarted, which needs every other node up."
    # --limit names node5 only (the config applied there, started if stopped on purpose); node7's way out is on the node
    failed_way = ["node7: the cassandra unit failed and nothing is to apply: see systemctl status cassandra / journalctl -u"
                  " cassandra there, fix it, then start it with start_rack, which checks the start first:",
                  "    ansible-playbook community.cassandra.start_rack --limit node7 -e cassandra_target_dc=dc1"
                  " -e cassandra_target_rack=r2"]
    assert way == WAY + failed_way + [AGAIN]
    # node7 the only node down: no --limit sentence (nothing to apply there)
    hostvars.pop("node5")
    variables = dict(trusted(t["vars"]), hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    assert render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables) == [
        "node7: Cassandra not running, and node1, node2 would be restarted, which needs every other node up."] + failed_way + [AGAIN]
    # the fact the refusal reads, set with the node's needs
    note = task("Note what this node needs")
    assert note["ansible.builtin.set_fact"]["cassandra_apply_config_unit_failed"] == "{{ _unit_failed }}"


def test_a_node_left_stopped_by_an_interrupted_run_has_one_way_out_the_resume():
    # e2e: its start failed in its restart window (the mark kept, its unit failed): --check said resume in a WARNING,
    # and start_rack then run again in the refusal; the resume starts it first
    marked = {"cassandra_apply_config_todo": False, "cassandra_apply_config_then": "write",
              "cassandra_apply_config_unit_failed": True, "cassandra_apply_config_marked": True,
              "_cassandra_preflight": {"ring_address": "10.0.0.7", "cassandra_dc": "dc1", "cassandra_rack": "r2"}}
    hostvars = dict((k, v) for k, v in HOSTVARS.items() if k != "node5")
    hostvars["node7"] = marked
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trusted(t["vars"]), hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    # node1 restarted before node7 in a resume too: the resume on node7 alone first, said at once
    assert render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables) == [
        "node7: Cassandra not running, and node1, node2 would be restarted, which needs every other node up.",
        "node7: left stopped by an interrupted apply_config (in its restart window), reached by a resume only"
        " after node1 (inventory order), whose restart needs every other node up: see systemctl status cassandra"
        " / journalctl -u cassandra there, fix what stopped its start, then resume on it alone first (the same"
        " command plus -e cassandra_rolling_resume=true --limit node7), then resume again without --limit."]
    # before the first node to restart: the resume starts it first
    first = dict(hostvars, node0=marked)
    first.pop("node7")
    assert render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"],
                  dict(variables, hostvars=first, ansible_play_hosts=sorted(first))) == [
        "node0: Cassandra not running, and node1, node2 would be restarted, which needs every other node up.",
        "node0: left stopped by an interrupted apply_config (in its restart window): see systemctl status cassandra /"
        " journalctl -u cassandra there, fix what stopped its start, then resume (the same command plus"
        " -e cassandra_rolling_resume=true), which starts it first."]
    # that resume, node7 still down (after a restart in the inventory's order): still the resume's way out,
    # on it alone first, never start_rack nor "the unit failed and nothing is to apply"
    for then, todo in (("write", False), ("start", True)):
        resumed = dict(hostvars, node7=dict(marked, cassandra_apply_config_then=then, cassandra_apply_config_todo=todo))
        refused = render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"],
                         dict(variables, hostvars=resumed, cassandra_rolling_resume=True))
        assert refused == [
            "node7: Cassandra not running, and node1, node2 would be restarted, which needs every other node up.",
            "node7: left stopped by an interrupted apply_config (in its restart window), reached by a resume only"
            " after node1 (inventory order), whose restart needs every other node up: see systemctl status cassandra"
            " / journalctl -u cassandra there, fix what stopped its start, then resume on it alone first (the same"
            " command plus --limit node7), then resume again without --limit."], then
    note = task("Note what this node needs")["ansible.builtin.set_fact"]
    assert note["cassandra_apply_config_marked"] == "{{ not _running | bool and _clock.mark is defined }}"


def test_the_start_rack_of_the_way_out_starts_these_nodes_alone():
    # start_rack without --limit starts every stopped node of the rack (one stopped on purpose too): the nodes down
    # named, a command per rack, its datacenter and rack given
    down = {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "write"}
    hostvars = dict(HOSTVARS,
                    node6=dict(down, _cassandra_preflight={"ring_address": "10.0.0.6", "cassandra_dc": "dc1",
                                                           "cassandra_rack": "r1"}),
                    node8=dict(down, _cassandra_preflight={"ring_address": "10.0.0.8", "cassandra_dc": "dc2",
                                                           "cassandra_rack": "r1"}))
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trusted(t["vars"]), hostvars=hostvars, ansible_play_hosts=sorted(hostvars),
                     ansible_inventory_sources=["/srv/inv/prod.yml"], cassandra_hosts="prod")
    way = render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables)[2:4]
    assert way == ["    ansible-playbook -i /srv/inv/prod.yml community.cassandra.start_rack -e cassandra_hosts=prod"
                   " --limit node5,node6 -e cassandra_target_dc=dc1 -e cassandra_target_rack=r1",
                   "    ansible-playbook -i /srv/inv/prod.yml community.cassandra.start_rack -e cassandra_hosts=prod"
                   " --limit node8 -e cassandra_target_dc=dc2 -e cassandra_target_rack=r1"]
    # the plan's note of a failed unit left stopped: start_rack on that node alone (the note's own expression)
    note = task("Note what this node needs")["ansible.builtin.set_fact"]["cassandra_apply_config_notes"]
    start = note.index("'start_rack' | community.cassandra.cassandra_command(")
    expr = "{{ " + note[start:note.index(") ~ ')']", start) + 1] + " }}"
    variables = {"_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": "r2"}, "inventory_hostname": "node7"}
    assert render(expr, variables) == ("ansible-playbook community.cassandra.start_rack --limit node7"
                                       " -e cassandra_target_dc=dc1 -e cassandra_target_rack=r2")


def test_the_plan_says_a_failed_unit_left_stopped_is_not_stopped_on_purpose():
    # node7's unit failed, only its JMX files to write (nothing that starts it): left stopped, but not on purpose
    hostvars = dict(HOSTVARS, node7={"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "write",
                                     "cassandra_apply_config_unit_failed": True})
    t = task("Show the plan and confirm the run")
    nodes = render(t["vars"]["_nodes"], dict(hostvars=hostvars, ansible_play_hosts=sorted(hostvars)))
    assert [n["unit_failed"] for n in nodes] == [False, False, False, False, True]
    assert cassandra_apply_config_outcomes(nodes, plan=True)[1:] == [
        "node5         write, left stopped (Cassandra stopped: left stopped, reads its config when it starts)",
        "node7         write, left stopped (its cassandra unit failed: left as it is)",
        "node3         up to date"]


def test_a_real_run_refuses_before_any_change_check_goes_on():
    t = task("Refuse restarts while a node is not running")
    for check, refused, runs in ((False, [REASON] + WAY, True), (True, [REASON] + WAY, False), (False, [], False)):
        variables = {"ansible_check_mode": check, "_cassandra_apply_config_refused": refused}
        assert all(render("{{ %s | bool }}" % w, variables) in (True, "True") for w in t["when"]) is runs
    variables = dict(trusted(t["vars"]), _cassandra_apply_config_refused=[REASON] + WAY + [AGAIN], hostvars={"localhost": {}},
                     cassandra_cluster_name="c")
    # a block: its verdict, then the reason and the way out, at 79 columns; the command cut between its arguments,
    # a continuation at the end of each line but the last (it pastes as one command)
    assert render(t["ansible.builtin.fail"]["msg"], variables).split("\n") == [
        "REFUSED  apply_config  c  nothing was changed",
        "  node5: Cassandra not running, and node1, node2 would be restarted, which",
        "    needs every other node up.",
        "  stopped on purpose: apply the config there first (--limit node5), then start",
        "    it with start_rack, which checks the start first:",
        "      ansible-playbook community.cassandra.start_rack --limit node5 \\",
        "        -e cassandra_target_dc=dc1 -e cassandra_target_rack=r1",
        "  then run again."]
    # the refusal comes before anything is written: in the first play, before the plan and its question
    names = [x.get("name") for x in PLAYS[1]["tasks"]]
    assert names.index("Refuse restarts while a node is not running") < names.index("Show the plan and confirm the run")


def test_check_lets_the_stopped_node_stay_down_for_a_restart_node():
    # under --check every node goes in the batch play: node1's check must not stop on node5 (refused, said in the
    # recap); a real run never gets there with node5 down
    play = "Apply the config where nothing is restarted, on these nodes at once"
    expr = task("Apply the config on these nodes", play)["vars"]["cassandra_service_health_down_ok"]
    variables = dict(hostvars=HOSTVARS, groups={"prod": HOSTS}, cassandra_hosts="prod", ansible_play_hosts=HOSTS,
                     inventory_hostname="node1")
    assert render(expr, dict(variables, ansible_check_mode=True)) == ["10.0.0.5"]
    assert render(expr, dict(variables, ansible_check_mode=False)) == []


def test_the_check_recap_ends_with_the_refusal():
    t = task("Print the changes")
    summary = next(p for p in PLAYS if p.get("name") == "Summary")
    hostvars = dict((h, dict(v, _cassandra_apply_config_refused=[REASON] + WAY + [AGAIN], cassandra_op_done=v["cassandra_apply_config_todo"],
                             cassandra_op_result="would apply" if v["cassandra_apply_config_todo"] else "nothing to apply"))
                    for h, v in HOSTVARS.items())
    variables = dict(trusted(dict(summary["vars"], **t["vars"])), hostvars=hostvars, groups={"prod": HOSTS}, cassandra_hosts="prod",
                     ansible_play_hosts_all=HOSTS, cassandra_cluster_name="c", ansible_check_mode=True)
    # no plan shown (a later play stopped first): the recap says it, last
    lines = render(t["ansible.builtin.debug"]["msg"], variables)
    assert lines[0] == "CHECK  apply_config  c  2 would restart, 1 would apply, 1 up to date"
    warning = lines.index("WARNING  a real run is refused, before changing anything: " + REASON)
    assert lines[warning - 1] == ""
    # the way out under it, a fact per line, the command on a line of its own (it pastes as one command)
    assert lines[warning + 1:] == ["  " + line if not line.startswith(" ") else line for line in WAY + [AGAIN]]
    # the nodes' outcomes are all there, before it
    assert "  node1, node2  would apply, then restart" in lines
    # the plan said it: said once, the recap points at it
    for h in hostvars:
        hostvars[h]["_cassandra_apply_config_planned"] = HOSTS
    lines = render(t["ansible.builtin.debug"]["msg"], variables)
    assert lines[-2:] == ["", "1 warning in the plan above (the refusal)."]
    assert not [line for line in lines if line.startswith("WARNING")]


def test_the_recap_says_no_refusal_after_a_real_run():
    nodes = [{"name": "node1", "todo": True, "done": True, "then": "restart", "result": "", "notes": []}]
    assert cassandra_apply_config_recap(nodes, refused=[REASON] + WAY) == ["DONE  apply_config  1 restarted", "node1  applied, restarted"]


def test_the_plan_never_says_the_refusal():
    # a real run refuses before its plan; --check shows no plan: its recap says it
    t = next(x for x in PLAYS[1]["tasks"] if x.get("name") == "Show the plan and confirm the run")
    assert "_cassandra_apply_config_refused" not in t["vars"]["cassandra_screen"]["warnings"]
    assert t["when"] == ["_todo | length > 0", "not ansible_check_mode"]


def test_the_owner_and_mode_changes_are_not_shown_twice():
    # the role (or apply_config's view) shows them: no raw before/after diff under --diff
    role = load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "main.yml"))

    def walk(tasks):
        for t in tasks or []:
            yield t
            yield from walk(t.get("block"))
    tasks = dict((t.get("name"), t) for t in walk(role))
    for name in ("Set the owner and mode of the files left as they are", "Set the owner and mode of the existing ones",
                 "Template Cassandra config files"):
        assert tasks[name]["diff"] is False, name


OUTSIDE = {"stdout": "not started by the cassandra unit (pid 4242 is not in its control group)"}


@pytest.mark.parametrize("unit_running, jvm, cgroup, then, refused", [
    (False, {"pid": "4242"}, OUTSIDE, "restart", True),  # started by hand, its unit inactive: a restart would start another
    # its unit not "running" but the JVM in its control group (an init script's unit, active (exited)): its own
    (False, {"pid": "4242"}, {"stdout": ""}, "restart", False),
    (True, {"pid": "4242"}, {"skipped": True}, "restart", False),  # the unit's own JVM: restarted by it
    (False, {}, {"skipped": True}, "restart", False),  # (no JVM: nothing to refuse)
    (False, {"pid": "4242"}, OUTSIDE, "none", False),  # nothing restarted
])
def test_a_cassandra_its_unit_did_not_start_is_not_restarted(unit_running, jvm, cgroup, then, refused):
    # systemctl restart cassandra next to a JVM outside the unit's control group would start a second one
    t = task("Refuse to restart a Cassandra its unit did not start")
    way_out = next(p for p in PLAYS if p["name"] == "Find what each node needs")["vars"]["_outside_way_out"]
    variables = {"cassandra_apply_config_then": then, "_cassandra_preflight_running": unit_running,
                 "cassandra_jvm": jvm, "cassandra_apply_config_jvm_outside": cgroup, "ansible_check_mode": False,
                 "inventory_hostname": "node1", "_outside_way_out": way_out}
    assert all(render("{{ %s }}" % w, variables) for w in t["when"]) is refused
    assert not render("{{ %s }}" % "not ansible_check_mode", dict(variables, ansible_check_mode=True))  # (--check shows all)
    assert "not ansible_check_mode" in t["when"]
    # drift's compare (apply_config compare-only): not refused either, drift says what differs
    assert not all(render("{{ %s }}" % w, dict(variables, _cassandra_apply_config_compare_only=True)) for w in t["when"])
    if refused:
        msg = " ".join(render(t["ansible.builtin.fail"]["msg"], dict(variables, _why=render(t["vars"]["_why"],
                                                                                         variables))).split())
        # (the fact observed, no guessed cause: audit round 8)
        assert msg.startswith("node1: Cassandra runs (pid 4242) outside its cassandra unit (not in its control"
                              " group). ")
        assert "The restart this run plans (its config) would start a second Cassandra next to it." in msg, msg
        assert "Nothing was changed." in msg
        assert msg.endswith("start it with its unit (systemctl start cassandra), then run apply_config again.")
    # the plan (--check too) says it on the node, with the same way out as the refusal (audit round 6)
    note = task("Note what this node needs")
    if jvm:
        warning = render(note["vars"]["_outside_warning"], dict(variables, _hand_started=True))
        assert warning == ["WARNING  Cassandra runs (pid 4242) outside its cassandra unit (not in its control group): a"
                           " real run refuses its restart (it would start a second Cassandra). " + way_out]
    assert way_out.startswith("Stop that Cassandra (nodetool drain, then stop what started it)")
    hand = render(note["vars"]["_hand_started"], dict(variables, _then=then))
    assert hand is refused


def test_the_jvm_outside_its_unit_is_told_by_its_control_group():
    t = task("Tell whether its unit started that JVM")
    assert "jvm_proof.sh" in t["ansible.builtin.shell"] and "jvm_untrusted" in t["ansible.builtin.shell"]
    assert t["register"] == "cassandra_apply_config_jvm_outside" and t["check_mode"] is False
    assert t["when"] == ["not _cassandra_preflight_running | default(true) | bool", "cassandra_jvm.pid is defined"]
