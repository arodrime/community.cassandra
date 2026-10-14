"""The state of a node (plugins/filter/cassandra_node_standing.py): one place, every consumer decides from it."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_node_reset import cassandra_reset_choice
from ansible_collections.community.cassandra.plugins.filter.cassandra_node_standing import (
    RESETTABLE, STATES, cassandra_add_node_reset_check, cassandra_node_add_state, cassandra_node_data_dirs,
    cassandra_node_reset_refusal, cassandra_node_standing)

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


def test_replace_of_itself_is_not_in_the_ring():
    """replace_node, the host replacing itself (same address): the dead node it replaces is not this node."""
    out = _state(answers=_answers(_me("D", "N")), replace_address=ME, **OURS)
    assert out["state"] == "former_member"
    assert out["reasons"][-1] == "node1 sees 10.0.0.7 down (DN, host ID id-7): the node being replaced"
    assert cassandra_node_reset_refusal(out) == []
    up = _state(answers=_answers(_me("U", "N")), replace_address=ME)
    assert up["state"] == "address_conflict" and cassandra_node_reset_refusal(up)


def test_blank_and_evidence():
    """blank: what cassandra_config's question and create_cluster's new cluster read."""
    assert _state(**BLANK)["blank"] is True
    assert _state(**BLANK)["evidence"] == []
    assert _state(**dict(BLANK, held=["/var/lib/cassandra/commitlog/CommitLog-7-1.log"]))["evidence"] == [
        "/var/lib/cassandra/commitlog/CommitLog-7-1.log in its Cassandra directories"]
    assert _state()["evidence"] == [SYSTEM]
    assert _state(**dict(BLANK, running=True))["evidence"] == ["Cassandra is running"]
    assert _state(**dict(BLANK, live="unreadable"))["evidence"] == ["the live cassandra.yaml can not be read"]
    # an old system keyspace elsewhere: not its own, said apart (the caller decides)
    out = _state(**dict(BLANK, elsewhere=["/var/lib/cassandra/data/system"]))
    assert out["blank"] and out["elsewhere"] == ["/var/lib/cassandra/data/system"]


LONE = [{"host": "node1", "addresses": ["10.0.0.1"], "status": _ring(N1)}]
STOCK_HOST = {"host": "node3", "addresses": ["10.0.0.3"],
              "status": _ring({"address": "127.0.0.1", "status": "U", "state": "N", "host_id": "id-3"})}
STRAY = {"host": "node8", "addresses": ["10.0.0.8"],
         "status": _ring({"address": "10.0.0.8", "status": "U", "state": "N", "host_id": "id-8"})}


@pytest.mark.parametrize("answers, blank, holding", [
    (LONE, "blank", "failed_bootstrap"),  # a one-node cluster adding its second node
    (LONE + [STOCK_HOST], "blank", "failed_bootstrap"),  # another host of the group runs the package's own instance
    (LONE + [{"host": "node3", "addresses": ["10.0.0.3"], "status": None}], "blank", "unknown"),  # node3 did not answer
    (LONE + [{"host": "node3", "addresses": ["10.0.0.3"], "status": None, "asked": False}], "blank", "unknown"),  # stopped
    ([STRAY, {"host": "node1", "addresses": ["10.0.0.1"], "status": None, "asked": False}], "blank", "unknown"),
])
def test_a_node_alone_in_its_ring(answers, blank, holding):
    """A node alone in its ring stands for the cluster for a node without data (nothing to lose), not for one
    holding data while another node of the group did not answer, or was stopped (a stray host, the members down)."""
    assert _state(answers=answers, **BLANK)["state"] == blank
    # add_node plans with that ring (new_node_state.yml needs it known)
    assert _state(answers=answers, **BLANK)["ring"]["known"] is True
    out = _state(answers=answers, **OURS)
    assert out["state"] == (holding if holding != "failed_bootstrap" else "former_member")
    if holding == "unknown":
        assert cassandra_node_reset_refusal(out) and cassandra_add_node_reset_check(out)["reset"] is False
        assert out["reasons"][1].startswith("only ")
        assert out["ring"]["problems"][0].startswith("only ") and "nothing is reset" in out["ring"]["problems"][0]
        assert "node1" not in out["ring"]["tried"] or answers[0]["status"]  # a stopped node is not said asked


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


# add_node (and topology): the decision of each state, for each cassandra_add_node_reset
@pytest.mark.parametrize("facts, auto, given, refused", [
    #  facts                                  auto (default)  true             false
    (BLANK, "new", "new", "new"),
    ({}, "reset", "reset", "orphan"),
    (dict(OURS, bootstrap_started=True), "reset", "reset", "orphan"),
    (OURS, "refused", "reset", "orphan"),
    (dict(keyspaces=["system", "shop"]), "refused", "refused", "orphan"),
    (dict(live_cluster="billing"), "refused", "refused", "orphan"),
    (RUNNING_ALONE, "refused", "refused", "orphan"),
    (dict(BLANK, **RUNNING_ALONE), "running", "running", "running"),  # never added while Cassandra runs there
    (dict(BLANK, unreadable=["/x: can not be read (without root?)"]), "unknown", "unknown", "unknown"),
    (dict(BLANK, live="unreadable"), "unknown", "unknown", "unknown"),
    (dict(BLANK, running=True, own=_ring(_me("U", "N"), {"address": "10.9.9.9", "status": "U", "state": "N"})),
     "other_cluster", "other_cluster", "other_cluster"),
    (dict(answers=_answers(_me("D", "N")), running=True, own=None), "unknown", "unknown", "unknown"),
    (dict(unreadable=["/x: can not be read (without root?)"]), "refused", "refused", "orphan"),
    (dict(answers=[]), "refused", "refused", "orphan"),
    (dict(answers=_answers(_me("U", "N")), running=True, host_id="id-7"), "joined", "joined", "joined"),
    (dict(answers=_answers(_me("U", "J")), running=True, host_id="id-7"), "joining", "joining", "joining"),
    (dict(answers=_answers(_me("D", "N"))), "down", "down", "down"),
    (dict(answers=_answers(_me("U", "L")), running=True, host_id="id-7"), "busy", "busy", "busy"),
    (dict(BLANK, answers=_answers(_me("D", "N"))), "replace", "replace", "replace"),
    (dict(answers=_answers(_me("U", "N")), running=True, host_id="id-9"), "other_host", "other_host", "other_host"),
])
def test_add_node_decides_from_the_state(facts, auto, given, refused):
    state = _state(**facts)
    for choice, expected in (("auto", auto), ("true", given), ("false", refused)):
        add = cassandra_node_add_state(state, choice, ME)
        if expected in ("reset", "refused"):
            assert add["state"] == "new", (choice, add)
            check = cassandra_add_node_reset_check(state, explicit=choice == "true")
            assert check["reset"] is (expected == "reset"), (choice, check)
            assert bool(check["problems"]) is (expected == "refused")
        else:
            assert add["state"] == expected, (choice, add)
            assert bool(add["refusal"]) is (expected not in ("new", "joined", "joining"))
            if expected in ("running", "unknown", "other_cluster"):
                assert add["refusal"] and "it holds data" not in add["refusal"]


@pytest.mark.parametrize("facts, explicit, reset", [
    ({}, False, True), ({}, True, True),
    (OURS, False, False), (OURS, True, True),
    (dict(OURS, bootstrap_started=True), False, True),
    (dict(keyspaces=["system", "shop"]), True, False),  # given: a former member only, never data of no known cluster
    (dict(live_cluster="billing"), True, False),
    (RUNNING_ALONE, True, False),
    (dict(answers=[]), True, False),
    (dict(answers=_answers(_me("D", "N"))), True, False),
    (dict(unreadable=["/x: can not be read (without root?)"]), True, False),
])
def test_add_node_resets_only_what_its_state_allows(facts, explicit, reset):
    check = cassandra_add_node_reset_check(_state(**facts), explicit=explicit)
    assert check["reset"] is reset, (facts, explicit)
    assert (check["problems"] == []) is reset


def test_a_problem_always_stops_the_reset():
    """Never joined, but with a ring problem (a state made by hand, as a drift of the states would make it): refused."""
    state = dict(_state(), ring=dict(_state()["ring"], problems=["node1 sees 10.0.0.7 in its ring"]))
    assert state["state"] == "failed_bootstrap"
    check = cassandra_add_node_reset_check(state)
    assert check["reset"] is False and check["problems"] == ["node1 sees 10.0.0.7 in its ring"]


# reset_node and replace_node's reset: what may be emptied
@pytest.mark.parametrize("facts, may", [
    (BLANK, True),
    (dict(BLANK, answers=[]), False),  # no node answered: whether it is in the ring can't be checked, nothing is reset
    (dict(BLANK, live_cluster="billing"), False),  # another cluster's cassandra.yaml
    ({}, True),
    (OURS, True),
    (dict(keyspaces=["system", "shop"]), True),  # reset_node is told to: user keyspaces of no known cluster
    (dict(live="absent", live_cluster=None), True),
    (RUNNING_ALONE, True),  # stopped first
    (dict(live_cluster="billing"), False),
    (dict(RUNNING_ALONE, live_cluster="billing"), False),
    (dict(answers=_answers(_me("D", "N")), live_cluster="billing"), False),
    (dict(RUNNING_ALONE, own=_ring(_me("U", "N"), {"address": "10.9.9.9", "status": "U", "state": "N"})), False),
    (dict(answers=[]), False),
    (dict(answers=_answers(_me("U", "N")), running=True, host_id="id-7"), False),
    (dict(answers=_answers(_me("D", "N"))), False),
    (dict(BLANK, answers=_answers(_me("D", "N"))), False),
    (dict(answers=_answers(_me("U", "N")), running=True, host_id="id-9"), False),
    (dict(running=True, own=None), False),
    (dict(unreadable=["/x: can not be read (without root?)"]), False),
    (dict(answers=_answers({"address": "10.0.0.50", "status": "D", "state": "N", "host_id": "id-50"})), False),
    (dict(RUNNING_ALONE, answers=_answers({"address": "10.0.0.50", "status": "D", "state": "N", "host_id": "id-50"})),
     False),
    (dict(running=True, jvm=False, own=None,
          answers=_answers({"address": "10.0.0.50", "status": "D", "state": "N", "host_id": "id-50"})), False),
])
def test_reset_node_decides_from_the_state(facts, may):
    state = _state(**facts)
    problems = cassandra_node_reset_refusal(state)
    assert (problems == []) is may, (state["state"], problems)
    if may:
        assert state["state"] in RESETTABLE


def test_the_state_is_never_the_only_guard_of_a_reset():
    """A resettable state with a ring problem (as a drift of the states would make it): refused anyway."""
    state = dict(_state(), ring=dict(_state()["ring"], problems=["node1 sees 10.0.0.7 in its ring"]))
    assert state["state"] in RESETTABLE
    assert cassandra_node_reset_refusal(state) == ["node1 sees 10.0.0.7 in its ring"]


def test_whole_cluster_reset_refuses_another_cluster():
    """create_cluster's reset checked the ring itself: the node's cassandra.yaml still counts."""
    assert cassandra_node_reset_refusal(_state(answers=None, live_cluster="billing"), whole_cluster=True) == [
        "the live cassandra.yaml is for cluster 'billing', not 'my_cluster' (nor the package's stock 'Test Cluster'):"
        " a node of another cluster?"]
    for live in ("my_cluster", "Test Cluster", None):
        assert cassandra_node_reset_refusal(_state(answers=None, live_cluster=live), whole_cluster=True) == []
    # the ring is not looked at there (not read)
    assert cassandra_node_reset_refusal(_state(answers=[]), whole_cluster=True) == []


def test_reset_refusal_texts():
    assert cassandra_node_reset_refusal(_state(answers=_answers(_me("D", "N")))) == [
        "node1 sees 10.0.0.7 in its ring (DN, host ID id-7): node7 (dc1/rack_b) is a member of the cluster, never"
        " reset it. A node leaves with decommission_node (remove_dead_node when dead), then can be reset"]
    assert cassandra_node_reset_refusal(_state(live_cluster="billing")) == [
        "the live cassandra.yaml is for cluster 'billing', not 'my_cluster' (nor the package's stock 'Test Cluster'):"
        " a node of another cluster?"]
    # what could not be read, in the reset plan's own words (said once there)
    assert cassandra_node_reset_refusal(_state(unreadable=["/x: can not be read (without root?)"])) == [
        "/x: can not be read (without root?)"]


# add_node's reset check: what it says (ported from the checks it made itself before the state)
def _auto(ring="clear", explicit=False, size=12 * 1024 ** 3, has_data=True, **facts):
    answers = {"clear": _answers(), "seen": _answers(_me("U", "N")), "none": []}[ring]
    if not has_data:
        facts.update(held=[], system=[], keyspaces=[])
    return cassandra_add_node_reset_check(_state(answers=answers, **facts), size=size, explicit=explicit)


def test_auto_reset_of_a_stock_node_that_is_down():
    assert _auto() == {"reset": True, "problems": [],
                       "line": "node7 (dc1/rack_b): has data (12.0 GiB, cluster 'Test Cluster', not in any ring, down)"
                               " — will be reset",
                       "holds": "holds 12.0 GiB of cluster 'Test Cluster', not in the ring, stopped", "why": "",
                       "former": False, "way": ""}


def test_auto_reset_nothing_to_do_without_data():
    assert _auto(has_data=False, running=True, live_cluster="other") == {
        "reset": False, "problems": [], "line": "", "holds": "", "why": "", "former": False, "way": ""}


def test_auto_reset_refused_on_a_running_node():
    out = _auto(**RUNNING_ALONE)
    assert not out["reset"]
    assert out["problems"] == ["Cassandra runs on it: add_node resets only a node where Cassandra is down. Stop it"
                               " (systemctl stop cassandra) if it holds nothing you need, then run add_node again"]
    assert out["line"].startswith("node7 (dc1/rack_b): has data (12.0 GiB, cluster 'Test Cluster', not in any ring,"
                                  " running): not reset automatically, Cassandra runs on it")


def test_auto_reset_refused_when_in_the_ring():
    out = _auto(ring="seen", live_cluster="my_cluster")
    assert out["problems"] == [
        "node1 sees 10.0.0.7 in its ring (UN, host ID id-7): node7 (dc1/rack_b) is a member of the cluster, never reset"
        " it. A node leaves with decommission_node (remove_dead_node when dead), then can be reset"]
    assert "(12.0 GiB, cluster 'my_cluster', in the ring of this cluster, down)" in out["line"]


def test_auto_reset_refused_when_the_ring_can_not_be_read():
    out = _auto(ring="none")
    assert out["problems"] == ["no other node of the cluster answered as up and normal (UN) (asked: none): whether"
                               " node7 (dc1/rack_b) is in the ring can't be checked, nothing is reset"]
    assert "ring unknown" in out["line"]


def test_auto_reset_refused_for_another_cluster():
    out = _auto(live_cluster="billing")
    assert out["problems"] == ["its data is of cluster 'billing', neither 'my_cluster' nor the package's stock 'Test Cluster'"]
    assert "cluster 'billing'" in out["line"]


def test_auto_reset_refused_when_the_cluster_is_unknown():
    out = _auto(live="absent", live_cluster=None)
    assert out["problems"] == ["the cluster its data belongs to is unknown (no readable cassandra.yaml)"]
    assert "cluster unknown" in out["line"]


def test_auto_reset_refused_for_a_member_of_another_test_cluster_ring():
    out = _auto(peers=True)
    assert out["problems"] == ["its system.peers lists other nodes: a member of another 'Test Cluster' ring"]
    assert "in another ring" in out["line"]


def test_auto_reset_refused_for_user_keyspaces_of_test_cluster():
    out = _auto(keyspaces=["system", "shop", "users", "system_auth"])
    assert out["problems"] == ["it holds user keyspaces (shop, users): only a failed bootstrap of this cluster may"]
    assert "user keyspaces shop, users" in out["line"]


def test_auto_reset_of_a_failed_bootstrap_of_this_cluster():
    out = _auto(live_cluster="my_cluster", keyspaces=["system", "shop"], peers=True, cluster_keyspaces={"shop": {}},
                bootstrap_started=True)
    assert out["reset"] and out["problems"] == []
    assert out["line"] == ("node7 (dc1/rack_b): has data (12.0 GiB, cluster 'my_cluster', user keyspaces shop (a failed"
                           " bootstrap of this cluster), not in any ring, down) — will be reset")


def test_a_former_member_of_this_cluster_only_with_the_reset_given():
    """Its cluster and keyspaces, no bootstrap left unfinished (removed with removenode, put back in the
    inventory): the writes only it holds would be lost. Refused by default, reset when given on purpose."""
    former = dict(live_cluster="my_cluster", keyspaces=["system", "shop"], peers=True, cluster_keyspaces={"shop": {}})
    out = _auto(**former)
    assert not out["reset"]
    assert out["problems"] == [
        "it holds this cluster's keyspaces (shop) and no bootstrap add_node left unfinished: a former member of this"
        " cluster (removed with removenode?). The writes only it holds would be lost: snapshot or copy its data"
        " directories first if in doubt, then -e cassandra_add_node_reset=true resets it"]
    assert out["line"].startswith("node7 (dc1/rack_b): has data (12.0 GiB, cluster 'my_cluster', user keyspaces shop,"
                                  " not in any ring, down): not reset automatically, it holds this cluster's keyspaces")
    assert (out["holds"], out["why"], out["former"]) == (
        "holds 12.0 GiB of cluster 'my_cluster' (keyspaces shop), not in the ring, stopped",
        "a former member of this cluster: the writes only it holds would be lost if wiped", True)
    # refused for another reason too: not only a former member (the reset given would not do)
    assert _auto(**dict(former, **RUNNING_ALONE))["former"] is False
    out = _auto(explicit=True, **former)
    assert out == {"reset": True, "problems": [], "line": (
        "node7 (dc1/rack_b): has data (12.0 GiB, cluster 'my_cluster', user keyspaces shop (a former member of this"
        " cluster), not in any ring, down) — will be reset (-e cassandra_add_node_reset=true: the writes only it"
        " holds are lost; snapshot or copy its data first if in doubt)"),
        "holds": "holds 12.0 GiB of cluster 'my_cluster' (keyspaces shop), not in the ring, stopped", "why": "",
        "former": False, "way": ""}
    # the explicit reset changes nothing else: still refused when running, or in the ring
    assert not _auto(explicit=True, **dict(former, **RUNNING_ALONE))["reset"]
    assert not _auto(explicit=True, ring="seen", **former)["reset"]
    # stock or empty nodes: as before, given or not
    assert _auto()["reset"] and _auto(explicit=True)["reset"]


def test_auto_reset_refused_for_user_keyspaces_when_the_cluster_keyspaces_are_unknown():
    out = _auto(live_cluster="my_cluster", keyspaces=["shop"])
    assert not out["reset"]
    assert out["problems"] == ["it holds user keyspaces (shop) and the cluster's keyspaces could not be read: whether"
                               " they are a failed bootstrap's can't be checked"]
    assert _auto(live_cluster="my_cluster", keyspaces=["system"])["reset"]


def test_auto_reset_refused_for_keyspaces_this_cluster_does_not_have():
    out = _auto(live_cluster="my_cluster", keyspaces=["shop", "legacy"], cluster_keyspaces={"shop": {}})
    assert out["problems"] == ["it holds keyspaces this cluster does not have (legacy)"]


def test_auto_reset_every_reason_at_once():
    out = _auto(live_cluster="billing", keyspaces=["orders"], **RUNNING_ALONE)
    assert len(out["problems"]) == 3
    assert out["line"].endswith("Nothing was changed: check what it holds; if nothing is needed, empty it (reset_node)"
                                " or remove it from cassandra_target_nodes")


def test_auto_reset_refused_for_a_directory_that_can_not_be_read():
    """Its state can't be told: refused, and said (the reset's plan adds the directory too)."""
    out = _auto(unreadable=["/data/cassandra: can not be read (without root?)"])
    assert not out["reset"]
    assert out["problems"] == ["/data/cassandra: can not be read (without root?)"]


def test_auto_reset_a_cluster_with_the_stock_name_does_not_own_stock_nodes():
    out = _auto(cluster_name="Test Cluster", peers=True)
    assert out["problems"] == ["its system.peers lists other nodes: a member of another 'Test Cluster' ring"]
    out = _auto(cluster_name="Test Cluster", keyspaces=["system", "app"], cluster_keyspaces=["app"])
    assert out["problems"] == ["it holds user keyspaces (app): only a failed bootstrap of this cluster may"]
    assert _auto(cluster_name="Test Cluster")["reset"]


@pytest.mark.parametrize("kwargs, way", [
    (dict(ring="seen"), "a member of this cluster: never reset it; take it out of cassandra_target_nodes"),
    (dict(ring="none"), "its ring could not be checked: wait until the cluster's nodes answer"),
    (dict(live_cluster="billing"), "a node of another cluster: take it out of cassandra_target_nodes, or, if nothing it"
     " holds is needed, empty its directories by hand (reset_node refuses another cluster's node)"),
    (RUNNING_ALONE, "if it holds nothing you need, stop Cassandra on it (systemctl stop cassandra): add_node then"
     " resets it"),
    (dict(live_cluster="my_cluster", keyspaces=["shop"]),
     "let add_node read the cluster's keyspaces (CQL login: cassandra_cql_username, cassandra_cql_password)"),
    (dict(keyspaces=["shop"]), "check what it holds; if nothing is needed, empty it (reset_node), else take it out of"
     " cassandra_target_nodes"),
])
def test_each_refusal_says_its_way_out(kwargs, way):
    out = _auto(**kwargs)
    assert not out["reset"] and not out["former"] and out["way"] == way


def test_the_way_out_of_a_former_member_goes_through():
    """add_node's TO DO for a former member: -e cassandra_add_node_reset=true (the plan then resets it), or reset_node
    (a former member is RESETTABLE)."""
    former = dict(live_cluster="my_cluster", keyspaces=["system", "shop"], peers=True, cluster_keyspaces={"shop": {}})
    assert _auto(**former)["former"] is True
    given = cassandra_reset_choice("true")  # the value the command gives
    assert _auto(explicit=given == "true", **former)["reset"] is True
    assert cassandra_node_reset_refusal(_state(**former)) == []


def test_the_way_out_of_unidentified_data_goes_through():
    """add_node says: empty it with reset_node: reset_node does."""
    for facts in (dict(keyspaces=["shop"]), dict(peers=True), dict(live="absent", live_cluster=None)):
        assert "empty it (reset_node)" in _auto(**facts)["way"]
        assert cassandra_node_reset_refusal(_state(**facts)) == []


def test_add_node_refusal_texts():
    """The reasons add_node gives for the nodes it does not add."""
    def refusal(**facts):
        return cassandra_node_add_state(_state(**facts), "auto", ME)["refusal"]
    assert refusal(answers=_answers(_me("D", "N")), **BLANK) == (
        "a dead node with this address is in the ring (DN, host ID id-7). Adding this node would be a replace, not an"
        " add: run replace_node (-e cassandra_target_nodes=node7 -e cassandra_replace_address=10.0.0.7).")
    assert refusal(answers=_answers(_me("D", "J"))).startswith("down in the middle of its bootstrap (DJ, host ID id-7)")
    assert refusal(answers=_answers(_me("D", "N"))).startswith("already in the ring (host ID id-7) but down")
    assert refusal(answers=_answers(_me("U", "N"))) == (
        "the ring has UN host ID id-7 at this address, but this node does not answer (not running, or JMX unreachable):"
        " the address is used by another node (IP reused, or a replace in progress). Find which machine owns id-7"
        " before going on.")
    assert "has host ID id-9" in refusal(answers=_answers(_me("U", "N")), running=True, host_id="id-9")
    assert refusal(answers=_answers(_me("U", "M")), running=True, host_id="id-7").startswith("UM in the ring")
    out = cassandra_node_add_state(_state(), "false", ME)
    assert out["state"] == "orphan" and out["refusal"].startswith(
        "has data (/var/lib/cassandra/data/system) but is not in the ring")


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


def test_import_says_a_stopped_ring_node_in_these_words():
    from ansible_collections.community.cassandra.plugins.filter import cassandra_import_nodes
    with open(cassandra_import_nodes.__file__) as f:
        text = f.read()
    assert 'standing.WORDS["member_down"]' in text


def test_the_guide_lists_every_state_with_its_words():
    """One word per meaning: the operator guide gives each state its words, as the output says them."""
    with open(os.path.join(ROOT, "docs", "docsite", "rst", "guide_output.rst")) as f:
        guide = re.sub(r"\s+", " ", f.read())
    for state, words in STATES:
        assert "``%s``" % state in guide, state
        assert words in guide, state
