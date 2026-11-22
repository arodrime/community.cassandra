from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The clocks of the nodes: what clock_probe.yml prints (chronyc, timedatectl, ntpq outputs as the daemons print
# them), what it says, and the verdicts of sync_clocks and of the check before create_cluster and add_node start
# nodes.

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_clocks import (
    cassandra_clock_health, cassandra_clock_in_sync, cassandra_clock_lines, cassandra_clock_probe,
    cassandra_clock_refusal, cassandra_clock_settled, cassandra_clock_verdicts, cassandra_clock_wait_line,
    cassandra_clock_warnings, parse_chrony_sources, parse_chrony_tracking, parse_ntpq, parse_timesync)

TRACKING_SYNCED = """\
Reference ID    : C0000201 (192.0.2.1)
Stratum         : 3
Ref time (UTC)  : Wed Oct 14 09:58:12 2026
System time     : 0.000012345 seconds slow of NTP time
Last offset     : -0.000002345 seconds
RMS offset      : 0.000012345 seconds
Frequency       : 1.234 ppm fast
Residual freq   : -0.001 ppm
Skew            : 0.012 ppm
Root delay      : 0.000800000 seconds
Root dispersion : 0.000300000 seconds
Update interval : 64.2 seconds
Leap status     : Normal
"""
TRACKING_UNSYNCED = """\
Reference ID    : 00000000 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000000000 seconds fast of NTP time
Last offset     : +0.000000000 seconds
RMS offset      : 0.000000000 seconds
Frequency       : 0.000 ppm slow
Residual freq   : +0.000 ppm
Skew            : 0.000 ppm
Root delay      : 1.000000000 seconds
Root dispersion : 1.000000000 seconds
Update interval : 0.0 seconds
Leap status     : Not synchronised
"""
TRACKING_LOCAL = (TRACKING_SYNCED.replace("C0000201 (192.0.2.1)", "7F7F0101 ()")
                  .replace("Stratum         : 3", "Stratum         : 10"))


def tracking(seconds, way="slow", delay="0.000800000", dispersion="0.000300000", leap="Normal", skew="0.012"):
    return (TRACKING_SYNCED.replace("0.000012345 seconds slow", "%.9f seconds %s" % (seconds, way))
            .replace("Skew            : 0.012 ppm", "Skew            : %s ppm" % skew)
            .replace("Root delay      : 0.000800000", "Root delay      : " + delay)
            .replace("Root dispersion : 0.000300000", "Root dispersion : " + dispersion)
            .replace("Leap status     : Normal", "Leap status     : " + leap))


SOURCES = """\
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^* 192.0.2.1                     2   6   377    34    -12us[  -15us] +/-  410us
^- 192.0.2.2                     2   6   377    35   +221us[ +221us] +/- 1023us
"""
SOURCES_NONE_REACHED = """\
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^? 192.0.2.1                     0   6     0     -     +0ns[   +0ns] +/-    0ns
^? 192.0.2.2                     0   6     0     -     +0ns[   +0ns] +/-    0ns
"""
SOURCES_EMPTY = """\
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
"""
TIMESYNC = """\
       Server: 192.0.2.1 (ntp1.example.net)
Poll interval: 34min 8s (min: 32s; max 34min 8s)
         Leap: normal
      Version: 4
      Stratum: 2
    Reference: C0000264
    Precision: 1us (-25)
Root distance: 1.981ms (max: 5s)
       Offset: +2.513ms
        Delay: 1.695ms
       Jitter: 0.565ms
 Packet count: 599
    Frequency: +35.297ppm
"""
NTPQ = """\
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*192.0.2.1       .GPS.            1 u   33   64  377    0.512   -3.250   0.120
+192.0.2.2       192.0.2.1        2 u   12   64  377    0.611    1.020   0.210
"""


def units(active="chronyd", installed=()):
    lines = ["==units=="]
    for unit in ("chronyd", "chrony", "systemd-timesyncd", "ntpd", "ntpsec", "ntp"):
        if unit == active:
            lines.append("%s|active|enabled|loaded" % unit)
        elif unit in installed:
            lines.append("%s|inactive|enabled|loaded" % unit)
        else:
            lines.append("%s|inactive||not-found" % unit)
    return lines


def probe_text(active="chronyd", installed=(), tracking_text=TRACKING_SYNCED, sources=SOURCES, conf=("pool pool.example.net",),
               virt="none", timesync=None, ntpq=None, synced=None, query=None, window=False):
    synced = synced or ("yes" if active or virt != "none" else "no")
    lines = units(active, installed) + ["==virt==", virt]
    if tracking_text is not None:
        lines += ["==tracking=="] + tracking_text.splitlines() + ["==sources=="] + sources.splitlines()
    lines += ["==chrony_conf=="] + list(conf)
    lines += ["==timedatectl==", "Timezone=UTC", "NTP=yes", "NTPSynchronized=%s" % synced]
    if timesync is not None:
        lines += ["==timesync=="] + timesync.splitlines() + ["==timesync_servers==", "server ntp1.example.net"]
    if ntpq is not None:
        lines += ["==ntpq=="] + ntpq.splitlines()
    if window:
        lines.append("==window==")
    if query is not None:
        lines += ["==query==", "2026-10-14T10:00:00Z chronyd version 4.5 starting",
                  "2026-10-14T10:00:08Z System clock wrong by %s seconds (ignored)" % query, "chronyd exiting"]
    return "\n".join(lines) + "\n"


# --- what the daemons print ---------------------------------------------------------------------------------------


def test_chrony_tracking_slow_is_behind_fast_is_ahead():
    behind = parse_chrony_tracking(tracking(0.0123).splitlines())
    assert behind["offset"] == pytest.approx(-0.0123)
    assert behind["synchronised"] and behind["source"] == "192.0.2.1" and behind["leap"] == "normal"
    assert behind["bound"] == pytest.approx(0.0004 + 0.0003)  # half the root delay plus the root dispersion
    ahead = parse_chrony_tracking(tracking(0.25, "fast").splitlines())
    assert ahead["offset"] == pytest.approx(0.25)


def test_chrony_tracking_unsynchronised_and_local_reference():
    unsynced = parse_chrony_tracking(TRACKING_UNSYNCED.splitlines())
    assert not unsynced["synchronised"] and unsynced["leap"] == "unsynchronised" and unsynced["source"] == ""
    local = parse_chrony_tracking(TRACKING_LOCAL.splitlines())
    assert not local["synchronised"] and local["local"]  # local stratum 10: its own clock, no source


def test_chrony_tracking_no_daemon():
    assert parse_chrony_tracking(["506 Cannot talk to daemon"]) == {}


def test_chrony_tracking_leap_pending_is_synchronised():
    assert parse_chrony_tracking(tracking(0.001, leap="Insert second").splitlines())["leap"] == "insert"


def test_chrony_sources():
    assert parse_chrony_sources(SOURCES.splitlines()) == {"selected": "192.0.2.1", "count": 2, "reachable": 2}
    assert parse_chrony_sources(SOURCES_NONE_REACHED.splitlines()) == {"selected": "", "count": 2, "reachable": 0}
    assert parse_chrony_sources(SOURCES_EMPTY.splitlines()) == {"selected": "", "count": 0, "reachable": 0}
    assert parse_chrony_sources(["#* PHC0                          0   2   377     5    +12ns[  +20ns] +/-  110ns"])[
        "selected"] == "PHC0"


def test_timesync_offset_is_the_servers_minus_the_nodes():
    sync = parse_timesync(TIMESYNC.splitlines())
    # Offset +2.513ms: the server ahead of the node, the node 2.5 ms behind
    assert sync["offset"] == pytest.approx(-0.002513)
    assert sync["bound"] == pytest.approx(0.001981 + 0.001695 / 2)  # root distance plus half the delay
    assert sync["source"] == "192.0.2.1 (ntp1.example.net)" and sync["stratum"] == 2
    assert parse_timesync(TIMESYNC.replace("+2.513ms", "-1min 2.5s").splitlines())["offset"] == pytest.approx(62.5)
    assert parse_timesync(TIMESYNC.replace("+2.513ms", "+523us").splitlines())["offset"] == pytest.approx(-0.000523)


def test_timesync_no_server_yet():
    sync = parse_timesync(["       Server: n/a", "Poll interval: 0 (min: 32s; max 34min 8s)", " Packet count: 0"])
    assert sync["source"] == "" and sync["offset"] is None


def test_ntpq_selected_peer():
    peer = parse_ntpq(NTPQ.splitlines())
    assert peer["source"] == "192.0.2.1" and peer["offset"] == pytest.approx(0.00325) and peer["stratum"] == 1
    assert parse_ntpq(NTPQ.replace("*192", " 192").splitlines()) == {}


# --- what a node's probe says -------------------------------------------------------------------------------------


def test_probe_chrony_synchronised():
    p = cassandra_clock_probe(probe_text())
    assert p["daemon"] == "chrony" and p["service"] == "chronyd" and p["active"] and p["synchronised"]
    assert p["source"] == "192.0.2.1" and p["sources"] == 2 and p["reachable"] == 2
    assert p["configured"] == ["pool.example.net"] and p["container"] == "" and p["kernel_synced"] is True
    assert cassandra_clock_in_sync(p, 50)


def test_probe_chrony_running_without_a_selected_source():
    p = cassandra_clock_probe(probe_text(sources=SOURCES_NONE_REACHED, tracking_text=TRACKING_UNSYNCED, synced="no"))
    assert p["active"] and not p["synchronised"] and p["reachable"] == 0 and p["source"] == ""
    assert not cassandra_clock_in_sync(p, 50)


def test_probe_no_daemon_but_chrony_installed_measured_by_chronyd_q():
    p = cassandra_clock_probe(probe_text(active="", installed=("chronyd",), tracking_text="506 Cannot talk to daemon",
                                         sources="506 Cannot talk to daemon", query="-1.234567"))
    assert p["service"] == "chronyd" and p["installed"] and not p["active"] and not p["synchronised"]
    assert p["query"] == pytest.approx(1.234567) and p["query_ahead"] is True  # (wrong by -X: ahead)
    behind = cassandra_clock_probe(probe_text(active="", installed=("chronyd",), tracking_text=None, query="0.5"))
    assert behind["query"] == pytest.approx(0.5) and behind["query_ahead"] is False
    assert cassandra_clock_probe(probe_text(active="", tracking_text=None))["query_ahead"] is None


def test_probe_timesyncd():
    p = cassandra_clock_probe(probe_text(active="systemd-timesyncd", tracking_text=None, conf=(), timesync=TIMESYNC))
    assert p["daemon"] == "timesyncd" and p["synchronised"] and p["offset"] == pytest.approx(-0.002513)
    assert p["configured"] == ["ntp1.example.net"]
    unsynced = cassandra_clock_probe(probe_text(active="systemd-timesyncd", tracking_text=None, conf=(), timesync=TIMESYNC,
                                                synced="no"))
    assert not unsynced["synchronised"]  # the kernel says not synchronised


def test_probe_ntpd():
    p = cassandra_clock_probe(probe_text(active="ntpd", tracking_text=None, conf=(), ntpq=NTPQ))
    assert p["daemon"] == "ntp" and p["synchronised"] and p["source"] == "192.0.2.1"


def test_probe_container_and_window_mark():
    p = cassandra_clock_probe(probe_text(active="", tracking_text=None, virt="docker", window=True))
    assert p["container"] == "docker" and p["window"] and not p["active"]


def test_probe_two_daemons():
    text = probe_text().replace("systemd-timesyncd|inactive||not-found", "systemd-timesyncd|active|enabled|loaded")
    assert cassandra_clock_probe(text)["others"] == ["systemd-timesyncd"]


def test_probe_empty():
    p = cassandra_clock_probe("")
    assert p["daemon"] == "" and not p["active"] and not p["installed"]


# --- the wait of a daemon just started ----------------------------------------------------------------------------


def test_settled():
    synced = cassandra_clock_probe(probe_text())
    assert cassandra_clock_settled(synced, 50)
    unsynced = cassandra_clock_probe(probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED))
    assert not cassandra_clock_settled(unsynced, 50)
    slewing = cassandra_clock_probe(probe_text(tracking_text=tracking(0.2, "fast")))
    assert not cassandra_clock_settled(slewing, 50)  # chrony still corrects it
    assert cassandra_clock_settled(slewing, 50, synchronised_only=True)  # a step follows
    far_source = cassandra_clock_probe(probe_text(tracking_text=tracking(0.001, delay="0.200000000")))
    assert cassandra_clock_settled(far_source, 50)  # its error bound alone is over: no wait helps
    # a daemon just started: synchronised, its bound still huge (its skew from a few samples): it shrinks
    fresh = cassandra_clock_probe(probe_text(tracking_text=tracking(0.0001, "fast", dispersion="62.040000000")))
    assert not cassandra_clock_settled(fresh, 50)
    assert cassandra_clock_settled(fresh, 50, synchronised_only=True)  # a step follows
    assert not cassandra_clock_settled(fresh, 200)  # over 1 s and over ten times the threshold
    assert cassandra_clock_settled(fresh, 10000)  # under ten times a 10 s threshold: a far source
    big = cassandra_clock_probe(probe_text(tracking_text=tracking(0.0001, "fast", dispersion="1.200000000")))
    assert not cassandra_clock_settled(big, 50)
    # right after one of its first updates: a small bound, but chrony's skew still its initial 100%
    just_updated = cassandra_clock_probe(probe_text(tracking_text=tracking(0.0001, "fast", dispersion="0.500000000",
                                                                           skew="1000000.000")))
    assert just_updated["skew"] == 1000000.0
    assert not cassandra_clock_settled(just_updated, 50)
    assert "converging" in cassandra_clock_wait_line(just_updated, "node4", 180)
    nothing = cassandra_clock_probe(probe_text(active="", tracking_text=None))
    assert cassandra_clock_settled(nothing, 50)  # nothing runs to wait for
    with pytest.raises(AnsibleFilterError):
        cassandra_clock_settled("text")


def test_wait_line():
    fresh = cassandra_clock_probe(probe_text(tracking_text=tracking(0.0001, "fast", dispersion="62.040000000")))
    assert cassandra_clock_wait_line(fresh, "node4", 180) == u"node4: chronyd converging (\u00b162.04 s), waiting up to 3m"
    unsynced = cassandra_clock_probe(probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED))
    assert cassandra_clock_wait_line(unsynced, "node4", 60) == "node4: chronyd not synchronised yet, waiting up to 1m"
    slewing = cassandra_clock_probe(probe_text(tracking_text=tracking(0.2, "fast")))
    assert cassandra_clock_wait_line(slewing, "node4", 30) == "node4: chronyd correcting +200.0 ms, waiting up to 30s"
    with pytest.raises(AnsibleFilterError):
        cassandra_clock_wait_line("text", "node4", 30)


# --- verdicts -----------------------------------------------------------------------------------------------------

T0 = 1791936000.0


def node(name, text, skew=0.0, running=True, **more):
    """A node of the verdicts: its probe, three date samples skew seconds off the controller."""
    samples = ["%.6f %.6f" % (T0 + i, T0 + i + skew + 0.01 * (i + 1)) for i in range(3)]
    return dict({"node": name, "probe": cassandra_clock_probe(text), "samples": samples, "running": running,
                 "manage": True, "offline": False, "addresses": [name]}, **more)


def by_node(verdicts):
    return dict((v["node"], v) for v in verdicts["nodes"])


def test_verdicts_all_in_sync():
    v = cassandra_clock_verdicts([node("node1", probe_text()), node("node2", probe_text(tracking_text=tracking(0.004)))])
    assert v["ok"] and v["warnings"] == [] and [n["verdict"] for n in v["nodes"]] == ["ok", "ok"]
    assert all(n["action"] == "none" for n in v["nodes"])


def test_verdicts_threshold_counts_the_error_bound():
    # 48 ms off plus 0.7 ms of error bound: within 50; 49.5 + 0.7: over it
    inside = cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(0.048)))], max_ms=50)
    assert inside["ok"]
    over = cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(0.0495)))], max_ms=50)
    assert not over["ok"] and over["nodes"][0]["verdict"] == "off"
    assert cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(0.0495)))], max_ms=100)["ok"]


def test_verdicts_behind_and_ahead():
    v = by_node(cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(0.2))),
                                          node("node2", probe_text(tracking_text=tracking(0.2, "fast"))),
                                          node("node3", probe_text())]))
    assert v["node1"]["verdict"] == "off" and not v["node1"]["ahead"] and "behind its source by 200.0 ms" in v["node1"]["why"]
    assert v["node2"]["verdict"] == "off" and v["node2"]["ahead"] and "ahead of its source" in v["node2"]["why"]
    # within the step threshold (400 ms): chrony slews it, Cassandra running or not
    assert v["node1"]["action"] == v["node2"]["action"] == "slew"


def test_verdicts_ahead_warning_explains_future_timestamps():
    v = cassandra_clock_verdicts([node("node2", probe_text(tracking_text=tracking(1.5, "fast")))])
    assert len(v["warnings"]) == 1
    warning = v["warnings"][0]
    assert warning.startswith("node2's clock is AHEAD by about 1.50 s")
    assert "future timestamps" in warning and "loses against them" in warning and "latest timestamp wins" in warning
    behind = cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(1.5, "slow")))])
    assert not any("AHEAD" in w for w in behind["warnings"])
    # Cassandra not running there (a new node): it stamped nothing
    new = cassandra_clock_verdicts([node("node2", probe_text(tracking_text=tracking(1.5, "fast")), running=False)])
    assert not any("AHEAD" in w for w in new["warnings"])
    # ahead by the dates over SSH: only past what they can tell
    blank = probe_text(active="", tracking_text=None)
    near = cassandra_clock_verdicts([node("node1", probe_text()), node("node2", blank, skew=0.3)])
    assert not any("AHEAD" in w for w in near["warnings"])
    far = cassandra_clock_verdicts([node("node1", probe_text()), node("node2", blank, skew=3.0)])
    assert any(w.startswith("node2's clock is AHEAD by about 3.00 s") for w in far["warnings"])


def test_no_ahead_warning_for_a_wide_error_bound_alone():
    # S28 (R15): chronyd's error bound wide (just converging), the offset itself tiny: OFF, but no clock ahead,
    # no data-loss warning ("AHEAD by about 0.0 ms" before); the offset alone past the threshold keeps it
    wide = probe_text(tracking_text=tracking(0.00001, "fast", dispersion="51.980000000"))
    near = probe_text(tracking_text=tracking(0.0008, "fast", dispersion="0.055000000", skew="1000000.000"))
    v = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", wide), node("h-n3", near)])
    assert [n["verdict"] for n in v["nodes"]] == ["ok", "off", "off"]
    assert not any("AHEAD" in w for w in v["warnings"])
    # within the threshold alone, over it with the bound (0.04 + 0.02): no warning either
    edge = probe_text(tracking_text=tracking(0.04, "fast", dispersion="0.020000000"))
    assert not any("AHEAD" in w for w in cassandra_clock_verdicts([node("h-n2", edge)])["warnings"])
    # a real offset past the threshold, under a wide bound too: kept
    for text in (tracking(0.2, "fast"), tracking(0.2, "fast", dispersion="51.980000000")):
        ahead = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", probe_text(tracking_text=text))])
        assert [w.split("\n")[0] for w in ahead["warnings"]] == ["h-n2's clock is AHEAD by about 200.0 ms"]


def test_ahead_warning_one_fact_per_line_within_79_columns():
    v = cassandra_clock_verdicts([node("h-n2", probe_text(tracking_text=tracking(0.2, "fast")))])
    lines = cassandra_clock_health(v)["lines"]
    start = lines.index("WARNING  h-n2's clock is AHEAD by about 200.0 ms")
    assert lines[start + 1] == "         the writes it stamped as coordinator carry future timestamps"
    assert lines[-1] == "         check what was written through it lately"
    assert all(len(line) <= 79 for line in lines)


def test_verdicts_a_step_on_a_running_node_is_a_restart_window():
    v = by_node(cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(2.0)), running=True),
                                          node("node2", probe_text(tracking_text=tracking(2.0)), running=False)]))
    assert v["node1"]["action"] == "window"
    assert v["node2"]["action"] == "step"  # Cassandra not running there: stepped live


def test_verdicts_step_threshold():
    text = probe_text(tracking_text=tracking(0.3))
    assert cassandra_clock_verdicts([node("node1", text)], step_ms=400)["nodes"][0]["action"] == "slew"
    assert cassandra_clock_verdicts([node("node1", text)], step_ms=200)["nodes"][0]["action"] == "window"


def test_verdicts_unsynchronised_reasons():
    v = by_node(cassandra_clock_verdicts([
        node("node1", probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)),
        node("node2", probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_EMPTY, conf=())),
        node("node3", probe_text(tracking_text=TRACKING_LOCAL, sources=SOURCES_EMPTY))]))
    assert v["node1"]["verdict"] == "unsynchronised" and v["node1"]["why"] == "chronyd reaches none of its 2 sources"
    assert v["node2"]["why"] == "chronyd has no source configured"
    assert "own clock (local stratum)" in v["node3"]["why"]
    assert all(n["action"] == "manual" for n in v.values())  # the NTP config, by hand


def test_verdicts_no_time_sync():
    v = by_node(cassandra_clock_verdicts([
        node("node1", probe_text()),
        node("node2", probe_text(active="", installed=("chronyd",), tracking_text=None), running=False),
        node("node3", probe_text(active="", installed=("chronyd",), tracking_text=None, query="0.012"), running=True),
        node("node4", probe_text(active="", installed=("chronyd",), tracking_text=None, query="2.5"), running=True),
        node("node5", probe_text(active="", tracking_text=None), running=True, skew=0.9),
        node("node6", probe_text(active="", tracking_text=None), running=True, manage=False),
        node("node7", probe_text(active="", tracking_text=None), running=False, offline=True)]))
    assert v["node2"]["verdict"] == "no_sync" and v["node2"]["why"] == "chronyd installed, not running"
    assert v["node2"]["action"] == "step"  # Cassandra not running: started, then stepped
    assert v["node3"]["action"] == "start" and v["node3"]["estimate"] == pytest.approx(0.012)  # chronyd -Q: small
    assert v["node4"]["action"] == "window"  # 2.5 s: the start would step it under Cassandra
    # no chrony to ask: the dates against the synchronised node, 0.9 s apart: a window
    assert v["node5"]["why"] == "no time sync installed" and v["node5"]["action"] == "window"
    assert v["node5"]["ahead"] and v["node5"]["estimate"] == pytest.approx(0.9, abs=1e-3)
    assert v["node6"]["action"] == "manual" and "cassandra_linux_timesync false" in v["node6"]["why"]
    assert v["node7"]["action"] == "manual" and "cassandra_offline" in v["node7"]["why"]


def test_verdicts_unknown_offset_of_a_running_node_is_a_window():
    # no node synchronised to compare with, nothing to measure with: unknown, a window
    v = cassandra_clock_verdicts([node("node1", probe_text(active="", tracking_text=None), running=True)])
    assert v["nodes"][0]["action"] == "window" and v["nodes"][0]["estimate"] is None


def test_verdicts_container_coarse_comparison():
    container = probe_text(active="", tracking_text=None, virt="docker")
    v = cassandra_clock_verdicts([node("node1", container, skew=0.0), node("node2", container, skew=0.02)])
    assert v["ok"] and all(n["how"] == "coarse" for n in v["nodes"])
    assert any("coarse measure" in n for n in v["notes"])
    apart = by_node(cassandra_clock_verdicts([node("node1", probe_text()), node("node2", container, skew=1.5)]))
    assert apart["node2"]["verdict"] == "off" and apart["node2"]["action"] == "manual"
    assert "the host's clock (docker container)" in apart["node2"]["why"]
    # within a second: coarse only, accepted though over the threshold
    near = cassandra_clock_verdicts([node("node1", probe_text()), node("node2", container, skew=0.3)])
    assert near["ok"]
    host_unsynced = cassandra_clock_verdicts([node("node1", probe_text()),
                                              node("node2", container.replace("NTPSynchronized=yes", "NTPSynchronized=no"))])
    assert by_node(host_unsynced)["node2"]["verdict"] == "unsynchronised"


def test_verdicts_never_step_a_container_clock():
    container = probe_text(tracking_text=tracking(2.0), virt="docker")
    v = cassandra_clock_verdicts([node("node1", container, running=True)])
    assert v["nodes"][0]["action"] == "manual" and "never from a container" in v["nodes"][0]["why"]


def test_verdicts_unrelated_sources():
    a = probe_text(conf=("server ntp1.example.net", "server ntp2.example.net"))
    b = probe_text(conf=("server ntp2.example.net",)).replace("192.0.2.1 ", "192.0.2.9 ").replace("(192.0.2.1)", "(192.0.2.9)")
    c = probe_text(conf=("pool other.example.org",)).replace("192.0.2.1 ", "198.51.100.7 ").replace(
        "(192.0.2.1)", "(198.51.100.7)")
    related = cassandra_clock_verdicts([node("node1", a), node("node2", b)])
    assert related["warnings"] == []  # a configured server in common
    unrelated = cassandra_clock_verdicts([node("node1", a), node("node2", b), node("node3", c)])
    assert unrelated["ok"]  # each within its source: a warning, not a refusal
    assert len(unrelated["warnings"]) == 1 and "unrelated time sources" in unrelated["warnings"][0]
    assert "node1, node2:" in unrelated["warnings"][0] and "node3: 198.51.100.7, other.example.org" in unrelated["warnings"][0]


def test_verdicts_a_node_following_another_node_is_related():
    a = probe_text(conf=("server ntp1.example.net",))
    b = probe_text(conf=("server node1",)).replace("192.0.2.1 ", "10.0.0.1 ").replace("(192.0.2.1)", "(10.0.0.1)")
    v = cassandra_clock_verdicts([node("node1", a, addresses=["node1", "10.0.0.1"]), node("node2", b)])
    assert v["warnings"] == []


def test_verdicts_leap_second_and_timesyncd_notes():
    leap = probe_text(tracking_text=tracking(0.001, leap="Insert second"))
    timesyncd = probe_text(active="systemd-timesyncd", tracking_text=None, conf=(), timesync=TIMESYNC)
    v = cassandra_clock_verdicts([node("node1", leap, addresses=["node1"]), node("node2", timesyncd)])
    assert any("leap second is pending on node1" in w for w in v["warnings"])
    assert any("systemd-timesyncd on node2" in n and "steps the clock" in n for n in v["notes"])


def test_verdicts_error_bound_alone_over_the_threshold():
    v = cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(0.001, delay="0.150000000")))])
    assert v["nodes"][0]["verdict"] == "off" and v["nodes"][0]["action"] == "manual"
    assert "error bound alone" in v["nodes"][0]["why"]


def test_verdicts_a_daemon_still_converging_is_said_so():
    young = probe_text(tracking_text=tracking(0.0001, "fast", dispersion="62.040000000", skew="1000000.000"))
    v = cassandra_clock_verdicts([node("node1", young)])
    assert v["nodes"][0]["verdict"] == "off" and v["nodes"][0]["action"] == "manual"
    # what the daemon said, no guessed cause (R16: no start seen in what was read)
    assert v["nodes"][0]["why"] == (
        "chronyd's error bound is 62.04 s, over 50.0 ms; chronyd's frequency skew is 1000000 ppm (settled: a few"
        " ppm); run again later")
    assert "closer or better" not in v["nodes"][0]["why"]
    # a wide bound with a settled skew: the bound alone said
    wide = probe_text(tracking_text=tracking(0.0001, "fast", dispersion="62.040000000"))
    why = cassandra_clock_verdicts([node("node1", wide)])["nodes"][0]["why"]
    assert why.startswith("chronyd's error bound is 62.04 s, over 50.0 ms; run again") and "skew" not in why
    assert not any("just started" in n["why"] or "converging" in n["why"]
                   for n in cassandra_clock_verdicts([node("n1", young), node("n2", wide)])["nodes"])


def test_off_rows_one_fact_per_line_within_79_columns():
    # S28 (R16): 199 columns before; now the row, then each fact under its text, the action (sync_clocks) last
    young = probe_text(tracking_text=tracking(0.0013, "slow", dispersion="0.054300000", skew="1000000.000"))
    v = cassandra_clock_verdicts([node("h-n1", young), node("h-n2", probe_text())])
    assert cassandra_clock_lines(v) == [
        "  h-n1  OFF  chronyd, synchronised to 192.0.2.1, -1.3 ms (\u00b154.7 ms)",
        "             chronyd's error bound is 54.7 ms, over 50.0 ms",
        "             chronyd's frequency skew is 1000000 ppm (settled: a few ppm)",
        "             run again later",
        "             \u2192 to fix by hand",
        "  h-n2  ok   chronyd, synchronised to 192.0.2.1, -0.0 ms (\u00b10.7 ms)"]
    assert all(len(line) <= 77 for line in cassandra_clock_lines(v, width=77))  # (sync_clocks' screen: 2 in)
    refusal = cassandra_clock_refusal(v, operation="create_cluster", cluster="zorglub")
    assert all(len(line) <= 79 for line in refusal + cassandra_clock_health(v)["lines"]), refusal


def test_the_longer_wait_is_said_only_where_a_wait_is():
    # S28 audit (A1): health_check only reads, sync_clocks' reading leaves a converging daemon to the operator
    # ("manual"): "run again later" there; a start's refusal after a wait on cassandra_clock_sync_timeout
    # (clocks_check.yml: waited), as sync_clocks' clock_fix.yml (test_clocks_check.py); audit 12 (domain A1,
    # adversarial B2): no wait ran (--check, create_cluster's check before the wipe: later_ok), no timeout said
    young = probe_text(tracking_text=tracking(0.0013, "slow", dispersion="0.054300000", skew="1000000.000"))
    v = cassandra_clock_verdicts([node("h-n1", young), node("h-n2", probe_text())])
    assert "             run again later, or raise cassandra_clock_sync_timeout" in cassandra_clock_refusal(v, waited=True)
    for kwargs in ({}, {"check": True}, {"later_ok": True}):
        lines = cassandra_clock_refusal(v, **kwargs)
        assert "             run again later" in lines and "cassandra_clock_sync_timeout" not in "\n".join(lines), kwargs
    for lines in (cassandra_clock_health(v)["lines"], cassandra_clock_lines(v), cassandra_clock_lines(v, width=77)):
        assert "             run again later" in lines and "cassandra_clock_sync_timeout" not in "\n".join(lines)
    # a far source (a settled skew): no wait helps, neither way out
    far = cassandra_clock_verdicts([node("h-n1", probe_text(tracking_text=tracking(0.001, delay="0.150000000")))])
    assert "run again later" not in "\n".join(cassandra_clock_refusal(far) + cassandra_clock_health(far)["lines"])


@pytest.mark.parametrize("skew, bound, settled", [
    ("150.000", "0.060000000", False),  # its skew still over 100 ppm: converging, waited for
    ("90.000", "0.060000000", True),  # a settled skew, a bound over half the threshold: a far source, at once
    ("0.012", "0.900000000", True),  # a bound under a second: a far source
    ("0.012", "1.100000000", False),  # a bound over a second (and ten times the threshold): converging
])
def test_converging_thresholds(skew, bound, settled):
    probe = cassandra_clock_probe(probe_text(tracking_text=tracking(0.0001, "fast", dispersion=bound, skew=skew)))
    assert cassandra_clock_settled(probe, 50) is settled


def test_verdicts_coarse_skew_takes_the_least_delayed_sample():
    samples = ["%f %f" % (T0, T0 + 0.300), "%f %f" % (T0 + 1, T0 + 1.020), "garbage"]
    v = cassandra_clock_verdicts([dict(node("node1", probe_text()), samples=samples)])
    assert v["nodes"][0]["coarse"] == pytest.approx(0.0, abs=1e-4)  # the reference: itself (its offset 12 us)


# --- the table and the refusal ------------------------------------------------------------------------------------


def test_lines():
    v = cassandra_clock_verdicts([node("node1", probe_text()), node("node22", probe_text(tracking_text=tracking(2.0, "fast")))])
    lines = cassandra_clock_lines(v)
    assert lines[0].startswith("  node1   ok   chronyd, synchronised to 192.0.2.1, -0.0 ms (\u00b10.7 ms)")
    assert lines[1] == "  node22  OFF  chronyd, synchronised to 192.0.2.1, +2.00 s (\u00b10.7 ms)"
    assert lines[2] == "               ahead of its source by 2.00 s, more than 50.0 ms"
    assert lines[3] == "               \u2192 clock stepped in a restart window (drain, stop, step, start)"
    assert cassandra_clock_lines(v, actions=False)[2:] == ["               ahead of its source by 2.00 s, more than 50.0 ms"]


def test_refusal():
    v = cassandra_clock_verdicts([node("node1", probe_text()),
                                  node("node2", probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)),
                                  node("node3", probe_text(tracking_text=tracking(0.2, "fast")))])
    lines = cassandra_clock_refusal(v, operation="create_cluster", cluster="Orders",
                                    command="ansible-playbook -i inv.yml community.cassandra.sync_clocks")
    assert lines[0] == "REFUSED  clocks  Orders  nothing was started"
    assert lines[1] == "  node2  NOT SYNCHRONISED  chronyd, NOT synchronised"
    assert lines[2].startswith("                           chronyd reaches none of its 2")
    assert lines[3].startswith("  node3  OFF")
    assert not any(line.startswith("  node1 ") for line in lines)
    text = "\n".join(lines)
    # (true for a node judged by the dates too: no "of its source", audit adversarial C5; of what: audit 12 C4)
    assert lines[lines.index("Cassandra needs each clock within 50.0 ms of true time") + 1] == (
        "  (cassandra_clock_max_offset_ms)")
    assert "WARNING  node3's clock is AHEAD" in text
    assert "     ansible-playbook -i inv.yml community.cassandra.sync_clocks" in lines
    assert "then run create_cluster again" in text and "-e cassandra_clock_check=false" in text
    assert all(len(line) <= 79 for line in lines)
    assert cassandra_clock_refusal(cassandra_clock_verdicts([node("node1", probe_text())])) == []


def test_refusal_under_check():
    # --check started nothing: a node cassandra_linux would set up is left to the real run
    blank = probe_text(active="", tracking_text=None)
    v = cassandra_clock_verdicts([node("node1", blank, running=False), node("node2", blank, running=False)])
    assert cassandra_clock_refusal(v, check=True) == [
        "NOTE  clocks: time sync would be started on node1, node2 (cassandra_linux), its",
        "  clock checked by the real run"]
    assert cassandra_clock_refusal(v)[0] == "REFUSED  clocks  nothing was started"
    off = cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(1.0)))])
    assert cassandra_clock_refusal(off, check=True)[0] == "NOTE  clocks  a real run would refuse to start the nodes"


def test_health_block_every_clock_in_sync():
    # health_check's CLOCKS block: what in sync means, sync_clocks' table without its actions, no finding
    v = cassandra_clock_verdicts([node("node1", probe_text()), node("node2", probe_text(tracking_text=tracking(0.004)))])
    health = cassandra_clock_health(v, command="ansible-playbook -i inv.yml community.cassandra.sync_clocks")
    assert health["finding"] is None
    assert health["lines"] == [
        "CLOCKS  in sync: synchronised to a source, offset plus the daemon's error",
        "        bound within 50 ms (cassandra_clock_max_offset_ms)",
        "  node1  ok  chronyd, synchronised to 192.0.2.1, -0.0 ms (\u00b10.7 ms)",
        "  node2  ok  chronyd, synchronised to 192.0.2.1, -4.0 ms (\u00b10.7 ms)"]
    assert cassandra_clock_health(cassandra_clock_verdicts([node("node1", probe_text())], max_ms=120))["lines"][1].endswith(
        "within 120 ms (cassandra_clock_max_offset_ms)")
    # health_check's heading within 79 columns (124 in S28)
    assert all(len(line) <= 79 for line in health["lines"][:2])


def test_health_block_a_clock_off_or_not_read_is_a_finding():
    # the same verdict words as sync_clocks and the start refusal; the nodes not read listed last; the finding
    # names them with sync_clocks --check as the way out
    v = cassandra_clock_verdicts([node("node1", probe_text()), node("node2", probe_text(tracking_text=tracking(0.2, "fast"))),
                                  node("node3", probe_text(active="", installed=("chronyd",), tracking_text=None))])
    health = cassandra_clock_health(v, unread={"node4": "MODULE FAILURE"},
                                    command="ansible-playbook -i inv.yml community.cassandra.sync_clocks")
    lines = health["lines"][1:]  # (after the heading's first line)
    assert lines[1] == "  node1  ok            chronyd, synchronised to 192.0.2.1, -0.0 ms (\u00b10.7 ms)"
    assert lines[2] == "  node2  OFF           chronyd, synchronised to 192.0.2.1, +200.0 ms (\u00b10.7 ms)"
    assert lines[3] == "                       ahead of its source by 200.0 ms, more than 50.0 ms"
    assert lines[4].startswith("  node3  NO TIME SYNC  chronyd installed, not running")
    assert lines[6] == "  node4  NOT READ      its clock could not be read: MODULE FAILURE"
    assert lines[7] == "WARNING  node2's clock is AHEAD by about 200.0 ms"  # Cassandra runs there
    assert "\u2192" not in "\n".join(lines)  # no action: health_check does nothing
    assert health["finding"] == {
        "kind": "clocks", "text": "node2 OFF, node3 NO TIME SYNC, node4 NOT READ (CLOCKS above says why)",
        "command": "ansible-playbook -i inv.yml community.cassandra.sync_clocks --check"}
    assert cassandra_clock_health({}, unread={"node1": ""})["finding"]["command"] == (
        "ansible-playbook community.cassandra.sync_clocks --check")


# --- found by the audit -------------------------------------------------------------------------------------------


def test_sources_with_a_negative_poll():
    line = "^* 192.0.2.1                     1  -4   377     1    -12us[  -15us] +/-  410us"
    assert parse_chrony_sources([line])["selected"] == "192.0.2.1"


def test_timesync_leap_sentences_and_delay_in_the_bound():
    sync = parse_timesync(TIMESYNC.replace("Leap: normal", "Leap: last minute of the day has an extra second").splitlines())
    assert sync["leap"] == "insert"
    assert parse_timesync(TIMESYNC.replace("Leap: normal", "Leap: not synchronized").splitlines())["leap"] == "unsynchronised"
    assert parse_timesync(TIMESYNC.splitlines())["bound"] == pytest.approx(0.001981 + 0.001695 / 2)


def test_ntpd_on_its_local_clock_is_not_synchronised():
    local = NTPQ.replace("*192.0.2.1       .GPS.            1 u", "*127.127.1.0     .LOCL.          10 l")
    p = cassandra_clock_probe(probe_text(active="ntpd", tracking_text=None, conf=(), ntpq=local))
    assert not p["synchronised"] and p["local"]
    v = cassandra_clock_verdicts([node("node1", probe_text(active="ntpd", tracking_text=None, conf=(), ntpq=local))])
    assert "own clock (local stratum)" in v["nodes"][0]["why"]


def test_a_live_start_respects_the_sites_makestep():
    # chronyd -Q measured 200 ms: with makestep 0.1 a start would step it under Cassandra, a window
    blank = dict(active="", installed=("chronyd",), tracking_text=None, query="0.2")
    assert cassandra_clock_verdicts([node("node1", probe_text(**blank))])["nodes"][0]["action"] == "start"
    tight = probe_text(conf=("pool pool.example.net", "makestep 0.1 3"), **blank)
    assert cassandra_clock_verdicts([node("node1", tight)])["nodes"][0]["action"] == "window"
    # only the dates over SSH to go by: too coarse for a start under Cassandra
    coarse = probe_text(active="", installed=("chronyd",), tracking_text=None)
    v = cassandra_clock_verdicts([node("node0", probe_text()), node("node1", coarse, skew=0.01)])
    assert by_node(v)["node1"]["action"] == "window"


def test_timesyncd_slews_slowly_and_steps_past_400_ms():
    ts = dict(active="systemd-timesyncd", tracking_text=None, conf=())
    slow = cassandra_clock_verdicts([node("node1", probe_text(timesync=TIMESYNC.replace("+2.513ms", "+200ms"), **ts))])
    assert slow["nodes"][0]["action"] == "slew" and "about 7 min" in slow["nodes"][0]["why"]
    far = cassandra_clock_verdicts([node("node1", probe_text(timesync=TIMESYNC.replace("+2.513ms", "+600ms"), **ts))],
                                   step_ms=1000)
    assert far["nodes"][0]["action"] == "window"


def test_a_clock_synchronised_by_another_means():
    # no chrony, timesyncd or ntpd, the kernel synchronised (PTP, the hypervisor's): not another daemon, compared coarsely
    other = probe_text(active="", tracking_text=None, synced="yes")
    v = by_node(cassandra_clock_verdicts([node("node1", probe_text()), node("node2", other, skew=0.01)]))
    assert v["node2"]["verdict"] == "ok" and v["node2"]["action"] == "none"
    assert "synchronised by another means" in v["node2"]["text"]
    far = by_node(cassandra_clock_verdicts([node("node1", probe_text()), node("node2", other, skew=2.0)]))
    assert far["node2"]["verdict"] == "off" and far["node2"]["action"] == "manual"
    unsynced = by_node(cassandra_clock_verdicts([node("node1", probe_text()),
                                                 node("node2", other.replace("NTPSynchronized=yes", "NTPSynchronized=no"))]))
    assert unsynced["node2"]["verdict"] == "no_sync"


# --- the recap of sync_clocks -------------------------------------------------------------------------------------


def test_recap_of_sync_clocks():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import cassandra_op_recap
    lines = cassandra_op_recap([{"node": "node1", "result": "already in sync"},
                                {"node": "node2", "result": "clock_start_step done in 12s"},
                                {"node": "node3", "result": "sync_clock done in 95s"},
                                {"node": "node4", "result": "clock_slewing done in 1s"},
                                {"node": "node5", "result": "sync_clock refused: chronyd reaches none of its 2 sources"
                                                            " (to fix by hand)"}],
                               cluster="Orders", operation="sync_clocks")
    text = "\n".join(lines)
    assert lines[0].startswith("FAILED  sync_clocks  Orders")
    assert "time sync started, clock stepped" in text and "clock set in a restart window" in text
    assert "clock being corrected by its daemon (minutes)" in text
    assert "node5  FAILED\n    refused: chronyd reaches none of its 2 sources (to fix by hand)" in text
    assert "node1  not touched (already in sync)" in text
    check = cassandra_op_recap([{"node": "node1", "result": "already in sync"},
                                {"node": "node2", "result": "would set the clock in a restart window (--check: nothing"
                                                            " was changed)"},
                                {"node": "node3", "result": "would need a fix by hand: chronyd has no source configured"}],
                               check=True, cluster="Orders", operation="sync_clocks")
    assert check[0].startswith("CHECK  sync_clocks  Orders") and "FAILED" not in "\n".join(check)


def test_refusal_leaves_to_later_only_the_nodes_cassandra_linux_sets_up():
    # a running node with no time sync: cassandra_linux never touches it, --check and the check before a wipe refuse it
    blank = probe_text(active="", tracking_text=None)
    v = cassandra_clock_verdicts([node("new1", blank, running=False), node("old1", blank, running=True)])
    lines = cassandra_clock_refusal(v, check=True)
    assert any(line.startswith("  old1  NO TIME SYNC") for line in lines)
    assert not any(line.startswith("  new1 ") for line in lines)
    # before a wipe (create_cluster's reset): every node is stopped then set up by cassandra_linux, running or not
    assert cassandra_clock_refusal(v, later_ok=True) == []
    off = cassandra_clock_verdicts([node("old1", probe_text(tracking_text=tracking(1.0)), running=True)])
    assert cassandra_clock_refusal(off, later_ok=True)[0] == "REFUSED  clocks  nothing was started"


def test_refusal_names_a_node_whose_clock_could_not_be_read():
    v = cassandra_clock_verdicts([node("node1", probe_text())])
    lines = cassandra_clock_refusal(v, unread={"node2": "/bin/sh: not found"})
    assert lines[0] == "REFUSED  clocks  nothing was started"
    assert lines[1] == "  node2  NOT READ  its clock could not be read: /bin/sh: not found"


def test_a_daemon_stopped_and_disabled_is_not_another_means():
    # the kernel keeps its synchronised flag for hours after a daemon stops
    stopped = probe_text(active="", installed=("systemd-timesyncd",), tracking_text=None, synced="yes").replace(
        "systemd-timesyncd|inactive|enabled|loaded", "systemd-timesyncd|inactive|disabled|loaded")
    v = cassandra_clock_verdicts([node("node1", stopped, running=False)])
    assert v["nodes"][0]["verdict"] == "no_sync"


def test_ntpd_is_only_read():
    stopped = probe_text(active="", installed=("ntpd",), tracking_text=None)
    v = cassandra_clock_verdicts([node("node1", stopped, running=False)])
    assert v["nodes"][0]["action"] == "manual" and "start ntpd" in v["nodes"][0]["why"]
    far = NTPQ.replace("-3.250", "900.000")
    v = cassandra_clock_verdicts([node("node1", probe_text(active="ntpd", tracking_text=None, conf=(), ntpq=far))])
    assert v["nodes"][0]["action"] == "manual" and "its start (-g)" in v["nodes"][0]["why"]


def test_the_variable_that_leaves_time_sync_alone_is_named():
    blank = probe_text(active="", tracking_text=None)
    v = cassandra_clock_verdicts([node("node1", blank, manage=False, unmanaged_by="cassandra_linux_manage")])
    assert "cassandra_linux_manage false" in v["nodes"][0]["why"]


def test_old_systemd_timesyncd_kernel_only():
    # no timedatectl show, no timesync-status (systemd 237): the kernel's word, compared coarsely
    text = probe_text(active="systemd-timesyncd", tracking_text=None, conf=()).replace(
        "==timedatectl==\nTimezone=UTC\nNTP=yes\nNTPSynchronized=yes",
        "==timedatectl_status==\n               Local time: Wed 2026-10-14 10:00:00 UTC\nSystem clock synchronized: yes")
    p = cassandra_clock_probe(text)
    assert p["kernel_only"] and p["kernel_synced"] is True
    v = cassandra_clock_verdicts([node("node1", probe_text()), node("node2", text, skew=0.01)])
    assert by_node(v)["node2"]["verdict"] == "ok" and "this systemd tells no offset" in by_node(v)["node2"]["text"]


def test_a_daemon_disabled_on_a_node_left_alone_is_another_means():
    # PTP: chronyd disabled, phc2sys sets the clock, cassandra_linux_timesync false: not a missing time sync
    ptp = probe_text(active="", installed=("chronyd",), tracking_text=None, synced="yes").replace(
        "chronyd|inactive|enabled|loaded", "chronyd|inactive|disabled|loaded")
    v = cassandra_clock_verdicts([node("node0", probe_text()), node("node1", ptp, manage=False)])
    assert by_node(v)["node1"]["verdict"] == "ok" and "another means" in by_node(v)["node1"]["text"]
    assert "chronyd disabled, its time sync left alone" in by_node(v)["node1"]["text"]
    managed = cassandra_clock_verdicts([node("node0", probe_text()), node("node1", ptp)])
    assert by_node(managed)["node1"]["verdict"] == "no_sync"
    # only stopped (still enabled) on a node left alone: the kernel's flag outlives it, not another means
    stopped = probe_text(active="", installed=("chronyd",), tracking_text=None, synced="yes")
    left = by_node(cassandra_clock_verdicts([node("node0", probe_text()), node("node1", stopped, manage=False)]))
    assert left["node1"]["verdict"] == "no_sync" and left["node1"]["action"] == "manual"


@pytest.mark.parametrize("did", [["start"], ["step"], ["start", "step"], ["slewing"], ["start", "slewing"],
                                 ["step", "slewing"], ["start", "step", "slewing"], []])
def test_every_live_result_has_its_words(did):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import DONE, PLAYBOOKS
    action = "clock_" + ("_".join(did) or "slew")  # sync_clocks.yml's "Record the result"
    assert action in DONE and PLAYBOOKS[action] == "sync_clocks"


def test_verdict_within_the_threshold_alone_says_the_error_bound():
    # 25 ms off, an error bound of 40 ms (half the root delay plus the dispersion): over 50 together, not alone
    v = by_node(cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(0.025, dispersion="0.039600000"))),
                                          node("node2", probe_text(tracking_text=tracking(0.025, "fast", dispersion="0.039600000")))],
                                         max_ms=50))
    assert v["node1"]["verdict"] == "off"
    assert v["node1"]["why"] == "25.0 ms behind its source, within 50.0 ms but not with its error bound of 40.0 ms"
    assert v["node2"]["why"] == "25.0 ms ahead of its source, within 50.0 ms but not with its error bound of 40.0 ms"
    assert v["node1"]["action"] == "slew"


def aheads(verdicts):
    return [w.split("\n")[0] for w in verdicts["warnings"] if "AHEAD" in w]


def test_an_unsynchronised_clock_ahead_by_the_dates_is_warned_about():
    # S28 audit (B1): chronyd reaches none of its sources, the dates over SSH put the node 10 s ahead of the
    # synchronised ones, Cassandra runs there: it stamps writes in the future, said; its verdict unchanged
    lost = probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)
    v = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", lost, skew=10.0)])
    assert by_node(v)["h-n2"]["verdict"] == "unsynchronised" and by_node(v)["h-n2"]["action"] == "manual"
    assert aheads(v) == ["h-n2's clock is AHEAD by about 10.00 s"]
    assert v["warnings"][0].split("\n")[1] == "by the dates over SSH, against h-n1"  # the full gap to it
    # behind, within what the dates can tell, Cassandra not running, or no synchronised node to compare with
    for nodes in ([node("h-n1", probe_text()), node("h-n2", lost, skew=-10.0)],
                  [node("h-n1", probe_text()), node("h-n2", lost, skew=0.5)],
                  [node("h-n1", probe_text()), node("h-n2", lost, skew=10.0, running=False)],
                  [node("h-n1", lost), node("h-n2", lost, skew=10.0)]):
        assert aheads(cassandra_clock_verdicts(nodes)) == []


def test_a_synchronised_clock_is_judged_by_its_daemon_alone():
    # S28 audit 11 (domain A1/A2/B1, adversarial B1/C2): the dates over SSH include the SSH delay; a node whose
    # daemon is synchronised gets no AHEAD from them: neither a node on another source 5 s ahead (2 or 3 nodes:
    # not detected by the dates, the guide says so), nor 1.3 s of SSH delay on a node of the same source
    for nodes in ([node("h-n1", probe_text()), node("h-n2", probe_text()), node("h-n3", probe_text(), skew=5.0)],
                  [node("h-n1", probe_text()), node("h-n2", probe_text(), skew=5.0)],
                  [node("h-n1", probe_text(), skew=0.05), node("h-n2", probe_text(), skew=0.06),
                   node("h-n3", probe_text(), skew=1.3)]):
        v = cassandra_clock_verdicts(nodes)
        assert v["ok"] and aheads(v) == [], aheads(v)
    # past the threshold by its daemon: its offset (200 ms), not the dates (5.2 s), and no dates line
    for skew in (0.2, 5.2):
        off = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", probe_text()),
                                        node("h-n3", probe_text(tracking_text=tracking(0.2, "fast")), skew=skew)])
        assert aheads(off) == ["h-n3's clock is AHEAD by about 200.0 ms"]
        assert "dates over SSH" not in off["warnings"][0]


def test_an_unsynchronised_clock_is_compared_with_the_other_synchronised_nodes():
    # S28 audit 11 (domain A1): the dates of a daemon not synchronised against the other synchronised nodes,
    # named; with one of them, the full gap to it (not half); with several, their median, said (audit 12 C2)
    lost = probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)
    two = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", lost, skew=10.0)])
    assert two["warnings"][0].split("\n")[:2] == ["h-n2's clock is AHEAD by about 10.00 s",
                                                  "by the dates over SSH, against h-n1"]
    four = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", probe_text(), skew=0.1),
                                     node("h-n3", probe_text(), skew=-0.1), node("h-n4", lost, skew=10.0)])
    assert four["warnings"][0].split("\n")[:2] == ["h-n4's clock is AHEAD by about 10.00 s",
                                                   "by the dates over SSH, against the median of h-n1, h-n2, h-n3"]
    # more nodes than a line names: their count
    many = cassandra_clock_verdicts([node("h-n%d" % i, probe_text()) for i in range(1, 6)] + [node("h-n6", lost, skew=10.0)])
    assert many["warnings"][0].split("\n")[1] == "by the dates over SSH, against the median of 5 synchronised nodes"
    # none synchronised, or the node behind: nothing
    for nodes in ([node("h-n1", lost), node("h-n2", lost, skew=10.0)],
                  [node("h-n1", probe_text()), node("h-n2", probe_text()), node("h-n3", lost, skew=-10.0)]):
        assert aheads(cassandra_clock_verdicts(nodes)) == []


def test_containers_alone_are_compared_with_each_other():
    # no daemon on any node (containers): the other nodes' dates; 2 containers, one 3 s ahead: the full gap; audit
    # 12 (domain B2): the row says the same figure as the warning (one clock, one figure), its verdict unchanged
    box = probe_text(active="", tracking_text=None, virt="docker")
    v = cassandra_clock_verdicts([node("h-n1", box), node("h-n2", box, skew=3.0)])
    assert [w.split("\n")[:2] for w in v["warnings"] if "AHEAD" in w] == [
        ["h-n2's clock is AHEAD by about 3.00 s", "by the dates over SSH, against h-n1"]]
    assert [(n["verdict"], n["coarse"]) for n in v["nodes"]] == [("off", -1.5), ("off", 1.5)]  # (judged as before)
    rows = cassandra_clock_lines(v, actions=False)
    assert rows[4:8] == ["  h-n2  OFF  the host's clock (docker container), +3.00 s vs the other",
                         "               containers (coarse)",
                         "             the host's clock (docker container), ahead of the other containers",
                         "               by about 3.00 s (dates over SSH, coarse)"]


def test_two_containers_over_a_second_apart_get_the_warning():
    # S28 audit 12 (domain B1): 1.6 s apart, each 0.8 s from their mean: both ok (unchanged), but 1.6 s of future
    # timestamps: the warning, with the figure of its row
    box = probe_text(active="", tracking_text=None, virt="docker")
    v = cassandra_clock_verdicts([node("a", box), node("b", box, skew=1.6)])
    assert v["ok"] and [n["coarse"] for n in v["nodes"]] == [pytest.approx(-0.8), pytest.approx(0.8)]
    assert [w.split("\n")[:2] for w in v["warnings"]] == [["b's clock is AHEAD by about 1.60 s",
                                                          "by the dates over SSH, against a"]]
    assert cassandra_clock_lines(v)[2] == "  b  ok  the host's clock (docker container), +1.60 s vs the other containers"
    # within a second: neither
    near = cassandra_clock_verdicts([node("a", box), node("b", box, skew=0.9)])
    assert near["ok"] and aheads(near) == []
    # 3 containers at 0, 1 and 3 s: each row against the median of the others, the warning the same figure
    three = cassandra_clock_verdicts([node("a", box), node("b", box, skew=1.0), node("c", box, skew=3.0)])
    assert [n["verdict"] for n in three["nodes"]] == ["ok", "ok", "off"]
    assert [n["gap"] for n in three["nodes"]] == [pytest.approx(-2.0), pytest.approx(-0.5), pytest.approx(2.5)]
    assert [w.split("\n")[:2] for w in three["warnings"]] == [["c's clock is AHEAD by about 2.50 s",
                                                              "by the dates over SSH, against the median of a, b"]]
    assert "  c  OFF  the host's clock (docker container), +2.50 s vs the other containers" in cassandra_clock_lines(three)
    # 5 containers: past 3 names, their count, not called synchronised
    five = cassandra_clock_verdicts([node("h-n%d" % i, box) for i in range(1, 5)] + [node("h-n5", box, skew=3.0)])
    assert five["warnings"][0].split("\n")[1] == "by the dates over SSH, against the median of 4 other nodes"


def test_containers_are_never_compared_with_a_node_without_time_sync():
    # S28 audit 13 (domain B2): no synchronised node, a VM with no time sync 1.6 s slow (or not synchronised) and a
    # correct container: the container is compared with the other containers only, never with the clock known
    # to drift: no AHEAD, no figure on its row; its verdict as before (judged against the median of all)
    box = probe_text(active="", tracking_text=None, virt="docker")
    stopped = probe_text(active="", installed=("chrony",), tracking_text=None)
    lost = probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)
    for vm in (stopped, lost):
        v = cassandra_clock_verdicts([node("a", vm, skew=-1.6), node("b", box)])
        assert aheads(v) == [] and by_node(v)["b"]["verdict"] == "ok" and by_node(v)["b"]["gap"] is None
        assert by_node(v)["b"]["text"] == "the host's clock (docker container)"
    # 3 s slow: the container's verdict OFF as before (the median of the two), said with the figure it was
    # judged by, no warning
    v = cassandra_clock_verdicts([node("a", stopped, skew=-3.0), node("b", box)])
    assert by_node(v)["b"]["verdict"] == "off" and by_node(v)["b"]["coarse"] == pytest.approx(1.5) and aheads(v) == []
    assert by_node(v)["b"]["why"] == ("the host's clock (docker container), ahead of the median of all the nodes by"
                                      " about 1.50 s (dates over SSH, coarse)")
    # a VM with no time sync and two containers 1.6 s apart: the warning against the other container alone
    v = cassandra_clock_verdicts([node("a", stopped), node("b", box, skew=1.6), node("c", box)])
    assert [w.split("\n")[:2] for w in v["warnings"]] == [["b's clock is AHEAD by about 1.60 s",
                                                          "by the dates over SSH, against c"]]
    assert [by_node(v)[n]["gap"] for n in "bc"] == [pytest.approx(1.6), pytest.approx(-1.6)]


def test_chronyd_q_and_dates_over_ssh_are_never_cut():
    # S28 audit 13 (adversarial C1, domain C3): a NO TIME SYNC row cut inside "chronyd -Q" or "dates over SSH"
    # left "-Q)" or "SSH, coarse)" alone on a line: each kept whole, a normal space in it once wrapped
    stopped = probe_text(active="", installed=("chrony",), tracking_text=None, query="0.8")
    lines = cassandra_clock_lines(cassandra_clock_verdicts([node("a", probe_text()), node("c", stopped)]),
                                  actions=False)
    assert lines[1:3] == ["  c  NO TIME SYNC  chrony installed, not running (about 800.0 ms off,",
                          "                     chronyd -Q)"], lines
    # a long name: the text 6 columns in, "dates over SSH, coarse)" whole on the next line
    timesyncd = probe_text(active="", installed=("systemd-timesyncd",), tracking_text=None)
    lines = cassandra_clock_lines(cassandra_clock_verdicts([node("h-n1", probe_text()), node(LONG, probe_text()),
                                                            node("h-n3", timesyncd, skew=0.15)]), actions=False)
    assert "        dates over SSH, coarse)" in lines and all(u"\xa0" not in line for line in lines), lines


def test_no_time_sync_measured_by_chronyd_q_is_judged_by_that_measure():
    # S28 audit 12 (domain B3): no daemon running, chronyd -Q measured the offset ("wrong by -0.8": 800 ms ahead,
    # as apply_config.yml reads it): the AHEAD warning by it over cassandra_clock_max_offset_ms, as a daemon's
    # offset, whatever the dates over SSH say; the dates only when there is no such measure
    def measured(query, skew):
        blank = probe_text(active="", installed=("chrony",), tracking_text=None, query=query)
        return cassandra_clock_verdicts([node("a", probe_text()), node("b", probe_text()), node("c", blank, skew=skew)])
    small = measured("-0.005", 1.3)  # 5 ms by chronyd -Q, 1.3 s of SSH delay in the dates: no warning
    assert aheads(small) == [] and by_node(small)["c"]["verdict"] == "no_sync"
    far = measured("-0.8", 0.0)  # 800 ms ahead by chronyd -Q (under the dates' second, and not in them): warned
    assert far["warnings"][0].split("\n")[:2] == ["c's clock is AHEAD by about 800.0 ms", "by chronyd -Q"]
    assert "dates over SSH" not in far["warnings"][0]
    assert "next 800.0 ms loses" in far["warnings"][0].replace("\n", " ")
    assert by_node(far)["c"]["verdict"] == "no_sync" and by_node(far)["c"]["estimate"] == pytest.approx(0.8)
    assert aheads(measured("0.8", 5.0)) == []  # behind by chronyd -Q, whatever the dates
    assert aheads(measured("-0.03", 0.0)) == []  # within the threshold
    # no chronyd -Q measure: the dates, past what they can tell
    blank = probe_text(active="", installed=("chrony",), tracking_text=None)
    dated = cassandra_clock_verdicts([node("a", probe_text()), node("c", blank, skew=1.3)])
    assert dated["warnings"][0].split("\n")[:2] == ["c's clock is AHEAD by about 1.30 s",
                                                    "by the dates over SSH, against a"]
    assert aheads(cassandra_clock_verdicts([node("a", probe_text()), node("c", blank, skew=0.8)])) == []


def test_a_behind_clock_or_a_new_node_gets_no_ahead_warning():
    # S28 audit: behind past the threshold (its daemon, or the dates), Cassandra not running: no AHEAD
    blank = probe_text(active="", tracking_text=None)
    for nodes in ([node("h-n1", probe_text(tracking_text=tracking(0.2, "slow")), skew=-0.2)],
                  [node("h-n1", probe_text()), node("h-n2", blank, skew=-3.0)],
                  [node("h-n1", probe_text()), node("h-n2", probe_text(tracking_text=tracking(0.2, "fast")), running=False)],
                  [node("h-n1", probe_text()), node("h-n2", blank, skew=3.0, running=False)]):
        assert aheads(cassandra_clock_verdicts(nodes)) == []
    # the coarse path still warns (no daemon, past what the dates can tell)
    far = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", blank, skew=3.0)])
    assert aheads(far) == ["h-n2's clock is AHEAD by about 3.00 s"]


LONG = "h-n2." + "zorglub-datacenter-one.example.net".rjust(55, "x")  # 60 characters (an FQDN)


def test_a_long_node_name_puts_the_text_on_the_next_lines():
    # S28 audit (adversarial B1): a name leaving under 40 columns: the name and the verdict alone, the text and
    # the facts 6 columns in, every line within 79 (77 on sync_clocks' screen)
    nodes = [node("h-n1", probe_text()), node(LONG, probe_text(tracking_text=tracking(0.3, "fast")), skew=0.3),
             node("h-n3", probe_text(active="", installed=("systemd-timesyncd",), tracking_text=None))]
    v = cassandra_clock_verdicts(nodes)
    lines = cassandra_clock_lines(v)
    assert lines[:3] == ["  h-n1  ok", "      chronyd, synchronised to 192.0.2.1, -0.0 ms (\u00b10.7 ms)",
                         "  %s  OFF" % LONG]
    assert lines[6:9] == ["  h-n3  NO TIME SYNC",
                          "      systemd-timesyncd installed, not running (about 0.0 ms off,",
                          "        dates over SSH, coarse)"]
    assert all(len(line) <= 77 for line in cassandra_clock_lines(v, width=77))
    every = lines + cassandra_clock_health(v)["lines"] + cassandra_clock_refusal(v, cluster="zorglub")
    assert all(len(line) <= 79 for line in every), [line for line in every if len(line) > 79]


def test_a_line_that_goes_on_is_told_from_the_next_fact():
    # S28 audit (adversarial C2): a row's text, a fact or a warning's fact that goes on: 2 columns more than the
    # next fact
    v = cassandra_clock_verdicts([node("h-n1", probe_text()), node(LONG, probe_text(tracking_text=tracking(0.3, "fast")),
                                                                   skew=0.3)])
    lines = cassandra_clock_health(v)["lines"]
    warning = lines[lines.index("WARNING  %s's clock" % LONG):]
    assert warning[1] == "           is AHEAD by about 300.0 ms"
    assert warning[2] == "         the writes it stamped as coordinator carry future timestamps"
    assert warning[3] == "           (lightweight transactions, drivers without client-side timestamps)"
    timesyncd = probe_text(active="", installed=("systemd-timesyncd",), tracking_text=None)
    rows = cassandra_clock_lines(cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n3", timesyncd, skew=0.15)]))
    assert rows[1:4] == ["  h-n3  NO TIME SYNC  systemd-timesyncd installed, not running (about 150.0 ms",
                         "                        off, dates over SSH, coarse)",
                         "                      \u2192 clock stepped in a restart window (drain, stop, step,"]


def test_the_warnings_of_a_start_one_fact_per_line():
    # S28 audit (adversarial C3): clocks_check.yml's WARNING lines (a node ahead by the dates); audit 11 (C3): no
    # word alone on a line
    lost = probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)
    v = cassandra_clock_verdicts([node("h-n1", probe_text()), node("h-n2", probe_text()), node("h-n3", lost, skew=5.0)])
    lines = cassandra_clock_warnings(v, label="clocks: ")
    assert lines == [
        "WARNING  clocks: h-n3's clock is AHEAD by about 5.00 s",
        "         by the dates over SSH, against the median of h-n1, h-n2",
        "         the writes it stamped as coordinator carry future timestamps",
        "           (lightweight transactions, drivers without client-side timestamps)",
        "         an update or a delete of the same data from a correct clock in the",
        "           next 5.00 s loses against them, silently",
        "         the latest timestamp wins",
        "         TTLs it set expire late, its compactions purge tombstones early",
        "         check what was written through it lately"]
    assert all(line.startswith(("WARNING  ", "         ")) and len(line) <= 79 for line in lines)
    assert cassandra_clock_warnings({}) == [] and cassandra_clock_warnings(None) == []


LONG76 = "h-n2." + "zorglub-datacenter-one.example.net".rjust(71, "x")  # 76 characters


def test_a_name_longer_than_a_line_is_kept_whole():
    # S28 audit 11 (domain C2, adversarial C4): the name and its verdict on 2 lines; audit 12 (adversarial B3):
    # a word is never cut (a name, a path, a URL): only a line holding a word longer than the room goes over 79
    lost = probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)
    v = cassandra_clock_verdicts([node("h-n1", probe_text()), node(LONG76, probe_text(tracking_text=tracking(0.3, "fast"))),
                                  node("h-n3", lost, skew=5.0), node("h-n4", probe_text())])
    lines = cassandra_clock_lines(v)
    assert lines[2:4] == ["  " + LONG76, "    OFF"]
    every = (lines + cassandra_clock_lines(v, width=77) + cassandra_clock_health(v)["lines"]
             + cassandra_clock_refusal(v, cluster="zorglub") + cassandra_clock_warnings(v, label="clocks: "))
    assert all(len(line) <= 79 or LONG76 in line for line in every), [line for line in every if len(line) > 79]
    assert [line for line in every if len(line) > 79] == [
        "WARNING  %s's" % LONG76, "WARNING  %s's" % LONG76, "WARNING  clocks: %s's" % LONG76]
    assert "           clock is AHEAD by about 300.0 ms" in cassandra_clock_refusal(v, cluster="zorglub")
    # a list of names past a line: their count
    assert "         by the dates over SSH, against the median of 3 synchronised nodes" in cassandra_clock_warnings(v)


N65 = "h-n2." + "zorglub-datacenter-one.example.net".rjust(60, "x")  # 65 characters (an FQDN)


@pytest.mark.parametrize("probe, verdict", [
    (probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED), "NOT SYNCHRONISED"),
    (probe_text(active="", installed=("chrony",), tracking_text=None), "NO TIME SYNC"),
])
def test_a_long_name_keeps_its_verdict_whole(probe, verdict):
    # S28 audit 12 (adversarial B1): name and verdict past the width: the whole verdict on the next line
    v = cassandra_clock_verdicts([node("h-n1", probe_text()), node(N65, probe, skew=0.0)])
    for lines in (cassandra_clock_lines(v), cassandra_clock_lines(v, width=77), cassandra_clock_health(v)["lines"],
                  cassandra_clock_refusal(v)):
        at = lines.index("  " + N65)
        assert lines[at + 1] == "    " + verdict and all(len(line) <= 79 for line in lines), lines
    # a verdict that fits after the name: on its line
    unread = cassandra_clock_health(cassandra_clock_verdicts([node("h-n1", probe_text())]), unread={N65: "timeout"})
    assert "  %s  NOT READ" % N65 in unread["lines"]


def test_a_path_or_a_url_is_never_cut():
    # S28 audit 12 (adversarial B3): the failure message of a clock not read, its path and URL whole
    path = "/usr/local/lib/very/long/path/to/the/interpreter/somewhere/deep/python3.12"
    url = "https://docs.ansible.com/ansible-core/2.16/reference_appendices/interpreter_discovery.html"
    v = cassandra_clock_verdicts([node("h-n1", probe_text())])
    for lines in (cassandra_clock_health(v, unread={"h-n2": "%s: not found; see %s" % (path, url)})["lines"],
                  cassandra_clock_refusal(v, unread={"h-n2": "%s: not found; see %s" % (path, url)})):
        assert "                    %s:" % path in lines and "                    %s" % url in lines, lines
        assert all(len(line) <= 79 or path in line or url in line for line in lines)
