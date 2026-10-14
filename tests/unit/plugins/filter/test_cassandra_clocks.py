from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The clocks of the nodes: what clock_probe.yml prints (chronyc, timedatectl, ntpq outputs as the daemons print
# them), what it says, and the verdicts of sync_clocks and of the check before create_cluster and add_node start
# nodes.

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_clocks import (
    cassandra_clock_in_sync, cassandra_clock_lines, cassandra_clock_probe, cassandra_clock_refusal,
    cassandra_clock_settled, cassandra_clock_verdicts, parse_chrony_sources, parse_chrony_tracking, parse_ntpq,
    parse_timesync)

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


def tracking(seconds, way="slow", delay="0.000800000", dispersion="0.000300000", leap="Normal"):
    return (TRACKING_SYNCED.replace("0.000012345 seconds slow", "%.9f seconds %s" % (seconds, way))
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
    assert p["query"] == pytest.approx(1.234567)


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
    nothing = cassandra_clock_probe(probe_text(active="", tracking_text=None))
    assert cassandra_clock_settled(nothing, 50)  # nothing runs to wait for
    with pytest.raises(AnsibleFilterError):
        cassandra_clock_settled("text")


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


def test_verdicts_coarse_skew_takes_the_least_delayed_sample():
    samples = ["%f %f" % (T0, T0 + 0.300), "%f %f" % (T0 + 1, T0 + 1.020), "garbage"]
    v = cassandra_clock_verdicts([dict(node("node1", probe_text()), samples=samples)])
    assert v["nodes"][0]["coarse"] == pytest.approx(0.0, abs=1e-4)  # the reference: itself (its offset 12 us)
    assert v["spread"] is None


# --- the table and the refusal ------------------------------------------------------------------------------------


def test_lines():
    v = cassandra_clock_verdicts([node("node1", probe_text()), node("node22", probe_text(tracking_text=tracking(2.0, "fast")))])
    lines = cassandra_clock_lines(v)
    assert lines[0].startswith("  node1   ok   chronyd, synchronised to 192.0.2.1, -0.0 ms (\u00b10.7 ms)")
    assert lines[1].startswith("  node22  OFF  chronyd, synchronised to 192.0.2.1, +2.00 s")
    assert lines[1].endswith("\u2192 clock stepped in a restart window (drain, stop, step, start)")
    assert cassandra_clock_lines(v, actions=False)[1].endswith("ahead of its source by 2.00 s, more than 50.0 ms")


def test_refusal():
    v = cassandra_clock_verdicts([node("node1", probe_text()),
                                  node("node2", probe_text(tracking_text=TRACKING_UNSYNCED, sources=SOURCES_NONE_REACHED)),
                                  node("node3", probe_text(tracking_text=tracking(0.2, "fast")))])
    lines = cassandra_clock_refusal(v, operation="create_cluster", cluster="Orders",
                                    command="ansible-playbook -i inv.yml community.cassandra.sync_clocks")
    assert lines[0] == "REFUSED  clocks  Orders  nothing was started"
    assert lines[1].startswith("  node2  NOT SYNCHRONISED  chronyd, NOT synchronised: chronyd reaches none of its 2")
    assert lines[2].startswith("  node3  OFF")
    assert not any(line.startswith("  node1 ") for line in lines)
    text = "\n".join(lines)
    assert "within 50.0 ms of each other (cassandra_clock_max_offset_ms)" in text
    assert "WARNING  node3's clock is AHEAD" in text
    assert "     ansible-playbook -i inv.yml community.cassandra.sync_clocks" in lines
    assert "then run create_cluster again" in text and "-e cassandra_clock_check=false" in text
    assert cassandra_clock_refusal(cassandra_clock_verdicts([node("node1", probe_text())])) == []


def test_refusal_under_check():
    # --check started nothing: a node cassandra_linux would set up is left to the real run
    blank = probe_text(active="", tracking_text=None)
    v = cassandra_clock_verdicts([node("node1", blank, running=False), node("node2", blank, running=False)])
    assert cassandra_clock_refusal(v, check=True) == [
        "NOTE  clocks: time sync would be started on node1, node2 (cassandra_linux), its clock checked by the real run"]
    assert cassandra_clock_refusal(v)[0] == "REFUSED  clocks  nothing was started"
    off = cassandra_clock_verdicts([node("node1", probe_text(tracking_text=tracking(1.0)))])
    assert cassandra_clock_refusal(off, check=True)[0] == "NOTE  clocks  a real run would refuse to start the nodes"


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
    assert "node5  FAILED: refused: chronyd reaches none of its 2 sources (to fix by hand)" in text
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
