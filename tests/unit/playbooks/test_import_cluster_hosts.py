from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's hosts.yml: every node named the same way (its hostname by
# default) with ansible_host its address in the ring, whether it was given by
# name, given by IP or found in the ring.

import os
import re

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import_nodes import (
    KEYS, cassandra_import_host_vars, cassandra_import_nodes)
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)

FIND = next(play for play in PLAYS if play["name"] == "Find the cluster from the given nodes")

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


def nodes(address, hostvars=None, **options):
    ring = [{"address": address, "dc": "dc1", "rack": "r1"}]
    return cassandra_import_nodes(ring, hostvars or HOSTVARS, GIVEN, ["10.0.0.3", "10.0.0.4"], **options)[0]


def entry(address, **options):
    out = nodes(address, **options)
    return out["name"], out["ansible_host"]


@pytest.mark.parametrize("address, expected", [
    ("10.0.0.1", ("node1", "10.0.0.1")),  # given by name (its own ansible_host is not the ring address)
    ("10.0.0.2", ("node2", "10.0.0.2")),  # given by IP
    ("10.0.0.3", ("node3", "10.0.0.3")),  # found in the ring
    ("10.0.0.4", ("10.0.0.4", "10.0.0.4")),  # found, no facts: its address
])
def test_hostname_by_default(address, expected):
    assert entry(address) == expected


def test_fqdn_and_ip():
    assert [entry(a, host_names="fqdn")[0] for a in ("10.0.0.1", "10.0.0.2", "10.0.0.3")] == [
        "node1.example.com", "node2.example.com", "node3.example.com"]
    assert [entry(a, host_names="ip")[0] for a in ("10.0.0.1", "10.0.0.2", "10.0.0.3")] == [
        "10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_given_node_without_facts_keeps_its_name():
    hostvars = dict(HOSTVARS, node1={"ansible_host": "10.0.0.1"})  # matched by its connection address
    assert nodes("10.0.0.1", hostvars)["name"] == "node1"


def test_ansible_host_can_be_left_out():
    assert entry("10.0.0.1", set_ansible_host=False) == ("node1", "")


def test_host_names_option_checked():
    check = next(t for t in FIND["tasks"] if t["name"] == "Check the options")
    assert check["name"] == "Check the options"
    that = trust_as_template("{{ " + check["ansible.builtin.assert"]["that"] + " }}")
    assert Templar(loader=DataLoader(), variables={}).template(that) is True
    for value, ok in (("fqdn", True), ("ip", True), ("hostname", True), ("FQDN", False), ("name", False)):
        assert Templar(loader=DataLoader(), variables={"import_cluster_host_names": value}).template(that) is ok


def test_hostname_given_to_the_layout():
    # for the Medusa fqdn rule: <short hostname>.<domain>
    assert nodes("10.0.0.2", host_names="ip")["hostname"] == "node2"


def test_host_names_message_renders_unset():
    # 2.19+ templates fail_msg even when the assert passes
    msg = trust_as_template(next(t for t in FIND["tasks"] if t["name"] == "Check the options")["ansible.builtin.assert"]["fail_msg"])
    assert "hostname, fqdn or ip" in Templar(loader=DataLoader(), variables={}).template(msg)


def test_other_connection_address_noted():
    notes = nodes("10.0.0.1")["notes"]
    assert "reached at node1.mgmt, its hosts file has its ring address 10.0.0.1" in notes[-1]
    notes = nodes("10.0.0.2")["notes"]
    assert not any("reached at" in n for n in notes)


class _Vars(dict):
    """hostvars as Ansible gives them: a host's variables worked out when it is looked up."""
    def __init__(self, *args, **kwargs):
        super(_Vars, self).__init__(*args, **kwargs)
        self.looked_up = []

    def __getitem__(self, host):
        self.looked_up.append(host)
        return dict.__getitem__(self, host)


def test_each_host_read_once_with_what_the_import_needs():
    hostvars = _Vars(HOSTVARS, node1=dict(HOSTVARS["node1"], import_cluster_config={"vars": {}}, other_var=1))
    data = cassandra_import_host_vars(hostvars, ["node1", "10.0.0.2", "node1", "10.0.0.9"])
    assert hostvars.looked_up == ["node1", "10.0.0.2"]  # once each; a name that is not a host left out
    assert set(data) == {"node1", "10.0.0.2"}
    assert set(data["node1"]) == {"ansible_host", "ansible_facts", "import_cluster_config"}  # what the import reads only


def test_variables_read_are_the_ones_taken():
    # each import_cluster_* variable the filters read is one cassandra_import_host_vars takes
    root = os.path.join(os.path.dirname(PLAYBOOK), "..", "plugins", "filter")
    used = set()
    for name in ("cassandra_import_nodes.py", "cassandra_import_check.py"):
        with open(os.path.join(root, name), encoding="utf-8") as f:
            used |= set(re.findall(r"""hv(?:\.get\(|\[)"((?:import_cluster|ansible)_\w+)\"""", f.read()))
    assert used and used <= set(KEYS)


def test_task_gives_the_filter_what_it_reads():
    load_collection_filters()
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    match = next(t for t in next(t for t in write["tasks"] if t.get("name") == "Build the inventory")["block"]
                 if t["name"] == "Match the ring with the hosts")
    ring = [{"address": a, "dc": "dc1", "rack": "r1"} for a in ("10.0.0.1", "10.0.0.2", "10.0.0.3")]
    variables = dict(match["vars"], hostvars=HOSTVARS, _given=GIVEN, _ring=ring, _jmx={},
                     groups={"import_cluster_found": ["10.0.0.3"]}, _java_default=write["vars"]["_java_default"])
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    nodes = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(match["ansible.builtin.set_fact"]["_nodes"]))
    assert [(n["host"], n["name"]) for n in nodes] == [("node1", "node1"), ("10.0.0.2", "node2"), ("10.0.0.3", "node3")]
