from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_firewall_manage: false leaves the firewall as it is (nothing
# installed, started or opened); import_cluster sets it for the nodes it imports.

import os

import yaml


ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


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
