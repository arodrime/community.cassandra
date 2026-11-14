from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_firewall_manage: false leaves the firewall as it is (nothing
# installed, started or opened); cassandra_firewall_enabled: false leaves the node without one. import_cluster
# carries the firewall of the nodes it reads (rc10), and keeps it as it is on the nodes it could not read.

import os

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.tests.unit.import_nodes import node

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def test_role_does_nothing_when_not_managed():
    with open(os.path.join(ROOT, "roles", "cassandra_firewall", "tasks", "main.yml")) as f:
        main = yaml.safe_load(f)
    setup = next(t for t in main if t["name"] == "Set up the firewall")
    assert setup["ansible.builtin.include_tasks"] == "firewall.yml"
    assert setup["when"] == ["cassandra_firewall_manage | bool", "cassandra_firewall_enabled | bool"]
    none = next(t for t in main if t["name"] == "Leave the node without a firewall")
    assert none["when"] == ["cassandra_firewall_manage | bool", "not cassandra_firewall_enabled | bool"]
    # every change is in firewall.yml or no_firewall.yml
    assert [t for t in main if set(t) & {"ansible.builtin.package", "ansible.builtin.service"}] == []
    with open(os.path.join(ROOT, "roles", "cassandra_firewall", "defaults", "main.yml")) as f:
        defaults = yaml.safe_load(f)
    assert defaults["cassandra_firewall_manage"] is True and defaults["cassandra_firewall_enabled"] is True


def test_import_keeps_the_firewall_of_the_nodes_not_read():
    hv = {"import_cluster_keep": {}, "import_cluster_medusa": {}}
    assert node(hv)["keep"] == {"cassandra_dsbulk_install": False}  # read: carried (cassandra_os)
    assert node({}, read=False)["keep"]["cassandra_firewall_manage"] is False
    base = {"dc": "dc1", "rack": "r1", "read": True, "vars": {"cassandra_cluster_name": "c"}, "hand_edits": [],
            "normalized": [], "notes": []}
    layout = cassandra_inventory_layout([dict(base, name="n1", address="10.0.0.1",
                                              keep={"cassandra_firewall_manage": False})], "c")
    assert layout["host_vars"]["n1"] == {"cassandra_firewall_manage": False}  # nodes added later get the role's
    assert "the firewall (its package, service and ports), or none (cassandra_firewall_manage: false)" in layout["report"]
