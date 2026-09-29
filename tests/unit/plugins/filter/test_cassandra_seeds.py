from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_seeds import cassandra_seed_layout, cassandra_start_order


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


def fact(name, rack, seed=False, dc="dc1"):
    """A host's _cassandra_preflight fact, as cassandra_start_order reads it."""
    return {"address": "ip-" + name, "layout": {"name": name, "dc": dc, "rack": rack, "seed": seed}}


def allocator_accepts(order, facts, hint, ring=None):
    """Replays the token allocator's rule (TokenAllocation.createStrategy, 4.0 to 5.0) on the order."""
    by_name = dict((f["layout"]["name"], f["layout"]) for f in facts)
    racks = dict((dc, [n["rack"] for n in st["nodes"]]) for dc, st in (ring or {}).items())
    for name in order:
        if name not in by_name:
            continue
        layout = by_name[name]
        have = racks.setdefault(layout["dc"], [])
        if layout["rack"] in have and 1 < len(set(have)) < hint:
            return False
        have.append(layout["rack"])
    return True


def test_start_order_keeps_seeds_first_when_it_works():
    facts = [fact("s1", "r1", True), fact("n1", "r1"), fact("s2", "r2", True), fact("n2", "r2"),
             fact("s3", "r3", True), fact("n3", "r3")]
    assert cassandra_start_order(facts, 3) == ["s1", "s2", "s3", "n1", "n2", "n3"]
    # no rack rule (no hint, or 1): seeds then the others, in inventory order
    assert cassandra_start_order(facts, None) == ["s1", "s2", "s3", "n1", "n2", "n3"]
    assert cassandra_start_order(facts, "None") == ["s1", "s2", "s3", "n1", "n2", "n3"]


def test_start_order_puts_a_new_rack_before_a_refused_seed():
    # 2 seeds in r1 and r2, then a third seed in r1: seeds first would start it in a 2-rack ring
    facts = [fact("s1", "r1", True), fact("s2", "r2", True), fact("s3", "r1", True), fact("n1", "r3"), fact("n2", "r2")]
    assert not allocator_accepts(["s1", "s2", "s3", "n1", "n2"], facts, 3)
    order = cassandra_start_order(facts, "3")
    assert order == ["s1", "s2", "n1", "s3", "n2"]
    assert allocator_accepts(order, facts, 3)


def test_start_order_fills_the_single_rack_first():
    # 2 racks for a hint of 3 is refused by preflight, unless the ring... here r1 already runs, and the
    # other r1 node must join before r2 appears
    ring = {"dc1": {"nodes": [{"address": "ip-n1", "rack": "r1"}]}}
    facts = [fact("n1", "r1", True), fact("n2", "r2"), fact("n3", "r1")]
    order = cassandra_start_order(facts, 3, ring)
    assert order == ["n1", "n3", "n2"]
    assert allocator_accepts(order[1:], facts, 3, ring)


def test_start_order_per_datacenter():
    facts = [fact("a1", "r1", True), fact("b1", "r1", True, dc="dc2"), fact("a2", "r2", True), fact("a3", "r1"),
             fact("b2", "r1", dc="dc2"), fact("a4", "r3")]
    order = cassandra_start_order(facts, 3)
    assert order == ["a1", "b1", "a2", "b2", "a4", "a3"]
    assert allocator_accepts(order, facts, 3)
