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


def _firewall():
    from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import load, walk
    return [t.get("name") for t in walk(load("roles", "cassandra_firewall", "tasks", "firewall.yml"))], load


def test_the_ports_are_open_before_the_firewall_starts_and_never_closed_first():
    names, load = _firewall()
    start = names.index("Ensure firewall service is started")
    for name in ("Ensure ports for cassandra open (RedHat)", "Ensure ports for cassandra open (Debian)",
                 "Open them to their sources (RedHat)", "Open them to their sources (Debian)"):
        assert names.index(name) < start, name
    # to their sources first, then closed to everyone else: no moment with the port closed to them
    for family in ("RedHat", "Debian"):
        assert names.index("Open them to their sources (%s)" % family) < names.index("Close them to everyone (%s)" % family)


def test_the_node_s_moved_ports_are_said_not_opened():
    # (opened to everyone, an imported node reaching them through a zone of its own got them on its public side:
    # a change after the import)
    from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import load, render
    variables = dict(load("roles", "cassandra_firewall", "defaults", "main.yml"))
    variables.update(load("roles", "cassandra_firewall", "vars", "main.yml"))
    unopened = "{{ _cassandra_firewall_own_unopened }}"
    assert render(unopened, variables) == []
    moved = dict(variables, cassandra_storage_port=7100, cassandra_native_transport_port=9142, cassandra_jmx_port=7299,
                 cassandra_local_jmx=False)
    assert render(unopened, moved) == ["7100/tcp", "9142/tcp", "7299/tcp"]
    assert render(unopened, dict(moved, open_ports=["7100/tcp"], cassandra_firewall_port_sources={"7299/tcp": ["10.0.0.1"]})) \
        == ["9142/tcp"]
    tasks = load("roles", "cassandra_firewall", "tasks", "firewall.yml")
    assert "_cassandra_firewall_open_ports" not in str(tasks)
