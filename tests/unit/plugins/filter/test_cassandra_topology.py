from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The plan of the playbook topology: the inventory is the desired state.

from ansible_collections.community.cassandra.plugins.filter.cassandra_topology import (
    cassandra_seed_change, cassandra_topology_plan, cassandra_topology_screen, cassandra_topology_steps)


def screen(plan, hosts, **kwargs):
    return "\n".join(cassandra_topology_screen(plan, hosts, **kwargs))


def warnings_of(lines):
    return [line for line in lines if line.startswith("WARNING  ")]


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
    assert "remove_dead_node -e cassandra_target_nodes=10.0.0.4" in plan["problems"][0]


def test_absent_refusals():
    hosts = [host(1), host(2), host(3), host(4), host(5, absent=True, seed=True), host(6, absent=True),
             host(7, absent=True, refused="its decommission failed"), host(8, absent=True)]
    # node6 down in the ring; node8 runs, out of the ring
    entries = [entry(i, status="D" if i == 6 else "U") for i in range(1, 8)]
    plan = cassandra_topology_plan(hosts, ring(*entries))
    problems = plan["problems"]
    assert any(p.startswith("node5 is marked absent but is still in cassandra_seeds: take it out of cassandra_seeds in"
                            " the inventory") for p in problems)
    assert any(p.startswith("node6 (10.0.0.6) is marked absent and down in the ring") for p in problems)
    assert "node7: its decommission failed" in problems
    assert any(p.startswith("node8 is marked absent and runs Cassandra, but is not in the ring") for p in problems)
    assert plan["remove"] == []


def test_finish_a_decommission_and_wait_for_one():
    # node4 still leaving (UL), node5 decommissioned but running: stopped and disabled only
    hosts = [host(1), host(2), host(3), host(4, absent=True, state="leaving"), host(5, absent=True, state="decommissioned"),
             host(6, absent=True, state="normal")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4, state="L"), entry(6), entry(7), entry(8)))
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


def test_no_cap_on_the_removals():
    # 3 of 8 removed: no cap, no warning (not more than half of dc1)
    hosts = [host(i) for i in range(1, 6)] + [host(i, absent=True) for i in range(6, 9)]
    plan = cassandra_topology_plan(hosts, ring(*[entry(i) for i in range(1, 9)]))
    assert plan["problems"] == [] and plan["remove"] == ["node6", "node7", "node8"] and plan["large_removals"] == []
    assert warnings_of(cassandra_topology_screen(plan, hosts)) == []


def test_more_than_half_a_datacenter_a_warning():
    # 2 of 3: not refused, said on the screen (under --check too)
    hosts = [host(1), host(2, absent=True), host(3, absent=True)]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3)))
    assert plan["problems"] == [] and plan["remove"] == ["node2", "node3"]
    assert plan["large_removals"] == ["2 of 3 nodes of dc1 removed (node2, node3): 1 node left to hold their data"]
    for check in (False, True):
        assert warnings_of(cassandra_topology_screen(plan, hosts, check=check)) == [
            "WARNING  2 of 3 nodes of dc1 removed (node2, node3): 1 node left to hold their data"]
    # 3 of 5, a range of names
    hosts = [host(1), host(2)] + [host(i, absent=True) for i in (3, 4, 5)]
    plan = cassandra_topology_plan(hosts, ring(*[entry(i) for i in range(1, 6)]))
    assert plan["problems"] == []
    assert plan["large_removals"] == ["3 of 5 nodes of dc1 removed (node3..node5): 2 nodes left to hold their data"]
    # half exactly is not more than half
    hosts = [host(1), host(2), host(3, absent=True), host(4, absent=True)]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))
    assert plan["problems"] == [] and plan["large_removals"] == []


def test_same_address_on_two_hosts():
    hosts = [host(1), host(2), host(3), host(4, absent=True), host(9, address="10.0.0.4")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))
    assert "node4 (marked absent) and node9 have the same address 10.0.0.4: fix the inventory" in plan["problems"][0]
    # two present hosts too
    hosts = [host(1), host(2), host(3), host(8, address="10.0.0.3", state="new")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3)))
    assert plan["problems"] == ["node3 and node8 have the same address 10.0.0.3: fix the inventory (an address reused?)"
                                " before adding or removing any of them."]


def test_adds_joining_first_a_seed_too_and_refusals():
    # node6 in cassandra_seeds, not in the ring: added (it joins as a regular node, made a seed after)
    hosts = [host(1), host(2), host(3), host(4, state="new"), host(5, state="joining"), host(6, seed=True),
             host(7, refused="has data but is not in the ring")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(5, state="J")))
    assert plan["add"] == ["node5", "node4", "node6"]
    assert plan["joining"] == ["node5"]
    assert plan["problems"] == ["node7: has data but is not in the ring"]


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
    lines = cassandra_topology_screen(plan, hosts, ring=r, keyspaces=KEYSPACES, names=names, cluster="Orders",
                                      version="5.0.4")
    assert lines == [
        "PLAN  topology  Orders (Cassandra 5.0.4)  2 steps, one node at a time",
        "  1.  add node5           dc1/r2  bootstrap, ~32.1 GiB to stream (the load of dc1 / 5 nodes)",
        "  2.  decommission node4  dc1/r2  load 40.1 GiB, owns 25.0% -> node1..node3, node5",
        "",
        "dc1 after:        4 nodes: node1..node3, node5   r1 2  r2 2   highest RF 3 (orders)",
        "already removed:  node6 (marked absent, out of the ring, Cassandra stopped)",
        "then:             empty node4, node6 (reset_node) before reusing the hosts, re-import the cluster to drop them"
        " from the inventory: the commands at the end",
        "cleanup:          of the nodes that hand data over: its command is printed after the adds (topology runs"
        " none, the removals move data again)"]
    # confirm.yml asks right below; no question asked: said; --check: said, nothing asked
    lines = cassandra_topology_screen(plan, hosts, ring=r, confirm=False)
    assert lines[-2:] == ["", "cassandra_operation_confirm is false: no question, the run goes on."]
    lines = cassandra_topology_screen(plan, hosts, ring=r, check=True)
    assert lines[-2:] == ["", "--check: nothing will be changed"]


def test_screen_warnings_last_and_the_session_on_a_real_run_only():
    hosts = [host(1), host(2), host(3, absent=True, state="normal"), host(4, absent=True, state="normal")]
    r = ring(entry(1), entry(2), entry(3), entry(4, status="U"), entry(9))
    plan = cassandra_topology_plan(hosts, r)
    plan["problems"] = []  # the unknown node stops a real plan: the screen only here
    lines = cassandra_topology_screen(plan, hosts, ring=r, session="not inside tmux or screen", confirm=False)
    warned = warnings_of(lines)
    assert warned[0] == "WARNING  2 of 5 nodes of dc1 removed (node3, node4): 3 nodes left to hold their data" or \
        warned[0].startswith("WARNING  10.0.0.9")
    assert warned[-1] == "WARNING  not inside tmux or screen"
    # the WARNING lines together, then one blank line and the question's place
    first = lines.index(warned[0])
    assert lines[first:] == warned + ["", "cassandra_operation_confirm is false: no question, the run goes on."]
    check = cassandra_topology_screen(plan, hosts, ring=r, session="not inside tmux or screen", check=True)
    assert "WARNING  not inside tmux or screen" not in check
    assert [line for line in check if line.startswith("real run:")] == [
        "real run:   would also warn about: not inside tmux or screen"]


def test_screen_reset_and_unknown_warnings():
    reset = {"stop": True, "disable": True, "delete": ["/d/data/system"], "dirs": ["/d/data: 1 entries: system"]}
    hosts = [host(1), host(2), host(3), host(4, state="new", reset=reset)]
    r = ring(entry(1), entry(2), entry(3), entry(9))
    plan = cassandra_topology_plan(hosts, r)
    lines = cassandra_topology_screen(plan, hosts, ring=r)
    assert lines[1].endswith("); will be reset first")
    # add_node's own line, without the node's name (the step has it)
    reset["line"] = "node4 (dc1/r1): has data (1.0 GiB, cluster 'Test Cluster', not in any ring, down) \u2014 will be reset"
    lines = cassandra_topology_screen(plan, hosts, ring=r)
    assert lines[1].endswith("); has data (1.0 GiB, cluster 'Test Cluster', not in any ring, down) \u2014 will be"
                             " reset")
    assert ("WARNING  data loss: node4: Cassandra stopped, kept from starting at boot, then 1 entries DELETED for good"
            " (no snapshot, no backup), in:") in lines
    assert lines[lines.index("WARNING  10.0.0.9 (dc1 / r1, UN) is in the ring but in no host of the inventory: never"
                             " touched (an address mistyped, a host missing, or a dead node: remove_dead_node -e"
                             " cassandra_target_nodes=10.0.0.9)") + 1].startswith("WARNING  data loss")
    assert "           /d/data: 1 entries: system" in lines  # under its warning's text
    check = cassandra_topology_screen(plan, hosts, ring=r, check=True)
    assert not [line for line in check if "data loss:" in line]
    assert [line for line in check if line.endswith("would also warn about: data loss")]


def test_screen_racks_once_done():
    hosts = [host(1), host(2, rack="r2"), host(3, rack="r2"), host(4, absent=True, state="normal")]
    r = ring(entry(1), entry(2, rack="r2"), entry(3, rack="r2"), entry(4))
    lines = cassandra_topology_screen(cassandra_topology_plan(hosts, r), hosts, ring=r)
    assert ("WARNING  dc1 once done: r1 1, r2 2 nodes per rack: with as many replicas as racks, a node of a smaller"
            " rack holds a bigger share (1/1 against 1/2)") in lines


def test_screen_joining_leaving_and_one_token():
    hosts = [host(1, single=True, token="0"), host(2, single=True, token="1"), host(3, single=True, token="2"),
             host(5, single=True, state="joining", token="5"), host(6, single=True, state="new")]
    r = ring(entry(1), entry(2), entry(3), entry(5, state="J"))
    lines = cassandra_topology_screen(cassandra_topology_plan(hosts, r, token_auto="bisect"), hosts, ring=r)
    assert lines[1].endswith("add node5  dc1/r1  still bootstrapping (UJ, an earlier run): waited for")
    assert lines[2].endswith("(the load of dc1 / 5 nodes); token from cassandra_token_auto")
    hosts = [host(1), host(2), host(3), host(4, absent=True, state="leaving"), host(5, absent=True, state="decommissioned")]
    r = ring(entry(1), entry(2), entry(3), entry(4, state="L"))
    lines = cassandra_topology_screen(cassandra_topology_plan(hosts, r), hosts, ring=r)
    assert "still leaving (an earlier run): waited for; load 40.1 GiB, owns 25.0% -> node1..node3" in lines[1]
    assert lines[2].endswith("decommission node5  dc1/r1  already out of the ring (an earlier run): Cassandra stopped"
                             " and disabled only")


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
    assert plan["large_removals"] == []


def test_one_token_per_node_add_and_remove_in_one_run_refused():
    hosts = [host(1, single=True, token="0"), host(2, single=True, token="1"), host(3, single=True, token="2"),
             host(4, absent=True, single=True), host(5, single=True, state="new", token="3")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2), entry(3), entry(4)))
    assert len(plan["problems"]) == 1
    assert plan["problems"][0].startswith("one token per node: adding node5 and removing node4 in one run")


def seeded(i, live="10.0.0.1,10.0.0.2", **extra):
    """A host with the names a seed entry may give it and the seed list of its cassandra.yaml."""
    extra.setdefault("names", ["node%d" % i, "10.0.0.%d" % i])
    return host(i, live=live, **extra)


def test_seed_change_replaces_a_seed():
    hosts = [seeded(1), seeded(2, absent=True, rack="r2"), seeded(3), seeded(5, live="", rack="r2")]
    change = cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.5:7000"])
    assert change["new"] == "10.0.0.1,10.0.0.5:7000"
    assert change["old"] == ["10.0.0.1", "10.0.0.2"]
    assert change["old_hosts"] == ["node1", "node2"]
    assert change["removed"] == ["node2 (dc1/r2)"] and change["added"] == ["node5 (dc1/r2)"]
    assert change["differ"] == ["node1", "node3"]  # node5 has no cassandra.yaml yet, node2 leaves
    assert change["step"] and change["problems"] == []


def test_seed_change_nothing_to_do_and_a_rewrite():
    hosts = [seeded(1), seeded(2), seeded(3)]
    assert cassandra_seed_change(hosts, "10.0.0.1,10.0.0.2")["step"] is False
    assert cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.2"])["step"] is False
    # the same seeds, written another way on one node: rewritten there
    hosts[2]["live"] = "node1,node2"
    change = cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.2"])
    assert change["removed"] == [] and change["added"] == [] and change["differ"] == ["node3"] and change["step"]


def test_seed_change_refusals():
    # dc2's only seed leaves the list: dc2 keeps nodes but no seed
    hosts = [seeded(1, live="10.0.0.1,10.0.0.4"), seeded(2, live="10.0.0.1,10.0.0.4"),
             seeded(4, dc="dc2", live="10.0.0.1,10.0.0.4"), seeded(5, dc="dc2", live="10.0.0.1,10.0.0.4")]
    assert cassandra_seed_change(hosts, ["10.0.0.1"])["problems"] == [
        "dc2 would be left with no seed (node4 leaves the seed list): put a node of dc2 in cassandra_seeds."]
    assert cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.5"])["problems"] == []
    # dc2 removed for good: no node left there, nothing to seed
    hosts[2]["absent"] = hosts[3]["absent"] = True
    assert cassandra_seed_change(hosts, ["10.0.0.1"])["problems"] == []
    assert cassandra_seed_change(hosts, [])["problems"][0].startswith("cassandra_seeds is empty")


def test_plan_replaces_a_seed_in_one_run():
    # node2 (a seed) marked absent and out of cassandra_seeds, node5 new and in it
    hosts = [seeded(1, seed=True), seeded(2, absent=True, state="normal"), seeded(3), seeded(4),
             seeded(5, live="127.0.0.1:7000", seed=True, state="new", rack="r2")]
    r = ring(entry(1), entry(2), entry(3), entry(4))
    plan = cassandra_topology_plan(hosts, r, seeds=["10.0.0.1", "10.0.0.5"])
    # node5's own cassandra.yaml (the package's, not set up yet) is no list the cluster runs with
    assert plan["seeds"]["old"] == ["10.0.0.1", "10.0.0.2"]
    assert plan["problems"] == []
    assert plan["add"] == ["node5"] and plan["remove"] == ["node2"]
    assert plan["seeds"]["step"] and plan["seeds"]["removed"] == ["node2 (dc1/r1)"]
    # the plan in order: adds, seeds, removals
    assert [s["node"] for s in cassandra_topology_steps(plan, hosts)] == ["add node5", "seeds", "decommission node2"]
    lines = cassandra_topology_screen(plan, hosts, ring=r)
    assert lines[:4] == [
        "PLAN  topology  3 steps, one node at a time",
        "  1.  add node5           dc1/r2  bootstrap, ~32.1 GiB to stream (the load of dc1 / 5 nodes); joins as a"
        " regular node, a seed at the seed step",
        "  2.  seeds                       10.0.0.1,10.0.0.2 -> 10.0.0.1,10.0.0.5  written and reloaded live on every"
        " node, no restart",
        "  3.  decommission node2  dc1/r1  load 40.1 GiB, owns 25.0% -> node1, node3..node5"]
    assert warnings_of(lines)[0] == ("WARNING  the seeds will change on every node: 10.0.0.1,10.0.0.2 -> 10.0.0.1,10.0.0.5"
                                     " (from cassandra_seeds in the inventory)")
    # the seed still in cassandra_seeds: refused, whatever the rest
    hosts[1]["seed"] = True
    plan = cassandra_topology_plan(hosts, r, seeds=["10.0.0.1", "10.0.0.2", "10.0.0.5"])
    assert plan["remove"] == []
    assert plan["problems"][0].startswith("node2 is marked absent but is still in cassandra_seeds")


def test_plan_seed_change_alone_and_its_refusal():
    hosts = [seeded(1), seeded(2, rack="r2"), seeded(3, dc="dc2", live="10.0.0.1,10.0.0.2,10.0.0.3"),
             seeded(4, dc="dc2", live="10.0.0.1,10.0.0.2,10.0.0.3")]
    for h in hosts[:2]:
        h["live"] = "10.0.0.1,10.0.0.2,10.0.0.3"
    r = {"dc1": {"nodes": [entry(1), entry(2, rack="r2")]}, "dc2": {"nodes": [entry(3), entry(4)]}}
    plan = cassandra_topology_plan(hosts, r, seeds=["10.0.0.1", "10.0.0.2", "10.0.0.4"])
    assert plan["add"] == [] and plan["remove"] == [] and plan["problems"] == []
    assert cassandra_topology_steps(plan, hosts) == [
        {"node": "seeds", "text": "10.0.0.1,10.0.0.2,10.0.0.3 -> 10.0.0.1,10.0.0.2,10.0.0.4"}]
    # nothing added nor removed: the seed change alone, warned about, under --check too
    for check in (False, True):
        lines = cassandra_topology_screen(plan, hosts, ring=r, check=check)
        assert lines[0] == "PLAN  topology  1 step, one node at a time"
        assert warnings_of(lines) == ["WARNING  the seeds will change on every node: 10.0.0.1,10.0.0.2,10.0.0.3 ->"
                                      " 10.0.0.1,10.0.0.2,10.0.0.4 (from cassandra_seeds in the inventory)"]
    # dc2 left with no seed: refused
    plan = cassandra_topology_plan(hosts, r, seeds=["10.0.0.1", "10.0.0.2"])
    assert plan["problems"] == ["dc2 would be left with no seed (node3 leaves the seed list): put a node of dc2 in"
                                " cassandra_seeds."]
    # nothing given (an old caller): no seed step
    assert cassandra_topology_plan(hosts, r)["seeds"]["step"] is False


def test_seed_warning_only_when_the_seeds_change():
    # a node added, one removed, cassandra_seeds as the nodes run it: no seed step, no warning
    hosts = [seeded(1), seeded(2), seeded(3), seeded(4, absent=True, state="normal"),
             seeded(5, live="", state="new", rack="r2")]
    r = ring(entry(1), entry(2), entry(3), entry(4))
    plan = cassandra_topology_plan(hosts, r, seeds="10.0.0.1,10.0.0.2")
    assert plan["problems"] == [] and plan["add"] == ["node5"] and plan["remove"] == ["node4"]
    assert not plan["seeds"]["step"]
    assert not [w for w in warnings_of(cassandra_topology_screen(plan, hosts, ring=r)) if "seed" in w]
    # the same seeds written another way on one node: written again there, said so (no seed change)
    hosts[2]["live"] = "node1,node2"
    plan = cassandra_topology_plan(hosts, r, seeds="10.0.0.1,10.0.0.2")
    lines = cassandra_topology_screen(plan, hosts, ring=r)
    assert not [w for w in warnings_of(lines) if "seed" in w]
    assert "seeds:      written again on node3, the same seeds: 10.0.0.1,10.0.0.2" in lines


def test_steps_count_and_group_the_nodes():
    hosts = [host(1), host(2, rack="r2"), host(3, absent=True), host(4, absent=True, rack="r2"),
             host(5, state="new"), host(6, state="new", rack="r2")]
    plan = cassandra_topology_plan(hosts, ring(entry(1), entry(2, rack="r2"), entry(3), entry(4, rack="r2")))
    assert plan["problems"] == []
    assert [(s["node"], s["dc"], s["rack"]) for s in cassandra_topology_steps(plan, hosts)] == [
        ("add node5", "dc1", "r1"), ("add node6", "dc1", "r2"),
        ("decommission node3", "dc1", "r1"), ("decommission node4", "dc1", "r2")]


def test_a_datacenter_removed_whole_needs_no_seed():
    # dc2 (node3 its seed, node4) marked absent and out of cassandra_seeds: removed, no seed needed there
    hosts = [seeded(1, seed=True, live="10.0.0.1,10.0.0.3"), seeded(2, live="10.0.0.1,10.0.0.3"),
             seeded(3, absent=True, dc="dc2", state="normal", live="10.0.0.1,10.0.0.3"),
             seeded(4, absent=True, dc="dc2", state="normal", live="10.0.0.1,10.0.0.3")]
    r = {"dc1": {"nodes": [entry(1), entry(2)]}, "dc2": {"nodes": [entry(3), entry(4)]}}
    plan = cassandra_topology_plan(hosts, r, seeds=["10.0.0.1"])
    assert plan["problems"] == [] and plan["remove"] == ["node3", "node4"]
    assert plan["nodes_left"] == {"dc1": 2, "dc2": 0}
    lines = cassandra_topology_screen(plan, hosts, ring=r)
    assert "dc2 after:  no node left" in lines
    assert lines[3].endswith("decommission node4  dc2/r1  load 40.1 GiB, owns 25.0% -> none (no node left in dc2)")
    assert "WARNING  dc2 removed whole (node3, node4): no node left there" in lines
    # node4 stays in dc2: dc2 still in use, a seed needed there
    hosts[3]["absent"] = False
    plan = cassandra_topology_plan(hosts, r, seeds=["10.0.0.1"])
    assert plan["problems"] == ["dc2 would be left with no seed (node3 leaves the seed list): put a node of dc2 in"
                                " cassandra_seeds."]


def test_screen_big_datacenter_counted_once_and_one_node_left():
    hosts = [host(i) for i in range(1, 9)] + [host(9, state="new")]
    r = ring(*[entry(i) for i in range(1, 9)])
    lines = cassandra_topology_screen(cassandra_topology_plan(hosts, r), hosts, ring=r)
    assert "dc1 after:  9 nodes: node1..node9" in lines
    # 1 of 1: the datacenter removed whole
    hosts = [host(1), host(2), host(3, dc="dc2", absent=True)]
    r = {"dc1": {"nodes": [entry(1), entry(2)]}, "dc2": {"nodes": [entry(3)]}}
    assert cassandra_topology_plan(hosts, r)["large_removals"] == ["dc2 removed whole (node3): no node left there"]


def test_screen_notes_one_line_each_once():
    hosts = [host(1), host(2), host(3),
             host(4, state="new", info=["java: installed", "port 9042 reached"], checks=["could not list the listening ports (ss)"]),
             host(5, state="new", info=["java: installed"], checks=["could not list the listening ports (ss)"])]
    r = ring(entry(1), entry(2), entry(3))
    notes = ["WARNING  seeds: dc1 has one seed", "WARNING  seeds: dc1 has one seed", "cassandra_foo is not read"]
    lines = cassandra_topology_screen(cassandra_topology_plan(hosts, r), hosts, ring=r, notes=notes, confirm=False)
    assert "NOTE  node4, node5: java: installed" in lines and "NOTE  node4: port 9042 reached" in lines
    assert "NOTE  cassandra_foo is not read" in lines
    assert warnings_of(lines) == ["WARNING  seeds: dc1 has one seed",
                                  "WARNING  node4, node5: could not list the listening ports (ss)"]
    # NOTE lines with the facts, before the WARNING block; every line plain text
    assert lines.index("NOTE  node4, node5: java: installed") < lines.index(warnings_of(lines)[0])
    assert all("\n" not in line for line in lines)
