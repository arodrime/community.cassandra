from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_firewall_manage: false leaves the firewall as it is (nothing
# installed, started or opened); import_cluster sets it for the nodes it imports.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_role_does_nothing_when_not_managed():
    with open(os.path.join(ROOT, "roles", "cassandra_firewall", "tasks", "main.yml")) as f:
        main = yaml.safe_load(f)
    setup = next(t for t in main if t["name"] == "Set up the firewall")
    assert setup["ansible.builtin.include_tasks"] == "firewall.yml"
    assert setup["when"] == "cassandra_firewall_manage | bool"
    # every change is in firewall.yml
    assert [t for t in main if t is not setup and set(t) & {"ansible.builtin.package", "ansible.builtin.service"}] == []
    with open(os.path.join(ROOT, "roles", "cassandra_firewall", "defaults", "main.yml")) as f:
        assert yaml.safe_load(f)["cassandra_firewall_manage"] is True


def test_import_keeps_the_firewall_of_every_node():
    with open(os.path.join(ROOT, "playbooks", "import_cluster.yml")) as f:
        plays = yaml.safe_load(f)
    todo = [t for p in plays for t in p.get("tasks", [])]
    match = None
    while todo:
        t = todo.pop(0)
        if t.get("name") == "Match the ring with the hosts":
            match = t["vars"]
        todo += t.get("block", [])
    keep = match["_node"]["keep"]
    hv = {"import_cluster_keep": {}, "import_cluster_medusa": {}}
    assert render(keep, _hv=hv, _read=True, _env_log_dir={}, _java_link={}, _hand_kept={}, _repo={"manage": True},
                  _installed={}) == {
        "cassandra_firewall_manage": False}
    assert render(keep, _hv={}, _read=False)["cassandra_firewall_manage"] is False
    base = {"dc": "dc1", "rack": "r1", "read": True, "vars": {"cassandra_cluster_name": "c"}, "hand_edits": [],
            "normalized": [], "notes": []}
    layout = cassandra_inventory_layout([dict(base, name="n1", address="10.0.0.1",
                                              keep={"cassandra_firewall_manage": False})], "c")
    assert layout["host_vars"]["n1"] == {"cassandra_firewall_manage": False}  # nodes added later get the role's
    assert "the firewall (its package, service and ports), or none (cassandra_firewall_manage: false)" in layout["report"]
