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
    show = task("Show the plan")
    assert show["vars"]["cassandra_output"] is True  # printed by the ops callback
    assert "check=ansible_check_mode" in show["ansible.builtin.debug"]["msg"]  # under --check too
    assert "confirm=cassandra_operation_confirm | bool" in show["ansible.builtin.debug"]["msg"]
    assert "ansible_check_mode" not in str(show.get("when"))
    confirm = task("Confirm the plan")
    assert confirm["ansible.builtin.include_role"]["tasks_from"] == "confirm.yml"
    names = [t.get("name") for t in PLAN["tasks"]]
    assert names.index("Show the plan") + 1 == names.index("Confirm the plan")  # the prompt right under the plan
    plan = {"add": ["n4"], "remove": ["n2"], "seeds": {"step": True}}
    prompt = confirm["vars"]["cassandra_confirm_prompt"]
    steps = confirm["vars"]["_tp_steps"]
    assert render(prompt, _tp_steps=render(steps, cassandra_topology_plan=plan)) == "Run these 3 steps?"
    plan = {"add": [], "remove": [], "seeds": {"step": True}}
    assert render(prompt, _tp_steps=render(steps, cassandra_topology_plan=plan)) == "Run this step?"
    assert "_cassandra_screen_asked_by" not in PLAN.get("vars", {})
    imports = [p for p in PLAYS if "ansible.builtin.import_playbook" in p]
    assert [p["ansible.builtin.import_playbook"] for p in imports] == [
        "community.cassandra.preflight", "community.cassandra.add_node", "community.cassandra.decommission_node"]
    for step in imports[1:]:
        assert step["vars"]["_cassandra_screen_asked_by"] == "topology"
        assert step["vars"]["_cassandra_preflight_skip"] is True


def test_operator_messages_marked_for_the_ops_callback():
    refuse = task("Refuse a plan that can't be done")
    assert refuse["vars"]["cassandra_output"] is True
    msg = render(refuse["ansible.builtin.assert"]["fail_msg"], _tp_cluster="my_cluster", _tp_problems=["a", "b"], _nl="\n",
                 ansible_play_hosts_all=["n1"], hostvars={"n1": {"_cassandra_notes": ["WARNING  cassandra_sedes is not known"]}})
    # the checks' notes kept for the plan come with the refusal (they often say why)
    assert msg == "REFUSED  topology  my_cluster  nothing was changed\n  a\n  b\nWARNING  cassandra_sedes is not known"
    nothing = task("Say there is nothing to do")
    assert nothing["vars"]["cassandra_output"] is True
    plan = {"gone": ["n5", "n6", "n7"], "silent": [], "unknown": ["10.0.0.9 (dc1 / r1, UN)"]}
    hostvars = {"n1": {"_cassandra_notes": ["WARNING  seeds: dc1 has one seed"]}}
    msg = render(nothing["ansible.builtin.debug"]["msg"], _tp_cluster="my_cluster", cassandra_topology_plan=plan,
                 ansible_play_hosts_all=["n1", "n2", "n3"], hostvars=hostvars, _nl="\n").split("\n")
    assert msg == ["NOTHING TO DO  topology  my_cluster  the ring has the 3 nodes of the inventory, they run with the"
                   " seeds of cassandra_seeds",
                   "  already removed: n5..n7 (marked absent, out of the ring, Cassandra stopped): delete them from the"
                   " inventory, or leave them",
                   "WARNING  10.0.0.9 (dc1 / r1, UN) is in the ring but in no host of the inventory: never touched",
                   "WARNING  seeds: dc1 has one seed"]  # the checks' notes kept for the plan
    done = next(p for p in PLAYS if p.get("name") == "Say what is left to do")["tasks"][0]
    assert done["vars"]["cassandra_output"] is True
    plan = {"add": ["n4"], "remove": ["n2"], "gone": [], "silent": [], "seeds": {"step": True, "new": "n1,n4"}}
    variables = dict(done["vars"], hostvars={"n1": {"cassandra_topology_plan": plan,
                                                    "_cassandra_preflight": {"cassandra_cluster_name": "my_cluster"}}},
                     ansible_play_hosts_all=["n1"])
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    msg = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(done["ansible.builtin.debug"]["msg"]))
    assert msg.split("\n") == [
        "DONE  topology  my_cluster  ring = inventory: added n4; seeds now n1,n4; removed n2", "", "TO DO",
        "  1. delete n2 from the inventory, or leave it marked absent (the playbooks leave it out)",
        "  2. wipe its data directories before reusing the host"]


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


def test_notes_kept_for_the_plan_not_printed_on_their_own():
    preflight = next(p for p in PLAYS if p.get("ansible.builtin.import_playbook") == "community.cassandra.preflight")
    assert preflight["vars"]["_cassandra_notes_deferred"] is True
    assert PLAN["vars"]["_cassandra_notes_deferred"] is True
    absent = next(p for p in PLAYS if p.get("name") == "Read the hosts marked absent")
    assert absent["vars"]["_cassandra_notes_deferred"] is True
    for name in ("Add the nodes", "Remove the nodes"):
        assert next(p for p in PLAYS if p.get("name") == name)["vars"]["_cassandra_notes_deferred"] is True
    show = task("Show the plan")["ansible.builtin.debug"]["msg"]
    assert "notes=hostvars[ansible_play_hosts_all[0]]._cassandra_notes" in show and "join(_nl)" in show
    # a check's notes: kept when deferred, printed (one string, marked) otherwise
    with open(os.path.join(TOP, "roles", "cassandra_service", "tasks", "note.yml"), encoding="utf-8") as f:
        keep, say = yaml.safe_load(f)
    kept = render(keep["ansible.builtin.set_fact"]["_cassandra_notes"], _note="WARNING  a\nNOTE b\nWARNING  a",
                  _cassandra_notes=["WARNING  a"])
    assert kept == ["WARNING  a", "NOTE b"]
    assert keep["when"] == "_cassandra_notes_deferred | default(false) | bool"
    assert say["when"] == "not _cassandra_notes_deferred | default(false) | bool"
    assert say["vars"]["cassandra_output"] is True and say["ansible.builtin.debug"]["msg"] == "{{ _note }}"
    # the per-host dumps of the checks: not printed under topology
    with open(os.path.join(TOP, "roles", "cassandra_service", "tasks", "new_node_checks.yml"), encoding="utf-8") as f:
        text = f.read()
    assert text.count("not _cassandra_notes_deferred | default(false) | bool") == 3
    # the stop: a verdict (printed as is) outside topology, unmarked there (topology says it in its refusal)
    checks = yaml.safe_load(text)

    def stops(tasks):
        for t in tasks:
            if str(t.get("name", "")).startswith("Stop before installing anything"):
                yield t
            for key in ("block", "rescue", "always"):
                yield from stops(t.get(key) or [])
    marked, plain = list(stops(checks))
    assert marked["vars"]["cassandra_output"] is True and "cassandra_output" not in plain["vars"]
    assert marked["when"][1] == "not _cassandra_notes_deferred | default(false) | bool"
    assert plain["when"][1] == "_cassandra_notes_deferred | default(false) | bool"
    assert marked["ansible.builtin.fail"] == plain["ansible.builtin.fail"]


def test_a_refused_host_to_add_gives_its_problems_and_the_reset_hint():
    block = task("Check the hosts to add")
    record = block["rescue"][0]
    template = record["ansible.builtin.set_fact"]["_cassandra_topology_refused"]
    msg = "n5 is not ready (the problems are listed above): ... -e cassandra_add_node_reset=true (add_node) ..."

    def refused(problems):
        variables = dict(record["vars"], ansible_failed_result={"msg": msg},
                         _cassandra_new_node_check={"problems": problems})
        variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
        return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template)).strip()

    assert refused(["data directory /d is not empty", "port 7000 in use"]) == (
        "data directory /d is not empty; port 7000 in use; -e cassandra_add_node_reset=true empties it first, if it"
        " holds nothing you need")
    assert refused([]) == msg  # another failure: its message


def test_preflight_notes_render_one_line_each():
    with open(os.path.join(TOP, "playbooks", "preflight.yml"), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    found = {}
    for play in plays:
        for t in play.get("tasks") or []:
            if (t.get("ansible.builtin.include_role") or {}).get("tasks_from") == "note.yml":
                found[t["name"]] = t
    assert sorted(found) == ["Going on without the nodes that did not answer", "Seed layout", "Warn about them"]
    hostvars = {"n1": {"_cassandra_preflight_unknown": {"cassandra_sedes": "cassandra_seeds", "cassandra_x": ""}},
                "n2": {"_cassandra_preflight_unknown": {"cassandra_sedes": "cassandra_seeds"}}}
    t = found["Warn about them"]
    note = render(t["vars"]["_note"], ansible_play_hosts=["n1", "n2"], hostvars=hostvars, _nl="\n")
    assert note.split("\n") == [
        "WARNING  cassandra_sedes (set for n1, n2) is not known to this collection: its roles and playbooks do not read it"
        " (did you mean cassandra_seeds?)",
        "WARNING  cassandra_x (set for n1) is not known to this collection: its roles and playbooks do not read it"]
    t = found["Seed layout"]
    layout = {"lines": ["Seeds  dc1  n1 (r1), n2 (r2)  ok", "WARNING  Seeds  dc2  b1 (r1)  1 seed: 2 to 3 per datacenter"],
              "problems": ["dc2: 1 seed: 2 to 3 per datacenter"], "notes": [], "suggested": ["n1", "n2", "b1", "b2"]}
    cmd = "ansible-playbook community.cassandra.change_seeds -e cassandra_hosts=prod"
    for deferred, lines in ((True, ['WARNING  Seeds  dc2  b1 (r1)  1 seed: 2 to 3 per datacenter',
                                    '  suggested in the group_vars of prod: cassandra_seeds: ["n1", "n2", "b1", "b2"]'
                                    ' (update the inventory: topology then applies it live)']),
                            (False, ['Seeds  dc1  n1 (r1), n2 (r2)  ok',
                                     'WARNING  Seeds  dc2  b1 (r1)  1 seed: 2 to 3 per datacenter',
                                     '  suggested in the group_vars of prod: cassandra_seeds: ["n1", "n2", "b1", "b2"]',
                                     '  then apply it live: ' + cmd])):
        note = render(t["vars"]["_note"], _layout=layout, _cluster="prod", _nl="\n", _change_seeds=cmd,
                      _deferred=deferred)
        assert note.split("\n") == lines
    # a note only (more than 3 seeds): the lines, no suggestion
    layout = {"lines": ["Seeds  dc1  n1 (r1), n2 (r1), n3 (r1), n4 (r1)  note: 4 seeds: 2 to 3 are enough"],
              "problems": [], "notes": ["dc1: 4 seeds: 2 to 3 are enough"], "suggested": []}
    assert render(t["vars"]["_note"], _layout=layout, _cluster="prod", _nl="\n", _change_seeds=cmd,
                  _deferred=False) == layout["lines"][0]
    for problems, notes in (([], []), (["x"], []), ([], ["y"])):
        shown = render("{{ %s }}" % t["when"], _layout={"problems": problems, "notes": notes})
        assert shown is bool(problems or notes)  # a boolean (ansible-core 2.19+ refuses a list)


def test_a_step_of_topology_prints_only_its_token_tables():
    with open(os.path.join(TOP, "roles", "cassandra_service", "tasks", "screen.yml"), encoding="utf-8") as f:
        block = yaml.safe_load(f)[0]["block"]
    plan, step = block[0], block[1]
    assert plan["when"] == "not _cassandra_screen_asked_by | default('')"
    assert step["vars"]["cassandra_output"] is True
    screen = {"intro": ["prose", {"pre": ["node5  token 42"]}],
              "warnings": [{"label": "tokens", "each": ["node5: 42 is taken"]}, {"label": "replication", "text": "x"}]}
    assert render(step["vars"]["_step"], cassandra_screen=screen) == ["node5  token 42", "WARNING  tokens: node5: 42 is taken"]
    assert render(step["vars"]["_step"], cassandra_screen={"intro": ["prose"], "warnings": []}) == []
    # conditionals with a boolean result (ansible-core 2.19+ refuses a string)
    for when in [plan["when"]] + step["when"]:
        for asked_by in ("", "topology"):
            assert isinstance(render("{{ %s }}" % when, _cassandra_screen_asked_by=asked_by, _step=["x"]), bool), when
