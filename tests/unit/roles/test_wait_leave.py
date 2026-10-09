from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The real wait of a decommission (stream_wait.yml through wait_loop.yml), run
# by ansible-playbook on a local host whose nodetool says LEAVING with a
# stream that moves, then DECOMMISSIONED: one check each time, the wait over
# at the check that sees it, then only what is left of the round skipped (a
# few tasks, never a burst of checks), and nothing more printed by the ops
# callback than the progress lines.

import os
import re
import subprocess
import sys

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))

LEAVING = """Mode: LEAVING
Unbootstrap 5a8d3a10-7c5e-11ef-9b1c-5f2e8c1d4a7b
    /127.0.0.2 (using /127.0.0.2)
        Sending 12 files, 104857600 bytes total. Already sent %(files)d files (%(pct)d.00%%), %(bytes)d bytes total (%(pct)d.00%%)
Read Repair Statistics:
Attempted: 0
Mismatch (Blocking): 0
Mismatch (Background): 0
Pool Name                    Active   Pending      Completed   Dropped
Large messages                  n/a         0              0         0
Small messages                  n/a         0              3         0
Gossip messages                 n/a         0            120         0
"""

DONE = """Mode: DECOMMISSIONED
Not sending any streams.
Read Repair Statistics:
Attempted: 0
Mismatch (Blocking): 0
Mismatch (Background): 0
Pool Name                    Active   Pending      Completed   Dropped
Large messages                  n/a         0              0         0
"""

# netstats: the answers in turn, the last one again once they are used up
NODETOOL = """#!/bin/sh
for a in "$@"; do [ "$a" = version ] && { echo "ReleaseVersion: 4.1.5"; exit 0; }; done
n=$(cat "%(dir)s/count" 2>/dev/null || echo 0)
echo $((n + 1)) > "%(dir)s/count"
f="%(dir)s/answer$n.txt"
[ -f "$f" ] || f="%(dir)s/last.txt"
cat "$f"
"""

PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Start following it, a fact the checks update
      ansible.builtin.set_fact:
        _cassandra_stream_state: {}

    - name: Wait for the decommission
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_service
        tasks_from: stream_wait.yml
      vars:
        _cassandra_stream_what: decommission
        _cassandra_stream_operations: [Unbootstrap]
        _cassandra_stream_from: [node1]
        _cassandra_stream_leave: true
        _cassandra_stream_job: {}
        _cassandra_stream_index: 1
        _cassandra_stream_steps: 1

    - name: After the wait
      ansible.builtin.debug:
        msg: AFTER THE WAIT
      vars:
        cassandra_output: true
"""


def run(tmp_path, callback, checks):
    answers = tmp_path / "answers"
    answers.mkdir()
    for i in range(checks - 1):
        done = {"files": i, "bytes": (i + 1) * 8 * 1024 * 1024, "pct": (i + 1) * 8}
        (answers / ("answer%d.txt" % i)).write_text(LEAVING % done)
    (answers / "last.txt").write_text(DONE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nodetool").write_text(NODETOOL % {"dir": answers})
    (bin_dir / "nodetool").chmod(0o755)
    (tmp_path / "wait.yml").write_text(PLAYBOOK)
    (tmp_path / "hosts.ini").write_text("node1\n")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_DISPLAY_SKIPPED_HOSTS="1", ANSIBLE_STDOUT_CALLBACK=callback,
               ANSIBLE_DEPRECATION_WARNINGS="0", PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "")))
    env.pop("ANSIBLE_CONFIG", None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.ini"),
            "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable,
            # no wait between the checks
            "-e", "cassandra_stream_early_check_interval=0", "-e", "cassandra_stream_check_interval=1",
            str(tmp_path / "wait.yml")]
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=600, check=False)
    output = result.stdout.decode(errors="replace")
    count = answers / "count"
    return result.returncode, output, int(count.read_text()) if count.exists() else output


def test_a_decommission_waited_for_check_by_check_then_no_burst(tmp_path):
    rc, out, calls = run(tmp_path, "default", checks=4)
    assert rc == 0, out
    assert calls == 4  # one nodetool netstats per check, none after the one that saw DECOMMISSIONED
    assert out.count("TASK [community.cassandra.cassandra_service : Read the streams]") == 4
    assert "Checks, by groups of 27]" in out  # rounds of 720 checks: a few include levels for days of waiting
    # after that check: the rest of its group and of its round, one skipped task each at most (27: the
    # group size of 720-check rounds), the round not started again, no check run
    tail = out.rsplit("TASK [community.cassandra.cassandra_service : Keep where it stands]", 1)[1]
    tail = tail.split("TASK [After the wait]", 1)[0]
    results = [line for line in tail.splitlines() if re.match(r"(ok|skipping|changed|included):", line)]
    assert len(results) <= 2 + 2 * 27, "\n".join(results)
    assert "Read the streams" not in tail


def test_the_ops_callback_prints_the_progress_lines_only(tmp_path):
    rc, out, calls = run(tmp_path, "community.cassandra.ops", checks=3)
    assert rc == 0, out
    lines = [line for line in out.splitlines() if line.strip() and not line.startswith(("----", "===="))]
    progress = [line for line in lines if line.startswith("[1/1] node1 decommission")]
    expected = [["LEAVING", "[----------]"], ["LEAVING", "[#---------]"], ["DECOMMISSIONED", "done"]]
    assert [line.split("  ")[1:3] for line in progress] == expected, out
    assert lines[-3].startswith("[1/1] node1 decommission  DECOMMISSIONED  done  100.0/100.0 MiB"), out
    assert lines[-2] == "      to 127.0.0.2 100.0/100.0 MiB done"
    assert lines[-1] == "AFTER THE WAIT"
    assert "skipping" not in out and "TASK" not in out
