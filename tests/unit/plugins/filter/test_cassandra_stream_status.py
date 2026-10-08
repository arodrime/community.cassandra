from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Where a streaming wait stands after a check (stream_check.yml), for each
# kind of wait: a join (add_node, replace_node, topology), a decommission, an
# async job (decommission, removenode), a removal resumed, and the stops.

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import (
    cassandra_ring_seen, cassandra_stream_own_error, cassandra_stream_status, cassandra_stream_waiting)

BANNER = "Picked up JAVA_TOOL_OPTIONS: -Dcom.sun.jndi.rmiURLParsing=legacy"
AUTH = ("nodetool: Failed to connect to '127.0.0.1:7199' - SecurityException: "
        "'Authentication failed! Invalid username or password'.")
REFUSED = "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused (Connection refused)'."
NO_MBEAN = "error: org.apache.cassandra.db:type=StorageService\n-- StackTrace --\njavax.management.InstanceNotFoundException"
GOING = {"now": 100, "start": 0, "stalled": False}


def failed(stderr):
    return {"failed": True, "msg": "netstats command failed", "stderr": stderr, "item": "node5"}


def ring(address, status="U", state="N"):
    return {"cluster_status": {"dc1": {"nodes": [{"address": "10.100.100.1", "status": "U", "state": "N"},
                                                 {"address": address, "status": status, "state": state}]}}}


@pytest.mark.parametrize("own, seen, expected", [
    ({"mode": "NORMAL"}, "", "done"),
    ({"mode": "JOINING"}, "", "going"),
    ({"mode": "JOINING_FAILED"}, "", "join_failed"),
    # its own nodetool can't answer: the other nodes tell
    (failed(BANNER + "\n" + AUTH), "UN", "done"),
    (failed(BANNER + "\n" + AUTH), "UJ", "going"),
    (failed(BANNER + "\n" + AUTH), "", "going"),
    (failed(NO_MBEAN), "UN", "done"),
    ({"unreachable": True, "msg": "Failed to connect to the host via ssh"}, "UN", "done"),
    # ssh's own "Connection refused" (a reboot, sshd restarting): a check without an answer, not a stop
    ({"unreachable": True, "msg": "Failed to connect to the host via ssh: ssh: connect to host node5 port 22:"
                                  " Connection refused"}, "UJ", "going"),
    ({"unreachable": True, "msg": "Failed to connect to the host via ssh: ssh: connect to host node5 port 22:"
                                  " Connection refused"}, "UN", "done"),
    # stopped: never done, even if a peer still shows it UN (gossip not caught up)
    (failed(BANNER + "\n" + REFUSED), "UN", "stopped"),
    (failed(REFUSED), "", "stopped"),
])
def test_join(own, seen, expected):
    assert cassandra_stream_status(GOING, join=True, own=own, seen=seen) == expected


def test_join_not_done_by_the_peers_while_its_own_mode_answers():
    # the node tells its own mode: the peers' view does not end it early
    assert cassandra_stream_status(GOING, join=True, own={"mode": "JOINING"}, seen="UN") == "going"


@pytest.mark.parametrize("own, job, expected", [
    ({"mode": "DECOMMISSIONED"}, None, "done"),
    ({"mode": "LEAVING"}, None, "going"),
    ({"mode": "DECOMMISSION_FAILED"}, None, "leave_failed"),
    (failed(REFUSED), None, "stopped"),
    # the job changed something: over; a job that changed nothing (LEAVING already): its mode tells
    ({"mode": "LEAVING"}, {"finished": 1, "changed": True}, "done"),
    ({"mode": "LEAVING"}, {"finished": 1, "changed": False}, "going"),
    ({"mode": "LEAVING"}, {"finished": 1, "failed": True}, "job_failed"),
    ({"mode": "LEAVING"}, {"failed": True, "msg": "could not find job"}, "job_lost"),
    ({"mode": "LEAVING"}, {"finished": 0}, "going"),
])
def test_decommission(own, job, expected):
    assert cassandra_stream_status(GOING, leave=True, own=own, job=job) == expected


@pytest.mark.parametrize("job, expected", [
    ({"finished": 1, "changed": True}, "done"),
    ({"finished": 1, "changed": False}, "done"),
    ({"finished": 0}, "going"),
    ({"finished": 1, "failed": True}, "job_failed"),
])
def test_job(job, expected):
    # removenode, move, rebuild: the job's end is the end
    assert cassandra_stream_status(GOING, job=job) == expected


@pytest.mark.parametrize("removal, expected", [
    ({"rc": 0, "stdout": "RemovalStatus: No removals in process."}, "done"),
    ({"rc": 0, "stdout": "RemovalStatus: Removing token (-1234). Waiting for replication confirmation"}, "going"),
    ({"rc": 1, "stdout": ""}, "going"),
])
def test_removal(removal, expected):
    assert cassandra_stream_status(GOING, removal=removal) == expected


def test_stalled_and_too_long():
    assert cassandra_stream_status(dict(GOING, stalled=True), join=True, own={"mode": "JOINING"}) == "stalled"
    assert cassandra_stream_status(GOING, join=True, own={"mode": "JOINING"}, max_time=100) == "too_long"
    assert cassandra_stream_status(GOING, join=True, own={"mode": "JOINING"}, max_time=101) == "going"
    # done wins over stalled (the last check saw it over)
    assert cassandra_stream_status(dict(GOING, stalled=True), join=True, own={"mode": "NORMAL"}) == "done"


def test_own_error_skips_the_jvm_banner():
    assert cassandra_stream_own_error(failed(BANNER + "\n" + AUTH)) == AUTH
    assert cassandra_stream_own_error({"mode": "JOINING", "stderr": BANNER}) == ""
    assert cassandra_stream_own_error({}) == ""
    assert cassandra_stream_own_error({"failed": True, "stderr": BANNER, "msg": "netstats command failed"}) == \
        "netstats command failed"


def test_ring_seen():
    assert cassandra_ring_seen([ring("10.100.100.5", "U", "J")], "10.100.100.5") == "UJ"
    assert cassandra_ring_seen([{"skipped": True}, ring("10.100.100.5:7000")], "10.100.100.5") == "UN"
    assert cassandra_ring_seen([ring("10.100.100.6")], "10.100.100.5") == ""
    assert cassandra_ring_seen([], "10.100.100.5") == ""
    assert cassandra_ring_seen([{"failed": True, "msg": "x"}], "10.100.100.5") == ""
    # nodetool prints IPv6 in full
    assert cassandra_ring_seen([ring("2001:db8:0:0:0:0:0:7")], "2001:db8::7") == "UN"
    assert cassandra_ring_seen([ring("[2001:db8:0:0:0:0:0:7]:7000")], "2001:db8::7") == "UN"


def test_waiting_says_why():
    line = cassandra_stream_waiting(failed(BANNER + "\n" + AUTH), "UJ", node="node5", join=True)
    assert line == ["      node5: its own nodetool does not answer (%s): done once the other nodes see it UN"
                    " (they see it UJ)" % AUTH]
    assert "not in their nodetool status yet" in cassandra_stream_waiting(failed(AUTH), "", node="node5", join=True)[0]
    # nothing to say when it answers, is stopped, or for another wait than a join
    assert cassandra_stream_waiting({"mode": "JOINING"}, "UJ", node="node5", join=True) == []
    assert cassandra_stream_waiting(failed(REFUSED), "", node="node5", join=True) == []
    assert cassandra_stream_waiting(failed(AUTH), "", node="node5", join=False) == []


def test_unreachable_is_not_stopped_for_a_decommission():
    own = {"unreachable": True, "msg": "Failed to connect to the host via ssh: ssh: connect to host n port 22: Connection refused"}
    assert cassandra_stream_status(GOING, leave=True, own=own) == "going"
