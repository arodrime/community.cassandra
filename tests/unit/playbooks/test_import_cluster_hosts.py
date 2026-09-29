from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's hosts.yml: every node named the same way (its hostname by
# default) with ansible_host its address in the ring, whether it was given by
# name, given by IP or found in the ring.

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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)

MATCH = next(t for play in PLAYS for t in play.get("tasks", []) for t in t.get("block", [t])
             if t.get("name") == "Match the ring with the hosts")

# node1 given by name, 10.0.0.2 given by IP, 10.0.0.3 found in the ring (add_host names it by its address)
HOSTVARS = {
    "node1": {"ansible_host": "node1.mgmt", "ansible_facts": {
        "hostname": "node1", "fqdn": "node1.example.com", "all_ipv4_addresses": ["10.0.0.1"],
        "default_ipv4": {"address": "10.0.0.1"}}},
    "10.0.0.2": {"ansible_facts": {"hostname": "node2", "fqdn": "node2.example.com",
                                   "all_ipv4_addresses": ["10.0.0.2"], "default_ipv4": {"address": "10.0.0.2"}}},
    "10.0.0.3": {"ansible_host": "10.0.0.3", "ansible_facts": {"hostname": "node3", "fqdn": "node3.example.com"}},
    "10.0.0.4": {"ansible_host": "10.0.0.4"},  # found, unreachable: no facts
}
GIVEN = ["node1", "10.0.0.2"]


def entry(address, **options):
    variables = {k: trust_as_template(v) if isinstance(v, str) else v for k, v in MATCH["vars"].items()}
    variables.update(hostvars=HOSTVARS, _given=GIVEN, item={"address": address}, **options)
    templar = Templar(loader=DataLoader(), variables=variables)
    name = templar.template(trust_as_template(MATCH["vars"]["_node"]["name"]))
    return name, templar.template(trust_as_template(MATCH["vars"]["_node"]["ansible_host"]))


@pytest.mark.parametrize("address, expected", [
    ("10.0.0.1", ("node1", "10.0.0.1")),  # given by name (its own ansible_host is not the ring address)
    ("10.0.0.2", ("node2", "10.0.0.2")),  # given by IP
    ("10.0.0.3", ("node3", "10.0.0.3")),  # found in the ring
    ("10.0.0.4", ("10.0.0.4", "10.0.0.4")),  # found, no facts: its address
])
def test_hostname_by_default(address, expected):
    assert entry(address) == expected


def test_fqdn_and_ip():
    assert [entry(a, import_cluster_host_names="fqdn")[0] for a in ("10.0.0.1", "10.0.0.2", "10.0.0.3")] == [
        "node1.example.com", "node2.example.com", "node3.example.com"]
    assert [entry(a, import_cluster_host_names="ip")[0] for a in ("10.0.0.1", "10.0.0.2", "10.0.0.3")] == [
        "10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_given_node_without_facts_keeps_its_name():
    hostvars = dict(HOSTVARS, node1={"ansible_host": "10.0.0.1"})  # matched by its connection address
    variables = {k: trust_as_template(v) if isinstance(v, str) else v for k, v in MATCH["vars"].items()}
    variables.update(hostvars=hostvars, _given=GIVEN, item={"address": "10.0.0.1"})
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template(MATCH["vars"]["_node"]["name"])) == "node1"


def test_ansible_host_can_be_left_out():
    assert entry("10.0.0.1", import_cluster_set_ansible_host=False) == ("node1", "")


def test_host_names_option_checked():
    check = PLAYS[0]["tasks"][0]
    assert check["name"] == "Check the options"
    that = trust_as_template("{{ " + check["ansible.builtin.assert"]["that"] + " }}")
    assert Templar(loader=DataLoader(), variables={}).template(that) is True
    for value, ok in (("fqdn", True), ("ip", True), ("hostname", True), ("FQDN", False), ("name", False)):
        assert Templar(loader=DataLoader(), variables={"import_cluster_host_names": value}).template(that) is ok
