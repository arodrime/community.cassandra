from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The playbook topology: its guards live in its plays (the plan itself is
# tested with the filter cassandra_topology_plan).

import os
import warnings

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
with open(os.path.join(TOP, "playbooks", "topology.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
PLAN = next(p for p in PLAYS if p.get("name") == "Work out the plan and confirm it")


def task(name):
    for t in PLAN["tasks"]:
        if t.get("name") == name:
            return t
    raise AssertionError(name)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_the_plan_asks_once_and_the_steps_ask_nothing():
    screen = task("Show the plan and confirm it")
    assert screen["ansible.builtin.include_role"]["tasks_from"] == "screen.yml"
    assert screen["vars"]["cassandra_screen_question"].strip()
    assert "_cassandra_screen_asked_by" not in screen["vars"] and "_cassandra_screen_asked_by" not in PLAN.get("vars", {})
    imports = [p for p in PLAYS if "ansible.builtin.import_playbook" in p]
    assert [p["ansible.builtin.import_playbook"] for p in imports] == [
        "community.cassandra.preflight", "community.cassandra.add_node", "community.cassandra.decommission_node"]
    for step in imports[1:]:
        assert step["vars"]["_cassandra_screen_asked_by"] == "topology"
        assert step["vars"]["_cassandra_preflight_skip"] is True


def test_check_mode_lines_up_nothing():
    # the steps run on the groups these tasks make: none under --check
    for name in ("Line up the nodes to add, in order", "Line up the nodes to remove, in order"):
        assert task(name)["when"] == "not ansible_check_mode"
    groups = [p["vars"]["cassandra_new_nodes"] for p in PLAYS if p.get("name") == "Add the nodes"]
    groups += [p["vars"]["cassandra_leaving_nodes"] for p in PLAYS if p.get("name") == "Remove the nodes"]
    for template in groups:
        assert render(template, groups={"all": []}) == "localhost:!localhost"
    assert render(groups[0], groups={"cassandra_topology_add": ["n4", "n5"]}) == "n4,n5"


def test_step_host_lists_and_silent_nodes_refused():
    t = task("The run sees the whole group, and a cluster that runs")
    assert "_tp_step_vars | length == 0" in t["ansible.builtin.assert"]["that"]
    assert "_tp_no_facts | length == 0" in t["ansible.builtin.assert"]["that"]
    assert "cassandra_(new|leaving|reset)_nodes" in t["vars"]["_tp_step_vars"]
    hostvars = {"n1": {"inventory_hostname": "n1", "_cassandra_preflight": {}}, "n2": {"inventory_hostname": "n2"}}
    assert render(t["vars"]["_tp_no_facts"], ansible_play_hosts_all=["n1", "n2"], hostvars=hostvars) == ["n2"]


def test_the_add_step_runs_no_cleanup():
    step = next(p for p in PLAYS if p.get("name") == "Add the nodes")
    assert step["vars"]["cassandra_add_node_cleanup"] == "none"


def left_out(limit=None, play_hosts=("n1", "n2", "n3"), absent=("n4",)):
    t = task("The run sees the whole group, and a cluster that runs")
    variables = {"groups": {"all": ["n1", "n2", "n3", "n4"], "prod": ["n1", "n2", "n3", "n4"]}, "_tp_group": "prod",
                 "ansible_play_hosts_all": list(play_hosts), "_tp_absent": list(absent)}
    if limit is not None:
        variables["ansible_limit"] = limit
    return render(t["vars"]["_tp_left_out"], **variables)


def test_limit_that_hides_part_of_the_group_refused():
    assert left_out() == []
    assert left_out(limit="all", play_hosts=("n1", "n2", "n3")) == []
    assert left_out(limit="n1,n2", play_hosts=("n1", "n2")) == ["n3", "n4"]
    assert left_out(limit="n1,n2,n3", play_hosts=("n1", "n2", "n3")) == ["n4"]  # the absent one too


def problems(plan, keyspaces, force=False, error=""):
    variables = dict(PLAN["vars"], cassandra_topology_plan=plan, cassandra_keyspaces=keyspaces,
                     cassandra_decommission_force=force, _cassandra_topology_keyspaces_error=error,
                     _tp_problems=task("Refuse a plan that can't be done")["vars"]["_tp_problems"])
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template("{{ _tp_problems }}"))


PLAN_REMOVE = {"add": [], "remove": ["n3"], "in_ring": ["n3"], "nodes_left": {"dc1": 2}, "problems": []}


def test_replication_refusals():
    rf3 = {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 3}}}
    found = problems(PLAN_REMOVE, rf3)
    assert found[0] == "Removing n3 leaves too few nodes:" and "cassandra_decommission_force" in found[-1]
    assert problems(PLAN_REMOVE, rf3, force=True) == []
    assert problems(PLAN_REMOVE, {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 2}}}) == []
    found = problems(PLAN_REMOVE, None, error="AuthenticationFailed")
    assert found == ["the keyspaces' replication could not be read (AuthenticationFailed): a removal is not checked"
                     " blind. With CQL authentication on, set cassandra_cql_username and cassandra_cql_password."]
    # nothing to remove: the replication does not matter
    assert problems(dict(PLAN_REMOVE, remove=[], in_ring=[]), None) == []


def test_plan_vars_do_not_shadow_the_included_tasks_vars():
    # the plan play includes new_node_checks.yml and the like: a play var they also define as a task var
    # (e.g. _ring) would be read with their value in this play's own expressions
    tasks_dir = os.path.join(TOP, "roles", "cassandra_service", "tasks")
    used = set()
    for name in os.listdir(tasks_dir):
        with open(os.path.join(tasks_dir, name), encoding="utf-8") as f:
            for task in yaml.safe_load(f) or []:
                stack = [task]
                while stack:
                    t = stack.pop()
                    used.update((t.get("vars") or {}).keys())
                    used.update((t.get("ansible.builtin.set_fact") or {}).keys())
                    if t.get("register"):
                        used.add(t["register"])
                    for key in ("block", "rescue", "always"):
                        stack.extend(t.get(key) or [])
    for play in PLAYS:
        clash = set((play.get("vars") or {}).keys()) & used
        assert not clash, (play.get("name"), clash)


def test_steps_in_order_adds_seeds_removals():
    names = [p.get("name") for p in PLAYS]
    assert names.index("Add the nodes") < names.index("Change the seeds") < names.index("Remove the nodes")
    seeds = next(p for p in PLAYS if p.get("name") == "Change the seeds")
    assert seeds["tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "seeds_apply.yml"
    assert render(seeds["hosts"], groups={"all": []}) == []
    assert render(seeds["hosts"], groups={"cassandra_topology_seeds": ["n1", "n2"]}) == ["n1", "n2"]
    line_up = task("Line up the nodes for the seed step")
    assert line_up["when"] == "not ansible_check_mode"  # --check: the plan only
    hosts = ["n1", "n2"]
    # anything to do: the seeds too, a reload at least (files written, a run stopped before the reload)
    assert render(line_up["loop"], ansible_play_hosts_all=hosts, _tp_todo=True) == hosts
    assert render(line_up["loop"], ansible_play_hosts_all=hosts, _tp_todo=False) == []


def test_the_plan_gets_the_seeds_and_no_cap():
    plan = task("Work out the plan")["ansible.builtin.set_fact"]["cassandra_topology_plan"]
    assert "seeds=cassandra_seeds" in plan
    assert "max_removals" not in plan and "confirm=" not in plan and "allow_large" not in plan
    todo = PLAN["vars"]["_tp_todo"]
    assert render(todo, cassandra_topology_plan={"add": [], "remove": [], "seeds": {"step": True}}) is True
    assert render(todo, cassandra_topology_plan={"add": [], "remove": [], "seeds": {"step": False}}) is False
