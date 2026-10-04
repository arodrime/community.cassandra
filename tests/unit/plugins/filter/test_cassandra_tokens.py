# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function
__metaclass__ = type

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_tokens import (
    cassandra_ring_tokens, cassandra_token_add_plan, cassandra_token_assign, cassandra_token_disk_problems,
    cassandra_token_move_plan)

M3 = "org.apache.cassandra.dht.Murmur3Partitioner"
T3 = ["-9223372036854775808", "-3074457345618258603", "3074457345618258602"]


def node(name, dc="dc1", rack="r1", token=""):
    return {"name": name, "dc": dc, "rack": rack, "token": token, "address": "10.100.100.%s" % name[1:]}


def ring(*tokens, **kw):
    """nodetool ring parsed: n1, n2... at 10.100.100.1, .2... in dc1"""
    dc = kw.get("dc", "dc1")
    racks = kw.get("racks") or ["r1"] * len(tokens)
    return {dc: [{"address": "10.100.100.%d" % (i + 1 + kw.get("start", 0)), "rack": racks[i], "status": "Up",
                  "state": kw.get("state", "Normal"), "token": t} for i, t in enumerate(tokens)]}


HOSTS = dict(("10.100.100.%d" % i, "n%d" % i) for i in range(1, 20))


# --- create_cluster ---

def test_assign_three_nodes():
    out = cassandra_token_assign([node("n1"), node("n2"), node("n3")], M3)
    assert out["tokens"] == dict(zip(["n1", "n2", "n3"], T3))
    assert out["problems"] == [] and out["warnings"] == []
    assert out["lines"][0] == "dc1: 3 node(s), effective ownership 100.00% to 100.00%"
    assert any("n1 *" in line and T3[0] in line and "33.33%" in line for line in out["lines"])


def test_assign_two_datacenters_offset():
    nodes = [node("n1", "dc2"), node("n2", "dc2"), node("n3", "dc1"), node("n4", "dc1")]
    out = cassandra_token_assign(nodes, M3)
    # dc1 (first by name) on the convention, dc2 100 further
    assert out["tokens"] == {"n3": str(-2 ** 63), "n4": "0", "n1": str(-2 ** 63 + 100), "n2": "100"}


def test_assign_racks_alternate():
    nodes = [node("n1", rack="r1"), node("n2", rack="r1"), node("n3", rack="r2"), node("n4", rack="r2"),
             node("n5", rack="r3"), node("n6", rack="r3")]
    out = cassandra_token_assign(nodes, M3)
    order = sorted(out["tokens"], key=lambda n: int(out["tokens"][n]))
    assert order == ["n1", "n3", "n5", "n2", "n4", "n6"]
    assert out["lines"][0] == "dc1: 6 node(s), effective ownership 50.00% to 50.00%"


def test_assign_uneven_racks_warned():
    out = cassandra_token_assign([node("n1", rack="r1"), node("n2", rack="r1"), node("n3", rack="r2")], M3)
    assert out["warnings"] == ["dc1: racks of different sizes (r1 2, r2 1): with one token per node the ring can't"
                               " alternate racks all the way round, some nodes hold more"]


def test_assign_given_tokens_kept():
    out = cassandra_token_assign([node("n1", token=T3[0]), node("n2", token=T3[1]), node("n3", token=T3[2])], M3)
    assert out["tokens"] == {} and out["problems"] == []


def test_assign_mix_refused_then_allowed():
    nodes = [node("n1", token=T3[0]), node("n2", token=""), node("n3", token=None)]
    out = cassandra_token_assign(nodes, M3)
    assert out["tokens"] == {}
    assert out["problems"] == [
        "dc1: cassandra_initial_token is set on n1 but not on n2, n3: set it on every node of the datacenter or on"
        " none (then the tokens are worked out), or -e cassandra_token_allow_partial=true (the others split the"
        " largest ranges)"]
    out = cassandra_token_assign(nodes, M3, allow_partial=True)
    assert out["problems"] == [] and set(out["tokens"]) == set(["n2", "n3"])
    assert out["tokens"]["n2"] == "0"  # half way round from n1


@pytest.mark.parametrize("token, match", [("abc", "not a single token"), ("9223372036854775808", "outside")])
def test_assign_bad_token(token, match):
    out = cassandra_token_assign([node("n1", token=token), node("n2", token="0")], M3)
    assert len(out["problems"]) == 1 and match in out["problems"][0] and out["problems"][0].startswith("n1: ")


def test_assign_same_token_twice():
    out = cassandra_token_assign([node("n1", token="5"), node("n2", "dc2", token="5")], M3)
    assert out["problems"] == ["n1 and n2 have the same token 5"]


def test_assign_rerun_after_half_a_create():
    nodes = [node("n1"), node("n2"), node("n3")]
    out = cassandra_token_assign(nodes, M3, ring=ring(T3[0]), hosts=HOSTS)
    assert out["problems"] == [] and len(out["tokens"]) == 3
    out = cassandra_token_assign(nodes, M3, ring=ring("12"), hosts=HOSTS)
    assert len(out["problems"]) == 1 and "n1 (10.100.100.1) runs in the ring with token 12" in out["problems"][0]
    out = cassandra_token_assign(nodes, M3, ring=ring(T3[0], start=8), hosts=HOSTS)
    assert "n9 (10.100.100.9) runs in the ring" in out["problems"][0]


def test_assign_unknown_partitioner():
    with pytest.raises(AnsibleFilterError, match="cassandra_token_assign: .*only computed for Murmur3"):
        cassandra_token_assign([node("n1")], "org.apache.cassandra.dht.ByteOrderedPartitioner")


def test_assign_rf_from_keyspaces():
    ks = {"app": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 2}}, "system_auth": {"class": "x", "rf": {"dc1": 1}}}
    out = cassandra_token_assign([node("n1"), node("n2"), node("n3"), node("n4")], M3, keyspaces=ks)
    assert "with RF 2" in out["lines"][1] and out["lines"][0].endswith("50.00% to 50.00%")


# --- add_node ---

def test_add_one_node_to_three():
    out = cassandra_token_add_plan(ring(*T3), [node("n4")], M3, hosts=HOSTS)
    assert out["problems"] == []
    # the middle of (T3[2], T3[0]], the largest range (one token more than the others: 2^64 is not a multiple of 3)
    assert out["bisect"] == {"n4": "6148914691236517205"}
    assert len(out["moves"]) == 2 and out["even"] == {"bisect": False, "balanced": True}
    assert out["warnings"] == ["dc1: bisect leaves the ring uneven; 3 new node(s) instead of 1 (the datacenter doubled"
                               " to 6) would split every range in two: even, with no move"]
    assert out["balanced_problems"] == []
    assert any(line.startswith("dc1 bisect (no move)") for line in out["lines"])
    assert any(line.startswith("dc1 balanced (2 moves)") for line in out["lines"])


def test_add_doubling_is_even_without_moves():
    out = cassandra_token_add_plan(ring(*T3), [node("n4"), node("n5"), node("n6")], M3, hosts=HOSTS)
    assert out["even"]["bisect"] and out["moves"] == [] and out["warnings"] == []
    assert sorted(int(t) for t in out["bisect"].values()) == [-6148914691236517206, -1, 6148914691236517205]


def test_add_node_already_in_ring_left_out():
    out = cassandra_token_add_plan(ring(*(T3 + ["0"])), [node("n4")], M3, hosts=HOSTS)
    assert out["bisect"] == {} and out["lines"] == []


def test_add_given_token_kept():
    out = cassandra_token_add_plan(ring(*T3), [node("n4", token="7")], M3, hosts=HOSTS)
    assert out["bisect"] == {"n4": "7"} and out["balanced"]["n4"] == "7"


def test_add_new_datacenter():
    out = cassandra_token_add_plan(ring(*T3), [node("n4", "dc2"), node("n5", "dc2")], M3, hosts=HOSTS)
    assert out["bisect"] == out["balanced"] == {"n4": str(-2 ** 63 + 100), "n5": "100"}


def test_add_new_datacenter_offset_taken():
    # dc0 sorts first and would get offset 0, which dc1's tokens already use
    out = cassandra_token_add_plan(ring(*T3), [node("n4", "dc0"), node("n5", "dc0"), node("n6", "dc0")], M3,
                                   hosts=HOSTS)
    assert sorted(int(t) for t in out["bisect"].values()) == [int(t) + 100 for t in T3]


def test_add_vnodes_refused():
    vn = {"dc1": [{"address": "10.100.100.1", "rack": "r1", "status": "Up", "state": "Normal", "token": t}
                  for t in ("1", "2")]}
    out = cassandra_token_add_plan(vn, [node("n4")], M3, hosts=HOSTS)
    assert out["problems"] == ["dc1: n1 has several tokens (vnodes): these plans are for one token per node"]


# --- move_node ---

STATUS = {"dc1": {"nodes": [{"address": "10.100.100.%d" % i, "load": "30 GiB"} for i in (1, 2, 3, 4)]}}


def test_move_uneven_ring():
    # n4 bisected into a 3-node ring: 4 nodes, uneven
    out = cassandra_token_move_plan(ring(*(T3 + ["0"])), M3, hosts=HOSTS, status=STATUS)
    assert out["problems"] == []
    assert len(out["steps"]) == 2
    tokens = dict((s["name"], s["to"]) for s in out["steps"])
    final = sorted([int(tokens.get("n%d" % (i + 1), t)) for i, t in enumerate(T3 + ["0"])])
    gaps = [b - a for a, b in zip(final, final[1:])]
    assert max(gaps) - min(gaps) <= 2
    for s in out["steps"]:
        assert s["gain"] and s["loss"]
        assert all(b is not None and b > 0 for b in s["gain_bytes"].values())
    assert out["cleanup"]
    assert out["lines"][-1].startswith("Then a cleanup on the nodes that lose ranges: ")


def test_move_even_ring_nothing():
    out = cassandra_token_move_plan(ring(*T3), M3, hosts=HOSTS)
    assert out["steps"] == [] and out["problems"] == []
    assert "  dc1: nothing to move (the ring is as even as it gets)" in out["lines"]


def test_move_targets():
    out = cassandra_token_move_plan(ring(*T3), M3, hosts=HOSTS, targets={"n2": "-3074457345618258000"})
    assert [(s["name"], s["to"]) for s in out["steps"]] == [("n2", "-3074457345618258000")]
    assert out["steps"][0]["gain"] == {}  # RF 3 on 3 nodes: every node has everything already
    out = cassandra_token_move_plan(ring(*T3), M3, hosts=HOSTS, targets={"n2": "-3074457345618258000"}, default_rf=1)
    assert out["steps"][0]["gain"] == {"n2": 603 / 2.0 ** 64} and out["steps"][0]["loss"] == {"n3": 603 / 2.0 ** 64}
    assert out["steps"][0]["gain_bytes"] == {"n2": None}  # no load known


def test_move_targets_problems():
    out = cassandra_token_move_plan(ring(*T3), M3, hosts=HOSTS, targets={"n9": "1", "n1": T3[1]})
    assert "n9 is not in the ring (nodetool ring): no token to move" in out["problems"]
    assert any("block each other" in p for p in out["problems"])


def test_move_refused_while_a_node_is_busy():
    out = cassandra_token_move_plan(ring(*T3, state="Moving"), M3, hosts=HOSTS)
    assert out["problems"] == ["dc1: n1, n2, n3 not Normal (joining, leaving or moving): wait until it is"]


def test_move_skips_vnode_datacenters():
    vn = {"dc9": [{"address": "10.100.100.9", "rack": "r1", "status": "Up", "state": "Normal", "token": t}
                  for t in ("1", "2")]}
    r = ring(*T3)
    r.update(vn)
    out = cassandra_token_move_plan(r, M3, hosts=HOSTS)
    assert out["problems"] == [] and out["warnings"] == [
        "dc9: n9 has several tokens (vnodes): these plans are for one token per node"]


def test_disk_problems():
    step = {"name": "n2", "gain_bytes": {"n2": 50 * 1024 ** 3, "n3": None}}
    free = {"n2": {"free": 60 * 1024 ** 3, "total": 100 * 1024 ** 3}}
    assert cassandra_token_disk_problems(step, free, 20) == [
        "n2 gets about 50.0 GiB from moving n2, it has 60.0 GiB free of 100.0 GiB: less than 20% would be left free"
        " (cassandra_move_min_free_percent)"]
    assert cassandra_token_disk_problems(step, free, 5) == []
    assert cassandra_token_disk_problems(step, {}, 20) == []
    mounts = [{"mount": "/", "size_available": 900 * 1024 ** 3, "size_total": 1000 * 1024 ** 3},
              {"mount": "/var/lib/cassandra", "size_available": 60 * 1024 ** 3, "size_total": 100 * 1024 ** 3},
              {"mount": "/var/lib/cassandra2", "size_available": 1, "size_total": 2}]
    disks = {"n2": {"path": "/var/lib/cassandra/data", "mounts": mounts}}
    assert cassandra_token_disk_problems(step, disks, 20) == cassandra_token_disk_problems(step, free, 20)
    disks = {"n2": {"path": "/srv/data", "mounts": mounts}}  # on /: plenty of room
    assert cassandra_token_disk_problems(step, disks, 20) == []


def test_ring_tokens_filter():
    assert cassandra_ring_tokens("Datacenter: dc1\n10.100.100.1  r1  Up  Normal  1 KiB  ?  5\n") == {
        "dc1": [{"address": "10.100.100.1", "rack": "r1", "status": "Up", "state": "Normal", "token": "5"}]}
