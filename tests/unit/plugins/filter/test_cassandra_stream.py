from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import time

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_netstats import parse_netstats
from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import (
    cassandra_add_node_plan, cassandra_cleanup_view, cassandra_compactionstats, cassandra_stream_progress,
    cassandra_stream_report, cassandra_host_addresses)

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


def report(s, **kwargs):
    """The report as printed, one line each, 88 columns at most (100 in a debug msg list)."""
    lines = cassandra_stream_report(s, **dict({"node": "n4", "what": "bootstrap"}, **kwargs))
    assert all(len(line) <= 88 for line in lines) or len(kwargs.get("node", "")) > 20, lines
    return "\n".join(lines)


def header(s, **kwargs):
    return report(s, **kwargs).split("\n")[0]


def finish(s, **kwargs):
    """The Finish line's text, None without one."""
    found = [line.split("Finish:", 1)[1].strip() for line in report(s, **kwargs).split("\n") if "Finish:" in line]
    return found[0] if found else None


def test_progress_and_report():
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 0, 100 * GIB), session("10.0.0.2", 0, 100 * GIB))],
                                  None, now=1000, operations=["Bootstrap"])
    assert s["progressed"] and not s["stalled"]
    assert report(s).startswith("n4  bootstrap  [--------------------]   0%\n\n      data:      0.0 GiB / 200.0 GiB\n")
    assert finish(s) == "unknown, no rate yet"
    files = [{"path": "/d/ks/t-%s/nb-1-big-Data.db" % ("0" * 32), "table": "ks.t", "done": 5, "total": 10}]
    s = cassandra_stream_progress([read("n4", session("10.0.0.1", 60 * GIB, 100 * GIB, files=files),
                                        session("10.0.0.2", 40 * GIB, 100 * GIB))],
                                  s, now=1300, operations=["Bootstrap"])
    assert s["progressed"] and s["last_progress"] == 1300
    # 100 GiB in 300s
    assert report(s, names={"10.0.0.1": "n1"}) == """\
n4  bootstrap  [##########----------]  50%   341 MiB/s

      data:      100.0 GiB / 200.0 GiB
                 10 / 20 files

      from:      10.0.0.2   40% done  (40.0 / 100.0 GiB)
                 n1         60% done  (60.0 / 100.0 GiB)

      Now:       current - 00:21 UTC
      Started:   5m ago  - 00:16 UTC
      Finish:    in 5m   - 00:26 UTC"""


def test_stall_after_checks_in_a_row_without_bytes():
    views = [read("n4", session("10.0.0.1", 10, 100))]
    s = cassandra_stream_progress(views, None, now=0, stall_checks=3)
    s = cassandra_stream_progress(views, s, now=300, stall_checks=3)
    assert not s["progressed"] and not s["stalled"] and report(s).endswith("\n\n      Progress:  none for 1 check (5m), stops after 3")
    s = cassandra_stream_progress(views, s, now=600, stall_checks=3)
    assert not s["stalled"] and s["idle_checks"] == 2
    # once it has ended (DECOMMISSIONED, NORMAL...): one line, no no-progress count
    assert report(s, status="done") == "n4  bootstrap  done  10 B, after 10m"
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
    assert s["bytes_total"] == 0 and report(s) == """\
n4  bootstrap  total unknown

      data:      nothing in progress yet

      Now:       current - 00:00 UTC
      Started:   0s ago  - 00:00 UTC"""
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=100, stall_checks=1, quiet_factor=1)
    assert not s["answered"] and s["stalled"]
    assert report(s, status="stalled") == """\
n4  bootstrap  STALLED  total unknown

      data:      no answer at this check, the figures are from the last answer
                 nothing in progress yet

      Now:       current - 00:01 UTC
      Started:   1m ago  - 00:00 UTC

      Progress:  none for 1 check (1m)"""


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
    assert s["bytes_total"] == 4194304
    assert "      on:        n1   25% done  (1.0 / 4.0 MiB)\n" in report(s, files_label="tasks")


def test_quiet_phases_get_more_checks():
    # every session at 100% (e.g. views written through the write path) or none yet
    for views in ([read("n4", session("10.0.0.1", 100, 100))], [read("n4")]):
        s = cassandra_stream_progress(views, None, now=0, stall_checks=3, quiet_factor=4)
        for i in range(1, 12):
            s = cassandra_stream_progress(views, s, now=i * 300, stall_checks=3, quiet_factor=4)
        assert not s["transferring"] and s["idle_checks"] == 11 and not s["stalled"]
        s = cassandra_stream_progress(views, s, now=12 * 300, stall_checks=3, quiet_factor=4)
        assert s["stalled"] and report(s, status="stalled").endswith("\n      Progress:  none for 12 checks (1h00m)")


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
    assert header(s).endswith("]   5%") and "/s" not in report(s) and finish(s) == "unknown, no rate yet"
    s = eta(40 * GIB, 300, s)  # no byte since the first check: rate 0
    assert header(s).endswith("]   5%") and finish(s) == "unknown, no rate yet"
    # nothing left: no time left, the sessions finish
    s = eta(710 * GIB, 600, s)
    assert header(s).startswith("n4  bootstrap  [####################] 100%   ") and finish(s) == "all sent, finishing"


def test_eta_from_the_rate_of_the_last_three_checks():
    # 12 MiB/s for 3 checks, after a first check at 40 GiB
    s = eta(40 * GIB, 0, None)
    s = eta(40 * GIB + 3600 * 12 * MIB, 3600, s)
    assert header(s).endswith("]  11%   12 MiB/s")
    s = eta(40 * GIB + 7200 * 12 * MIB, 7200, s)
    s = eta(40 * GIB + 10800 * 12 * MIB, 10800, s)
    left = (710 * GIB - (40 * GIB + 10800 * 12 * MIB)) / (12.0 * MIB)
    assert header(s).endswith("   12 MiB/s") and finish(s) == "in %dh%02dm - %s UTC" % (
        left // 3600, left % 3600 // 60, time.strftime("%H:%M", time.gmtime(10800 + left)))
    # then 6 MiB/s: the first 12 MiB/s hour leaves the window after three more checks
    for i in (1, 2, 3):
        s = eta(s["bytes_done"] + 3600 * 6 * MIB, 10800 + 3600 * i, s)
        assert header(s).endswith("   %s MiB/s" % {1: 10, 2: "8.0", 3: "6.0"}[i]), header(s)
    assert len(s["samples"]) == 3


def test_eta_over_a_day_shows_the_date():
    s = eta(0, 0, None, total=100 * GIB)
    s = eta(100 * MIB, 100, s, total=100 * GIB)  # 1 MiB/s: 99.9 GiB left, about 28h
    assert header(s).endswith("   1.0 MiB/s") and finish(s) == "in 1d04h - 1970-01-02 04:26 UTC"


def test_eta_ignores_a_check_without_answer():
    s = eta(0, 0, None, total=100 * GIB)
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=300)
    assert s["samples"] == [[0, 0]] and header(s).endswith("]   0%") and finish(s) == "unknown, no rate yet"
    s = eta(300 * MIB, 600, s, total=100 * GIB)
    assert header(s).endswith("   512 KiB/s")
    # no answer after two good checks: no made-up rate
    s = cassandra_stream_progress([{"item": "n4", "failed": True, "msg": "x"}], s, now=900)
    assert header(s).endswith("]   0%") and "/s" not in report(s) and "      data:      no answer at this check" in report(s)


def test_eta_unknown_beyond_30_days():
    s = eta(0, 0, None, total=100 * 1024 * GIB)
    s = eta(1, 3600, s, total=100 * 1024 * GIB)  # 1 byte an hour: no absurd date, no error
    assert header(s).endswith("   0 B/s") and finish(s) == "unknown, too slow to tell"
    s = eta(0, 0, None, total=100 * GIB)
    assert finish(eta(40 * 1024, 1, s, total=100 * GIB)) == "unknown, too slow to tell"  # 30.3 days
    s = eta(50 * 1024, 1, s, total=100 * GIB)
    assert header(s).endswith("   50 KiB/s") and finish(s) == "in 24d06h - 1970-01-25 06:32 UTC"


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
        assert s["sessions"] == 2 and "\n      on:        n1  " in report(s)


def test_real_40_41_bootstrap_progress_between_two_checks():
    for version in ("40", "41"):
        s = cassandra_stream_progress([netstats_view("n2", "nodetool_netstats_%s_bootstrap_receiving_early.txt" % version)],
                                      None, now=0, operations=["Bootstrap"])
        assert s["progressed"] and s["transferring"] and s["sessions"] == 1
        s = cassandra_stream_progress([netstats_view("n2", "nodetool_netstats_%s_bootstrap_receiving_late.txt" % version)],
                                      s, now=300, operations=["Bootstrap"])
        assert s["progressed"] and s["idle_checks"] == 0 and not s["stalled"]
        assert header(s).startswith("n4  bootstrap  [#########-----------]  4") and header(s).endswith(" KiB/s")
        assert "      data:      4" in report(s) and " MiB / 9" in report(s) and "      from:      192.168.0.2   4" in report(s)


def test_an_unreachable_node_keeps_its_cleanup_running():
    running = ({"rc": 0, "stdout": fixture("nodetool_compactionstats_41_cleanup.txt")}, "n1")
    s = cassandra_stream_progress([cassandra_cleanup_view(running)], None, now=0, operations=["Cleanup"])
    s = cassandra_stream_progress([cassandra_cleanup_view(({"unreachable": True, "msg": "ssh timeout"}, "n1"))], s,
                                  now=300, operations=["Cleanup"])
    assert not s["progressed"] and not s["answered"] and s["idle_checks"] == 1 and "100%" not in header(s)


def test_run_again_for_the_cleanup_keeps_the_earlier_new_nodes():
    new = [{"host": "n7", "address": "10.0.0.7", "dc": "dc1", "rack": "r1", "in_ring": True, "state": "joined"},
           {"host": "n8", "address": "10.0.0.8", "dc": "dc1", "rack": "r1", "in_ring": True, "state": "joined"}]
    ring = {"dc1": {"nodes": RING["dc1"]["nodes"] + [node("10.0.0.7", "r1"), node("10.0.0.8", "r1")]}}
    plan = cassandra_add_node_plan(ring, new, hosts=HOSTS, keyspaces=NTS3)
    assert plan["cleanup"]["dc1"] == ["n1", "n4", "n7"]


def test_eta_says_the_day_when_the_end_is_tomorrow():
    s = eta(0, 84600, None, total=10 * GIB)  # 23:30
    s = eta(1024 * MIB, 84900, s, total=10 * GIB)  # 1 GiB in 5 minutes: 45 minutes left
    assert finish(s) == "in 45m  - 1970-01-02 00:20 UTC"


def test_cleanup_report():
    view = cassandra_cleanup_view(({"rc": 0, "stdout": fixture("nodetool_compactionstats_50_cleanup.txt")}, "n1"))
    s = cassandra_stream_progress([view], None, now=0, operations=["Cleanup"])
    for v in view["sessions"]:
        v["bytes_done"] += 3 * MIB
    s = cassandra_stream_progress([view], s, now=100, operations=["Cleanup"])
    assert report(s, node="n1, n2", what="cleanup", files_label="tasks",
                  extra=[["cleaning", "1 of 2 nodes (Finish counts the running tasks only)"]]) == """\
n1, n2  cleanup  [###-----------------]  15%   61 KiB/s

      data:      7.0 MiB / 44.2 MiB
                 0 / 2 tasks
      cleaning:  1 of 2 nodes (Finish counts the running tasks only)

      on:        n1   15% done  (7.0 / 44.2 MiB)

      Now:       current - 00:01 UTC
      Started:   1m ago  - 00:00 UTC
      Finish:    in 10m  - 00:11 UTC"""


NAMES = {"10.0.0.1": "node1", "10.0.0.2": "node2", "10.0.0.3": "node5"}


def bootstrap(fractions, step=300, sizes=((u"10.0.0.1", 52 * GIB), (u"10.0.0.2", 31 * GIB), (u"10.0.0.3", 17 * GIB)),
              pace=None):
    """A bootstrap from three peers, checked every step seconds from 13:14, at the given
    fractions (times pace, per peer)."""
    s = None
    for i, frac in enumerate(fractions):
        sessions = []
        for n, (peer, size) in enumerate(sizes):
            part = min(1.0, frac * (pace[n] if pace else 1))
            sessions.append(dict(session(peer, int(part * size), size, plan="p%d" % n), files_total=1000,
                                 files_done=int(part * 1000)))
        sessions[0]["files"] = [{"path": "", "table": "ks.orders", "done": 1, "total": 2},
                                {"path": "", "table": "ks.items", "done": 2, "total": 2}]
        s = cassandra_stream_progress([read("n4", *sessions)], s, now=47640 + i * step, operations=["Bootstrap"])
    return s


def test_report_going():
    s = bootstrap([0.0, 0.25, 0.5], pace=(1.25, 1.0, 0.5))
    assert report(s, names=NAMES) == """\
n4  bootstrap  [##########----------]  52%   89 MiB/s

      data:      52.2 GiB / 100.0 GiB
                 1 375 / 3 000 files

      from:      node1   62% done  (32.5 / 52.0 GiB)
                 node2   50% done  (15.5 / 31.0 GiB)
                 node5   25% done  (4.2 / 17.0 GiB)

      Now:       current - 13:24 UTC
      Started:   10m ago - 13:14 UTC
      Finish:    in 9m   - 13:33 UTC"""


def test_report_stalled_then_failed():
    s = bootstrap([0.125, 0.25, 0.375, 0.375, 0.375, 0.375])
    assert s["stalled"] and report(s, status="stalled", names=NAMES) == """\
n4  bootstrap  STALLED  [#######-------------]  37%

      data:      37.5 GiB / 100.0 GiB
                 1 125 / 3 000 files

      from:      node1   37% done  (19.5 / 52.0 GiB)
                 node2   37% done  (11.6 / 31.0 GiB)
                 node5   37% done  (6.4 / 17.0 GiB)

      Now:       current - 13:39 UTC
      Started:   25m ago - 13:14 UTC

      Progress:  none for 3 checks (15m)"""
    for status, label in (("join_failed", "FAILED"), ("job_lost", "FAILED"), ("too_long", "TOO LONG"), ("stopped", "STOPPED")):
        assert header(s, status=status) == "n4  bootstrap  %s  [#######-------------]  37%%" % label


def test_report_done():
    s = bootstrap([0.125, 0.5, 1.0])
    s = cassandra_stream_progress([read("n4", mode="NORMAL")], s, now=47640 + 900, operations=["Bootstrap"])
    # the first 12.5 GiB were there at the first check (a wait resumed by a run again)
    assert report(s, status="done") == "n4  bootstrap  done  87.5 GiB in 15m (100.0 GiB in all), 99 MiB/s on average"
    s = bootstrap([0.0, 1.0])
    assert report(s, status="done") == "n4  bootstrap  done  100.0 GiB in 5m, 341 MiB/s on average"
    assert report(bootstrap([1.0]), status="done") == "n4  bootstrap  done  100.0 GiB, at the first check"
    s = cassandra_stream_progress([read("n4", mode="NORMAL")], None, now=0)
    s = cassandra_stream_progress([read("n4", mode="NORMAL")], s, now=65)
    assert report(s, status="done") == "n4  bootstrap  done  nothing streamed in 1m"


def test_report_very_large_and_very_small_rates():
    big = ((u"10.0.0.1", 8 * 1024 * GIB), (u"10.0.0.2", 4 * 1024 * GIB))
    s = bootstrap([0.125, 0.25], step=60, sizes=big)
    assert report(s) == """\
n4  bootstrap  [#####---------------]  25%   25.6 GiB/s

      data:      3.0 TiB / 12.0 TiB
                 500 / 2 000 files

      from:      10.0.0.1   25% done  (2.0 / 8.0 TiB)
                 10.0.0.2   25% done  (1.0 / 4.0 TiB)

      Now:       current - 13:15 UTC
      Started:   1m ago  - 13:14 UTC
      Finish:    in 6m   - 13:21 UTC"""
    small = ((u"10.0.0.1", 2 * MIB),)
    s = bootstrap([0.5, 0.5 + 300.0 / (2 * MIB)], step=300, sizes=small)  # 300 bytes in 5 minutes
    assert report(s) == """\
n4  bootstrap  [##########----------]  50%   1 B/s

      data:      1.0 MiB / 2.0 MiB
                 500 / 1 000 files

      from:      10.0.0.1   50% done  (1.0 / 2.0 MiB)

      Now:       current   - 13:19 UTC
      Started:   5m ago    - 13:14 UTC
      Finish:    in 12d03h - 1970-01-13 16:30 UTC"""


def test_report_zero_eta():
    s = bootstrap([0.5, 1.0 - 1.0 / (100 * GIB)], sizes=((u"10.0.0.1", 100 * GIB),))
    assert header(s) == "n4  bootstrap  [###################-]  99%   170 MiB/s" and finish(s) == "in 0s   - 13:19 UTC"


def test_report_long_names_narrow_the_bar_and_sum_up_the_peers():
    sizes = tuple(("2001:db8:85a3::8a2e:370:%04x" % i, (16 + i) * GIB) for i in range(6))
    s = bootstrap([0.125, 0.25], sizes=sizes)
    assert "\n".join(report(s, node="cass-node-17", what="removenode of 2001:db8::7334").split("\n")[:9]) == """\
cass-node-17  removenode of 2001:db8::7334  [#####---------------]  25%   47 MiB/s

      data:      27.8 GiB / 111.0 GiB
                 1 500 / 6 000 files

      from:      2001:db8:85a3::8a2e:370:0005   25% done  (5.2 / 21.0 GiB)
                 2001:db8:85a3::8a2e:370:0004   25% done  (5.0 / 20.0 GiB)
                 2001:db8:85a3::8a2e:370:0003   25% done  (4.8 / 19.0 GiB)
                 3 more                         25% done  (12.8 / 51.0 GiB)"""
    # too long for even the narrowest bar: the line is longer, the bar no narrower
    assert header(s, node="cassandra-eu-west-1-node-17", what="removenode of 2001:db8:85a3::8a2e:370:7334") == (
        "cassandra-eu-west-1-node-17  removenode of 2001:db8:85a3::8a2e:370:7334  [##--------]  25%   47 MiB/s")


def test_report_clock_shows_the_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    s = bootstrap([0.0, 0.5], step=3600)  # 1970-01-01 at 14:14 in Paris
    assert "      Now:       current   - 15:14 CET\n" in report(s) and finish(s) == "in 1h00m  - 16:14 CET"
    monkeypatch.setenv("TZ", "<+0530>-5:30")  # a zone without abbreviation: its offset
    time.tzset()
    assert "      Now:       current   - 19:44 +05:30\n" in report(s)


def test_host_addresses():
    hostvars = {"n1": {"ansible_host": "10.0.0.1", "cassandra_listen_address": "10.1.0.1"},
                "n2": {"ansible_facts": {"default_ipv4": {"address": "10.0.0.2"}}, "cassandra_listen_address": "localhost"},
                "10.0.0.3": {}}
    assert cassandra_host_addresses(["n1", "n2", "10.0.0.3"], hostvars) == {
        "10.1.0.1": "n1", "10.0.0.1": "n1", "n1": "n1", "10.0.0.2": "n2", "n2": "n2", "10.0.0.3": "10.0.0.3"}

    class Broken(dict):
        def get(self, key, default=None):
            raise ValueError("an undefined variable")
    assert cassandra_host_addresses(["n4"], {"n4": Broken()}) == {"n4": "n4"}
