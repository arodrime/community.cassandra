from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.filter.cassandra_replication import (
    cassandra_keyspaces,
    cassandra_rack_down_problems,
    cassandra_replication_alter,
    cassandra_replication_problems,
)

CQLSH = """
 [json]
-------------------------------------------------------------------------------------------------------------------
 {"keyspace_name": "orders", "replication": {"class": "org.apache.cassandra.locator.NetworkTopologyStrategy", "dc1": "3", "dc2": "2"}}
 {"keyspace_name": "system", "replication": {"class": "org.apache.cassandra.locator.LocalStrategy"}}
 {"keyspace_name": "system_auth", "replication": {"class": "org.apache.cassandra.locator.SimpleStrategy", "replication_factor": "1"}}
 {"keyspace_name": "system_traces", "replication": {"class": "org.apache.cassandra.locator.SimpleStrategy", "replication_factor": "2"}}

(4 rows)
"""


def test_keyspaces():
    assert cassandra_keyspaces(CQLSH) == {
        "orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 3, "dc2": 2}},
        "system": {"class": "LocalStrategy", "rf": {}},
        "system_auth": {"class": "SimpleStrategy", "rf": {"*": 1}},
        "system_traces": {"class": "SimpleStrategy", "rf": {"*": 2}},
    }


def test_enough_nodes():
    assert cassandra_replication_problems(cassandra_keyspaces(CQLSH), {"dc1": 3, "dc2": 2}) == []


def test_too_few_nodes_in_a_dc_and_in_the_cluster():
    assert cassandra_replication_problems(cassandra_keyspaces(CQLSH), {"dc1": 1, "dc2": 0}) == [
        "orders needs 3 replicas in dc1, which would keep 1 node(s)",
        "orders needs 2 replicas in dc2, which would keep 0 node(s)",
        "system_traces (SimpleStrategy, RF 2) needs 2 nodes, the cluster would keep 1",
    ]


def test_not_json():
    with pytest.raises(AnsibleFilterError):
        cassandra_keyspaces(" {not json")


def test_rack_down_safe_with_three_racks():
    out = cassandra_rack_down_problems(cassandra_keyspaces(CQLSH), "dc1", 3)
    assert out["problems"] == ["system_traces uses SimpleStrategy with RF 2, which ignores racks: "
                               "a rack down can hold several of its replicas"]
    assert out["warnings"] == []


def test_rack_down_too_few_racks_and_rf2_warning():
    keyspaces = {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 3, "dc2": 2}}}
    assert cassandra_rack_down_problems(keyspaces, "dc1", 2)["problems"] == [
        "orders has 3 replicas in dc1, which has 2 rack(s): a rack holds more than one of them"]
    assert cassandra_rack_down_problems(keyspaces, "dc2", 3) == {
        "problems": [], "warnings": ["orders has 2 replicas in dc2: with one down, (LOCAL_)QUORUM fails"]}


def test_alter_add_dc():
    keyspaces = cassandra_keyspaces(CQLSH)
    assert cassandra_replication_alter(keyspaces, "dc3", add={"orders": 2}) == [
        "ALTER KEYSPACE \"orders\" WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3, 'dc2': 2, 'dc3': 2};"]
    assert cassandra_replication_alter(keyspaces, "dc2", add={"orders": 2}) == []  # already so
    with pytest.raises(AnsibleFilterError, match="SimpleStrategy"):
        cassandra_replication_alter(keyspaces, "dc3", add={"system_traces": 1})


def test_alter_remove_dc():
    keyspaces = cassandra_keyspaces(CQLSH)
    assert cassandra_replication_alter(keyspaces, "dc2", remove=True) == [
        "ALTER KEYSPACE \"orders\" WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3};"]
    only_dc2 = {"logs": {"class": "NetworkTopologyStrategy", "rf": {"dc2": 1}}}
    with pytest.raises(AnsibleFilterError, match="only has replicas in dc2"):
        cassandra_replication_alter(only_dc2, "dc2", remove=True)
