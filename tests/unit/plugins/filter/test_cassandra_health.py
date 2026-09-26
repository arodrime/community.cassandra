from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import cassandra_health_problems

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
