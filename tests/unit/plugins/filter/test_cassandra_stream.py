from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import (
    cassandra_add_node_plan, cassandra_cleanup_view, cassandra_compactionstats, cassandra_stream_progress)

GIB = 1024 ** 3


def session(peer, done, total, op="Bootstrap", plan="p1", direction="receiving", files=None):
    return {"operation": op, "plan_id": plan, "peer": peer, "direction": direction, "files_total": 10,
            "files_done": done * 10 // total if total else 0, "bytes_total": total, "bytes_done": done,
            "files": files or []}


def read(host, *sessions, **kwargs):
    result = {"item": host, "mode": kwargs.get("mode", "JOINING"), "sessions": list(sessions)}
    result.update(kwargs.get("extra", {}))
    return result


def test_progress_and_line():
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 0, 100 * GIB), session("10.0.0.2", 0, 100 * GIB))],
                                  None, now=1000, operations=["Bootstrap"])
    assert s["progressed"] and not s["stalled"] and s["line"].startswith("[--------------------]   0%")
    files = [{"path": "/d/ks/t-%s/nb-1-big-Data.db" % ("0" * 32), "table": "ks.t", "done": 5, "total": 10}]
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 60 * GIB, 100 * GIB, files=files),
                                        session("10.0.0.2", 40 * GIB, 100 * GIB))],
                                  s, now=1300, operations=["Bootstrap"])
    assert s["progressed"] and s["last_progress"] == 1300
    assert s["line"] == ("[##########----------]  50%  100.0/200.0 GiB  tables: 0 done, 1 streaming  ETA ~5m00s"
                         "  2 sessions  now: ks.t (from 10.0.0.1)")


def test_stall_after_checks_in_a_row_without_bytes():
    views = [read("n4", session("10.0.0.1", 10, 100))]
    s = cassandra_stream_progress(views, None, now=0, stall_checks=3)
    s = cassandra_stream_progress(views, s, now=300, stall_checks=3)
    assert not s["progressed"] and not s["stalled"] and "NO PROGRESS for 5m00s (1 check)" in s["line"]
    s = cassandra_stream_progress(views, s, now=600, stall_checks=3)
    assert not s["stalled"] and s["idle_checks"] == 2
    # one more byte resets the count
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 11, 100))], s, now=900, stall_checks=3)
    assert s["progressed"] and s["idle_checks"] == 0
    for now in (1200, 1500, 1800):
        s = cassandra_stream_progress([read("n4", session("10.0.0.1", 11, 100))], s, now=now, stall_checks=3)
    assert s["stalled"] and s["idle_checks"] == 3


def test_finished_session_counts_as_done_and_as_progress():
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 50, 100), session("10.0.0.2", 90, 100))], None, now=0)
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 50, 100))], s, now=300)
    assert s["progressed"]
    assert (s["bytes_done"], s["bytes_total"]) == (150, 200)


def test_no_session_yet_then_no_answer():
    s = cassandra_stream_progress([read("n4")], None, now=0)
    assert "(no stream session yet)" in s["line"] and s["bytes_total"] == 0
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=100, stall_checks=1)
    assert not s["answered"] and s["stalled"] and "(no answer from nodetool netstats)" in s["line"]


def test_filters_operation_and_peer():
    views = [read("n1", session("10.0.0.9", 5, 10, op="Bootstrap", direction="sending"),
                  session("10.0.0.8", 1, 1000, op="Repair", direction="sending")),
             read("n2", session("10.0.0.7", 3, 10, op="Bootstrap", direction="sending"))]
    s = cassandra_stream_progress(views, None, now=0, operations=["Bootstrap"], peer="10.0.0.9")
    assert (s["bytes_done"], s["bytes_total"]) == (5, 10)
    assert list(s["streams"]) == ["n1|p1|10.0.0.9|sending"]


def test_skipped_and_failed_reads_are_ignored():
    s = cassandra_stream_progress([{"item": "n1", "skipped": True}, read("n2", session("10.0.0.1", 1, 2))], None, now=0)
    assert s["answered"] and s["bytes_total"] == 2


def node(address, rack, load="100.0 GiB", status="U", state="N"):
    return {"address": address, "rack": rack, "load": load, "status": status, "state": state}


RING = {"dc1": {"nodes": [node("10.0.0.1", "r1"), node("10.0.0.2", "r2"), node("10.0.0.3", "r3"),
                          node("10.0.0.4", "r1"), node("10.0.0.5", "r2"), node("10.0.0.6", "r3")]},
        "dc2": {"nodes": [node("10.1.0.1", "r1")]}}
HOSTS = dict(("10.0.0.%d" % i, "n%d" % i) for i in range(1, 7))
NTS3 = {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 3, "dc2": 1}},
        "system_auth": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 2}},
        "system_traces": {"class": "SimpleStrategy", "rf": {"*": 2}}}


def test_one_node_in_a_rack_of_three_racks():
    plan = cassandra_add_node_plan(RING, [{"host": "n7", "dc": "dc1", "rack": "r1"}], hosts=HOSTS, keyspaces=NTS3)
    assert plan["racks"] == {"dc1": {"r1": 3, "r2": 2, "r3": 2}}
    assert len(plan["warnings"]) == 1 and "dc1 after the add: r1 3, r2 2, r3 2" in plan["warnings"][0]
    assert plan["scope"] == {"dc1": "rack"}
    assert plan["cleanup"] == {"dc1": ["n1", "n4"]}
    assert plan["estimate"] == [{"host": "n7", "bytes": 200 * GIB // 3, "basis": "the load of rack r1 / 3 nodes"}]


def test_a_rack_each_is_balanced():
    new = [{"host": "n%d" % (7 + i), "dc": "dc1", "rack": r} for i, r in enumerate(["r1", "r2", "r3"])]
    plan = cassandra_add_node_plan(RING, new, hosts=HOSTS, keyspaces=NTS3)
    assert plan["warnings"] == [] and plan["cleanup"]["dc1"] == ["n1", "n2", "n3", "n4", "n5", "n6"]


def test_rf_not_the_rack_count_or_unknown_means_the_whole_dc():
    rf2 = {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 2}}}
    for keyspaces in (rf2, None):
        plan = cassandra_add_node_plan(RING, [{"host": "n7", "dc": "dc1", "rack": "r1"}], hosts=HOSTS, keyspaces=keyspaces)
        assert plan["scope"] == {"dc1": "dc"} and len(plan["cleanup"]["dc1"]) == 6
        assert plan["estimate"][0]["bytes"] == 600 * GIB // 7


def test_joining_node_not_counted_twice_and_not_cleaned():
    ring = {"dc1": {"nodes": [node("10.0.0.1", "r1"), node("10.0.0.2", "r1"), node("10.0.0.7", "r1", state="J")]}}
    plan = cassandra_add_node_plan(ring, [{"host": "n7", "dc": "dc1", "rack": "r1", "in_ring": True}],
                                   hosts={"10.0.0.1": "n1", "10.0.0.2": "n2"})
    assert plan["racks"] == {"dc1": {"r1": 3}} and plan["estimate"] == []
    assert plan["cleanup"] == {"dc1": ["n1", "n2"]}


def test_run_again_after_the_join_excludes_the_new_node():
    ring = {"dc1": {"nodes": [node("10.0.0.1", "r1"), node("10.0.0.7", "r1", load="50.0 GiB")]}}
    plan = cassandra_add_node_plan(ring, [{"host": "n7", "address": "10.0.0.7", "dc": "dc1", "rack": "r1", "in_ring": True}],
                                   hosts={"10.0.0.1": "n1"})
    assert plan["cleanup"] == {"dc1": ["n1"]} and plan["racks"] == {"dc1": {"r1": 2}} and plan["estimate"] == []


def test_unknown_load_gives_no_estimate():
    ring = {"dc1": {"nodes": [node("10.0.0.1", "r1", load="?")]}}
    plan = cassandra_add_node_plan(ring, [{"host": "n2", "dc": "dc1", "rack": "r1"}])
    assert plan["estimate"] == [] and plan["cleanup"] == {"dc1": ["10.0.0.1"]}


COMPACTIONSTATS_41 = """pending tasks: 2
- orders.items: 2

id                                   compaction type keyspace table completed total      unit  progress
5d8f0a10-7c2e-11ef-9c2b-4e8d2d1b2c3d Cleanup         orders   items 1048576   4194304    bytes 25.00%
6e9a1b20-7c2e-11ef-9c2b-4e8d2d1b2c3d Compaction      orders   items 10        20         bytes 50.00%
Active compaction remaining time :   0h00m03s
"""

COMPACTIONSTATS_50 = """concurrent compactors              2
pending tasks                      1
orders items 1
compactions completed              12
data compacted                     1.2 MiB
compactions aborted                0
compactions reduced                0
sstables dropped from compaction   0
15 minute rate                     0.00/minute
mean rate                          0.40/hour
compaction throughput (MiB/s)      64.0

id                                   compaction type keyspace table        completed total unit  progress
7fab2c30-7c2e-11ef-9c2b-4e8d2d1b2c3d Cleanup         orders   order_lines  2000      8000  bytes 25.00%
active compaction remaining time   0h00m01s
"""


def test_compactionstats_cleanup_tasks_only():
    for out, table, done, total in ((COMPACTIONSTATS_41, "orders.items", 1048576, 4194304),
                                    (COMPACTIONSTATS_50, "orders.order_lines", 2000, 8000)):
        view = cassandra_compactionstats({"item": "n1", "rc": 0, "stdout": out}, types=["Cleanup"])
        assert [(s["operation"], s["bytes_done"], s["bytes_total"], s["files"][0]["table"]) for s in view["sessions"]] == [
            ("Cleanup", done, total, table)]


def test_cleanup_progress_line_and_failed_read():
    views = [cassandra_cleanup_view(({"rc": 0, "stdout": COMPACTIONSTATS_41}, "n1")),
             cassandra_cleanup_view(({"rc": 1, "stdout": "", "stderr": "Connection refused", "msg": "non-zero"}, "n2"))]
    assert views[1]["failed"]
    s = cassandra_stream_progress(views, None, now=0, operations=["Cleanup"])
    assert s["bytes_total"] == 4194304 and "now: orders.items (on n1)" in s["line"]
