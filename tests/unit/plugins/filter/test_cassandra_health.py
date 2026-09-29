from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import (
    cassandra_health_problems, cassandra_leaving_state, cassandra_removal_state)

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


def removal(ring, **status):
    return cassandra_removal_state(ring, "10.0.0.4", status)


def test_removal_state():
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    dn = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D"))
    assert removal(dn, n1=IDLE_REMOVAL, n2=IDLE_REMOVAL) == {"state": "start", "on": ""}
    assert removal(dn, n1="", n2="") == {"state": "start", "on": ""}
    assert removal(dl, n1=REMOVING, n2=IDLE_REMOVAL) == {"state": "resume", "on": "n1"}
    assert removal(GONE, n1=IDLE_REMOVAL) == {"state": "absent", "on": ""}
    assert removal({"cluster_status": None}, n1=IDLE_REMOVAL) == {"state": "unknown", "on": ""}


def test_removal_found_on_any_node():
    # the first node is idle, the removal runs from another one: waited for there, not started again
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    assert removal(dl, n1=IDLE_REMOVAL, n2=REMOVING) == {"state": "resume", "on": "n2"}


def test_removal_dl_with_no_coordinator_in_the_run():
    # DL (gossip) but no node of the run removing it: coordinated from elsewhere, or its
    # coordinator restarted (or it died decommissioning): never a second removenode by itself
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    assert removal(dl, n1=IDLE_REMOVAL, n2=IDLE_REMOVAL) == {"state": "orphan", "on": ""}
    assert removal(dl, n1=IDLE_REMOVAL, n2="") == {"state": "orphan", "on": ""}


def test_removal_state_busy_with_another_node():
    # a node removes something while this node is only DN: another node
    dn = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D"))
    assert removal(dn, n1=IDLE_REMOVAL, n2=REMOVING) == {"state": "busy", "on": "n2"}
    # two nodes leaving: the removal in progress may be the other one's
    two = ring_result(node("10.0.0.1"), node("10.0.0.5", status="D", state="L"), node("10.0.0.4", status="D", state="L"))
    assert removal(two, n1=REMOVING) == {"state": "busy", "on": "n1"}
    # two coordinators: not one removal to wait for
    dl = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))
    assert removal(dl, n1=REMOVING, n2=REMOVING) == {"state": "busy", "on": "n1, n2"}
