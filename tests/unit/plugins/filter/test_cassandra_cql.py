from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_cql_host: where cqlsh, on a node, reaches its Cassandra: the address Cassandra binds by its own rule.

import glob
import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_cql import cassandra_cql_host

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
FACTS = {"nodename": "db1.example", "hostname": "db1", "fqdn": "db1.example",
         "eth1": {"ipv4": {"address": "192.0.2.11"}, "ipv6": [{"address": "fe80::1"}, {"address": "2001:db8::11"}]},
         "bond0_12": {"ipv4": {"address": "192.0.2.12"}}}


def node(**variables):
    return dict({"inventory_hostname": "db1", "ansible_host": "10.0.0.1", "ansible_facts": FACTS}, **variables)


@pytest.mark.parametrize("variables, host", [
    ({}, "127.0.0.1"),  # not set: the role's default, localhost
    ({"cassandra_rpc_address": "localhost"}, "127.0.0.1"),
    ({"cassandra_rpc_address": "127.0.0.2"}, "127.0.0.2"),
    ({"cassandra_rpc_address": "0.0.0.0"}, "127.0.0.1"),  # every address: loopback too
    ({"cassandra_rpc_address": "::"}, "127.0.0.1"),
    ({"cassandra_rpc_address": "10.0.0.9"}, "10.0.0.9"),
    # blank (or null): what the node's own name resolves to, as Cassandra does; not loopback
    ({"cassandra_rpc_address": ""}, "db1.example"),
    ({"cassandra_rpc_address": None}, "db1.example"),
    # rpc_interface: that interface's address (its IPv6 one, not link-local, when preferred)
    ({"cassandra_extra_settings": {"rpc_interface": "eth1"}}, "192.0.2.11"),
    ({"cassandra_extra_settings": {"rpc_interface": "eth1", "rpc_interface_prefer_ipv6": True}}, "2001:db8::11"),
    ({"cassandra_extra_settings": {"rpc_interface": "bond0.12"}}, "192.0.2.12"),
    ({"cassandra_extra_settings": {"rpc_interface": "eth9"}}, "10.0.0.1"),  # not in the facts: the node's address
    # what the import wrote: the hostname or fqdn fact, another fact (the node's address)
    ({"cassandra_rpc_address": "{{ ansible_facts['hostname'] }}"}, "db1"),
    ({"cassandra_rpc_address": "{{ ansible_facts['fqdn'] }}"}, "db1.example"),
    ({"cassandra_rpc_address": "{{ ansible_facts['default_ipv4']['address'] }}"}, "10.0.0.1"),
])
def test_the_address_cassandra_binds(variables, host):
    assert cassandra_cql_host(node(**variables)) == host


def test_without_facts_the_node_s_address():
    assert cassandra_cql_host({"cassandra_rpc_address": "", "ansible_host": "10.0.0.1"}) == "10.0.0.1"
    assert cassandra_cql_host({"cassandra_rpc_address": "", "inventory_hostname": "db1"}) == "db1"
    # the import: its own facts and address
    assert cassandra_cql_host({"cassandra_rpc_address": ""}, facts={"hostname": "db2"}, address="10.0.0.2") == "db2"
    assert cassandra_cql_host({"cassandra_rpc_address": "{{ ansible_facts['default_ipv4']['address'] }}"}, facts={},
                              address="10.0.0.2") == "10.0.0.2"


def test_every_cqlsh_call_takes_the_rule():
    # one rule for every cqlsh of the collection (storage_mode_read.yml: an instance's own address, its rule)
    found = []
    for path in glob.glob(os.path.join(TOP, "playbooks", "*.yml")) + glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml")):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for task in yaml.safe_load(text) or []:
            for block in [task] + [t for k in ("block", "rescue", "always") for t in task.get(k, [])] + [
                    t for p in [task] for k in ("tasks",) for t in p.get(k, []) for t in [t] + t.get("block", [])]:
                call = block.get("community.cassandra.cassandra_cqlsh") if isinstance(block, dict) else None
                if call:
                    found.append(os.path.basename(path))
                    assert ("cassandra_cql_host" in call["cqlsh_host"] or call["cqlsh_host"] in (
                        "{{ _cassandra_cql_login_host }}", "{{ _cassandra_storage_mode_cql_host }}")), path
    assert len(found) >= 9, found
