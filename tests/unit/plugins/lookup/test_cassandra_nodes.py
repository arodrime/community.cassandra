from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The hosts of the cluster without those marked cassandra_node_state: absent:
# what every operation playbook runs on.

import os
import re

import pytest

from ansible.errors import AnsibleError

from ansible_collections.community.cassandra.plugins.lookup.cassandra_nodes import node_states
from ansible_collections.community.cassandra.tests.unit.helpers import render_as_given, trust_as_template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
GROUPS = {"all": ["n1", "n2", "n3", "m1"], "ungrouped": [], "prod": ["n1", "n2", "n3"], "prod_dc1": ["n1", "n2", "n3"],
          "monitoring": ["m1"]}


@pytest.fixture(autouse=True)
def no_cluster_env(monkeypatch):
    monkeypatch.delenv("CASSANDRA_CLUSTER", raising=False)


def test_node_states():
    states = {"n1": None, "n2": "absent", "n3": " Present ", "n4": ""}
    assert node_states(["n1", "n2", "n3", "n4"], states.get) == {"n1": "present", "n2": "absent", "n3": "present",
                                                                 "n4": "present"}


def test_node_states_refuses_other_values():
    with pytest.raises(ValueError, match=r"cassandra_node_state must be present or absent: n2 \(gone\), n3 \(false\)"):
        node_states(["n1", "n2", "n3"], {"n2": "gone", "n3": False}.get)


def render(template, hostvars, **variables):
    variables = dict({"cassandra_hosts": "prod"}, **variables)
    variables.update(groups=variables.get("groups", GROUPS), hostvars=hostvars)
    return render_as_given(template, variables)


def test_lookup():
    hostvars = {"n1": {}, "n2": {"cassandra_node_state": "absent"}, "n3": {"cassandra_node_state": "present"}, "m1": {}}
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars) == ["n1", "n3"]
    assert render("{{ query('community.cassandra.cassandra_nodes', state='absent') }}", hostvars) == ["n2"]
    assert render("{{ query('community.cassandra.cassandra_nodes', group='all') }}", hostvars) == ["n1", "n3", "m1"]
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars, cassandra_hosts="prod_dc1") == ["n1", "n3"]


def test_lookup_cluster_from_the_environment(monkeypatch):
    hostvars = {"n1": {"cassandra_cluster_name": "Prod"}, "n2": {"cassandra_cluster_name": "Prod"},
                "n3": {"cassandra_cluster_name": "Prod", "cassandra_node_state": "absent"}, "m1": {}}
    monkeypatch.setenv("CASSANDRA_CLUSTER", "Prod")
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars, cassandra_hosts=None) == ["n1", "n2"]
    monkeypatch.setenv("CASSANDRA_CLUSTER", "monitoring")
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars, cassandra_hosts=None) == ["m1"]
    # -e cassandra_hosts=prod: another cluster than CASSANDRA_CLUSTER's, refused
    with pytest.raises(AnsibleError, match="-e cassandra_hosts=prod and CASSANDRA_CLUSTER=monitoring .from your shell. disagree"):
        render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars)
    monkeypatch.setenv("CASSANDRA_CLUSTER", "Prod")
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars) == ["n1", "n2"]  # the same one


def test_lookup_templated_state():
    # a group var templated from another var (hostvars hands it over templated in a play)
    hostvars = {"n1": {"cassandra_node_state": trust_as_template("{{ 'absent' if true else 'present' }}")}, "n2": {}, "n3": {},
                "m1": {}}
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars) == ["n2", "n3"]


def test_lookup_error():
    with pytest.raises(AnsibleError, match="must be present or absent: n1 [(]removed[)]"):
        render("{{ query('community.cassandra.cassandra_nodes') }}", {"n1": {"cassandra_node_state": "removed"}})


def test_playbooks_leave_the_absent_hosts_out():
    """The cluster's hosts come from cassandra_nodes; the whole group only where the hosts
    marked absent belong: a host to decommission or reset, the addresses the reset knows, the names."""
    allowed = {
        ("playbooks/decommission_node.yml", "            that: inventory_hostname in groups[lookup('community.cassandra.cassandra_hosts')]"),
        ("playbooks/preflight.yml", "                else []) | select('in', groups[lookup('community.cassandra.cassandra_hosts')]) | list) | unique }}"),
        # its check line: the hosts cassandra_target_nodes names (one to decommission or reset may be marked absent)
        ("playbooks/preflight.yml", "             else query('ansible.builtin.inventory_hostnames', _pattern)"
                                    " | select('in', groups[lookup('community.cassandra.cassandra_hosts')])"),
        ("playbooks/add_node.yml", "          {{ inventory_hostname }} is {{ 'marked cassandra_node_state: absent' if inventory_hostname in"
                                   " groups[lookup('community.cassandra.cassandra_hosts')]"),
        # reset_node empties a host marked absent (removed by topology)
        ("roles/cassandra_operation/tasks/reset_node_plan.yml",
         "    that: inventory_hostname in groups[lookup('community.cassandra.cassandra_hosts')] | default([])"),
        # the addresses where a node stands is read against (the reset's too)
        ("roles/cassandra_operation/tasks/node_standing_read.yml",
         "      {%- for h in groups[lookup('community.cassandra.cassandra_hosts')] | default([]) -%}"),
        # the hosts of the inventory outside the cluster's group (its hosts marked absent are in it)
        ("playbooks/decommission_node.yml",
         "               others=groups['all'] | difference(groups[lookup('community.cassandra.cassandra_hosts')]"
         " | default([])) | length) }}"),
    }
    found = set()
    for base in ("playbooks", "roles"):
        for root, dummy, files in os.walk(os.path.join(TOP, base)):
            for name in files:
                if not name.endswith(".yml"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, TOP)
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        assert not re.search(r"hosts: \"\{\{ lookup\('community.cassandra.cassandra_hosts'\) (\}\}\"|if )", line), rel
                        if "groups[lookup('community.cassandra.cassandra_hosts')]" in line and rel != "playbooks/topology.yml":
                            found.add((rel, line))
    assert found == allowed


class Counted(dict):
    """hostvars counting the reads of a host's variables (each one costs a build of all of them)"""

    def __init__(self, *args, **kwargs):
        super(Counted, self).__init__(*args, **kwargs)
        self.reads = 0

    def __getitem__(self, key):
        self.reads += 1
        return super(Counted, self).__getitem__(key)


def test_lookup_memo():
    hostvars = Counted({"n1": {}, "n2": {"cassandra_node_state": "absent"}, "n3": {}, "m1": {}})
    memo = render("{{ lookup('community.cassandra.cassandra_nodes', memo=true) }}", hostvars)
    assert memo == {"group": "prod", "hosts": ["n1", "n2", "n3"],
                    "states": {"n1": "present", "n2": "absent", "n3": "present"}}
    # with the memo: no host's variables read again
    hostvars.reads = 0
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars, _cassandra_nodes_memo=memo) == ["n1", "n3"]
    assert render("{{ query('community.cassandra.cassandra_nodes', state='absent') }}", hostvars,
                  _cassandra_nodes_memo=memo) == ["n2"]
    assert hostvars.reads == 0
    # another group, or the group's hosts changed (add_host): read again
    assert render("{{ query('community.cassandra.cassandra_nodes', group='all') }}", hostvars,
                  _cassandra_nodes_memo=memo) == ["n1", "n3", "m1"]
    groups = dict(GROUPS, prod=["n1", "n2", "n3", "m1"])
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars, _cassandra_nodes_memo=memo,
                  groups=groups) == ["n1", "n3", "m1"]
    assert hostvars.reads > 0
