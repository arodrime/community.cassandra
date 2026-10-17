from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_topology_hosts import cassandra_topology_hosts


def test_a_host_to_add_and_a_host_to_remove():
    hostvars = {
        "new": {"_cassandra_preflight": {"ring_address": "", "address": "10.0.0.5", "cassandra_dc": "dc1"},
                "_cassandra_topology_address": "10.0.0.5", "cassandra_new_node_state": "blank",
                "_cassandra_new_node_check": {"info": ["i"], "warnings": ["w"]},
                "_cassandra_node_reset_plan": {"delete": [], "stop": "false", "disable": False}},
        "old": {"_cassandra_topology_reachable": "true", "cassandra_leaving_node": {"state": "LEAVING"},
                "_cassandra_new_node_check": {"info": ["not for a removal"]}},
    }
    new, old = cassandra_topology_hosts(["new", "old"], hostvars, ["new"], ["old"], True)
    assert (new["reachable"], new["address"], new["dc"], new["rack"], new["state"]) == (True, "10.0.0.5", "dc1", "?", "blank")
    assert (new["info"], new["checks"], new["reset"], new["names"], new["single"]) == (["i"], ["w"], None, ["new"], True)
    assert (old["absent"], old["reachable"], old["state"], old["info"], old["address"]) == (True, True, "LEAVING", [], "")
