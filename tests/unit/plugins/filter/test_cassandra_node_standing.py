"""The state of a node (plugins/filter/cassandra_node_standing.py): one place, every consumer decides from it."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_node_standing import (
    STATES, cassandra_node_data_dirs, cassandra_node_standing)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")

ME = "10.0.0.7"
N1 = {"address": "10.0.0.1", "status": "U", "state": "N", "host_id": "id-1"}
N2 = {"address": "10.0.0.2", "status": "U", "state": "N", "host_id": "id-2"}
KNOWN = ["10.0.0.1", "10.0.0.2", ME]
SYSTEM = "/var/lib/cassandra/data/system"


def _ring(*nodes):
    return {"dc1": {"nodes": [dict(n) for n in nodes]}}


def _me(status, state, host_id="id-7"):
    return {"address": ME, "status": status, "state": state, "host_id": host_id}


def _answers(*extra):
    return [{"host": "node1", "addresses": ["10.0.0.1"], "status": _ring(N1, N2, *extra)}]


def _state(**kwargs):
    """A node stopped, holding a stock node's system keyspace, not in the ring (two nodes UN answer)."""
    args = dict(node="node7", cluster_name="my_cluster", title="node7 (dc1/rack_b)", addresses=[ME],
                answers=_answers(), known=KNOWN, live="read", live_cluster="Test Cluster", held=[SYSTEM],
                system=[SYSTEM], keyspaces=["system", "system_schema"])
    args.update(kwargs)
    return cassandra_node_standing(**args)


def _with(base, **kwargs):
    """base with some facts changed (dict(base, key=...) when base has the key too)."""
    out = dict(base)
    out.update(kwargs)
    return out


BLANK = dict(held=[], system=[], keyspaces=[], live="absent", live_cluster=None)
RUNNING_ALONE = dict(running=True, own=_ring(_me("U", "N")), host_id="id-7")
OURS = dict(live_cluster="my_cluster", keyspaces=["system", "shop"], cluster_keyspaces={"shop": {}})


# Every state, from the facts that make it (each row: the facts, the state)
ROWS = [
    (BLANK, "blank"),
    (dict(BLANK, answers=[]), "blank"),  # nothing to lose: the ring does not matter
    (dict(BLANK, answers=None), "blank"),  # cassandra_config, create_cluster: the ring is not read
    ({}, "failed_bootstrap"),  # the package's stock instance started once
    (dict(live_cluster="my_cluster"), "failed_bootstrap"),
    (dict(OURS, bootstrap_started=True), "failed_bootstrap"),  # add_node's mark: its bootstrap never ended
    (OURS, "former_member"),
    (dict(OURS, peers=True), "former_member"),
    (dict(live="absent", live_cluster=None), "unidentified_data"),
    (dict(keyspaces=["system", "shop"]), "unidentified_data"),  # user keyspaces of a stock-named cluster
    (dict(peers=True), "unidentified_data"),  # a stock node that met others
    (dict(OURS, cluster_keyspaces=None), "unidentified_data"),
    (dict(OURS, keyspaces=["shop", "legacy"]), "unidentified_data"),
    (dict(OURS, cluster_name="Test Cluster", live_cluster="Test Cluster", cluster_keyspaces={"shop": {}}),
     "unidentified_data"),  # this cluster kept the stock name: whose keyspaces can't be told
    (dict(live_cluster="billing"), "other_cluster"),
    (dict(RUNNING_ALONE, live_cluster="billing"), "other_cluster"),  # a one-node cluster of its own
    (dict(RUNNING_ALONE, own=_ring(_me("U", "N"), {"address": "10.9.9.9", "status": "U", "state": "N"})),
     "other_cluster"),
    (RUNNING_ALONE, "running_outside_ring"),
    (dict(BLANK, **RUNNING_ALONE), "running_outside_ring"),
    (dict(running=True, jvm=False, own=None), "running_outside_ring"),  # its unit active, no JVM yet
    (dict(answers=_answers(_me("U", "N")), running=True, own=_ring(N1, N2, _me("U", "N")), host_id="id-7"), "member"),
    (dict(answers=_answers(_me("U", "J")), running=True, host_id="id-7"), "member_joining"),
    (dict(answers=_answers(_me("U", "J")), running=True, no_mbean=True), "member_joining"),  # CASSANDRA-19902
    (dict(answers=_answers(_me("D", "N"))), "member_down"),
    (dict(answers=_answers(_me("D", "J"))), "member_down"),
    (dict(answers=_answers(_me("U", "L")), running=True, host_id="id-7"), "member_busy"),
    (dict(BLANK, answers=_answers(_me("U", "J")), running=True, host_id="id-7"), "member_busy"),
    (dict(BLANK, answers=_answers(_me("D", "N"))), "dead_address"),
    (dict(answers=_answers(_me("U", "N")), running=True, host_id="id-9"), "address_conflict"),
    (dict(answers=_answers(_me("U", "N"))), "address_conflict"),  # UN at its address, it does not answer
    (dict(answers=[]), "unknown"),  # holds data: no node answered UN
    (dict(answers=None), "unknown"),
    (dict(answers=[{"host": "node1", "addresses": ["10.0.0.1"], "status": None}]), "unknown"),
    # a host alone in its ring (not added yet, the package's own instance) while the members did not answer
    (dict(OURS, answers=[{"host": "node1", "addresses": ["10.0.0.1"], "status": None},
                         {"host": "node8", "addresses": ["10.0.0.8"],
                          "status": _ring({"address": "10.0.0.8", "status": "U", "state": "N", "host_id": "id-8"})}]),
     "unknown"),
    # node1 answers, but sees itself down: its ring does not count
    (dict(answers=[{"host": "node1", "addresses": ["10.0.0.1"], "status": _ring(dict(N1, status="D"), N2)}]), "unknown"),
    (dict(answers=_answers({"address": "10.0.0.50", "status": "D", "state": "N", "host_id": "id-50"})),
     "unknown"),  # a down node no host has: maybe this one under an old address
    (dict(unreadable=["/data/cassandra: can not be read (without root?)"]), "unknown"),
    (dict(live="unreadable", live_cluster=None), "unknown"),
    (dict(BLANK, live="invalid"), "unknown"),
    (dict(running=True, own=None), "unknown"),  # nodetool does not answer there
    (dict(running=True, own=_ring(_me("U", "N"), N1)), "unknown"),  # its own ring lists this cluster's nodes
    (dict(running=True, own=_ring(_me("U", "N")), answers=[]), "unknown"),
    (dict(running=True, own=_ring(_me("U", "N")), answers=None), "unknown"),  # running, the ring not read
    # running alone, holding data, a down node no host has: maybe this node under an old address
    (dict(RUNNING_ALONE, answers=_answers({"address": "10.0.0.50", "status": "D", "state": "N", "host_id": "id-50"})),
     "unknown"),
    (dict(answers=_answers(_me("D", "N")), running=True, own=None), "unknown"),  # it runs, the ring sees it down
    (dict(BLANK, answers=_answers(_me("D", "N")), running=True, own=None), "unknown"),
    # up under its own host ID, its system keyspace not where the inventory or its cassandra.yaml say: itself
    (dict(BLANK, answers=_answers(_me("U", "N")), running=True, host_id="id-7"), "member"),
]


@pytest.mark.parametrize("facts, state", ROWS)
def test_each_state(facts, state):
    out = _state(**facts)
    assert out["state"] == state, out["reasons"]
    assert out["words"] == dict(STATES)[state]


def test_every_state_is_reached_by_a_row():
    """No state without a row above: each one is tested."""
    assert set(state for facts, state in ROWS) == set(dict(STATES))


def test_blank_and_evidence():
    """blank: what cassandra_config's question and create_cluster's new cluster read."""
    assert _state(**BLANK)["blank"] is True
    assert _state(**BLANK)["evidence"] == []
    assert _state(**_with(BLANK, held=["/var/lib/cassandra/commitlog/CommitLog-7-1.log"]))["evidence"] == [
        "/var/lib/cassandra/commitlog/CommitLog-7-1.log in its Cassandra directories"]
    assert _state()["evidence"] == [SYSTEM]
    assert _state(**dict(BLANK, running=True))["evidence"] == ["Cassandra is running"]
    assert _state(**_with(BLANK, live="unreadable"))["evidence"] == ["the live cassandra.yaml can not be read"]
    # an old system keyspace elsewhere: not its own, said apart (the caller decides)
    out = _state(**dict(BLANK, elsewhere=["/var/lib/cassandra/data/system"]))
    assert out["blank"] and out["elsewhere"] == ["/var/lib/cassandra/data/system"]


LONE = [{"host": "node1", "addresses": ["10.0.0.1"], "status": _ring(N1)}]
STOCK_HOST = {"host": "node3", "addresses": ["10.0.0.3"],
              "status": _ring({"address": "127.0.0.1", "status": "U", "state": "N", "host_id": "id-3"})}
STRAY = {"host": "node8", "addresses": ["10.0.0.8"],
         "status": _ring({"address": "10.0.0.8", "status": "U", "state": "N", "host_id": "id-8"})}


def test_a_ring_that_does_not_count_makes_a_lone_one_set_aside():
    """node3 answers, but sees itself joining: its ring does not count, and node1 alone does not stand for the cluster
    for a node holding data."""
    joining = {"host": "node3", "addresses": ["10.0.0.3"],
               "status": _ring(N1, {"address": "10.0.0.3", "status": "U", "state": "J", "host_id": "id-3"})}
    out = _state(answers=LONE + [joining], **OURS)
    assert out["state"] == "unknown"
    assert out["ring"]["why"] == "only node1 answered, alone in its ring; node3 did not answer as up and normal"
    stopped = _state(answers=LONE + [{"host": "node3", "addresses": ["10.0.0.3"], "status": None, "asked": False}], **OURS)
    assert stopped["ring"]["why"] == "only node1 answered, alone in its ring; Cassandra stopped on node3"
    both = _state(answers=LONE + [STRAY, joining], **OURS)
    assert both["ring"]["why"] == ("node1, node8 answered, each alone in its ring: which one is the cluster can't be"
                                   " told; node3 did not answer as up and normal")


def test_several_lone_rings_can_not_be_told_apart():
    """A one-node cluster and a stray instance at its own address, each alone: which is the cluster can't be told."""
    out = _state(answers=LONE + [STRAY], **BLANK)
    assert out["ring"]["known"] is False and out["ring"]["from"] == ""
    assert out["ring"]["why"] == "node1, node8 answered, each alone in its ring: which one is the cluster can't be told"


def test_the_members_found_stopped_are_named():
    out = _state(answers=[{"host": "node3", "addresses": ["10.0.0.3"], "status": None, "asked": False}], **OURS)
    assert out["ring"]["why"] == ("no other node of the cluster answered as up and normal (UN) (asked: none; Cassandra"
                                  " stopped on node3)")
    assert out["ring"]["problems"][0].startswith(out["ring"]["why"] + ": whether node7 (dc1/rack_b) is in the ring")


def test_the_ring_add_node_plans_with_is_not_a_lone_one():
    assert _state(answers=[STRAY] + _answers(), **BLANK)["ring"]["from"] == "node1"


def test_another_host_id_up_at_one_of_its_addresses_stays_a_conflict():
    other = {"address": "10.0.0.70", "status": "U", "state": "N", "host_id": "id-x"}
    out = _state(answers=_answers(other, _me("U", "N")), addresses=[ME, "10.0.0.70"], running=True, host_id="id-7")
    assert out["state"] == "address_conflict"


def test_its_own_entry_at_its_addresses_first():
    """A dead node of the ring used another address of this host: its own entry, under its host ID, decides."""
    dead = {"address": "10.0.0.70", "status": "D", "state": "N", "host_id": "id-x"}
    out = _state(answers=_answers(dead, _me("U", "N")), addresses=[ME, "10.0.0.70"], running=True, host_id="id-7")
    assert out["state"] == "member" and out["ring"]["entry"]["host_id"] == "id-7"


def test_its_own_address_first():
    """Its own host ID UN under another address, its address DN under another host ID: a conflict, not this node."""
    other = {"address": "10.0.0.3", "status": "U", "state": "N", "host_id": "id-7"}
    out = _state(answers=_answers(other, _me("D", "N", "id-x")), running=True, own=_ring(_me("U", "N")), host_id="id-7")
    assert out["state"] == "address_conflict" and out["ring"]["entry"]["host_id"] == "id-x"


def test_a_member_by_its_host_id_under_another_address():
    """Its own ring gives its host ID: a node of the ring with that ID is this node (another address). Seen down
    while it runs here: can't be told (gossip late, or another machine)."""
    other = {"address": "10.0.0.77", "status": "D", "state": "N", "host_id": "id-7"}
    out = _state(answers=_answers(other), running=True, own=_ring(_me("U", "N")))
    assert out["state"] == "unknown" and out["ring"]["entry"]["address"] == "10.0.0.77"
    assert "Cassandra runs here but the ring sees it down" in out["reasons"]
    up = dict(other, status="U")
    assert _state(answers=_answers(up), running=True, own=_ring(_me("U", "N")))["state"] == "member"


def test_data_dirs_own_and_elsewhere():
    out = cassandra_node_data_dirs(["/data/cassandra/", ""], "data_file_directories: [/data/cassandra, /data2]\n",
                                   usual=["/var/lib/cassandra/data", "/data2"])
    assert out == {"own": ["/data/cassandra", "/data2"], "elsewhere": ["/var/lib/cassandra/data"], "problem": ""}
    # a live file without the key: Cassandra's default, the package's dir, is its own
    out = cassandra_node_data_dirs(["/data/cassandra"], "num_tokens: 16\n", usual=["/var/lib/cassandra/data"])
    assert out["own"] == ["/data/cassandra", "/var/lib/cassandra/data"] and out["elsewhere"] == []
    out = cassandra_node_data_dirs(["/data/cassandra"], "local_system_data_file_directory: /fast/system\n")
    assert out["own"] == ["/data/cassandra", "/var/lib/cassandra/data", "/fast/system"]
    # no live file: the inventory's only
    for live in (None, ""):
        out = cassandra_node_data_dirs(["/data/cassandra"], live, usual=["/var/lib/cassandra/data"])
        assert out == {"own": ["/data/cassandra"], "elsewhere": ["/var/lib/cassandra/data"], "problem": ""}
    assert cassandra_node_data_dirs("/data/cassandra")["own"] == ["/data/cassandra"]
    assert cassandra_node_data_dirs([], "a: [")["problem"].startswith("the live cassandra.yaml is not valid YAML")
    assert cassandra_node_data_dirs([], "- a\n")["problem"] == "the live cassandra.yaml is not a mapping"


def test_words():
    assert len(dict(STATES)) == len(STATES)
    for state, words in STATES:
        assert _state()["words"] == dict(STATES)[_state()["state"]] and words


def test_a_status_address_without_its_port():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_node_standing import status_address
    assert status_address("10.0.0.1:7000") == "10.0.0.1"
    assert status_address("[::1]:7000") == "::1"  # nodetool -pp's IPv6
    assert status_address("0:0:0:0:0:0:0:1") == "0:0:0:0:0:0:0:1"
    assert status_address(None) == ""
