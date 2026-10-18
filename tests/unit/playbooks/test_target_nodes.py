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


KINDS = {}
IMPORTERS = sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(TOP, "playbooks", "*.yml"))
                   if load("playbooks", os.path.basename(p))[0].get("ansible.builtin.import_playbook") == "community.cassandra.preflight")


def test_the_operations_import_preflight():
    assert IMPORTERS == ["create_cluster", "rolling_reboot", "rolling_restart", "stop_rack"]  # none takes targets yet


@pytest.mark.parametrize("playbook", IMPORTERS)
def test_preflight_told_what_the_targets_are(playbook):
    # every playbook that imports preflight says what it does with cassandra_target_nodes (unset: takes none)
    imported = load("playbooks", playbook + ".yml")[0]
    assert (imported.get("vars") or {}).get("_cassandra_preflight_target") == KINDS.get(playbook)


def test_status_says_it_takes_them():
    include = play("status.yml", "Check the cluster's group")["tasks"][0]
    assert include["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml"
    assert include["vars"]["_cassandra_preflight_target"] == "status"


CHECK = load("roles", "cassandra_operation", "tasks", "target_nodes_check.yml")[1]


@pytest.mark.parametrize("target, kind, extra, ok", [
    (None, "", {}, True),
    (None, "status", {}, True),
    ("", "", {}, True),
    ("n2", "", {}, False),  # a playbook that takes none: it would be ignored
    (["n2"], "status", {}, False),  # a pattern string, as every query of it takes it
    (5, "status", {}, False),
    ("n9", "status", {}, False),  # matches no host
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
    # (the fragment of the rename names its former name)
    assert found == {"config-user-naming.yml": ["cassandra_config_owner"]}


def test_former_names_refused_first():
    hosts_check = load("roles", "cassandra_operation", "tasks", "hosts_check.yml")
    assert hosts_check[0]["ansible.builtin.include_tasks"] == "target_nodes_check.yml"
    # (after the task that gives each node its own facts, and its check)
    first = play("preflight.yml", "Cassandra preflight checks")["tasks"][2]
    assert first["ansible.builtin.include_role"]["tasks_from"] == "target_nodes_check.yml"


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


HEALTH = load("roles", "cassandra_operation", "tasks", "cluster_health.yml")[0]


def test_health_peer_is_another_node():
    task = HEALTH["ansible.builtin.set_fact"]
    variables = dict(HEALTH["vars"], inventory_hostname="n1", groups={"all": ["n1", "n2", "n3"], "prod": ["n1", "n2", "n3"]},
                     cassandra_hosts="prod", hostvars={})
    assert render(task["_cassandra_health_peer"], **variables) == "n2"


def test_a_former_name_set_for_any_node_refused_in_preflight():
    """A former name in the vars of a group the first host is not in (a datacenter's, a rack's) or in another node's
    host_vars: variables_check.yml does not see it, preflight refuses it (not only a WARNING)."""
    tasks = play("preflight.yml", "Cassandra preflight checks")["tasks"]
    names = [t.get("name") for t in tasks]
    refuse = tasks[names.index("No variable under a former name on any node")]
    assert names.index("Find the cassandra_* variables no role or playbook knows") < names.index(refuse["name"]) \
        < names.index("Warn about them")
    assert refuse["run_once"] and refuse["any_errors_fatal"]
    hostvars = {"n1": {"_cassandra_preflight_unknown": {}},
                "n2": {"_cassandra_preflight_unknown": {"cassandra_config_owner": "cassandra_config_user",
                                                        "cassandra_heap_sise": "cassandra_heap_size"}},
                "n3": {"_cassandra_preflight_unknown": {"cassandra_config_owner": "cassandra_config_user"}},
                "n4": {}}  # not read (unreachable)
    variables = dict(refuse["vars"], ansible_play_hosts=["n1", "n2", "n3", "n4"], hostvars=hostvars)
    assert render("{{ %s }}" % refuse["ansible.builtin.assert"]["that"], **variables) is False
    msg = render(refuse["ansible.builtin.assert"]["fail_msg"], **variables)
    assert msg.splitlines() == ["REFUSED  variable names  nothing was changed",
                                "  cassandra_config_owner (set for n2, n3) is renamed cassandra_config_user:"
                                " set cassandra_config_user instead."]
    hostvars["n2"]["_cassandra_preflight_unknown"].pop("cassandra_config_owner")
    hostvars["n3"]["_cassandra_preflight_unknown"] = {}
    assert render("{{ %s }}" % refuse["ansible.builtin.assert"]["that"], **dict(variables, hostvars=hostvars)) is True
