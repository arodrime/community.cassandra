from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The hosts of the cluster without those marked cassandra_node_state: absent:
# what every operation playbook runs on.

import os
import re

import pytest

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.lookup.cassandra_nodes import node_states

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
GROUPS = {"all": ["n1", "n2", "n3", "m1"], "ungrouped": [], "prod": ["n1", "n2", "n3"], "prod_dc1": ["n1", "n2", "n3"],
          "monitoring": ["m1"]}


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
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_lookup():
    hostvars = {"n1": {}, "n2": {"cassandra_node_state": "absent"}, "n3": {"cassandra_node_state": "present"}, "m1": {}}
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars) == ["n1", "n3"]
    assert render("{{ query('community.cassandra.cassandra_nodes', state='absent') }}", hostvars) == ["n2"]
    assert render("{{ query('community.cassandra.cassandra_nodes', group='all') }}", hostvars) == ["n1", "n3", "m1"]
    assert render("{{ query('community.cassandra.cassandra_nodes') }}", hostvars, cassandra_hosts="prod_dc1") == ["n1", "n3"]


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
    marked absent belong: a host to decommission, the addresses the reset knows, the names."""
    allowed = {
        ("playbooks/decommission_node.yml", "            that: inventory_hostname in groups[lookup('community.cassandra.cassandra_hosts')]"),
        ("playbooks/preflight.yml", "               | select('in', groups[lookup('community.cassandra.cassandra_hosts')]) | list) | unique }}"),
        ("playbooks/add_node.yml", "          {{ inventory_hostname }} is {{ 'marked cassandra_node_state: absent' if inventory_hostname in"
                                   " groups[lookup('community.cassandra.cassandra_hosts')]"),
        ("roles/cassandra_service/tasks/reset_node_plan.yml",
         "      ', marked cassandra_node_state: absent' if inventory_hostname in groups[lookup('community.cassandra.cassandra_hosts')]"
         " else '' }}):"),
        ("roles/cassandra_service/tasks/reset_node_plan.yml",
         "      {%- for h in groups[lookup('community.cassandra.cassandra_hosts')] | default([]) -%}"),
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
