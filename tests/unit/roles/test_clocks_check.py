from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_operation clocks_check.yml (before create_cluster starts nodes) and clock_fix.yml (what
# sync_clocks does to a clock), run by ansible-playbook on local hosts with stand-in systemctl, chronyc,
# timedatectl and systemd-detect-virt: no clock is read or stepped for real (a container could not step it,
# and a test must not). Each host's own Python tells the stand-ins which node runs them.

import os
import stat
import subprocess
import sys

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))

SYSTEMCTL = """#!/bin/sh
d=$FAKE_DIR/$FAKE_NODE
for a in "$@"; do last=$a; done
u=${last%.service}
case "$1" in
  is-active) if [ "$(cat $d/active 2>/dev/null)" = "$u" ]; then echo active; else echo inactive; exit 3; fi;;
  is-enabled) if [ "$(cat $d/active 2>/dev/null)" = "$u" ] || grep -qx "$u" $d/installed 2>/dev/null; then echo enabled;
              else exit 1; fi;;
  cat) exit 1;;
  show) if [ "$(cat $d/active 2>/dev/null)" = "$u" ] || grep -qx "$u" $d/installed 2>/dev/null; then echo LoadState=loaded;
        else echo LoadState=not-found; fi;;
esac
"""
CHRONYC = """#!/bin/sh
d=$FAKE_DIR/$FAKE_NODE
echo "$FAKE_NODE chronyc $*" >> $FAKE_DIR/log
[ "$(cat $d/active 2>/dev/null)" = chronyd ] || { echo "506 Cannot talk to daemon"; exit 1; }
case "$*" in
  "-n tracking") n=$(( $(cat $d/calls 2>/dev/null || echo 0) + 1 )); echo $n > $d/calls
                 if [ -f $d/tracking.$n ]; then cat $d/tracking.$n; else cat $d/tracking; fi;;
  "-n sources") cat $d/sources;;
  makestep) [ -f $d/tracking.stepped ] && cp $d/tracking.stepped $d/tracking; echo "200 OK";;
esac
"""
TIMEDATECTL = """#!/bin/sh
[ "$1" = show ] && printf 'NTP=yes\\nNTPSynchronized=yes\\n'
exit 0
"""
DETECT_VIRT = """#!/bin/sh
echo none
exit 1
"""


def tracking(seconds, way="slow", synced=True):
    if not synced:
        return ("Reference ID    : 00000000 ()\nStratum         : 0\nSystem time     : 0.000000000 seconds fast of NTP"
                " time\nRoot delay      : 1.000000000 seconds\nRoot dispersion : 1.000000000 seconds\n"
                "Leap status     : Not synchronised\n")
    return ("Reference ID    : C0000201 (192.0.2.1)\nStratum         : 3\nSystem time     : %.9f seconds %s of NTP time\n"
            "Root delay      : 0.000800000 seconds\nRoot dispersion : 0.000300000 seconds\nLeap status     : Normal\n"
            % (seconds, way))


SOURCES = ("MS Name/IP address         Stratum Poll Reach LastRx Last sample\n"
           "===============================================================================\n"
           "^* 192.0.2.1                     2   6   377    34    -12us[  -15us] +/-  410us\n")
UNREACHED = SOURCES.replace("^*", "^?").replace("   377 ", "     0 ")


def setup(tmp_path, nodes):
    """nodes: {name: {active, tracking, sources, tracking.N...}} -> the inventory file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("systemctl", SYSTEMCTL), ("chronyc", CHRONYC), ("timedatectl", TIMEDATECTL),
                       ("systemd-detect-virt", DETECT_VIRT)):
        (bin_dir / name).write_text(text)
    fake = tmp_path / "fake"
    lines = []
    for name, files in nodes.items():
        (fake / name).mkdir(parents=True)
        for fname, content in files.items():
            (fake / name / fname).write_text(content)
        python = bin_dir / ("python-" + name)
        python.write_text('#!/bin/sh\nFAKE_NODE=%s PATH=%s:$PATH exec %s "$@"\n' % (name, bin_dir, sys.executable))
        lines.append("%s ansible_connection=local ansible_python_interpreter=%s" % (name, python))
    for f in bin_dir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    (fake / "log").write_text("")
    (tmp_path / "hosts.ini").write_text("[cassandra]\n" + "\n".join(lines) + "\n")
    return fake


def run(tmp_path, tasks, *args, extra=None):
    hosts = ",".join(sorted(d.name for d in (tmp_path / "fake").iterdir() if d.is_dir()))
    play = [{"hosts": hosts, "gather_facts": False, "vars": dict({"cassandra_clock_sync_timeout": 0}, **(extra or {})),
             "tasks": tasks},
            {"hosts": hosts, "gather_facts": False,
             "tasks": [{"ansible.builtin.debug": {"msg": "NEXT PLAY ON {{ inventory_hostname }}"}}]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", FAKE_DIR=str(tmp_path / "fake"),
               PATH="%s:%s" % (tmp_path / "bin", os.environ.get("PATH", "")))
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.ini"),
            str(tmp_path / "play.yml")] + list(args)
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=300, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


CHECK = [{"ansible.builtin.include_role": {"name": "community.cassandra.cassandra_operation", "tasks_from": "clocks_check.yml"},
          "vars": {"_cassandra_clock_operation": "create_cluster"}}]


def synced(seconds=0.002, way="slow"):
    return {"active": "chronyd", "tracking": tracking(seconds, way), "sources": SOURCES}


def test_clocks_in_sync_go_on(tmp_path):
    setup(tmp_path, {"node1": synced(), "node2": synced(0.003, "fast")})
    rc, out = run(tmp_path, CHECK)
    assert rc == 0, out
    assert "NEXT PLAY ON node1" in out and "NEXT PLAY ON node2" in out
    assert "REFUSED" not in out


def test_a_clock_off_refuses_the_start(tmp_path):
    setup(tmp_path, {"node1": synced(), "node2": synced(0.2, "fast")})
    rc, out = run(tmp_path, CHECK)
    assert rc != 0, out
    assert "NEXT PLAY" not in out
    assert "REFUSED  clocks  nothing was started" in out
    assert "node2  OFF  chronyd, synchronised to 192.0.2.1, +200.0 ms" in out
    assert "ahead of its source by 200.0 ms, more than 50.0 ms" in out
    assert "AHEAD" not in out  # a node never started stamped nothing
    assert "community.cassandra.sync_clocks" in out and "then run create_cluster again" in out
    assert "  node1  " not in out  # in sync: not listed


def test_the_threshold_is_a_variable(tmp_path):
    setup(tmp_path, {"node1": synced(), "node2": synced(0.2, "fast")})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_max_offset_ms": 300})
    assert rc == 0, out


def test_no_synchronised_source_refuses(tmp_path):
    setup(tmp_path, {"node1": synced(), "node2": {"active": "chronyd", "tracking": tracking(0, synced=False),
                                                  "sources": UNREACHED}})
    rc, out = run(tmp_path, CHECK)
    assert rc != 0, out
    assert "node2  NOT SYNCHRONISED  chronyd, NOT synchronised: chronyd reaches none of its 1 sources" in out


def test_no_time_sync_refuses_a_real_run_and_is_left_to_it_under_check(tmp_path):
    setup(tmp_path, {"node1": synced(), "node2": {"installed": "chronyd\n"}})
    rc, out = run(tmp_path, CHECK)
    assert rc != 0 and "node2  NO TIME SYNC  chronyd installed, not running" in out, out
    rc, out = run(tmp_path, CHECK, "--check")
    assert rc == 0, out
    assert "time sync would be started on node2 (cassandra_linux), its clock checked by the real run" in out
    assert "NEXT PLAY ON node2" in out


def test_a_time_sync_just_started_is_waited_for(tmp_path):
    # unsynchronised at the first read, synchronised at the second (5 s later)
    node2 = dict(synced(), **{"tracking.1": tracking(0, synced=False)})
    fake = setup(tmp_path, {"node1": synced(), "node2": node2})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_sync_timeout": 10})
    assert rc == 0, out
    assert (fake / "node2" / "calls").read_text().strip() == "2"


def test_a_wait_that_runs_out_is_judged_not_failed(tmp_path):
    # never synchronised within the wait: the REFUSED verdict, not a failed read that drops the node from the play
    fake = setup(tmp_path, {"node1": synced(), "node2": {"active": "chronyd", "tracking": tracking(0, synced=False),
                                                         "sources": UNREACHED}})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_sync_timeout": 5})
    assert rc != 0, out
    assert "REFUSED  clocks  nothing was started" in out and "node2  NOT SYNCHRONISED" in out
    assert (fake / "node2" / "calls").read_text().strip() == "3"  # the first read, then the wait's two


def test_the_wait_ends_at_its_timeout_whatever_its_reads_take(tmp_path):
    # each read 4 s: the wait of 10 s stops at its second read (past 10 s), not at the third its retries allow
    fake = setup(tmp_path, {"node1": synced(), "node2": {"active": "chronyd", "tracking": tracking(0, synced=False),
                                                         "sources": UNREACHED}})
    chronyc = tmp_path / "bin" / "chronyc"
    chronyc.write_text(chronyc.read_text().replace('case "$*" in\n', 'case "$*" in\n  "-n tracking") sleep 4;;\nesac\ncase "$*" in\n', 1))
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_sync_timeout": 10})
    assert rc != 0 and "node2  NOT SYNCHRONISED" in out, out
    assert (fake / "node2" / "calls").read_text().strip() == "3"


def converging(dispersion):
    # a daemon just started: synchronised, its error bound still large (its skew from a few samples)
    return (tracking(0.0001, "fast").replace("Root dispersion : 0.000300000", "Root dispersion : %.9f" % dispersion)
            + "Skew            : 1000000.000 ppm\n")


def test_a_daemon_just_started_is_waited_for_while_it_converges(tmp_path):
    # its error bound shrinks over its first samples: said, waited for, then in sync
    node2 = dict(synced(), **{"tracking.1": converging(62.04), "tracking.2": converging(30.5)})
    fake = setup(tmp_path, {"node1": synced(), "node2": node2})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_sync_timeout": 15})
    assert rc == 0, out
    assert "node2: chronyd converging (\u00b162.04 s), waiting up to 15s" in out, out
    assert "node1: chronyd" not in out  # in sync: no wait, no line
    assert (fake / "node2" / "calls").read_text().strip() == "3"


def test_a_daemon_right_after_an_update_is_waited_for_by_its_skew(tmp_path):
    # its bound small for a moment after an update, its skew still 100%: converging, not a far source
    node2 = dict(synced(), **{"tracking.1": converging(0.5)})
    fake = setup(tmp_path, {"node1": synced(), "node2": node2})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_sync_timeout": 15})
    assert rc == 0, out
    assert "node2: chronyd converging (\u00b1500.4 ms), waiting up to 15s" in out, out
    assert (fake / "node2" / "calls").read_text().strip() == "2"


def test_a_bound_that_stays_large_is_refused_once_the_wait_runs_out(tmp_path):
    fake = setup(tmp_path, {"node1": synced(), "node2": {"active": "chronyd", "tracking": converging(62.04),
                                                         "sources": SOURCES}})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_sync_timeout": 5})
    assert rc != 0, out
    assert "REFUSED  clocks  nothing was started" in out and "node2  OFF" in out
    assert "chronyd still converging (just started: error bound 62.04 s)" in out, out
    assert "raise cassandra_clock_sync_timeout" in out and "closer or better source" not in out
    assert (fake / "node2" / "calls").read_text().strip() == "3"


def test_a_far_source_is_refused_at_once(tmp_path):
    # an error bound over the threshold but under a second (a far source): no wait can win, refused at once
    far = tracking(0.0001, "fast").replace("Root delay      : 0.000800000", "Root delay      : 0.200000000")
    fake = setup(tmp_path, {"node1": synced(), "node2": {"active": "chronyd", "tracking": far, "sources": SOURCES}})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_sync_timeout": 15})
    assert rc != 0, out
    assert "REFUSED  clocks  nothing was started" in out and "node2  OFF" in out
    assert "waiting up to" not in out
    assert (fake / "node2" / "calls").read_text().strip() == "1"


def test_a_node_whose_read_fails_is_refused(tmp_path):
    # the probe itself fails there (its Python gone): refused, named, not left out of the comparison
    setup(tmp_path, {"node1": synced(), "node2": synced()})
    (tmp_path / "bin" / "python-node2").write_text("#!/bin/sh\nexit 127\n")
    rc, out = run(tmp_path, CHECK)
    assert rc != 0, out
    assert "  node2  NOT READ  its clock could not be read" in out


def test_the_check_can_be_skipped(tmp_path):
    fake = setup(tmp_path, {"node1": synced(), "node2": {"installed": "chronyd\n"}})
    rc, out = run(tmp_path, CHECK, extra={"cassandra_clock_check": False})
    assert rc == 0, out
    assert (fake / "log").read_text() == ""  # nothing read


FIX = [{"ansible.builtin.include_role": {"name": "community.cassandra.cassandra_operation", "tasks_from": "clock_probe.yml"}},
       {"ansible.builtin.include_role": {"name": "community.cassandra.cassandra_operation", "tasks_from": "clock_fix.yml"},
        "vars": {"_cassandra_clock_action": "step"}},
       {"ansible.builtin.debug": {"msg": "FIX {{ inventory_hostname }} did={{ _cassandra_clock_did | join(',') }}"
                                         " error={{ _cassandra_clock_fix_error }}"}}]


def test_a_step_sets_the_clock(tmp_path):
    node1 = dict(synced(2.0, "fast"), **{"tracking.stepped": tracking(0.001)})
    fake = setup(tmp_path, {"node1": node1})
    rc, out = run(tmp_path, FIX)
    assert rc == 0, out
    assert "FIX node1 did=step error=" in out
    assert "node1 chronyc makestep" in (fake / "log").read_text()


def test_a_step_that_does_not_help_says_why(tmp_path):
    fake = setup(tmp_path, {"node1": synced(2.0, "fast")})
    rc, out = run(tmp_path, FIX)
    assert rc == 0, out  # nothing fails there: the restart window starts Cassandra again first
    assert "FIX node1 did=step error=clock still not in sync after waiting up to 0s: chronyd, synchronised to 192.0.2.1, +2.00 s" in out
    assert "node1 chronyc makestep" in (fake / "log").read_text()


def test_a_clock_in_sync_is_not_stepped(tmp_path):
    fake = setup(tmp_path, {"node1": synced()})
    rc, out = run(tmp_path, FIX)
    assert rc == 0 and "FIX node1 did= error=" in out, out
    assert "makestep" not in (fake / "log").read_text()


def test_no_time_sync_offline_a_warning_elsewhere_a_refusal_for_sync_clocks():
    # cassandra_linux's timesync.yml: os.yml and apply_config's window warn, sync_clocks
    # (_cassandra_timesync_required) refuses; never both
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_linux", "tasks", "timesync.yml")
    with open(path, encoding="utf-8") as f:
        tasks = dict((t["name"], t) for t in yaml.safe_load(f))
    warn = tasks["Say the time sync service is missing (offline)"]["when"]
    refuse = tasks["Refuse a time sync that is not installed (offline)"]["when"]
    assert "not _cassandra_timesync_required | default(false) | bool" in warn
    assert "_cassandra_timesync_required | default(false) | bool" in refuse
    assert [w for w in warn if "required" not in w] == [w for w in refuse if "required" not in w and "check_mode" not in w]
    clock_fix = os.path.join(os.path.dirname(path), "..", "..", "cassandra_operation", "tasks", "clock_fix.yml")
    with open(clock_fix, encoding="utf-8") as f:
        text = f.read()
    assert "_cassandra_timesync_required: true" in text


def test_no_time_sync_offline_is_refused_by_sync_clocks(tmp_path):
    # the start path: cassandra_linux's timesync.yml (its vars through include_role), nothing installed, offline
    setup(tmp_path, {"node1": {}})
    tasks = [{"ansible.builtin.setup": {"gather_subset": ["!all", "!min", "distribution"]}}] + FIX
    rc, out = run(tmp_path, tasks, extra={"cassandra_offline": True})
    assert rc == 0, out
    assert "FIX node1 did= error=Refuse a time sync that is not installed (offline):" in out
    assert "is not installed on node1 and cassandra_offline downloads nothing: install it from your image or mirror" in out
