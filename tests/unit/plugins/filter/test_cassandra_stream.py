from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import time

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_netstats import parse_netstats
from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import (
    cassandra_add_node_plan, cassandra_cleanup_view, cassandra_compactionstats, cassandra_stream_progress)

GIB = 1024 ** 3
MIB = 1024 ** 2


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """End times in UTC: epoch 0 is 00:00."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "modules", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


def netstats_view(host, name):
    mode, lines, sessions = parse_netstats(fixture(name))
    return {"item": host, "mode": mode, "sessions": sessions}


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
    assert s["progressed"] and not s["stalled"] and s["line"].startswith("[--------------------]   0%  0.0/200.0 GiB  ETA ?  ")
    files = [{"path": "/d/ks/t-%s/nb-1-big-Data.db" % ("0" * 32), "table": "ks.t", "done": 5, "total": 10}]
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 60 * GIB, 100 * GIB, files=files),
                                        session("10.0.0.2", 40 * GIB, 100 * GIB))],
                                  s, now=1300, operations=["Bootstrap"])
    assert s["progressed"] and s["last_progress"] == 1300
    # 100 GiB in 300s
    assert s["line"] == ("[##########----------]  50%  100.0/200.0 GiB  341 MiB/s  ETA 5m00s (ends ~00:26)"
                         "  tables: 0 done, 1 streaming  2 sessions  now: ks.t (from 10.0.0.1)")


def test_stall_after_checks_in_a_row_without_bytes():
    views = [read("n4", session("10.0.0.1", 10, 100))]
    s = cassandra_stream_progress(views, None, now=0, stall_checks=3)
    s = cassandra_stream_progress(views, s, now=300, stall_checks=3)
    assert not s["progressed"] and not s["stalled"] and "NO PROGRESS for 5m00s (1/3 checks)" in s["line"]
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
    assert "(nothing in progress yet)" in s["line"] and s["bytes_total"] == 0
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=100, stall_checks=1, quiet_factor=1)
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
    new = [{"host": "n7", "address": "10.0.0.7", "dc": "dc1", "rack": "r1", "in_ring": True, "state": "joining"}]
    plan = cassandra_add_node_plan(ring, new, hosts={"10.0.0.1": "n1", "10.0.0.2": "n2"})
    assert plan["racks"] == {"dc1": {"r1": 3}} and plan["estimate"] == []
    assert plan["cleanup"] == {"dc1": ["n1", "n2"]}
    # seen UN already (it joined between the checks): still not cleaned, nor counted as a source
    ring["dc1"]["nodes"][2] = node("10.0.0.7", "r1", load="1.0 GiB")
    plan = cassandra_add_node_plan(ring, new, hosts={"10.0.0.1": "n1", "10.0.0.2": "n2"})
    assert plan["cleanup"] == {"dc1": ["n1", "n2"]}


def test_run_again_after_the_join_excludes_the_new_node():
    ring = {"dc1": {"nodes": [node("10.0.0.1", "r1"), node("10.0.0.7", "r1", load="50.0 GiB")]}}
    plan = cassandra_add_node_plan(ring, [{"host": "n7", "address": "10.0.0.7", "dc": "dc1", "rack": "r1", "in_ring": True,
                                           "state": "joined"}],
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


def test_quiet_phases_get_more_checks():
    # every session at 100% (e.g. views written through the write path) or none yet
    for views in ([read("n4", session("10.0.0.1", 100, 100))], [read("n4")]):
        s = cassandra_stream_progress(views, None, now=0, stall_checks=3, quiet_factor=4)
        for i in range(1, 12):
            s = cassandra_stream_progress(views, s, now=i * 300, stall_checks=3, quiet_factor=4)
        assert not s["transferring"] and s["idle_checks"] == 11 and not s["stalled"]
        s = cassandra_stream_progress(views, s, now=12 * 300, stall_checks=3, quiet_factor=4)
        assert s["stalled"] and "(12/12 checks)" in s["line"]


def test_simple_strategy_user_keyspace_cleans_every_dc():
    keyspaces = dict(NTS3, legacy={"class": "SimpleStrategy", "rf": {"*": 3}})
    plan = cassandra_add_node_plan(RING, [{"host": "n7", "dc": "dc1", "rack": "r1"}],
                                   hosts=dict(HOSTS, **{"10.1.0.1": "m1"}), keyspaces=keyspaces)
    assert plan["scope"] == {"dc1": "dc", "dc2": "dc"}
    assert plan["cleanup"] == {"dc1": ["n1", "n2", "n3", "n4", "n5", "n6"], "dc2": ["m1"]}


def test_node_joined_in_an_earlier_run_is_cleaned_when_another_joins_after():
    ring = {"dc1": {"nodes": [node("10.0.0.1", "r1"), node("10.0.0.7", "r1")]}}
    new = [{"host": "n7", "address": "10.0.0.7", "dc": "dc1", "rack": "r1", "in_ring": True, "state": "joined"},
           {"host": "n8", "address": "10.0.0.8", "dc": "dc1", "rack": "r1", "state": "new"}]
    plan = cassandra_add_node_plan(ring, new, hosts={"10.0.0.1": "n1", "10.0.0.7": "n7"})
    assert plan["cleanup"] == {"dc1": ["n1", "n7"]} and plan["racks"] == {"dc1": {"r1": 3}}


def test_a_host_that_does_not_answer_keeps_its_sessions():
    views = [read("n1", session("10.0.0.7", 10, 100, op="Restore replica count")),
             read("n2", session("10.0.0.8", 10, 100, op="Restore replica count"))]
    s = cassandra_stream_progress(views, None, now=0, stall_checks=3)
    for now in (300, 600):
        s = cassandra_stream_progress([views[0], {"item": "n2", "failed": True, "msg": "x"}], s, now=now, stall_checks=3)
        assert not s["progressed"] and (s["bytes_done"], s["bytes_total"]) == (20, 200)
    s = cassandra_stream_progress([views[0], {"item": "n2", "failed": True, "msg": "x"}], s, now=900, stall_checks=3)
    assert s["stalled"]


def test_nothing_answers_then_a_bigger_total_is_progress():
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 10, 100))], None, now=0)
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=300)
    assert not s["progressed"] and not s["answered"] and s["streams"]["n4|p1|10.0.0.1|receiving"]["gone"] is False
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 10, 120))], s, now=600)
    assert s["progressed"] and s["bytes_total"] == 120


def eta(done, now, s, total=710 * GIB):
    return cassandra_stream_progress([read("n4", session("10.0.0.1", done, total))], s, now=now)


def test_eta_unknown_until_two_checks_or_when_nothing_moves():
    s = eta(40 * GIB, 0, None)
    assert "  ETA ?  " in s["line"] and "/s" not in s["line"]
    s = eta(40 * GIB, 300, s)  # no byte since the first check: rate 0
    assert "  ETA ?  " in s["line"]
    # nothing left: no ETA
    assert "ETA" not in eta(710 * GIB, 600, s)["line"]


def test_eta_from_the_rate_of_the_last_three_checks():
    # 12 MiB/s for 3 checks, after a first check at 40 GiB
    s = eta(40 * GIB, 0, None)
    s = eta(40 * GIB + 3600 * 12 * MIB, 3600, s)
    assert "  12 MiB/s  ETA " in s["line"]
    s = eta(40 * GIB + 7200 * 12 * MIB, 7200, s)
    s = eta(40 * GIB + 10800 * 12 * MIB, 10800, s)
    left = (710 * GIB - (40 * GIB + 10800 * 12 * MIB)) / (12.0 * MIB)
    assert "  12 MiB/s  ETA %dh%02d (ends ~%s)" % (left // 3600, left % 3600 // 60,
                                                   time.strftime("%H:%M", time.gmtime(10800 + left))) in s["line"]
    # then 6 MiB/s: the first 12 MiB/s hour leaves the window after three more checks
    for i in (1, 2, 3):
        s = eta(s["bytes_done"] + 3600 * 6 * MIB, 10800 + 3600 * i, s)
        assert ("  %s MiB/s  " % {1: 10, 2: "8.0", 3: "6.0"}[i]) in s["line"], s["line"]
    assert len(s["samples"]) == 3


def test_eta_over_a_day_shows_the_date():
    s = eta(0, 0, None, total=100 * GIB)
    s = eta(100 * MIB, 100, s, total=100 * GIB)  # 1 MiB/s: 99.9 GiB left, about 28h
    assert "  1.0 MiB/s  ETA 1d04h (ends ~1970-01-02 04:26)" in s["line"]


def test_eta_ignores_a_check_without_answer():
    s = eta(0, 0, None, total=100 * GIB)
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=300)
    assert s["samples"] == [[0, 0]] and "ETA ?" in s["line"]
    s = eta(300 * MIB, 600, s, total=100 * GIB)
    assert "  512 KiB/s  ETA " in s["line"]
    # no answer after two good checks: no made-up rate
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=900)
    assert "  ETA ?  (no answer" in s["line"] and "/s" not in s["line"]


def test_eta_unknown_beyond_30_days():
    s = eta(0, 0, None, total=100 * 1024 * GIB)
    s = eta(1, 3600, s, total=100 * 1024 * GIB)  # 1 byte an hour: no absurd date, no error
    assert "  0 KiB/s  ETA ?  " in s["line"]
    s = eta(0, 0, None, total=100 * GIB)
    assert "  40 KiB/s  ETA ?" in eta(40 * 1024, 1, s, total=100 * GIB)["line"]  # 30.3 days
    assert "  50 KiB/s  ETA 24d06h (ends ~1970-01-25 06:32)" in eta(50 * 1024, 1, s, total=100 * GIB)["line"]


def test_nodes_added_in_the_same_run_clean_up_for_the_later_ones():
    new = [{"host": "n7", "address": "10.0.0.7", "dc": "dc1", "rack": "r1", "state": "new"},
           {"host": "n8", "address": "10.0.0.8", "dc": "dc1", "rack": "r1", "state": "new"},
           {"host": "n9", "address": "10.0.0.9", "dc": "dc1", "rack": "r2", "state": "new"}]
    plan = cassandra_add_node_plan(RING, new, hosts=HOSTS, keyspaces=NTS3)
    assert plan["scope"] == {"dc1": "rack"}
    # rack-aware: n7 hands over to n8 (same rack), n8 to nobody, n9 is the last one of r2
    assert plan["cleanup"]["dc1"] == ["n1", "n2", "n4", "n5", "n7"]
    plan = cassandra_add_node_plan(RING, new, hosts=HOSTS, keyspaces=None)
    assert plan["cleanup"]["dc1"] == ["n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8"]


def test_real_compactionstats_40_41_50():
    # captured during nodetool cleanup (compaction throughput 1 MiB/s): two Cleanup tasks each
    for version, tasks in (("40", [(0, 76376336), (1897868, 18048231)]),
                           ("41", [(1354216, 2427880), (627036, 18782813)]),
                           ("50", [(566099, 8139280), (503493, 38163184)])):
        view = cassandra_cleanup_view(({"rc": 0, "stdout": fixture("nodetool_compactionstats_%s_cleanup.txt" % version)}, "n1"))
        assert [(s["bytes_done"], s["bytes_total"], s["files"][0]["table"]) for s in view["sessions"]] == [
            (d, t, "ks.orders") for d, t in tasks], version
        s = cassandra_stream_progress([view], None, now=0, operations=["Cleanup"])
        assert "2 sessions" in s["line"] and "now: ks.orders (on n1)" in s["line"]


def test_real_40_41_bootstrap_progress_between_two_checks():
    for version in ("40", "41"):
        s = cassandra_stream_progress([netstats_view("n2", "nodetool_netstats_%s_bootstrap_receiving_early.txt" % version)],
                                      None, now=0, operations=["Bootstrap"])
        assert s["progressed"] and s["transferring"] and s["sessions"] == 1
        s = cassandra_stream_progress([netstats_view("n2", "nodetool_netstats_%s_bootstrap_receiving_late.txt" % version)],
                                      s, now=300, operations=["Bootstrap"])
        assert s["progressed"] and s["idle_checks"] == 0 and not s["stalled"]
        assert s["line"].startswith("[#########-----------]  4") and "/91." in s["line"] and "MiB" in s["line"] and "/s  ETA " in s["line"]


def test_an_unreachable_node_keeps_its_cleanup_running():
    running = ({"rc": 0, "stdout": fixture("nodetool_compactionstats_41_cleanup.txt")}, "n1")
    s = cassandra_stream_progress([cassandra_cleanup_view(running)], None, now=0, operations=["Cleanup"])
    s = cassandra_stream_progress([cassandra_cleanup_view(({"unreachable": True, "msg": "ssh timeout"}, "n1"))], s,
                                  now=300, operations=["Cleanup"])
    assert not s["progressed"] and not s["answered"] and s["idle_checks"] == 1 and "100%" not in s["line"]


def test_run_again_for_the_cleanup_keeps_the_earlier_new_nodes():
    new = [{"host": "n7", "address": "10.0.0.7", "dc": "dc1", "rack": "r1", "in_ring": True, "state": "joined"},
           {"host": "n8", "address": "10.0.0.8", "dc": "dc1", "rack": "r1", "in_ring": True, "state": "joined"}]
    ring = {"dc1": {"nodes": RING["dc1"]["nodes"] + [node("10.0.0.7", "r1"), node("10.0.0.8", "r1")]}}
    plan = cassandra_add_node_plan(ring, new, hosts=HOSTS, keyspaces=NTS3)
    assert plan["cleanup"]["dc1"] == ["n1", "n4", "n7"]


def test_eta_says_the_day_when_the_end_is_tomorrow():
    s = eta(0, 84600, None, total=10 * GIB)  # 23:30
    s = eta(1024 * MIB, 84900, s, total=10 * GIB)  # 1 GiB in 5 minutes: 45 minutes left
    assert "  ETA 45m00s (ends ~1970-01-02 00:20)" in s["line"]


def test_cleanup_eta_is_for_the_running_tasks():
    view = cassandra_cleanup_view(({"rc": 0, "stdout": fixture("nodetool_compactionstats_50_cleanup.txt")}, "n1"))
    s = cassandra_stream_progress([view], None, now=0, operations=["Cleanup"], eta_label="ETA of the running tasks")
    assert "  ETA of the running tasks ?  " in s["line"]
    for v in view["sessions"]:
        v["bytes_done"] += MIB
    s = cassandra_stream_progress([view], s, now=100, operations=["Cleanup"], eta_label="ETA of the running tasks")
    assert "  ETA of the running tasks " in s["line"] and "(ends ~" in s["line"]
