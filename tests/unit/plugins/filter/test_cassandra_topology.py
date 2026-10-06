from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The plan of the playbook topology: the inventory is the desired state.

from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen
from ansible_collections.community.cassandra.plugins.filter.cassandra_topology import (
    cassandra_topology_plan, cassandra_topology_screen)


def entry(i, rack="r1", status="U", state="N", load="40.1 GiB", owns="25.0%"):
    return {"address": "10.0.0.%d" % i, "rack": rack, "status": status, "state": state, "load": load, "owns": owns,
            "host_id": "id-%d" % i}


def host(i, absent=False, dc="dc1", rack="r1", **extra):
    h = {"name": "node%d" % i, "absent": absent, "address": "10.0.0.%d" % i, "dc": dc, "rack": rack, "seed": False,
         "reachable": True, "running": True, "state": None, "refused": "", "single": False, "token": "", "reset": None}
    h.update(extra)
    return h


def ring(*entries, dc="dc1"):
    return {dc: {"nodes": list(entries)}}


def test_add_and_remove():
    # node1-4 in the ring, node4 marked absent, node5 new
    hosts = [host(1, seed=True), host(2), host(3), host(4, absent=True, state="normal"), host(5, state="new")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))
    assert plan["add"] == ["node5"]
    assert plan["remove"] == ["node4"]
    assert plan["in_ring"] == ["node4"]
    assert plan["gone"] == [] and plan["unknown"] == [] and plan["problems"] == []
    assert plan["nodes_left"] == {"dc1": 4}


def test_nothing_to_do_and_already_removed():
    hosts = [host(1), host(2), host(3), host(4, absent=True, running=False)]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3)))
    assert plan["add"] == [] and plan["remove"] == []
    assert plan["gone"] == ["node4"]
    assert plan["problems"] == []


def test_unknown_ring_nodes_are_reported_and_stop_a_plan():
    hosts = [host(1), host(2), host(3)]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(9, status="D")))
    assert plan["unknown"] == ["10.0.0.9 (dc1 / r1, DN)"]
    assert plan["problems"] == []  # nothing to do: only reported
    plan = cassandra_topology_plan(hosts + [host(4)], ring(entry(1), entry(2), entry(3), entry(9)))
    assert plan["add"] == ["node4"]
    assert len(plan["problems"]) == 1 and "10.0.0.9 (dc1 / r1, UN)" in plan["problems"][0]


def test_absent_and_unreachable():
    hosts = [host(1), host(2), host(3), host(4, absent=True, reachable=False), host(5, absent=True, reachable=False)]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))
    # not answering, out of the ring: nothing to do, not said "stopped"
    assert plan["remove"] == [] and plan["gone"] == [] and plan["silent"] == ["node5"]
    assert len(plan["problems"]) == 1
    assert plan["problems"][0].startswith("node4 (10.0.0.4) is marked absent and still in the ring (dc1, UN) but does not answer")
    assert "remove_dead_node -e cassandra_dead_node_address=10.0.0.4" in plan["problems"][0]


def test_absent_refusals():
    hosts = [host(1), host(2), host(3), host(4), host(5, absent=True, seed=True), host(6, absent=True),
             host(7, absent=True, refused="its decommission failed"), host(8, absent=True)]
    # node6 down in the ring; node8 runs, out of the ring
    entries = [entry(i, status="D" if i == 6 else "U") for i in range(1, 8)]
    plan = cassandra_topology_plan(hosts, ring(*entries), max_removals=5)
    problems = plan["problems"]
    assert any(p.startswith("node5 is marked absent but is in cassandra_seeds") for p in problems)
    assert any(p.startswith("node6 (10.0.0.6) is marked absent and down in the ring") for p in problems)
    assert "node7: its decommission failed" in problems
    assert any(p.startswith("node8 is marked absent and runs Cassandra, but is not in the ring") for p in problems)
    assert plan["remove"] == []


def test_finish_a_decommission_and_wait_for_one():
    # node4 still leaving (UL), node5 decommissioned but running: stopped and disabled only
    hosts = [host(1), host(2), host(3), host(4, absent=True, state="leaving"), host(5, absent=True, state="decommissioned"),
             host(6, absent=True, state="normal")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4, state="L"), entry(6), entry(7), entry(8)),
                                   max_removals=3, allow_large=False)
    assert plan["remove"] == ["node4", "node5", "node6"]
    assert plan["finish"] == ["node5"]
    assert plan["in_ring"] == ["node4", "node6"]
    assert plan["leaving"] == ["node4"]


def test_no_add_while_a_node_leaves():
    for state, ring_state in (("new", None), ("joining", "J")):
        hosts = [host(1), host(2), host(3), host(4, absent=True, state="leaving"), host(5, state=state)]
        entries = [entry(1), entry(2), entry(3), entry(4, state="L")] + ([entry(5, state=ring_state)] if ring_state else [])
        plan = cassandra_topology_plan(hosts, ring(*entries))
        assert any(p.startswith("node4 still leaving") for p in plan["problems"]), state


def test_guard_on_large_removals():
    hosts = [host(i) for i in range(1, 6)] + [host(i, absent=True) for i in range(6, 9)]
    full = ring(*[entry(i) for i in range(1, 9)])
    plan = cassandra_topology_plan(hosts, full)
    assert len(plan["problems"]) == 1
    assert plan["problems"][0].startswith("the plan removes 3 nodes (node6, node7, node8), more than cassandra_topology_max_removals (2)")
    assert cassandra_topology_plan(hosts, full, max_removals=3)["problems"] == []
    assert cassandra_topology_plan(hosts, full, allow_large=True)["problems"] == []
    # more than half of a datacenter: 2 of 3
    hosts = [host(1), host(2, absent=True), host(3, absent=True)]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3)))
    assert plan["problems"] == ["the plan removes 2 of the 3 nodes of dc1 (node2, node3): more than half of the datacenter."
                                " A datacenter goes with remove_datacenter; else set cassandra_topology_allow_large_removal: true."]
    # half exactly is not more than half
    hosts = [host(1), host(2), host(3, absent=True), host(4, absent=True)]
    assert cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))["problems"] == []


def test_same_address_on_two_hosts():
    hosts = [host(1), host(2), host(3), host(4, absent=True), host(9, address="10.0.0.4")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))
    assert "node4 (marked absent) and node9 have the same address 10.0.0.4: fix the inventory" in plan["problems"][0]
    # two present hosts too
    hosts = [host(1), host(2), host(3), host(8, address="10.0.0.3", state="new")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3)))
    assert plan["problems"] == ["node3 and node8 have the same address 10.0.0.3: fix the inventory (an address reused?)"
                                " before adding or removing any of them."]


def test_adds_joining_first_seed_and_refusals():
    hosts = [host(1), host(2), host(3), host(4, state="new"), host(5, state="joining"), host(6, seed=True),
             host(7, refused="has data but is not in the ring")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(5, state="J")))
    assert plan["add"] == ["node5", "node4"]
    assert plan["joining"] == ["node5"]
    assert any(p.startswith("node6: in cassandra_seeds, and not in the ring yet") for p in plan["problems"])
    assert "node7: has data but is not in the ring" in plan["problems"]


def test_down_present_node_warned():
    hosts = [host(1), host(2), host(3), host(4, state="new")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3, status="D")))
    assert plan["down"] == ["node3"]
    assert plan["warnings"] == ["node3 down in the ring: the check before the first step stops the run unless it is back up."]


def test_one_token_per_node():
    hosts = [host(1, single=True, token="0"), host(2, single=True, token="1"), host(3, single=True, state="new")]
    r = ring(entry(1), entry(2))
    assert cassandra_topology_plan(hosts, r)["problems"] == [
        "one token per node: node3 has no cassandra_initial_token. Set it, or -e cassandra_token_auto=bisect"
        " (no node moves) or balanced (an even ring)."]
    assert cassandra_topology_plan(hosts, r, token_auto="bisect")["problems"] == []
    assert "asks a second question" in cassandra_topology_plan(hosts, r, token_auto=True)["problems"][0]
    hosts[2]["token"] = "42"
    assert cassandra_topology_plan(hosts, r)["problems"] == []


KEYSPACES = {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 3}}}


def test_screen():
    hosts = [host(1, seed=True), host(2), host(3, rack="r2"), host(4, absent=True, rack="r2", state="normal"),
             host(5, state="new", rack="r2"), host(6, absent=True, running=False)]
    r = ring(entry(1), entry(2), entry(3, rack="r2"), entry(4, rack="r2"))
    plan = cassandra_topology_plan(hosts, r)
    names = dict(("10.0.0.%d" % i, "node%d" % i) for i in range(1, 7))
    spec = cassandra_topology_screen(plan, hosts, ring=r, keyspaces=KEYSPACES, names=names)
    text = cassandra_screen(dict(spec, cluster="Orders", version="5.0.4"), session="")
    assert text.startswith("topology on cluster 'Orders' (Cassandra 5.0.4): add node5; remove node4\n")
    assert "\n  1. add node5  10.0.0.5  dc1 / r2\n    bootstraps (add_node): streams its share of dc1, about 32.1 GiB" \
           " (the load of dc1 / 5 nodes)\n    end state: up and normal (UN) in dc1 / r2\n" in text
    assert "\n  2. remove node4  10.0.0.4  dc1 / r2\n    not a seed\n    load 40.1 GiB, owns 25.0%\n" in text
    assert "data goes to the other nodes of dc1: node1, node2, node3, node5" in text
    assert "WARNING" not in text  # r1 2, r2 2 once done
    assert "Already removed (marked absent, out of the ring, Cassandra stopped): node6." in text
    assert "Afterwards dc1 has 4 nodes: node1, node2, node3, node5." in text
    assert "Then delete node4, node6 from the inventory (or leave them marked absent" in text
    check = cassandra_screen(dict(spec, cluster="Orders"), check=True)
    assert "--check: nothing will be changed (the plan only, no question)." in check


def test_screen_reset_and_unknown_warnings():
    reset = {"stop": True, "disable": True, "delete": ["/d/data/system"], "dirs": ["/d/data: 1 entries: system"]}
    hosts = [host(1), host(2), host(3), host(4, state="new", reset=reset)]
    r = ring(entry(1), entry(2), entry(3), entry(9))
    plan = cassandra_topology_plan(hosts, r)
    text = cassandra_screen(cassandra_topology_screen(plan, hosts, ring=r))
    assert "reset first (cassandra_add_node_reset): see the data loss warning" in text
    assert "WARNING - data loss: node4: Cassandra stopped, kept from starting at boot, then 1 entries DELETED" in text
    assert "WARNING - unknown: 10.0.0.9 (dc1 / r1, UN) is in the ring but no host of the inventory has this\n" in text


def test_screen_racks_once_done():
    hosts = [host(1), host(2, rack="r2"), host(3, rack="r2"), host(4, absent=True, state="normal")]
    r = ring(entry(1), entry(2, rack="r2"), entry(3, rack="r2"), entry(4))
    text = cassandra_screen(cassandra_topology_screen(cassandra_topology_plan(hosts, r), hosts, ring=r))
    assert "WARNING - racks: dc1 once done: r1 1, r2 2 nodes per rack." in text


def test_absent_joining_or_moving_refused():
    for state, word in (("J", "joining"), ("M", "moving")):
        hosts = [host(1), host(2), host(3), host(4, absent=True, state="normal")]
        plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4, state=state)))
        assert plan["remove"] == []
        assert plan["problems"][0].startswith("node4 (10.0.0.4) is marked absent and %s in the ring" % word)


def test_absent_without_address_and_unknown_ring_nodes():
    # does not answer, no address in the inventory: maybe the ring's unknown node, not "already removed"
    hosts = [host(1), host(2), host(3), host(4, absent=True, reachable=False, address="")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(9)))
    assert plan["silent"] == [] and plan["gone"] == []
    assert plan["problems"][0].startswith("node4 is marked absent, does not answer and the inventory gives no address")
    # every ring node known: it is not in the ring
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3)))
    assert plan["silent"] == ["node4"] and plan["problems"] == []


def test_half_a_datacenter_counts_the_adds():
    # replacing hardware: 3 nodes, 2 new ones added first, 2 old ones removed: the datacenter keeps 3
    hosts = [host(1), host(2, absent=True), host(3, absent=True), host(4, state="new"), host(5, state="new")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3)))
    assert plan["problems"] == [] and plan["add"] == ["node4", "node5"] and plan["remove"] == ["node2", "node3"]


def test_one_token_per_node_add_and_remove_in_one_run_refused():
    hosts = [host(1, single=True, token="0"), host(2, single=True, token="1"), host(3, single=True, token="2"),
             host(4, absent=True, single=True), host(5, single=True, state="new", token="3")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))
    assert len(plan["problems"]) == 1
    assert plan["problems"][0].startswith("one token per node: adding node5 and removing node4 in one run")
