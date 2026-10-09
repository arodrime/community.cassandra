from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node: the screen shown before the confirmation says whether the new
# nodes get Medusa, and with which fqdn (their folder in the backups). The
# expressions are read from the playbook and rendered by Ansible.

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
    init_plugin_loader()  # the collection's filters, under plain pytest too

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "add_node.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
TASK = [t for p in PLAYS for t in p.get("tasks", []) if t.get("name") == "Show the plan and confirm the add"][0]
with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_service", "tasks", "screen.yml"),
          encoding="utf-8") as f:
    SCREEN = yaml.safe_load(f)[0]


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def summary(new_nodes=("node7",), joining=(), **inventory):
    """The screen's text."""
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
        "cassandra_stream_check_interval": 30, "cassandra_stream_stall_time": 900, "_cassandra_session_warning": "",
        "_single": False, "_auto": "false", "ansible_check_mode": False, "cassandra_operation_confirm": True,
    }
    variables.update(inventory)
    variables.update(trust(TASK["vars"]))
    variables.update(trust(SCREEN["vars"]))
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template("{{ _cassandra_screen_text }}"))


def flat(text):
    """Wrapped lines joined again: one space between words."""
    return " ".join(text.split())


@pytest.mark.parametrize("enabled", [None, False, "false", "no"])
def test_medusa_off(enabled):
    text = summary() if enabled is None else summary(cassandra_medusa_enabled=enabled)
    assert ", Medusa off." in flat(text)
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
    assert line in flat(text)
    assert "\n  node7  10.0.0.7  dc1 / r1\n    Medusa fqdn %s\n" % fqdn in text


@pytest.mark.parametrize("new, joining, cleanup, shown", [
    (["node7"], [], "none", True),
    ([], ["node7"], "none", True),  # a run again that waits for a node still joining: shown and confirmed
    ([], [], "none", False),
    ([], [], "one", True),
])
def test_shown_and_confirmed_when_there_is_something_to_do(new, joining, cleanup, shown):
    variables = {"_new": new, "_joining": joining, "cassandra_add_node_cleanup": cleanup}
    condition = "{{ %s }}" % TASK["when"]
    assert Templar(loader=DataLoader(), variables=variables).template(trust_as_template(condition)) is shown


@pytest.mark.parametrize("cleanup, said", [
    ("none", "Cleanup afterwards (cassandra_add_node_cleanup=none): none, the commands are printed at the end"),
    ("sequential", "Cleanup afterwards (cassandra_add_node_cleanup=sequential): one node at a time"),
    ("one", "Cleanup afterwards (cassandra_add_node_cleanup=sequential): one node at a time"),  # the same, cleanup.yml's word
    ("all", "Cleanup afterwards (cassandra_add_node_cleanup=all): every node at once: heavy I/O"),
])
def test_cleanup_choice_in_the_cleanup_playbook_words(cleanup, said):
    assert said in flat(summary(cassandra_add_node_cleanup=cleanup))


def test_medusa_on_when_only_the_new_node_has_it():
    text = summary(node7={"cassandra_medusa_enabled": True})
    assert "Medusa on (" in flat(text) and "    Medusa fqdn " in text


def test_a_run_again_for_a_joining_node_says_it_waits():
    text = summary(new_nodes=(), joining=("node7",))
    assert "Still bootstrapping, waited for first: node7" in flat(text)
    assert "its progress printed every 30s (sooner at first); the run stops after 15m without progress" in flat(text) and "No node to add" not in text
    assert "No node to add (node7 already in the ring)" in flat(summary(new_nodes=()))


def test_the_copied_medusa_defaults_are_the_role_defaults():
    with open(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_medusa", "defaults", "main.yml"),
              encoding="utf-8") as f:
        defaults = yaml.safe_load(f)
    template = " ".join(TASK["vars"]["cassandra_screen"]["intro"][0].split())
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
    assert "dc1 now: 3 node(s)\ndc1 bisect (no move): 4 node(s)\n" in text
    assert "\n\nWARNING - tokens: dc1: bisect leaves it uneven" in text
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


@pytest.mark.parametrize("inventory, java", [
    # cassandra_install's defaults are loaded: the tarball of the offer, resolved
    ({"cassandra_java_version": "17", "cassandra_java_tarball": "https://mirror.example.com/java/jdk-17.tar.gz"},
     "Java 17 (tarball https://mirror.example.com/java/jdk-17.tar.gz)"),
    ({"cassandra_java_version": "11", "cassandra_java_tarball": "", "cassandra_java_home": "/opt/jdk-11"}, "Java 11 (in /opt/jdk-11)"),
    ({"cassandra_java_version": "17", "cassandra_java_tarball": "", "cassandra_java_home": "", "cassandra_install_java": True},
     "Java 17 (package)"),
    ({"cassandra_java_version": "17", "cassandra_install_java": False}, "Java 17 (set up by other means)"),
])
def test_java_line(inventory, java):
    assert ", %s, Medusa" % java in flat(summary(**inventory))


def test_check_mode_follows_bisect_without_asking():
    # --check asks nothing: neither the choice nor its check run, the tokens follow bisect
    tasks = [t for p in PLAYS for t in p.get("tasks", [])]
    for name in ("Choose where the new nodes go", "Check the choice"):
        when = next(t for t in tasks if t.get("name") == name)["when"]
        for check, runs in ((False, True), (True, False)):
            variables = {"_single": True, "_auto": "true", "_new": ["node7"], "ansible_check_mode": check}
            assert Templar(loader=DataLoader(), variables=variables).template(trust_as_template("{{ %s }}" % when)) is runs
    kind = next(t for t in tasks if t.get("name") == "Give the new node its token")["vars"]["_kind"]
    for check, expected in ((True, "bisect"), (False, "balanced")):
        variables = {"_auto": "true", "ansible_check_mode": check, "ansible_play_hosts_all": ["node7"],
                     "hostvars": {"node7": {"cassandra_token_choice": {"user_input": " Balanced "}}}}
        assert Templar(loader=DataLoader(), variables=variables).template(trust_as_template(kind)).strip() == expected
    text = summary(_single=True, _auto="true", cassandra_add_node_tokens=TOKENS, ansible_check_mode=True)
    assert "--check: a real run asks next, bisect or balanced; this one follows bisect." in flat(text)


def test_reset_line_on_the_node_block():
    # cassandra_add_node_reset (on by default): the plan line of a node that will be reset, before the question
    line = "node7 (dc1/r1): has data (12.0 GiB, cluster 'Test Cluster', not in any ring, down) — will be reset"
    text = summary(node7={"_cassandra_node_reset_plan": {"line": line, "delete": [], "stop": False, "disable": False}})
    assert "node7 10.0.0.7 dc1 / r1 %s" % line in flat(text)
    assert "will be reset" not in summary()
