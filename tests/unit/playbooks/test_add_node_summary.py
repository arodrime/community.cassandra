from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node: the screen shown before the confirmation says whether the new
# nodes get Medusa, and with which fqdn (their folder in the backups). The
# expressions are read from the playbook and rendered by Ansible.

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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "add_node.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
TASK = [t for p in PLAYS for t in p.get("tasks", []) if t.get("name") == "Show what the add will do"][0]


def summary(new_nodes=("node7",), joining=(), **inventory):
    own = inventory.pop("node7", {})
    new = {"_cassandra_preflight": {"address": "10.0.0.7", "cassandra_dc": "dc1", "cassandra_rack": "r1",
                                    "cassandra_cluster_name": "Orders", "cassandra_version": "50x"},
           "ansible_facts": {"hostname": "node7"}}
    new.update(inventory)  # group_vars: also node7's
    new.update(own)
    variables = {
        "hostvars": {"node7": new, "node1": {"cassandra_preflight_describe": {"stdout": ""}}},
        "_new": list(new_nodes), "_joining": list(joining), "_existing": ["node1"], "ansible_play_hosts_all": ["node7"],
        "cassandra_add_node_plan": {"estimate": [], "cleanup": {}, "scope": {}, "warnings": []},
        "cassandra_stream_check_interval": 300, "cassandra_stream_stall_checks": 3, "_cassandra_session_warning": "",
        "_single": False, "_auto": "false",
    }
    variables.update(inventory)
    variables.update((k, trust_as_template(v)) for k, v in TASK["vars"].items())
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(TASK["vars"]["_summary"]))


@pytest.mark.parametrize("enabled", [None, False, "false", "no"])
def test_medusa_off(enabled):
    text = summary() if enabled is None else summary(cassandra_medusa_enabled=enabled)
    assert ", Medusa off\n" in text
    assert "Medusa fqdn" not in text


@pytest.mark.parametrize("inventory, line, fqdn", [
    ({"cassandra_medusa_version": "0.30.1", "cassandra_medusa_venv": "/srv/tools/venv", "cassandra_medusa_fqdn_domain": "db.example.internal"},
     "Medusa on (0.30.1 in /srv/tools/venv)", "node7.db.example.internal"),
    ({}, "Medusa on (0.30.1 in /opt/cassandra-medusa)", "(Medusa works it out)"),
    ({"cassandra_medusa_venv": "", "node7": {"cassandra_medusa_fqdn": "n7.example.org"}},
     "Medusa on (0.30.1 in the system Python)", "n7.example.org"),
    # an explicit "" wins over the domain: medusa.ini gets no fqdn
    ({"cassandra_medusa_fqdn_domain": "db.example.internal", "node7": {"cassandra_medusa_fqdn": ""}},
     "Medusa on (0.30.1 in /opt/cassandra-medusa)", "(Medusa works it out)"),
])
def test_medusa_on(inventory, line, fqdn):
    text = summary(cassandra_medusa_enabled=True, **inventory)
    assert line in text
    assert "node7 (10.0.0.7): datacenter dc1, rack r1, Medusa fqdn %s\n" % fqdn in text


@pytest.mark.parametrize("new, joining, cleanup, shown", [
    (["node7"], [], "none", True),
    ([], ["node7"], "none", True),  # a run again that waits for a node still joining: shown and confirmed
    ([], [], "none", False),
    ([], [], "one", True),
])
def test_shown_and_confirmed_when_there_is_something_to_do(new, joining, cleanup, shown):
    confirm = [t for p in PLAYS for t in p.get("tasks", []) if t.get("name") == "Confirm the add"][0]
    for task in (TASK, confirm):
        variables = {"_new": new, "_joining": joining, "cassandra_add_node_cleanup": cleanup}
        condition = "{{ %s }}" % task["when"]
        assert Templar(loader=DataLoader(), variables=variables).template(trust_as_template(condition)) is shown


def test_medusa_on_when_only_the_new_node_has_it():
    text = summary(node7={"cassandra_medusa_enabled": True})
    assert "Medusa on (" in text and ", Medusa fqdn " in text


def test_a_run_again_for_a_joining_node_says_it_waits():
    text = summary(new_nodes=(), joining=("node7",))
    assert "still bootstrapping, waited for first: node7" in text
    assert "with a progress line" in text and "No node to add" not in text
    assert "No node to add (node7 already in the ring)" in summary(new_nodes=())


def test_the_copied_medusa_defaults_are_the_role_defaults():
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_medusa", "defaults", "main.yml"),
              encoding="utf-8") as f:
        defaults = yaml.safe_load(f)
    template = TASK["vars"]["_summary"]
    assert "cassandra_medusa_version | default('%s')" % defaults["cassandra_medusa_version"] in template
    assert "cassandra_medusa_venv | default('%s')" % defaults["cassandra_medusa_venv"] in template


TOKENS = {"lines": ["dc1 now: 3 node(s)", "dc1 bisect (no move): 4 node(s)"], "warnings": ["dc1: bisect leaves it uneven"],
          "moves": [{"name": "node2"}], "balanced_problems": []}


@pytest.mark.parametrize("auto, said", [
    ("bisect", "Following bisect: no node moves."),
    ("balanced", "Following balanced: then run move_node to move node2 (until then the ring is uneven)."),
    ("true", "You choose next: bisect or balanced."),
    ("false", "Tokens from the inventory (cassandra_initial_token)."),
])
def test_single_token_plan_shown(auto, said):
    text = summary(_single=True, _auto=auto, cassandra_add_node_tokens=TOKENS)
    assert "dc1 bisect (no move): 4 node(s)\n" in text and "WARNING: dc1: bisect leaves it uneven\n" in text
    assert said in text
    assert "One token per node" not in summary(_single=False, _auto=auto, cassandra_add_node_tokens=TOKENS)


CHOICE = [t for p in PLAYS for t in p.get("tasks", []) for t in t.get("block", [])
          if t.get("name") == "Check the token choice"][0]


@pytest.mark.parametrize("auto, confirm, no_token, ok, says", [
    ("false", True, ["node7"], False, "One token per node: node7 has no cassandra_initial_token"),
    ("false", True, [], True, ""),
    ("bisect", False, ["node7"], True, ""),
    ("true", True, ["node7"], True, ""),
    ("true", False, ["node7"], False, "choose one on the command line instead"),
    ("yes", True, [], False, "cassandra_token_auto must be false, true, bisect or balanced (got yes)"),
])
def test_token_choice_checked(auto, confirm, no_token, ok, says):
    variables = {"_auto": auto, "cassandra_operation_confirm": confirm, "_new": ["node7"], "_no_token": no_token}
    templar = Templar(loader=DataLoader(), variables=variables)
    passed = all(templar.template(trust_as_template("{{ %s }}" % c)) for c in CHOICE["ansible.builtin.assert"]["that"])
    assert passed is ok
    if not ok:
        assert says in templar.template(trust_as_template(CHOICE["ansible.builtin.assert"]["fail_msg"]))
