# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import absolute_import, division, print_function
__metaclass__ = type

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_tokens import cassandra_token_assign

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
    assert out["problems"] == [] and out["tokens"] == {"n2": T3[1], "n3": T3[2]}
    # n1 joined with another token: the cluster is not the one planned here
    out = cassandra_token_assign(nodes, M3, ring=ring("12"), hosts=HOSTS)
    assert out["tokens"] == {"n2": T3[1], "n3": T3[2]} and len(out["problems"]) == 1
    assert out["problems"][0].startswith("the cluster runs already (n1 in the ring, not with the tokens worked out")
    assert out["problems"][0].endswith("Add n2, n3 with add_node (cassandra_token_auto)")
    # a node outside the inventory runs
    out = cassandra_token_assign(nodes, M3, ring=ring(T3[0], start=8), hosts=HOSTS)
    assert "10.100.100.9 (dc1)" in out["problems"][0]


def test_assign_rerun_on_a_running_cluster():
    # every node runs, whatever its token (moved, bisected): nothing to give, nothing refused
    nodes = [node("n1"), node("n2", token=T3[1]), node("n3")]
    out = cassandra_token_assign(nodes, M3, ring=ring("1", "2", "3"), hosts=HOSTS)
    assert out["problems"] == [] and out["tokens"] == {}
    assert out["warnings"] == ["n2: cassandra_initial_token %s, the ring has 2 (the token a node joined with;"
                               " initial_token is not read again): fix the inventory" % T3[1]]
    assert "n1" in out["lines"][2] and " 1 " in out["lines"][2]


def test_assign_ipv6_ring_addresses():
    nodes = [node("n1"), node("n2")]
    r = {"dc1": [{"address": "2001:db8:0:0:0:0:0:1", "rack": "r1", "status": "Up", "state": "Normal", "token": "1"},
                 {"address": "2001:db8:0:0:0:0:0:2", "rack": "r1", "status": "Up", "state": "Normal", "token": "2"}]}
    out = cassandra_token_assign(nodes, M3, ring=r, hosts={"2001:db8::1": "n1", "2001:db8::2": "n2"})
    assert out["problems"] == []


def test_assign_only_bad_tokens_no_crash():
    out = cassandra_token_assign([node("n1", token="None"), node("n2", "dc2", token="abc")], M3)
    assert out["tokens"] == {"n1": str(-2 ** 63)}  # null in the inventory: no token
    assert out["problems"] == ["n2: cassandra_initial_token 'abc' is not a single token (an integer)"]


def test_assign_unknown_partitioner():
    with pytest.raises(AnsibleFilterError, match="cassandra_token_assign: .*only computed for Murmur3"):
        cassandra_token_assign([node("n1")], "org.apache.cassandra.dht.ByteOrderedPartitioner")


def test_assign_rf_from_keyspaces():
    ks = {"app": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 2}}, "system_auth": {"class": "x", "rf": {"dc1": 1}}}
    out = cassandra_token_assign([node("n1"), node("n2"), node("n3"), node("n4")], M3, keyspaces=ks)
    assert "with RF 2" in out["lines"][1] and out["lines"][0].endswith("50.00% to 50.00%")


# --- add_node ---

# --- move_node ---

STATUS = {"dc1": {"nodes": [{"address": "10.100.100.%d" % i, "load": "30 GiB"} for i in (1, 2, 3, 4)]}}


def test_ring_names_resolved_when_no_address_matches():
    # the inventory knows the node by a name (e.g. listen_address: localhost-like name): resolved on the controller
    r = {"dc1": [{"address": "127.0.0.1", "rack": "r1", "status": "Up", "state": "Normal", "token": T3[0]}]}
    out = cassandra_token_assign([node("n1")], M3, ring=r, hosts={"localhost": "n1"})
    assert out["problems"] == [] and out["tokens"] == {}
