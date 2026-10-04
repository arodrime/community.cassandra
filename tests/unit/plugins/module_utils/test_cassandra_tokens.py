# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function
__metaclass__ = type

import random
from fractions import Fraction

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.cassandra_tokens import (
    TokenError, _gap, balanced_positions, balanced_tokens, dc_offset, move_order, ownership, parse_ring,
    parse_token, partitioner_range, plan_balanced, plan_bisect, rack_order, tolerance, transfer)

M3 = "org.apache.cassandra.dht.Murmur3Partitioner"
RP = "org.apache.cassandra.dht.RandomPartitioner"
M3_SIZE = 2 ** 64
RP_SIZE = 2 ** 127


def ring_of(positions, racks=None):
    return [(p, "n%d" % i, (racks[i] if racks else "r1")) for i, p in enumerate(positions)]


# --- partitioners and tokens ---

@pytest.mark.parametrize("name, first, size", [
    (M3, -2 ** 63, 2 ** 64), ("Murmur3Partitioner", -2 ** 63, 2 ** 64), (RP, 0, 2 ** 127)])
def test_partitioner_range(name, first, size):
    assert partitioner_range(name) == (first, size)


@pytest.mark.parametrize("name", ["org.apache.cassandra.dht.ByteOrderedPartitioner", "", None])
def test_other_partitioners_refused(name):
    with pytest.raises(TokenError, match="only computed for Murmur3Partitioner and RandomPartitioner"):
        partitioner_range(name)


@pytest.mark.parametrize("value, partitioner, token", [
    ("-9223372036854775808", M3, -2 ** 63), ("9223372036854775807", M3, 2 ** 63 - 1), (0, M3, 0),
    (" 42 ", M3, 42), ("0", RP, 0), (str(2 ** 127), RP, 2 ** 127)])
def test_parse_token(value, partitioner, token):
    assert parse_token(value, partitioner) == token


@pytest.mark.parametrize("value, partitioner, match", [
    ("9223372036854775808", M3, "outside"), ("-9223372036854775809", M3, "outside"), ("-1", RP, "outside"),
    (str(2 ** 127 + 1), RP, "outside"), ("1,2", M3, "not a single token"), ("1.5", M3, "not a single token"),
    ("", M3, "not a single token"), ("abc", M3, "not a single token")])
def test_parse_token_refused(value, partitioner, match):
    with pytest.raises(TokenError, match=match):
        parse_token(value, partitioner)


def test_murmur3_classic_values():
    # the values every token calculator gives for 3 and 6 nodes
    assert balanced_tokens(3, M3) == [-9223372036854775808, -3074457345618258603, 3074457345618258602]
    assert balanced_tokens(6, M3) == [-9223372036854775808, -6148914691236517206, -3074457345618258603,
                                      0, 3074457345618258602, 6148914691236517205]
    assert balanced_tokens(4, M3) == [-2 ** 63, -2 ** 62, 0, 2 ** 62]


def test_random_classic_values():
    assert balanced_tokens(4, RP) == [0, 2 ** 125, 2 ** 126, 3 * 2 ** 125]
    assert balanced_tokens(3, RP) == [0, 56713727820156410577229101238628035242, 113427455640312821154458202477256070485]


@pytest.mark.parametrize("partitioner", [M3, RP])
@pytest.mark.parametrize("count", list(range(1, 40)) + [64, 100, 255, 1000])
def test_balanced_spacing_exact(partitioner, count):
    """Property: sorted, in range, gaps differ by at most 1 and add up to the ring."""
    first, size = partitioner_range(partitioner)
    tokens = balanced_tokens(count, partitioner)
    assert tokens == sorted(tokens) and len(set(tokens)) == count
    assert all(first <= t < first + size for t in tokens)
    gaps = [(b - a) for a, b in zip(tokens, tokens[1:])] + [tokens[0] + size - tokens[-1]]
    assert sum(gaps) == size
    assert max(gaps) - min(gaps) <= 1
    assert set(gaps) <= set([size // count, size // count + 1])


@pytest.mark.parametrize("partitioner", [M3, RP])
def test_no_collision_across_datacenters(partitioner):
    """Property: datacenters of any sizes (1..40 nodes, up to 10 of them) never share a token."""
    rnd = random.Random(7)
    for dummy in range(300):
        sizes = [rnd.randint(1, 40) for dummy2 in range(rnd.randint(2, 10))]
        tokens = [t for i, n in enumerate(sizes) for t in balanced_tokens(n, partitioner, dc_offset(i))]
        assert len(tokens) == len(set(tokens)), sizes


def test_offset_bigger_than_spacing_refused():
    with pytest.raises(TokenError):
        balanced_positions(4, 16, 4)


def test_rack_order_alternates():
    nodes = [("a1", "r1"), ("a2", "r1"), ("b1", "r2"), ("c1", "r3"), ("b2", "r2"), ("c2", "r3")]
    assert rack_order(nodes) == [("a1", "r1"), ("b1", "r2"), ("c1", "r3"), ("a2", "r1"), ("b2", "r2"), ("c2", "r3")]
    # racks sorted by name, whatever the inventory order; uneven racks: the bigger one ends the ring
    assert rack_order([("z", "rb"), ("y", "ra"), ("x", "ra")]) == [("y", "ra"), ("z", "rb"), ("x", "ra")]
    assert rack_order([]) == []


# --- ownership ---

def test_ownership_balanced_one_rack():
    own = ownership(ring_of(balanced_positions(4, M3_SIZE)), M3_SIZE, 3)
    assert all(v == (Fraction(1, 4), Fraction(3, 4)) for v in own.values())


def test_ownership_single_node_and_rf_above_nodes():
    assert ownership([(5, "a", "r1")], 100, 3) == {"a": (1, 1)}
    own = ownership(ring_of([0, 50]), 100, 3)
    assert own == {"n0": (Fraction(1, 2), 1), "n1": (Fraction(1, 2), 1)}


def test_ownership_primary_is_range_before_the_token():
    own = ownership(ring_of([10, 40, 90]), 100, 1)
    assert own == {"n0": (Fraction(20, 100),) * 2, "n1": (Fraction(30, 100),) * 2, "n2": (Fraction(50, 100),) * 2}


def test_ownership_racks_networktopologystrategy():
    # 6 nodes, 3 racks alternating, RF 3: one replica per rack, balanced
    pos = balanced_positions(6, 600)
    own = ownership(ring_of(pos, ["r1", "r2", "r3"] * 2), 600, 3)
    assert all(e == Fraction(1, 2) for dummy, e in own.values())
    # racks grouped (r1 r1 r2 r2 r3 r3): the walk skips same-rack nodes, uneven
    own = ownership(ring_of(pos, ["r1", "r1", "r2", "r2", "r3", "r3"]), 600, 3)
    assert sorted(e for dummy, e in own.values()) == [Fraction(1, 6)] * 3 + [Fraction(5, 6)] * 3


def test_ownership_rack_repeats_when_rf_above_racks():
    # 2 racks, RF 3: one rack repeat allowed, taken on the walk
    own = ownership(ring_of([0, 25, 50, 75], ["r1", "r1", "r2", "r2"]), 100, 3)
    assert sum(e for dummy, e in own.values()) == 3


@pytest.mark.parametrize("seed", range(40))
def test_ownership_sums(seed):
    """Property: primary shares add up to 1, effective to min(rf, nodes)."""
    rnd = random.Random(seed)
    n = rnd.randint(1, 15)
    racks = ["r%d" % rnd.randint(1, 3) for dummy in range(n)]
    rf = rnd.randint(1, 5)
    own = ownership(ring_of(rnd.sample(range(10 ** 6), n), racks), 10 ** 6, rf)
    assert sum(p for p, dummy in own.values()) == 1
    assert sum(e for dummy, e in own.values()) == min(rf, n)


def test_same_token_twice_refused():
    with pytest.raises(TokenError, match="same token"):
        ownership([(1, "a", "r1"), (1, "b", "r1")], 100, 1)


# --- transfer ---

def test_transfer_new_node_takes_half_a_range():
    before = ring_of([0, 50])
    after = before + [(25, "new", "r1")]
    t = transfer(before, after, 100, 1)
    assert t["new"] == (Fraction(25, 100), 0)
    assert t["n1"] == (0, Fraction(25, 100))  # n1 owned (0, 50]
    assert t["n0"] == (0, 0)


@pytest.mark.parametrize("seed", range(40))
def test_transfer_balance(seed):
    """Property: per node, after - before == gained - lost, and what is gained is lost elsewhere (RF copies)."""
    rnd = random.Random(seed)
    size, n, rf = 10 ** 6, rnd.randint(2, 10), rnd.randint(1, 4)
    racks = ["r%d" % rnd.randint(1, 2) for dummy in range(n)]
    before = ring_of(rnd.sample(range(size), n), racks)
    mover = rnd.randrange(n)
    target = rnd.randrange(size)
    while target in [b[0] for b in before]:
        target = rnd.randrange(size)
    after = [(target if i == mover else p, name, rack) for i, (p, name, rack) in enumerate(before)]
    t = transfer(before, after, size, rf)
    ob, oa = ownership(before, size, rf), ownership(after, size, rf)
    for name in ob:
        assert oa[name][1] - ob[name][1] == t[name][0] - t[name][1]
    assert sum(g for g, dummy in t.values()) == sum(lost for dummy, lost in t.values())


# --- plans ---

def test_bisect_largest_range_one_rack():
    placed = plan_bisect(ring_of([0, 10, 50]), [("new", "r1")], 100, 1)
    assert placed == [(75, "new", "r1")]  # (50, 0] is the largest: 50 wide


def test_bisect_doubling_is_balanced_without_moves():
    size = M3_SIZE
    ring = ring_of(balanced_positions(3, size))
    placed = plan_bisect(ring, [("a", "r1"), ("b", "r1"), ("c", "r1")], size, 3)
    own = ownership(ring + placed, size, 3)
    assert max(e for dummy, e in own.values()) - min(e for dummy, e in own.values()) <= Fraction(3, size)
    # the middles of the ranges: the 6-node positions, give or take a token
    assert all(abs(a - b) <= 1 for a, b in zip(sorted(p for p, dummy, dummy2 in placed), balanced_positions(6, size)[1::2]))


@pytest.mark.parametrize("seed", range(20))
def test_bisect_picks_the_best_middle_with_racks(seed):
    """Property: the middle chosen leaves the smallest largest effective share of all the middles."""
    rnd = random.Random(seed)
    size = 10 ** 6
    n = rnd.randint(2, 9)
    ring = ring_of(sorted(rnd.sample(range(size), n)), ["r%d" % rnd.randint(1, 3) for dummy in range(n)])
    rack, rf = "r%d" % rnd.randint(1, 3), rnd.randint(1, 3)
    placed = plan_bisect(ring, [("new", rack)], size, rf)
    pos = sorted(p for p, dummy, dummy2 in ring)
    middles = [(a + ((b - a) % size) // 2) % size for a, b in zip([pos[-1]] + pos[:-1], pos)]

    def worst(p):
        return max(e for dummy, e in ownership(ring + [(p, "new", rack)], size, rf).values())
    assert worst(placed[0][0]) == min(worst(p) for p in middles)


def test_balanced_from_three_to_four_canonical():
    size = M3_SIZE
    ring = ring_of(balanced_positions(3, size))
    plan = plan_balanced(ring, [("new", "r1")], size, 3)
    pos = sorted(p for p, dummy, dummy2 in plan["ring"])
    assert [b - a for a, b in zip(pos, pos[1:])] == [size // 4] * 3
    assert len(plan["moves"]) == 2  # one node stays, the spacing starts there
    assert [n for dummy, n, dummy2 in plan["new"]] == ["new"]


def test_balanced_doubling_no_move():
    size = M3_SIZE
    ring = ring_of(balanced_positions(4, size))
    plan = plan_balanced(ring, [("a", "r1"), ("b", "r1"), ("c", "r1"), ("d", "r1")], size, 3)
    assert plan["moves"] == []


def test_balanced_rotated_ring_kept():
    # a balanced ring that does not follow the convention: doubling still moves nothing
    size = 1000
    ring = ring_of([7, 257, 507, 757])
    plan = plan_balanced(ring, [("a", "r1"), ("b", "r1"), ("c", "r1"), ("d", "r1")], size, 3)
    assert plan["moves"] == []
    assert sorted(p for p, dummy, dummy2 in plan["new"]) == [132, 382, 632, 882]


def test_balanced_new_datacenter_follows_convention():
    plan = plan_balanced([], [("a", "r1"), ("b", "r2")], M3_SIZE, 3, dc_offset(2))
    assert sorted(p for p, dummy, dummy2 in plan["ring"]) == balanced_positions(2, M3_SIZE, 200)


@pytest.mark.parametrize("seed", range(30))
def test_balanced_minimal_moves(seed):
    """Property: no other order-keeping assignment to the chosen positions moves fewer nodes, and the result is
    balanced (gaps differ by at most 1, plus twice the tolerance of a node left where it is)."""
    rnd = random.Random(seed)
    size = 10 ** 4
    n, k = rnd.randint(1, 7), rnd.randint(1, 5)
    ring = ring_of(sorted(rnd.sample(range(size), n)))
    plan = plan_balanced(ring, [("new%d" % i, "r1") for i in range(k)], size, 3)
    pos = sorted(p for p, dummy, dummy2 in plan["ring"])
    gaps = [(b - a) for a, b in zip(pos, pos[1:])] + [pos[0] + size - pos[-1]]
    tol = tolerance(n + k, size)
    assert max(gaps) - min(gaps) <= 1 + 2 * tol and len(pos) == n + k
    # brute force: every rotation of every anchored spacing, any order-keeping choice of positions
    import itertools
    m = n + k
    spacing = size // m
    best = n
    for off in set([0] + [p % spacing for p, dummy, dummy2 in ring]):
        targets = balanced_positions(m, size, off)
        for s in range(m):
            rot = targets[s:] + targets[:s]
            for combo in itertools.combinations(range(m), n):
                moved = sum(1 for (p, dummy, dummy2), j in zip(ring, combo) if min((p - rot[j]) % size, (rot[j] - p) % size) > tol)
                best = min(best, moved)
    assert len(plan["moves"]) == best


def test_move_order_waits_for_the_holder():
    ring = ring_of([0, 10, 20])
    order = move_order(ring, [("n0", 0, 10), ("n1", 10, 15)], 100)
    assert [mv[0] for mv in order] == ["n1", "n0"]


def test_move_order_cycle_refused():
    with pytest.raises(TokenError, match="block each other"):
        move_order(ring_of([0, 10]), [("n0", 0, 10), ("n1", 10, 0)], 100)


# --- nodetool ring ---

RING_50 = """
Datacenter: dc1
==========
Address         Rack        Status State   Load            Owns                Token
                                                                               3074457345618258602
10.100.100.1    r1          Up     Normal  104.6 KiB       ?                   -9223372036854775808
10.100.100.2    rack two    Down   Normal  98.3 KiB        ?                   -3074457345618258603
10.100.100.3    r1          Up     Moving  ?               33.33%              3074457345618258602

Datacenter: dc2
==========
Address         Rack        Status State   Load            Owns                Token
10.100.100.4    r1          Up     Joining 1.2 GiB         ?                   -9223372036854775708

  Warning: "nodetool ring" is used to output all the tokens of a node.
  To view status related info of a node use "nodetool status" instead.
"""


def test_parse_ring():
    rings = parse_ring(RING_50)
    assert sorted(rings) == ["dc1", "dc2"]
    assert rings["dc1"][1] == {"address": "10.100.100.2", "rack": "rack two", "status": "Down", "state": "Normal",
                               "token": "-3074457345618258603"}
    assert [n["state"] for n in rings["dc1"]] == ["Normal", "Normal", "Moving"]
    assert rings["dc2"] == [{"address": "10.100.100.4", "rack": "r1", "status": "Up", "state": "Joining",
                             "token": "-9223372036854775708"}]
    assert parse_ring("") == {}


@pytest.mark.parametrize("seed", range(40))
def test_balanced_moves_can_run_in_order(seed):
    """Property: the moves of a balanced plan have an order where no node lands on a token another one holds."""
    rnd = random.Random(seed)
    size = 1000  # small: targets often fall on tokens other nodes hold
    n, k = rnd.randint(1, 12), rnd.randint(0, 6)
    racks = ["r%d" % rnd.randint(1, 3) for dummy in range(n)]
    pos = sorted(set(rnd.choice([rnd.randrange(size), (i * size) // (n + k)]) for i in range(n)))
    ring = ring_of(pos, racks[:len(pos)])
    plan = plan_balanced(ring, [("new%d" % i, "r%d" % (i % 3 + 1)) for i in range(k)], size, 3)
    state = dict((name, p) for p, name, dummy in ring)
    for name, src, dst in move_order(ring, plan["moves"], size):
        assert state[name] == src and dst not in state.values()
        state[name] = dst
    assert len(set(state.values())) == len(state)


@pytest.mark.parametrize("seed", range(20))
def test_bisect_keeps_the_ring(seed):
    """Property: bisect only adds nodes, at positions nobody holds."""
    rnd = random.Random(seed)
    size = 2 ** 64
    ring = ring_of(sorted(set(rnd.randrange(size) for dummy in range(rnd.randint(1, 10)))))
    placed = plan_bisect(ring, [("new%d" % i, "r1") for i in range(rnd.randint(1, 8))], size, 3)
    positions = [p for p, dummy, dummy2 in ring + placed]
    assert len(set(positions)) == len(positions)


def test_move_order_never_passes_a_node():
    # the case a review found: sorted by target, n3 jumped over n1 (twice the streaming)
    size = 2 ** 64
    tokens = [(-6957332811113955950, "n2"), (-2283165261425827812, "n0"), (-627383705726182493, "n3"),
              (1886718038715041137, "n1")]
    ring = [(t + 2 ** 63, n, "r1") for t, n in tokens]
    plan = plan_balanced(ring, [], size, 3)
    order = move_order(ring, plan["moves"], size)
    names = [mv[0] for mv in order]
    assert names.index("n1") < names.index("n3")


@pytest.mark.parametrize("seed", range(60))
def test_balanced_moves_resume(seed):
    """Property: from the ring after any number of the moves (a run that stopped), the plan is exactly the moves
    left, and no move passes another node."""
    rnd = random.Random(seed)
    size = 2 ** 64
    n = rnd.randint(2, 9)
    racks = ["r%d" % rnd.randint(1, 2) for dummy in range(n)]
    ring = ring_of(sorted(set(rnd.randrange(size) for dummy in range(n))), racks)
    plan = plan_balanced(ring, [], size, 3)
    order = move_order(ring, plan["moves"], size)
    state = list(ring)
    for k, (name, src, dst) in enumerate(order):
        others = sorted(p for p, m, dummy in state if m != name)
        if others:
            assert _gap(src, others) == _gap(dst, others)
        state = [(dst if m == name else p, m, r) for p, m, r in state]
        again = plan_balanced(state, [], size, 3)["moves"]
        assert sorted((m, d) for m, dummy, d in again) == sorted((m, d) for m, dummy, d in order[k + 1:])


@pytest.mark.parametrize("planner", ["bisect", "balanced"])
def test_doubling_three_racks_is_even(planner):
    # r0 r1 r2 evenly spaced, one new node per rack, RF 3: each new node must sit where its neighbours are
    # the two other racks (a review found 16.67% to 83.33% when only the rack before was looked at)
    size = 2 ** 64
    ring = ring_of(balanced_positions(3, size), ["r0", "r1", "r2"])
    new = [("a", "r0"), ("b", "r1"), ("c", "r2")]
    placed = plan_bisect(ring, new, size, 3) if planner == "bisect" else plan_balanced(ring, new, size, 3)["new"]
    eff = [e for dummy, e in ownership(ring + placed, size, 3).values()]
    assert max(eff) - min(eff) <= Fraction(10, size)


@pytest.mark.parametrize("seed", range(10))
def test_many_new_nodes_slot_by_slot(seed):
    # more than PLACE_SEARCH new nodes: placed slot by slot, every new node placed once
    rnd = random.Random(seed)
    size = 2 ** 64
    ring = ring_of(balanced_positions(4, size), ["r0", "r1", "r2", "r0"])
    new = [("x%d" % i, "r%d" % rnd.randint(0, 2)) for i in range(9)]
    placed = plan_balanced(ring, new, size, 3)["new"]
    assert sorted(n for dummy, n, dummy2 in placed) == sorted(n for n, dummy in new)
    assert dict((n, r) for dummy, n, r in placed) == dict(new)


# (2 racks alternating can't stay alternating once doubled: each new node sits between two nodes of different racks)
@pytest.mark.parametrize("n, racks, rf", [(4, ["r1"], 1), (4, ["r1"], 3), (6, ["r1", "r2", "r3"], 3),
                                          (5, ["r1"], 2), (3, ["r1", "r2", "r3"], 3), (5, ["r1", "r2"], 3)])
def test_bisect_doubling_splits_every_range(n, racks, rf):
    # reviews found 4 -> 8 at 1.56% to 25%, and 5 -> 10 on 2 racks at 0.30 to 0.40: every range split once
    size = 2 ** 64
    ring = ring_of(balanced_positions(n, size), [racks[i % len(racks)] for i in range(n)])
    new = [("new%d" % i, racks[i % len(racks)]) for i in range(n)]
    placed = plan_bisect(ring, new, size, rf)
    pos = sorted(p for p, dummy, dummy2 in ring + placed)
    gaps = [b - a for a, b in zip(pos, pos[1:])] + [pos[0] + size - pos[-1]]
    assert max(gaps) - min(gaps) <= 2
    eff = [e for dummy, e in ownership(ring + placed, size, rf).values()]
    assert max(eff) - min(eff) <= Fraction(10, size)


@pytest.mark.parametrize("seed", range(30))
def test_many_new_nodes_never_worse_than_alternating(seed):
    from ansible_collections.community.cassandra.plugins.module_utils.cassandra_tokens import _alternate, _balance_key
    rnd = random.Random(seed)
    size = 10 ** 6
    n = rnd.randint(2, 6)
    ring = ring_of(balanced_positions(n, size), ["r%d" % rnd.randint(0, 2) for dummy in range(n)])
    new = [("x%d" % i, "r%d" % rnd.randint(0, 2)) for i in range(rnd.randint(8, 11))]
    plan = plan_balanced(ring, new, size, 3)
    after = [p for p in plan["ring"] if p[1] not in dict(new)]
    free = [p for p, dummy, dummy2 in plan["new"]]
    alt = _alternate(after, free, new)
    assert _balance_key(plan["ring"], size, 3) <= _balance_key(after + alt, size, 3)


@pytest.mark.parametrize("n, k, racks, rf", [(3, 6, 1, 2), (3, 5, 2, 3), (5, 5, 2, 3), (4, 4, 1, 3), (6, 3, 3, 3)])
def test_bisect_not_worse_than_each_greedy(n, k, racks, rf):
    from ansible_collections.community.cassandra.plugins.module_utils.cassandra_tokens import (
        _balance_key, _greedy_bisect, _largest_only)
    size = 2 ** 64
    ring = ring_of(balanced_positions(n, size), ["r%d" % (i % racks) for i in range(n)])
    new = rack_order([("x%d" % i, "r%d" % (i % racks)) for i in range(k)])
    best = _balance_key(ring + plan_bisect(ring, new, size, rf), size, rf)
    assert best <= _balance_key(ring + _greedy_bisect(ring, new, size, rf, _largest_only), size, rf)
