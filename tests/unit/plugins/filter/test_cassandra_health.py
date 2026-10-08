from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import (
    cassandra_health_findings, cassandra_health_problems, cassandra_health_report, cassandra_leaving_state,
    cassandra_removal_force_target, cassandra_removal_state)

UP = {"is_up": True}
IDLE = {"mode": "NORMAL", "streaming": False}
AGREED = {"failed": False}


def node(address, status="U", state="N", rack="r1"):
    return {"address": address, "status": status, "state": state, "rack": rack}


def view(host, *nodes):
    return {"from": host, "result": {"cluster_status": {"dc1": {"nodes": list(nodes)}}}}


def problems(views, expected=3, **kwargs):
    checks = dict(gossip=UP, binary=UP, netstats=IDLE, schema=AGREED)
    checks.update(kwargs)
    return cassandra_health_problems(views, expected, "n1", **checks)


def test_healthy():
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3")]
    assert problems([view("n1", *ring), view("n2", *ring)]) == []


def test_down_and_joining_nodes_named_with_the_view():
    out = problems([view("n1", node("10.0.0.1"), node("10.0.0.2", status="D"), node("10.0.0.3", state="J"))])
    assert "10.0.0.2 (r1) is DN, seen from n1" in out
    assert "10.0.0.3 (r1) is UJ, seen from n1" in out


def test_node_missing_from_the_ring():
    out = problems([view("n2", node("10.0.0.1"), node("10.0.0.2"))])
    assert len(out) == 1
    assert out[0].startswith("the ring has 2 nodes, the inventory 3 (seen from n2)")


def test_view_that_failed():
    out = problems([{"from": "n2", "result": {"msg": "nodetool error: connection refused"}}], expected=1)
    assert out == ["nodetool status failed on n2: nodetool error: connection refused"]


def test_view_that_failed_shows_nodetool_stderr():
    result = {"msg": "Unable to determine Cassandra version: ", "stderr": "nodetool: Failed to connect to '127.0.0.1:7199'\n"}
    out = problems([{"from": "n2", "result": result}], expected=1)
    assert out == ["nodetool status failed on n2: Unable to determine Cassandra version:  (nodetool: Failed to connect to '127.0.0.1:7199')"]


def test_nodetool_error_during_poll():
    # cassandra_status sets cluster_status to None when nodetool itself fails
    out = problems([{"from": "n2", "result": {"cluster_status": None, "msg": "nodetool error: boom"}}], expected=1)
    assert out == ["nodetool status failed on n2: nodetool error: boom"]


def test_ports_not_answering():
    ports = {"results": [
        {"item": {"name": "storage", "host": "10.0.0.1", "port": 7000}, "failed": False},
        {"item": {"name": "CQL", "host": "10.0.0.1", "port": 9042}, "failed": True},
    ]}
    out = problems([view("n1", node("10.0.0.1"))], expected=1, ports=ports)
    assert out == ["CQL port 9042 is not answering on n1 (10.0.0.1)"]


def test_node_level_checks():
    ring = [node("10.0.0.1")]
    out = problems([view("n1", *ring)], expected=1,
                   gossip={"is_up": False}, binary={}, schema={"failed": True, "msg": "2 schema versions"},
                   netstats={"mode": "NORMAL", "streaming": True})
    assert out == [
        "gossip is not running on n1",
        "the native transport (CQL) is not running on n1",
        "streams in progress on n1 (nodetool netstats)",
        "schema disagreement: 2 schema versions",
    ]


def test_netstats_failure_is_not_reported_as_streams():
    out = problems([view("n1", node("10.0.0.1"))], expected=1,
                   netstats={"failed": True, "msg": "netstats command failed", "stderr": "connection refused\n"})
    assert out == ["nodetool netstats failed on n1: netstats command failed (connection refused)"]


def test_expected_down_node_is_not_a_problem():
    ring = [node("10.0.0.1"), node("10.0.0.2", status="D")]
    assert problems([view("n1", *ring)], expected=2, down_ok=["10.0.0.2"]) == []
    assert problems([view("n1", *ring)], expected=2, down_ok=["10.0.0.9"]) == ["10.0.0.2 (r1) is DN, seen from n1"]


def test_expected_joining_node_is_not_a_problem():
    ring = [node("10.0.0.1"), node("10.0.0.2", state="J")]
    assert problems([view("n1", *ring)], expected=2, joining_ok=["10.0.0.2"]) == []
    assert problems([view("n1", *ring)], expected=2, joining_ok=["10.0.0.9"]) == ["10.0.0.2 (r1) is UJ, seen from n1"]
    # only joining: the same node down or leaving is still a problem
    for status, state in (("D", "J"), ("U", "L"), ("D", "N")):
        down = [node("10.0.0.1"), node("10.0.0.2", status=status, state=state)]
        assert problems([view("n1", *down)], expected=2, joining_ok=["10.0.0.2"]) == [
            "10.0.0.2 (r1) is %s%s, seen from n1" % (status, state)]


def test_leaving_node_expected_by_the_caller():
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3", state="L")]
    assert problems([view("n2", *ring)], leaving_ok=["10.0.0.3"]) == []
    # only UL: down while leaving is still a problem
    ring[2] = node("10.0.0.3", status="D", state="L")
    assert problems([view("n2", *ring)], leaving_ok=["10.0.0.3"]) == ["10.0.0.3 (r1) is DL, seen from n2"]
    # another node leaving is not
    ring[2] = node("10.0.0.3", state="L")
    assert problems([view("n2", *ring)], leaving_ok=["10.0.0.2"]) == ["10.0.0.3 (r1) is UL, seen from n2"]


def ring_result(*nodes):
    return {"cluster_status": {"dc1": {"nodes": list(nodes)}}}


FULL = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3", state="L"))
GONE = ring_result(node("10.0.0.1"), node("10.0.0.2"))


def leaving(mode, ring, **netstats):
    netstats.update({"mode": mode} if mode else {})
    return cassandra_leaving_state(netstats, ring, "10.0.0.3")


def test_leaving_state_normal_node():
    ring = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3"))
    assert leaving("NORMAL", ring) == {"state": "normal", "in_ring": True, "reason": ""}


def test_leaving_state_decommission_in_progress():
    assert leaving("LEAVING", FULL) == {"state": "leaving", "in_ring": True, "reason": ""}


def test_leaving_state_announcing_it_left():
    # streams over, LEFT announced: gone from the others' ring, mode still LEAVING for ~30s
    assert leaving("LEAVING", GONE) == {"state": "leaving", "in_ring": False, "reason": ""}


def test_leaving_state_decommissioned():
    assert leaving("DECOMMISSIONED", GONE) == {"state": "decommissioned", "in_ring": False, "reason": ""}


def test_leaving_state_decommissioned_but_still_in_the_ring():
    out = leaving("DECOMMISSIONED", FULL)
    assert out["state"] == "failed"
    assert "still UL in the ring" in out["reason"]


def test_leaving_state_decommission_failed():
    out = leaving("DECOMMISSION_FAILED", FULL)
    assert out["state"] == "failed"
    assert "DECOMMISSION_FAILED" in out["reason"]
    assert "nodetool decommission on it resumes it" in out["reason"]


def test_leaving_state_not_running_and_gone():
    # decommissioned, then stopped (by hand, or a run that got that far): only stop and disable are left
    out = leaving(None, GONE, failed=True, msg="nodetool error", stderr="Connection refused")
    assert out == {"state": "decommissioned", "in_ring": False,
                   "reason": "not in the ring and its nodetool does not answer (nodetool error (Connection refused))"}


def test_leaving_state_not_answering_for_another_reason():
    # JMX refusing the login, or a node down under another address: never taken as decommissioned
    assert leaving(None, GONE, failed=True, msg="nodetool error", stderr="Authentication failed")["state"] == "normal"
    ring = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.9", status="D"))
    assert leaving(None, ring, failed=True, msg="nodetool error", stderr="Connection refused")["state"] == "normal"


def test_leaving_state_left_to_the_health_check():
    # not answering but in the ring (down), or no ring to compare with: the health check reports it
    assert leaving(None, FULL, failed=True, msg="boom")["state"] == "normal"
    assert leaving("LEAVING", {"msg": "nodetool status failed"})["state"] == "normal"
    assert leaving("LEAVING", {"cluster_status": None})["state"] == "normal"
    # a NORMAL node the ring does not know (wrong address): the ring count check reports it
    assert leaving("NORMAL", GONE) == {"state": "normal", "in_ring": False, "reason": ""}


REMOVING = "RemovalStatus: Removing token (-42). Waiting for replication confirmation from [/10.0.0.2]."
IDLE_REMOVAL = "RemovalStatus: No token removals in process."


# nodetool ring (5.0.7): the rows of a node start with its address and end with one of its tokens
RING_TOKENS = """
Datacenter: dc1
==========
Address   Rack  Status State   Load       Owns    Token
                                                  9000
10.0.0.4  r1    Down   Leaving 1.02 MiB   ?       -42
10.0.0.1  r1    Up     Normal  1.01 MiB   ?       -7
10.0.0.40 r1    Up     Normal  1.01 MiB   ?       5
10.0.0.4  r1    Down   Leaving 1.02 MiB   ?       9000

  Warning: "nodetool ring" is used to output all the tokens of a node.
"""
REMOVING_OTHER = "RemovalStatus: Removing token (5). Waiting for replication confirmation from [/10.0.0.2]."


def removal(ring, **status):
    return cassandra_removal_state(ring, "10.0.0.4", status, tokens=RING_TOKENS)


def state(state, on="", reason=""):
    return {"state": state, "on": on, "reason": reason}


def test_removal_state():
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    dn = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D"))
    assert removal(dn, n1=IDLE_REMOVAL, n2=IDLE_REMOVAL) == state("start")
    assert removal(dn, n1="", n2="") == state("start")
    assert removal(dl, n1=REMOVING, n2=IDLE_REMOVAL) == state("resume", "n1")
    assert removal(GONE, n1=IDLE_REMOVAL) == state("absent")
    assert removal({"cluster_status": None}, n1=IDLE_REMOVAL) == state("unknown")


def test_removal_found_on_any_node():
    # the first node is idle, the removal runs from another one: waited for there, not started again
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    assert removal(dl, n1=IDLE_REMOVAL, n2=REMOVING) == state("resume", "n2")


def test_removal_dl_with_no_coordinator_in_the_run():
    # DL (gossip) but no node of the run removing it: coordinated from elsewhere, or its
    # coordinator restarted (or it died decommissioning): never a second removenode by itself
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    assert removal(dl, n1=IDLE_REMOVAL, n2=IDLE_REMOVAL) == state("orphan")
    assert removal(dl, n1=IDLE_REMOVAL, n2="") == state("orphan")


# nodetool gossipinfo on a node that does not coordinate the removal (5.0.7): it shows the node DN in its status
GOSSIP_REMOVING = """/10.0.0.1
  generation:1790684482
  STATUS_WITH_PORT:76:NORMAL,-1112067028005469869
/10.0.0.4
  generation:1790685004
  heartbeat:2147483647
  HOST_ID:3:93a85637-b80c-4d14-9d9e-f041a120f498
  STATUS_WITH_PORT:588:removing,93a85637-b80c-4d14-9d9e-f041a120f498
  INTERNAL_ADDRESS_AND_PORT:9:10.0.0.4:7000
  REMOVAL_COORDINATOR:590:REMOVER,04d1969e-5b13-433d-a198-cf9e5288a55c
/10.0.0.40
  STATUS:12:NORMAL,42
"""


def test_removal_seen_from_a_node_that_does_not_coordinate_it():
    # the first node shows the dead node DN (only the coordinator shows DL): its gossip says "removing"
    dn = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D"))
    status = {"n1": IDLE_REMOVAL, "n2": REMOVING}
    assert cassandra_removal_state(dn, "10.0.0.4", status, gossip=GOSSIP_REMOVING, tokens=RING_TOKENS) == state("resume", "n2")
    # coordinated from outside the run (or its coordinator restarted): not started again
    idle = {"n1": IDLE_REMOVAL, "n2": IDLE_REMOVAL}
    assert cassandra_removal_state(dn, "10.0.0.4", idle, gossip=GOSSIP_REMOVING) == state("orphan")
    # gossip of other nodes only (10.0.0.40 is not 10.0.0.4), or with the port in the header
    assert cassandra_removal_state(dn, "10.0.0.40", idle, gossip=GOSSIP_REMOVING)["state"] == "absent"
    other = ring_result(node("10.0.0.1"), node("10.0.0.40", status="D"))
    assert cassandra_removal_state(other, "10.0.0.40", idle, gossip=GOSSIP_REMOVING)["state"] == "start"
    for header in ("/10.0.0.4:7000", "node4.example/10.0.0.4"):
        gossip = GOSSIP_REMOVING.replace("/10.0.0.4\n", header + "\n")
        assert cassandra_removal_state(dn, "10.0.0.4", idle, gossip=gossip)["state"] == "orphan"
    v6 = ring_result(node("10.0.0.1"), node("0:0:0:0:0:0:0:4", status="D"))
    for header in ("/[0:0:0:0:0:0:0:4]:7000", "/0:0:0:0:0:0:0:4"):
        gossip = GOSSIP_REMOVING.replace("/10.0.0.4\n", header + "\n")
        assert cassandra_removal_state(v6, "0:0:0:0:0:0:0:4", idle, gossip=gossip)["state"] == "orphan"
    # removed already (no more "removing"): not a removal in progress
    removed = GOSSIP_REMOVING.replace("removing,", "removed,")
    assert cassandra_removal_state(dn, "10.0.0.4", idle, gossip=removed)["state"] == "start"


def test_removal_state_busy_with_another_node():
    # a node removes another node (its token is not one of 10.0.0.4's): never waited for as this one's
    dn = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D"))
    assert removal(dn, n1=IDLE_REMOVAL, n2=REMOVING_OTHER) == state(
        "busy", "n2", "n2 is removing another node (token 5, not one of 10.0.0.4's)")
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    assert removal(dl, n1=REMOVING_OTHER, n2=REMOVING_OTHER)["reason"] == \
        "n1, n2 are removing another node (token 5, not one of 10.0.0.4's)"
    # this one's removal on n1, another on n2: not one removal to wait for
    assert removal(dl, n1=REMOVING, n2=REMOVING_OTHER) == state(
        "busy", "n1, n2", "n2 is removing another node (token 5, not one of 10.0.0.4's)")
    # its tokens unknown (nodetool ring failed): can't tell whose removal it is
    out = cassandra_removal_state(dl, "10.0.0.4", {"n1": REMOVING}, tokens="")
    assert out == state("busy", "n1", "n1 is removing a node, and nodetool ring did not list 10.0.0.4's tokens to tell which")
    # this one's removal, but other nodes are leaving too
    two = ring_result(node("10.0.0.1"), node("10.0.0.5", status="D", state="L"), node("10.0.0.4", status="D", state="L"))
    assert removal(two, n1=REMOVING) == state("busy", "n1", "n1 is removing 10.0.0.4, but other nodes are leaving too (10.0.0.5)")
    # two coordinators of this one
    assert removal(dl, n1=REMOVING, n2=REMOVING) == state("busy", "n1, n2", "n1, n2 are all removing 10.0.0.4")


def test_removal_state_token_of_this_node():
    # any of its tokens (the coordinator prints the first one it has), its address with a port or brackets
    dl = ring_result(node("10.0.0.1"), node("10.0.0.4", status="D", state="L"))
    other_token = REMOVING.replace("(-42)", "(9000)")
    assert removal(dl, n1=other_token) == state("resume", "n1")
    # the lone token line above the rows and the warning are not rows; 10.0.0.40 is not 10.0.0.4
    assert removal(dl, n1=REMOVING.replace("(-42)", "(-7)"))["state"] == "busy"
    with_port = RING_TOKENS.replace("10.0.0.4  ", "10.0.0.4:7000  ")
    assert cassandra_removal_state(dl, "10.0.0.4", {"n1": REMOVING}, tokens=with_port) == state("resume", "n1")
    v6 = ring_result(node("10.0.0.1"), node("0:0:0:0:0:0:0:4", status="D", state="L"))
    ring6 = RING_TOKENS.replace("10.0.0.4  ", "[0:0:0:0:0:0:0:4]:7000  ")
    assert cassandra_removal_state(v6, "0:0:0:0:0:0:0:4", {"n1": REMOVING}, tokens=ring6) == state("resume", "n1")


def force_ring(*nodes):
    return {"cluster_status": {"dc1": {"nodes": list(nodes)}}}


FORCE_DN = force_ring(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D"))
FORCE_DL = force_ring(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))


def test_force_runs_on_the_coordinator():
    # n2 coordinates the removal (only it shows the node DL): forced there, not on the first node
    rings = {"n1": FORCE_DN, "n2": FORCE_DL}
    assert cassandra_removal_force_target(rings, "10.0.0.4", state("resume", "n2")) == {"on": "n2", "reason": ""}


def test_force_without_a_coordinator_on_a_node_showing_it_dl():
    # it died decommissioning (DL on every node), or a node still has it DL: the first of those
    rings = {"n1": FORCE_DN, "n2": FORCE_DL, "n3": FORCE_DL}
    assert cassandra_removal_force_target(rings, "10.0.0.4", state("orphan")) == {"on": "n2", "reason": ""}


def test_force_refused():
    busy = state("busy", "n2", "n2 is removing another node (token 5, not one of 10.0.0.4's)")
    out = cassandra_removal_force_target({"n1": FORCE_DL}, "10.0.0.4", busy)
    assert out["on"] == "" and out["reason"].startswith(busy["reason"] + ": removenode force would finish that too")
    # the coordinator also shows another node leaving: forcing there would finish it too
    leaving = force_ring(node("10.0.0.1"), node("10.0.0.3", state="L"), node("10.0.0.4", status="D", state="L"))
    out = cassandra_removal_force_target({"n1": FORCE_DN, "n2": leaving}, "10.0.0.4", state("resume", "n2"))
    assert out["on"] == "" and out["reason"].startswith("Other nodes are leaving as n2 sees them (10.0.0.3)")
    # the coordinator's ring not readable
    out = cassandra_removal_force_target({"n1": FORCE_DN, "n2": {"msg": "boom"}}, "10.0.0.4", state("resume", "n2"))
    assert out["reason"] == "n2 is removing 10.0.0.4, but its ring could not be read (nodetool status): run this again."
    # a live node leaving: not a removal
    ul = force_ring(node("10.0.0.1"), node("10.0.0.4", state="L"))
    assert "is UL: a live node leaving" in cassandra_removal_force_target({"n1": ul}, "10.0.0.4", state("start"))["reason"]
    # being removed (gossip) with no coordinator nor DL in the run: says where to go, no dead end
    out = cassandra_removal_force_target({"n1": FORCE_DN, "n2": FORCE_DN}, "10.0.0.4", state("orphan"))
    assert "no node of this run is removing it or shows it DL" in out["reason"]
    assert "cassandra_dead_node_new_removal=true" in out["reason"]
    # no removal at all
    out = cassandra_removal_force_target({"n1": FORCE_DN}, "10.0.0.4", state("start"))
    assert out["reason"] == "10.0.0.4 is DN: no removal of it is in progress, run removenode first (the default method)."


def findings(host, views, expected=3, **kwargs):
    checks = dict(gossip=UP, binary=UP, netstats=IDLE, schema=AGREED)
    checks.update(kwargs)
    return cassandra_health_findings(views, expected, host, **checks)


NAMES = {"10.0.0.1": "n1", "10.0.0.2": "n2", "10.0.0.3": "n3"}


def test_findings_carry_the_sentences():
    ring = [node("10.0.0.1"), node("10.0.0.2", status="D")]
    found = findings("n1", [view("n1", *ring)])
    assert [f["text"] for f in found] == problems([view("n1", *ring)])
    assert found[0] == {"kind": "state", "address": "10.0.0.2", "rack": "r1", "state": "DN", "seen_from": "n1",
                        "text": "10.0.0.2 (r1) is DN, seen from n1"}
    assert found[1]["kind"] == "count" and found[1]["ring"] == 2


def test_report_healthy():
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3")]
    report = cassandra_health_report(dict((h, findings(h, [view(h, *ring)])) for h in ("n1", "n2", "n3")),
                                     "my_cluster", ["n1", "n2", "n3"], names=NAMES, members=3)
    assert report == {"healthy": True, "problems": 0,
                      "lines": ["HEALTHY  my_cluster  3 nodes  3 UN, schema agreed, no streams, ports open"]}


def test_report_each_problem_once_with_who_saw_it():
    ring = [node("10.0.0.1"), node("10.0.0.2", status="D", rack="r2"), node("10.0.0.3"), node("10.0.0.7"),
            node("10.0.0.8")]
    per_host = {}
    for host, address in (("n1", "10.0.0.1"), ("n3", "10.0.0.3")):
        cql = {"results": [{"item": {"name": "CQL", "host": address, "port": 9042}, "failed": True}]}
        per_host[host] = findings(host, [view(host, *ring), view("n1" if host != "n1" else "n3", *ring)],
                                  ports=cql, netstats={"mode": "NORMAL", "streaming": host == "n3"})
    report = cassandra_health_report(per_host, "my_cluster", ["n1", "n2", "n3"], unreachable=["n2"],
                                     absent=["n7", "n8"], names=NAMES,
                                     topology_command="ansible-playbook community.cassandra.topology")
    assert report["healthy"] is False and report["problems"] == 5
    assert report["lines"] == [
        "NOT HEALTHY  my_cluster  2 nodes checked, 5 problems",
        "  ring:     n2 10.0.0.2 (r2) DN   seen from n1, n3",
        "  ring:     5 members, inventory 3   seen from n1, n3",
        "  ports:    CQL 9042 not answering on n1, n3",
        "  streams:  in progress on n3",
        "  ssh:      not reached by Ansible: n2",
        "  not counted, marked cassandra_node_state: absent: n7, n8",
        "TO DO",
        "  1. start Cassandra on n2 (its server first if it is down); a node that can't be recovered: replace_node",
        "  2. remove n7, n8 from the ring:",
        "     ansible-playbook community.cassandra.topology",
        "  3. reach n2 over SSH, then run this again",
        "  4. wait for the streams to end (nodetool netstats), then run this again",
    ]


def test_report_node_level_failures():
    per_host = {"n1": findings("n1", [{"from": "n1", "result": {"msg": "nodetool error: boom"}}], expected=1,
                               gossip={"is_up": False}, binary={}, schema={"failed": True, "msg": "2 schema versions"},
                               netstats={"failed": True, "msg": "refused"})}
    lines = cassandra_health_report(per_host, "c", ["n1"])["lines"]
    assert lines == ["NOT HEALTHY  c  1 node checked, 5 problems",
                     "  nodetool:  status failed on n1: nodetool error: boom",
                     "  gossip:    not running on n1",
                     "  CQL:       native transport not running on n1",
                     "  netstats:  failed (refused) on n1",
                     "  schema:    disagreement: 2 schema versions"]
