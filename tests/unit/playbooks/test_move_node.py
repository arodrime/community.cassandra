from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# move_node: the cleanup record (this run's nodes that lost ranges and the
# ones an interrupted run left), its batches, what is kept when a node is out
# of the run, the reminder of stale inventory tokens and the refusal of moves
# outside the run. The expressions are read from the playbook, rendered by Ansible.

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
    init_plugin_loader()  # the file lookup, under plain pytest too

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "move_node.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
CLEANUP = next(p for p in PLAYS if p.get("name") == "Clean up the nodes that lost ranges")
PLAN = next(p for p in PLAYS if p.get("name") == "Plan the moves")
SUMMARY = next(p for p in PLAYS if p.get("name") == "Summary")


def task(play, name):
    return next(t for t in play["tasks"] if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def cleanup_vars(tmp_path, plan_cleanup, recorded, play_hosts, mode="none"):
    if recorded is not None:
        (tmp_path / "ca-move.cleanup").write_text("".join(h + "\n" for h in recorded))
    hostvars = {"n1": {"cassandra_move_plan": {"cleanup": plan_cleanup}}}
    for h in ["n1", "n2", "n3", "n4"]:
        hostvars.setdefault(h, {})["_cassandra_preflight"] = {"cassandra_dc": "dc1", "cassandra_rack": "r%s" % h[1]}
    return dict(CLEANUP["vars"], hostvars=hostvars, ansible_play_hosts_all=play_hosts, ansible_play_hosts=play_hosts,
                cassandra_progress_dir=str(tmp_path), cassandra_hosts="ca", cassandra_move_cleanup=mode)


@pytest.mark.parametrize("plan_cleanup, recorded, hosts, targets, left", [
    (["n2"], None, ["n1", "n2", "n3"], ["n2"], []),                      # nothing recorded yet
    (["n2"], ["n3", "n2"], ["n1", "n2", "n3"], ["n2", "n3"], []),        # an interrupted run's n3 too
    ([], ["n3"], ["n1", "n2", "n3"], ["n3"], []),                        # nothing moved this run
    (["n2"], ["n4", "10.100.100.9"], ["n1", "n2"], ["n2"], ["n4", "10.100.100.9"]),  # out of the run: kept
])
def test_cleanup_targets_and_what_stays_recorded(tmp_path, plan_cleanup, recorded, hosts, targets, left):
    variables = cleanup_vars(tmp_path, plan_cleanup, recorded, hosts)
    assert render("{{ _targets }}", **variables) == targets
    assert render("{{ _left }}", **variables) == left
    if left:
        content = render(task(CLEANUP, "Keep the cleanups this run could not reach")["ansible.builtin.copy"]["content"],
                         **variables)
        assert content.splitlines() == left


@pytest.mark.parametrize("mode, batches", [
    ("one", [["n2"], ["n3"]]), ("rack", [["n2"], ["n3"]]), ("dc", [["n2", "n3"]]), ("all", [["n2", "n3"]])])
def test_cleanup_batches(tmp_path, mode, batches):
    variables = cleanup_vars(tmp_path, ["n2", "n3"], None, ["n1", "n2", "n3"], mode)
    rendered = render(task(CLEANUP, "Clean up, by batches")["vars"]["_batches"], **variables)
    assert sorted(rendered) == batches


@pytest.mark.parametrize("mode, check, left, forget, keep", [
    ("none", False, [], False, False), ("one", False, [], True, False), ("one", True, [], False, False),
    ("one", False, ["n4"], False, True)])
def test_record_only_rewritten_after_a_cleanup_run(mode, check, left, forget, keep):
    variables = dict(CLEANUP["vars"], cassandra_move_cleanup=mode, ansible_check_mode=check, _left=left)
    assert render("{{ %s }}" % task(CLEANUP, "Forget the cleanups done")["when"], **variables) is forget
    assert render("{{ %s }}" % task(CLEANUP, "Keep the cleanups this run could not reach")["when"], **variables) is keep


def test_stale_inventory_tokens_named():
    reminder = task(SUMMARY, "Say which inventory tokens to update")
    hostvars = {"n1": {"cassandra_initial_token": "-9223372036854775808", "cassandra_move_step": {"to": "5"}},
                "n2": {"cassandra_move_step": {"to": "7"}},
                "n3": {"cassandra_initial_token": None, "cassandra_move_step": {"to": "9"}}}
    stale = render(reminder["vars"]["_stale"], hostvars=hostvars, ansible_play_hosts_all=["n1", "n2", "n3"])
    assert stale == ["n1: 5"]


def test_moves_outside_the_run_refused():
    refuse = task(PLAN, "Refuse a plan that can't be followed")
    plan = {"problems": [], "steps": [{"name": "n1"}, {"name": "10.100.100.9"}]}
    variables = dict(refuse["vars"], _plan=plan, ansible_play_hosts=["n1", "n2"], cassandra_hosts="ca")
    problems = render("{{ _problems }}", **variables)
    assert problems == ["10.100.100.9 would move but is not a node of this run (ca, --limit): add it to the inventory"
                        " group, or give the moves (cassandra_move_tokens)"]
    plan["steps"] = [{"name": "n1"}]
    assert render("{{ _problems }}", **dict(variables, _plan=plan)) == []
