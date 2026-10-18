# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function
__metaclass__ = type

import random
from fractions import Fraction

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_tokens import (
    TokenError, balanced_positions, ownership, parse_token, partitioner_range, plan_bisect, rack_order)
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_ring import parse_ring

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


@pytest.mark.parametrize("seed", range(20))
def test_bisect_keeps_the_ring(seed):
    """Property: bisect only adds nodes, at positions nobody holds."""
    rnd = random.Random(seed)
    size = 2 ** 64
    ring = ring_of(sorted(set(rnd.randrange(size) for dummy in range(rnd.randint(1, 10)))))
    placed = plan_bisect(ring, [("new%d" % i, "r1") for i in range(rnd.randint(1, 8))], size, 3)
    positions = [p for p, dummy, dummy2 in ring + placed]
    assert len(set(positions)) == len(positions)


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


@pytest.mark.parametrize("n, k, racks, rf", [
    (3, 6, 1, 2), (3, 5, 2, 3), (5, 5, 2, 3), (4, 4, 1, 3), (6, 3, 3, 3), (67, 14, 4, 2)])
def test_bisect_not_worse_than_each_greedy(n, k, racks, rf):
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_tokens import (
        _balance_key, _greedy_bisect, _largest_first, _largest_only, _split_largest)
    size = 2 ** 64
    ring = ring_of(balanced_positions(n, size), ["r%d" % (i % racks) for i in range(n)])
    new = rack_order([("x%d" % i, "r%d" % (i % racks)) for i in range(k)])
    best = _balance_key(ring + plan_bisect(ring, new, size, rf), size, rf)
    for greedy in (_greedy_bisect(ring, new, size, rf, _largest_first), _greedy_bisect(ring, new, size, rf, _largest_only),
                   _split_largest(ring, new, size)):
        assert best <= _balance_key(ring + greedy, size, rf)


def test_bisect_same_positions_other_racks_both_tried():
    # a review found the second greedy skipped for having the same positions, with better racks on them
    size = 2 ** 64
    ring = [(2208115789264903189, "n0", "r0"), (3436951878756879807, "n1", "r1"), (6906942652851629972, "n2", "r0")]
    new = [("x0", "r0"), ("x2", "r1"), ("x1", "r0"), ("x3", "r1"), ("x6", "r0"), ("x4", "r1"), ("x5", "r1"), ("x7", "r1")]
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_tokens import _balance_key, _split_largest
    best = _balance_key(ring + plan_bisect(ring, new, size, 3), size, 3)
    assert best <= _balance_key(ring + _split_largest(ring, new, size), size, 3)
