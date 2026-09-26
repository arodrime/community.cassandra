from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_seeds import cassandra_seed_layout


def node(name, rack, seed=False, dc="dc1"):
    return {"name": name, "address": "ip-" + name, "dc": dc, "rack": rack, "seed": seed}


def test_usual_layout():
    nodes = [node("n1", "r1", True), node("n2", "r2", True), node("n3", "r3", True), node("n4", "r1")]
    assert cassandra_seed_layout(nodes) == {"problems": [], "suggested": []}


def test_seeds_sharing_a_rack():
    nodes = [node("n1", "r1", True), node("n2", "r1", True), node("n3", "r2"), node("n4", "r3"), node("n5", "r1", True)]
    out = cassandra_seed_layout(nodes)
    assert out["problems"] == ["dc1: seeds n1, n2 and n5 all on r1, r2 and r3 have none"]
    assert out["suggested"] == ["ip-n1", "ip-n3", "ip-n4"]


def test_one_rack_keeps_current_seeds_first():
    nodes = [node("n1", "r1"), node("n2", "r1", True), node("n3", "r1"), node("n4", "r1")]
    out = cassandra_seed_layout(nodes)
    assert out["problems"] == [
        "dc1 has 1 rack (r1): 3, one per replica with RF=3, lets a whole rack go down",
        "dc1 has 1 seed (n2): 3 is the usual layout",
    ]
    assert out["suggested"] == ["ip-n2", "ip-n1", "ip-n3"]


def test_small_datacenter_and_several_dcs():
    nodes = [node("a1", "r1", True), node("a2", "r2", True), node("a3", "r3", True),
             node("b1", "r1", True, dc="dc2"), node("b2", "r1", dc="dc2")]
    out = cassandra_seed_layout(nodes)
    assert out["problems"] == ["dc2 has 1 seed (b1): 2 is the usual layout"]
    assert out["suggested"] == ["ip-a1", "ip-a2", "ip-a3", "ip-b1", "ip-b2"]


def test_more_racks_than_seeds_is_fine():
    nodes = [node("n%d" % i, "r%d" % i, seed=i <= 3) for i in range(1, 6)]
    assert cassandra_seed_layout(nodes)["problems"] == []
