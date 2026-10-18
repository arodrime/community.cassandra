from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config with a node not running and restarts to do: a real run refuses before any change; --check shows the
# whole view (every node, the settings) and says the refusal last, with the way out. The expressions are read from
# the playbook and rendered by Ansible with neutral data.

import os
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_outcomes
from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_recap
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    """The views laid out at the terminal's width (width='terminal'): a wide one here, whatever runs the tests."""
    from ansible_collections.community.cassandra.plugins.filter import cassandra_apply_config
    monkeypatch.setattr(cassandra_apply_config, "terminal_width", lambda: 200)


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def task(name, play_name=None):
    for play in PLAYS:
        if play_name and play.get("name") != play_name:
            continue
        for t in play.get("tasks") or []:
            if t.get("name") == name:
                return t
    raise AssertionError("no task %s" % name)


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


# node5 stopped on purpose (left stopped: write), node1 and node2 to restart
HOSTVARS = {
    "node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
              "_cassandra_preflight": {"ring_address": "10.0.0.1"}},
    "node2": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
              "_cassandra_preflight": {"ring_address": "10.0.0.2"}},
    "node3": {"cassandra_apply_config_todo": False, "cassandra_apply_config_then": "none",
              "_cassandra_preflight": {"ring_address": "10.0.0.3"}},
    "node5": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "write",
              "_cassandra_preflight": {"ring_address": "10.0.0.5"}}}
HOSTS = sorted(HOSTVARS)
REASON = ("node5: Cassandra not running, and node1, node2 would be restarted, which needs every other node up.")
WAY = ("Apply the config there first (--limit node5) and start it if stopped on purpose, or, if an interrupted run"
       " stopped it, resume it (-e cassandra_rolling_resume=true); then run again.")


def refused(hostvars):
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trust(t["vars"]), hostvars=hostvars, ansible_play_hosts=HOSTS)
    return render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables)


def test_the_refusal_reason_and_its_way_out():
    assert refused(HOSTVARS) == [REASON, WAY]
    # node5 started by the run before the restarts (its unit failed, it is first): nothing refused
    started = dict(HOSTVARS, node0={"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "start"})
    started.pop("node5")
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trust(t["vars"]), hostvars=started, ansible_play_hosts=sorted(started))
    assert render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables) == []


def test_the_refusal_names_a_failed_unit_this_run_does_not_start():
    # node7's unit failed and nothing is to apply there: in no play (then write, not to do), so --limit node7
    # would do nothing: the way out is on the node
    failed = {"cassandra_apply_config_todo": False, "cassandra_apply_config_then": "write", "cassandra_apply_config_unit_failed": True,
              "_cassandra_preflight": {"ring_address": "10.0.0.7"}}
    hostvars = dict(HOSTVARS, node7=failed)
    t = task("Note whether restarts are refused while a node is not running")
    variables = dict(trust(t["vars"]), hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    reason, way = render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables)
    assert reason == "node5, node7: Cassandra not running, and node1, node2 would be restarted, which needs every other node up."
    # --limit names node5 only (the config applied there, started if stopped on purpose); node7's way out is on the node
    failed_way = ("node7: the cassandra unit failed and nothing is to apply: see systemctl status cassandra / journalctl -u"
                  " cassandra there, fix it and start it, then run again.")
    assert way == WAY + " " + failed_way
    # node7 the only node down: no --limit sentence (nothing to apply there)
    hostvars.pop("node5")
    variables = dict(trust(t["vars"]), hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    assert render(t["ansible.builtin.set_fact"]["_cassandra_apply_config_refused"], variables) == [
        "node7: Cassandra not running, and node1, node2 would be restarted, which needs every other node up.", failed_way]
    # the fact the refusal reads, set with the node's needs
    note = task("Note what this node needs")
    assert note["ansible.builtin.set_fact"]["cassandra_apply_config_unit_failed"] == "{{ _unit_failed }}"


def test_the_plan_says_a_failed_unit_left_stopped_is_not_stopped_on_purpose():
    # node7's unit failed, only its JMX files to write (nothing that starts it): left stopped, but not on purpose
    hostvars = dict(HOSTVARS, node7={"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "write",
                                     "cassandra_apply_config_unit_failed": True})
    t = task("Show the plan and confirm the run")
    nodes = render(t["vars"]["_nodes"], dict(hostvars=hostvars, ansible_play_hosts=sorted(hostvars)))
    assert [n["unit_failed"] for n in nodes] == [False, False, False, False, True]
    assert cassandra_apply_config_outcomes(nodes, plan=True)[1:] == [
        "node5  will apply, left stopped (its Cassandra is stopped: left stopped, reads its config when it starts)",
        "node7  will apply, left stopped (its cassandra unit failed: left as it is)",
        "node3  nothing to apply"]


def test_a_real_run_refuses_before_any_change_check_goes_on():
    t = task("Refuse restarts while a node is not running")
    for check, refused, runs in ((False, [REASON, WAY], True), (True, [REASON, WAY], False), (False, [], False)):
        variables = {"ansible_check_mode": check, "_cassandra_apply_config_refused": refused}
        assert all(render("{{ %s | bool }}" % w, variables) in (True, "True") for w in t["when"]) is runs
    variables = {"_cassandra_apply_config_refused": [REASON, WAY]}
    assert render(t["ansible.builtin.fail"]["msg"], variables) == REASON + " Nothing was changed. " + WAY
    # the refusal comes before anything is written: in the first play, before the plan and its question
    names = [x.get("name") for x in PLAYS[1]["tasks"]]
    assert names.index("Refuse restarts while a node is not running") < names.index("Show the plan and confirm the run")


def test_check_lets_the_stopped_node_stay_down_for_a_restart_node():
    # under --check every node goes in the batch play: node1's check must not stop on node5 (refused, said in the
    # recap); a real run never gets there with node5 down
    t = task("Note the other nodes that may stay down", "Apply the config where nothing is restarted, on these nodes at once")
    expr = t["ansible.builtin.set_fact"]["_cassandra_apply_config_down_ok"]
    variables = dict(HOSTVARS["node1"], hostvars=HOSTVARS, groups={"prod": HOSTS}, cassandra_hosts="prod",
                     inventory_hostname="node1")
    assert render(expr, dict(variables, ansible_check_mode=True)) == ["10.0.0.5"]
    assert render(expr, dict(variables, ansible_check_mode=False)) == []


def test_the_check_recap_ends_with_the_refusal():
    t = task("Print the changes")
    hostvars = dict((h, dict(v, _cassandra_apply_config_refused=[REASON, WAY], cassandra_op_done=v["cassandra_apply_config_todo"],
                             cassandra_op_result="would apply" if v["cassandra_apply_config_todo"] else "nothing to apply"))
                    for h, v in HOSTVARS.items())
    variables = dict(trust(t["vars"]), hostvars=hostvars, groups={"prod": HOSTS}, cassandra_hosts="prod",
                     ansible_play_hosts_all=HOSTS, cassandra_cluster_name="c", ansible_check_mode=True)
    # no plan shown (a later play stopped first): the recap says it, last
    lines = render(t["ansible.builtin.debug"]["msg"], variables)
    assert lines[0] == "CHECK  apply_config  c  3 would apply, 1 nothing to apply"
    assert lines[-3:] == ["", "WARNING - a real run is refused, before changing anything: " + REASON, "  " + WAY]
    # the nodes' outcomes are all there, before it
    assert "node1, node2  would apply, then restart" in lines
    # the plan said it: said once, the recap points at it
    for h in hostvars:
        hostvars[h]["_cassandra_apply_config_planned"] = HOSTS
    lines = render(t["ansible.builtin.debug"]["msg"], variables)
    assert lines[-2:] == ["", "1 warning in the plan above (the refusal)."]
    assert not [line for line in lines if line.startswith("WARNING")]


def test_the_recap_says_no_refusal_after_a_real_run():
    nodes = [{"name": "node1", "todo": True, "done": True, "then": "restart", "result": "", "notes": []}]
    assert cassandra_apply_config_recap(nodes, refused=[REASON, WAY]) == ["DONE  apply_config  1 applied", "node1  applied, restarted"]


def test_the_check_plan_says_it_too():
    # once, in the plan: the recap only points at it
    t = next(x for x in PLAYS[1]["tasks"] if x.get("name") == "Show the plan and confirm the run")
    warnings = t["vars"]["cassandra_screen"]["warnings"]  # the nodes' warnings, then the refusal

    def refusal(variables):
        return render(warnings, dict(variables, _nodes=[]))[-1]

    variables = {"_cassandra_apply_config_refused": [REASON, WAY]}
    assert refusal(dict(variables, ansible_check_mode=True)) == {
        "label": "refused", "text": "a real run is refused, before changing anything: " + REASON + " " + WAY}
    assert refusal(dict(variables, ansible_check_mode=False))["text"] == ""
    assert refusal({"ansible_check_mode": True})["text"] == ""


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
