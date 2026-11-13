from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_seeds import cassandra_seed_layout, cassandra_start_order


def node(name, rack, seed=False, dc="dc1"):
    return {"name": name, "address": "ip-" + name, "dc": dc, "rack": rack, "seed": seed}


def test_usual_layout():
    nodes = [node("n1", "r1", True), node("n2", "r2", True), node("n3", "r3", True), node("n4", "r1")]
    out = cassandra_seed_layout(nodes)
    assert out["lines"] == ["Seeds  dc1  n1 (r1), n2 (r2), n3 (r3)  ok"]
    assert (out["problems"], out["notes"], out["suggested"]) == ([], [], [])


def test_two_seeds_on_one_rack_is_fine():
    nodes = [node("n1", "r1", True), node("n2", "r1", True), node("n3", "r1")]
    out = cassandra_seed_layout(nodes)
    assert out["lines"] == ["Seeds  dc1  n1 (r1), n2 (r1)  ok"] and out["problems"] == []


def test_seeds_sharing_a_rack():
    nodes = [node("n1", "r1", True), node("n2", "r1", True), node("n3", "r2"), node("n4", "r3"), node("n5", "r1", True)]
    out = cassandra_seed_layout(nodes)
    assert out["problems"] == ["dc1: 3 seeds on r1, none on r2, r3: put them on different racks"]
    assert out["lines"] == ["WARNING  Seeds  dc1  n1 (r1), n2 (r1), n5 (r1)  3 seeds on r1, none on r2, r3: put them on"
                            " different racks"]
    assert out["suggested"] == ["ip-n1", "ip-n3", "ip-n4"]


def test_one_seed_is_a_warning_current_seed_kept_first():
    nodes = [node("n1", "r1"), node("n2", "r1", True), node("n3", "r1"), node("n4", "r1")]
    out = cassandra_seed_layout(nodes)
    assert out["problems"] == ["dc1: 1 seed: 2 to 3 per datacenter"]
    assert out["suggested"] == ["ip-n2", "ip-n1"]


def test_no_seed_in_a_datacenter():
    nodes = [node("a1", "r1", True), node("a2", "r2", True), node("b1", "r1", dc="dc2"), node("b2", "r2", dc="dc2")]
    out = cassandra_seed_layout(nodes)
    assert out["lines"] == ["Seeds  dc1  a1 (r1), a2 (r2)  ok", "WARNING  Seeds  dc2  none  no seed"]
    assert out["suggested"] == ["ip-a1", "ip-a2", "ip-b1", "ip-b2"]


def test_small_datacenter_and_several_dcs():
    nodes = [node("a1", "r1", True), node("a2", "r2", True), node("a3", "r3", True),
             node("b1", "r1", True, dc="dc2"), node("b2", "r1", dc="dc2"), node("c1", "r1", True, dc="dc3")]
    out = cassandra_seed_layout(nodes)
    assert out["problems"] == ["dc2: 1 seed: 2 to 3 per datacenter"]  # dc3: one node, its seed
    assert out["suggested"] == ["ip-a1", "ip-a2", "ip-a3", "ip-b1", "ip-b2", "ip-c1"]


def test_more_than_three_seeds_is_a_note():
    nodes = [node("n%d" % i, "r%d" % i, seed=i <= 4) for i in range(1, 6)]
    out = cassandra_seed_layout(nodes)
    assert out["problems"] == [] and out["suggested"] == []
    assert out["notes"] == ["dc1: 4 seeds: 2 to 3 are enough"]
    assert out["lines"] == ["Seeds  dc1  n1 (r1), n2 (r2), n3 (r3), n4 (r4)  note: 4 seeds: 2 to 3 are enough"]


def test_limit_to_a_node_that_is_no_seed_says_nothing():
    # --limit h-n5, cassandra_seeds: [h-n1, h-n2]: h-n5 alone has no seed, the cluster does
    out = cassandra_seed_layout([node("h-n5", "r1")], unread=["h-n1", "h-n2", "h-n3", "h-n4"])
    assert out == {"lines": [], "dcs": [], "problems": [], "notes": [], "suggested": [], "stopped": []}


def test_suggestion_picks_running_nodes_first():
    nodes = [node("n1", "r1"), node("n2", "r2"), node("n3", "r3"), node("b1", "r1", dc="dc2"), node("b2", "r2", dc="dc2")]
    out = cassandra_seed_layout(nodes, unread=[], up=["n2", "n3"])
    # dc1 runs: its running nodes, on 2 racks, are enough; dc2 has none running: any of its nodes, stopped
    assert out["suggested"] == ["ip-n2", "ip-n3", "ip-b1", "ip-b2"]
    assert out["stopped"] == ["b1", "b2"]
    assert rejudged(nodes, out["suggested"]) == []


def test_a_datacenter_with_none_running_gets_the_minimum_stopped_seeds():
    # dc2, 3 racks, none running while dc1 runs: 2 seeds (not 3), both said stopped
    nodes = [node("a1", "r1", True), node("a2", "r2", True),
             node("b1", "r1", dc="dc2"), node("b2", "r2", dc="dc2"), node("b3", "r3", dc="dc2")]
    out = cassandra_seed_layout(nodes, up=["a1", "a2"])
    assert out["suggested"] == ["ip-a1", "ip-a2", "ip-b1", "ip-b2"]
    assert out["stopped"] == ["b1", "b2"]
    assert rejudged(nodes, out["suggested"]) == []
    # not known (help), or nothing running: the rule's 3, none said stopped
    for up in (None, []):
        nodes = [dict(n, seed=n["name"] in ("a1", "a2")) for n in nodes]
        out = cassandra_seed_layout(nodes, up=up)
        assert (out["suggested"][2:], out["stopped"]) == (["ip-b1", "ip-b2", "ip-b3"], [])


def rejudged(nodes, suggested):
    """The seed rule's verdict on nodes with the suggested list as their seeds."""
    for n in nodes:
        n["seed"] = n["address"] in suggested
    return cassandra_seed_layout(nodes)["problems"]


def test_suggestion_with_one_node_running_follows_the_rule():
    # 3 racks, only n2 runs: running only would suggest [n2] alone; one stopped node makes the minimum
    nodes = [node("n1", "r1"), node("n2", "r2"), node("n3", "r3")]
    out = cassandra_seed_layout(nodes, up=["n2"])
    assert out["suggested"] == ["ip-n2", "ip-n1"]
    assert out["stopped"] == ["n1"]
    assert rejudged(nodes, out["suggested"]) == []


def test_suggestion_leaves_out_a_stopped_node_it_does_not_need():
    # 3 racks, n1 and n2 run (2 racks): n3, stopped, is not needed
    nodes = [node("n1", "r1"), node("n2", "r2"), node("n3", "r3")]
    out = cassandra_seed_layout(nodes, up=["n1", "n2"])
    assert (out["suggested"], out["stopped"]) == (["ip-n1", "ip-n2"], [])
    assert rejudged(nodes, out["suggested"]) == []
    # 3 racks running: 3 seeds, all running; not known (help): the rule's 3
    nodes = [node("n1", "r1"), node("n2", "r2"), node("n3", "r3")]
    for up in (["n1", "n2", "n3"], None):
        out = cassandra_seed_layout(nodes, up=up)
        assert (out["suggested"], out["stopped"]) == (["ip-n1", "ip-n2", "ip-n3"], [])


def test_suggestion_with_one_rack_running_covers_the_racks():
    # running only on r1: running only would put both seeds on r1
    nodes = [node("n1", "r1"), node("n2", "r2"), node("n3", "r1"), node("n4", "r2")]
    out = cassandra_seed_layout(nodes, up=["n1", "n3"])
    assert out["suggested"] == ["ip-n1", "ip-n2"]
    assert out["stopped"] == ["n2"]
    assert rejudged(nodes, out["suggested"]) == []


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
