from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Every operation that targets particular nodes takes them in
# cassandra_target_nodes; a former name stops the run. The expressions are read
# from the playbooks, rendered by Ansible.

import glob
import os
import re
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_names import RENAMED

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the inventory_hostnames lookup, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def play(playbook, name):
    return next(p for p in load("playbooks", playbook) if p.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


KINDS = {"add_node": "add", "add_datacenter": "add", "replace_node": "add", "decommission_node": "leave",
         "reset_node": "reset", "remove_dead_node": "dead", "upgrade": "canary"}
IMPORTERS = sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(TOP, "playbooks", "*.yml"))
                   if load("playbooks", os.path.basename(p))[0].get("ansible.builtin.import_playbook") == "community.cassandra.preflight")


@pytest.mark.parametrize("playbook", IMPORTERS)
def test_preflight_told_what_the_targets_are(playbook):
    # every playbook that imports preflight says what it does with cassandra_target_nodes (unset: takes none)
    imported = load("playbooks", playbook + ".yml")[0]
    assert (imported.get("vars") or {}).get("_cassandra_preflight_target") == KINDS.get(playbook)


def test_status_says_it_takes_them():
    include = play("status.yml", "Check the cluster's group")["tasks"][0]
    assert include["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml"
    assert include["vars"]["_cassandra_preflight_target"] == "status"


CHECK = load("roles", "cassandra_service", "tasks", "target_nodes_check.yml")[1]


@pytest.mark.parametrize("target, kind, extra, ok", [
    (None, "", {}, True),
    (None, "add", {}, False),  # required
    (None, "leave", {}, False),
    (None, "dead", {}, False),
    (None, "add", {"_cassandra_preflight_skip": True}, True),  # topology with nothing to add
    ("n2", "add", {"_cassandra_preflight_skip": True}, True),
    (None, "status", {}, True),
    (None, "canary", {"cassandra_upgrade_phase": "canary"}, True),
    ("", "", {}, True),
    ("n2", "", {}, False),  # rolling_restart, apply_config...: it would be ignored
    ("n2", "", {"_cassandra_preflight_skip": True}, True),  # topology's steps
    ("n2", "add", {}, True),
    ("n2,n3", "leave", {}, True),
    (["n2", "n3"], "leave", {}, False),  # a pattern string, as every query of it takes it
    (["n2"], "status", {}, False),
    (5, "add", {}, False),
    ("n2", "canary", {}, False),  # no phase given
    ("n9", "reset", {}, False),  # matches no host
    ("10.0.0.4", "dead", {}, True),  # an address: checked by remove_dead_node
    ("n2", "canary", {"cassandra_upgrade_phase": "canary"}, True),
    ("n2", "canary", {"cassandra_upgrade_phase": "prepare"}, False),
    ("n2,n3", "status", {}, True),
])
def test_target_nodes_only_where_the_playbook_takes_them(target, kind, extra, ok):
    variables = dict(CHECK["vars"], groups={"all": ["n1", "n2", "n3"]}, **extra)
    if target is not None:
        variables["cassandra_target_nodes"] = target
    if kind:
        variables["_cassandra_preflight_target"] = kind
    assert render("{{ %s }}" % CHECK["ansible.builtin.assert"]["that"], **variables) is ok
    # Ansible renders the message whether the assert passes or not
    assert render(CHECK["ansible.builtin.assert"]["fail_msg"], **variables).endswith("Nothing was changed.")


def test_former_names_used_nowhere():
    paths = (glob.glob(os.path.join(TOP, "playbooks", "*.yml")) + glob.glob(os.path.join(TOP, "roles", "**", "*.*"), recursive=True)
             + glob.glob(os.path.join(TOP, "plugins", "*", "*.py")) + [os.path.join(TOP, "README.md")]
             + glob.glob(os.path.join(TOP, "docs", "docsite", "rst", "*.rst"))
             + glob.glob(os.path.join(TOP, "changelogs", "fragments", "*.yml")))
    former = re.compile(r"\b(%s)\b" % "|".join(RENAMED))
    found = {}
    for path in paths:
        if path.endswith("cassandra_names.py") or "__pycache__" in path or not os.path.isfile(path):
            continue  # the list itself
        with open(path, encoding="utf-8", errors="replace") as f:
            for m in former.findall(f.read()):
                found.setdefault(os.path.basename(path), []).append(m)
    # the guide names each one once, as a former name
    assert found == {"guide_roles.rst": sorted(RENAMED, key=list(RENAMED).index)}


def test_former_names_refused_first():
    hosts_check = load("roles", "cassandra_service", "tasks", "hosts_check.yml")
    # (after the ansible-core version)
    assert [t["ansible.builtin.include_tasks"] for t in hosts_check[:2]] == ["core_check.yml", "target_nodes_check.yml"]
    first = play("preflight.yml", "Cassandra preflight checks")["tasks"][:2]
    assert [t["ansible.builtin.include_role"]["tasks_from"] for t in first] == ["core_check.yml", "target_nodes_check.yml"]


STATUS = play("status.yml", "Cluster status")


@pytest.mark.parametrize("target, reached, from_, asked", [
    ("", ["n1", "n2", "n3"], "n1", ["n1", "n2", "n3"]),
    ("", ["n2", "n3"], "n2", ["n1", "n2", "n3"]),
    ("n3", ["n1", "n2", "n3"], "n3", ["n3"]),
    ("n3,n2", ["n1", "n2"], "n2", ["n3", "n2"]),  # the first of them that answers, in their order
    ("n3", ["n1", "n2"], "n3", ["n3"]),  # none answers: n3, which fails
])
def test_status_reads_from_the_target_nodes(target, reached, from_, asked):
    variables = dict(STATUS["vars"], cassandra_target_nodes=target, _reached=reached,
                     ansible_play_hosts_all=["n1", "n2", "n3"], groups={"all": ["n1", "n2", "n3"]})
    assert render("{{ _from }}", **variables) == from_
    assert render("{{ _asked }}", **variables) == asked


UPGRADE = play("upgrade.yml", "Pick the nodes and their order")
CANARY_CHECK = UPGRADE["tasks"][0]["ansible.builtin.assert"]


def canary(target, phase="canary"):
    hostvars = dict((h, {"inventory_hostname": h, "_cassandra_service_is_seed": h == "n1",
                         "_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": "r1"}}) for h in ("n1", "n2", "n3"))
    variables = dict(UPGRADE["vars"], cassandra_upgrade_phase=phase, hostvars=hostvars, cassandra_hosts="prod",
                     ansible_play_hosts=["n1", "n2", "n3"], groups={"all": ["n1", "n2", "n3"], "prod": ["n1", "n2", "n3"]})
    if target is not None:
        variables["cassandra_target_nodes"] = target
    render(CANARY_CHECK["fail_msg"], **variables)  # rendered whether the assert passes or not
    return render("{{ _canary }}", **variables), render("{{ %s }}" % CANARY_CHECK["that"], **variables)


def test_upgrade_canary_is_the_target_node():
    assert canary(None) == ("n2", True)  # the first non-seed
    assert canary("") == ("n2", True)
    assert canary("n3") == ("n3", True)
    assert canary("n2,n3")[1] is False  # one node
    assert canary("n9")[1] is False  # not a node of the run
    assert canary("n3", phase="rolling")[1] is False  # the canary phase only
    assert canary("", phase="rolling")[1] is True


HEALTH = load("roles", "cassandra_service", "tasks", "cluster_health.yml")[0]


@pytest.mark.parametrize("extra, peer", [
    ({}, "n2"),
    ({"cassandra_target_nodes": "n2"}, "n3"),  # a node added or removed is not the peer
    ({"cassandra_target_nodes": "10.0.0.4", "_cassandra_health_not_peers": ""}, "n2"),  # remove_dead_node
    ({"_cassandra_health_not_peers": "n2,n3"}, "n1"),  # remove_datacenter: its group; else this node
])
def test_health_peer_leaves_out_the_target_nodes(extra, peer):
    task = HEALTH["ansible.builtin.set_fact"]
    variables = dict(HEALTH["vars"], inventory_hostname="n1", groups={"all": ["n1", "n2", "n3"], "prod": ["n1", "n2", "n3"]},
                     cassandra_hosts="prod", hostvars={}, **extra)
    assert render(task["_cassandra_health_peer"], **variables) == peer
