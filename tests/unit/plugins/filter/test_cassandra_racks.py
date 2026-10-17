from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The check as preflight renders it: tests/unit/playbooks/test_preflight_racks.py

from ansible_collections.community.cassandra.plugins.filter.cassandra_racks import cassandra_rack_refusals


def host(name, rack, address):
    return {"cassandra_rack": rack, "address": address, "layout": {"name": name}}


def test_no_ring_the_inventory_racks():
    assert cassandra_rack_refusals([host("a", "r1", "1"), host("b", "r2", "2")], [], "3") == ["the inventory"]
    assert cassandra_rack_refusals([host("a", "r1", "1"), host("b", "r1", "2")], [], 3) == []


def test_each_new_node_joins_the_ring_the_next_one_sees():
    ring = [{"address": "1", "rack": "r1"}]
    new = [host("b", "r2", "2"), host("c", "r1", "3")]
    # b brings a 2nd rack (fewer than 3): c then joins a rack already there
    assert cassandra_rack_refusals(new, ring, 3) == ["c in r1 when the ring has r1, r2"]
    # in the other order, c joins the single rack first
    assert cassandra_rack_refusals(new, ring, 3, order=["c", "b"]) == []
    assert cassandra_rack_refusals(new, ring, 3, replacing=True) == []
