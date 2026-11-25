from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The steps of the ops callback: a phase line marked cassandra_output_step, the nodes through the task running and
# the time since it started rewritten in place on a terminal, "done in" once over; a line every 5 minutes at most in
# a log or a pipe.

import io
import os
import re
import select
import sys
import time

import pytest

from ansible import constants as C

from ansible_collections.community.cassandra.plugins.callback.ops import (
    LOG_EVERY, STEP, CallbackModule, step_done, step_line)
from ansible_collections.community.cassandra.tests.unit.helpers import playbook_command

from .test_ops import quiet_callback, run

PHASE = "Comparing the config with the inventory (3 nodes)..."


class Task(object):
    def __init__(self, run_once=False, action="ansible.builtin.command"):
        self.run_once, self.action, self.vars = run_once, action, {}


class Host(object):
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class Result(object):
    def __init__(self, host, task=None):
        self.host, self.task, self.result = Host(host), task or Task(), {}


class TerminalDisplay(object):
    """As FakeDisplay (shown: (line, colour, log_only)), with Ansible's Display.display arguments in its order, and
    what is not for the log only printed on the terminal (sys.stdout)."""
    columns = 100

    def __init__(self):
        self.shown = []

    def display(self, line, color=None, stderr=False, screen_only=False, log_only=False, newline=True):
        self.shown.append((line, color, log_only))
        if not log_only:
            sys.stdout.write(line + ("\n" if newline else ""))


def step_callback(tty, hosts=("node1", "node2", "node3")):
    """A quiet callback in a play of hosts, its step state set as __init__ and a play start do."""
    import threading
    callback = quiet_callback(tty)
    callback._display = TerminalDisplay() if tty else callback._display
    callback._gone, callback._through, callback._refused = set(), set(), {}
    callback._lock, callback._seen = threading.RLock(), set(hosts)  # (the play's first task: every host)
    callback._display.verbosity = 0
    return callback


def through(callback, *hosts, **kwargs):
    callback._task_starts(kwargs.get("task") or Task())
    for host in hosts:
        callback._count(Result(host, kwargs.get("task")))


def screen(raw):
    """The rows a terminal shows for raw (a line feed, a carriage return, a line cleared, the cursor up)."""
    rows, row, col = [""], 0, 0
    for token in re.findall(r"\x1b\[(\d*)([AKJ])|(\r)|(\n)|([^\x1b\r\n]+)", raw):
        count, code, cr, lf, text = token
        if code == "A":
            row = max(0, row - int(count or 1))
        elif code in "KJ" and code:
            rows[row] = rows[row][:col]
            if code == "J":
                del rows[row + 1:]
        elif cr:
            col = 0
        elif lf:
            row, col = row + 1, 0
            if row == len(rows):
                rows.append("")
        elif text:
            rows[row] = rows[row][:col] + text + rows[row][col + len(text):]
            col += len(text)
    return rows


def test_the_lines_of_a_step():
    assert step_line(PHASE, 2, 3, 72) == PHASE + "  2/3  1m12s"
    assert step_line(PHASE, 0, 0, 5) == PHASE + "  5s"  # the play's nodes unknown: the time alone
    assert step_done(PHASE, 252) == [PHASE + "  done in 4m12s"]
    # 79 columns at most: the phase line cut while going on, the time under it once done
    long = "Writing the config (node1, node3, node5, node7, node9, node11, node13, node15)..."
    assert step_line(long, 3, 8, 3725) == "Writing the config (node1, node3, node5, node7, node9, node11, n...  3/8  1h02m"
    assert len(step_line(long, 300, 300, 3725)) <= 79 and step_line(long, 3, 8, 1, width=12) == "3/8  1s"
    assert step_done(long, 61) == [long, "  done in 1m01s"]


def test_a_step_on_a_terminal_rewritten_in_place_the_messages_above_it():
    callback = step_callback(tty=True)
    callback._blank = False  # the run's first line above
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        callback._print(PHASE, step=True)
        assert screen(out.getvalue()) == ["", PHASE + "  0/3  0s"]  # under a blank row, no newline after it
        through(callback, "node1")
        callback.now[0] += 1
        through(callback, "node1", "node2")  # the next task: counted again from 0
        assert screen(out.getvalue())[-1] == PHASE + "  1/3  1s"  # a rewrite every 0.2 s at most
        callback.now[0] += 0.2
        callback._count(Result("node2"))
        assert screen(out.getvalue())[-1] == PHASE + "  2/3  1s"
        callback._count(Result("node3"))  # every node through the task: shown at once
        assert screen(out.getvalue())[-1] == PHASE + "  3/3  1s"
        callback._task_starts(Task())
        callback.now[0] += 70
        callback._print(["WARNING  something", "         its way out"])  # above the step's line
        through(callback, "node1", "node2", "node3")
        callback.now[0] += 181
        callback._print("Checking the cluster from each node (3 nodes)...")  # a phase line: the step is done
    finally:
        sys.stdout = real
    assert screen(out.getvalue()) == [
        "WARNING  something", "         its way out", "", PHASE + "  done in 4m12s", "",
        "Checking the cluster from each node (3 nodes)...", ""]
    assert "display" not in callback._display.__dict__  # Ansible's display as it was
    # the log file (log_path) gets the lines printed and the step's start, not its line going on
    assert [line for line, color, log_only in callback._display.shown if line.strip()] == [
        PHASE, "WARNING  something", "         its way out", PHASE + "  done in 4m12s", "Checking the cluster from each node (3 nodes)..."]
    assert [color for line, color, log_only in callback._display.shown if line.startswith(PHASE + "  ")] == ["cyan"]


def test_a_step_on_a_terminal_ansible_own_lines_above_it():
    """A warning Ansible prints itself (from a worker, a plugin) goes above the step's line, never over it."""
    callback = step_callback(tty=True)
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        callback._print(PHASE, step=True)
        callback._display.display("[WARNING]: Collection community.general does not support Ansible version 2.16.19")
        callback._display.display("written to the log only", log_only=True)
        through(callback, "node2")
        callback._print("NOTHING TO DO  apply_config  prod")  # a recap: done
    finally:
        sys.stdout = real
    rows = screen(out.getvalue())
    assert rows[:3] == ["[WARNING]: Collection community.general does not support Ansible version 2.16.19", "",
                        PHASE + "  done in 0s"], rows
    assert "NOTHING TO DO  apply_config  prod" in rows


def test_a_step_stopped_by_an_error_says_no_time():
    callback = step_callback(tty=True)
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        callback._print(PHASE, step=True)
        through(callback, "node1")
        callback._print(["REFUSED  apply_config  prod", "  node2: why"])
    finally:
        sys.stdout = real
    assert screen(out.getvalue())[:2] == [PHASE, ""]  # its phase line alone, as without a step
    # logged once (its start), shown once (screen only)
    assert [log_only for line, color, log_only in callback._display.shown if line == PHASE] == [True, False]
    callback = step_callback(tty=False)
    callback._print(PHASE, step=True)
    callback._print("node2: Stopped at node2", color=C.COLOR_ERROR)
    assert [line for line, color, log_only in callback._display.shown] == [PHASE, "", "node2: Stopped at node2"]


def test_a_step_without_a_terminal_its_line_then_one_every_five_minutes_then_done():
    callback = step_callback(tty=False)
    callback._print(PHASE, step=True)
    for minute in range(1, 12):
        callback.now[0] += 60
        through(callback, "node1", "node2")  # one line at 5m and at 10m, not one per result
    callback._print("WARNING  in between")
    callback.now[0] += 60
    callback._print("Checking the cluster from each node (3 nodes)...", step=True)
    callback.now[0] += 5
    callback._print("CHECK  apply_config  prod  nothing to apply")
    shown = [line for line, color, log_only in callback._display.shown]
    assert shown[:11] == [PHASE, PHASE + "  1/3  5m00s", PHASE + "  1/3  10m00s", "", "WARNING  in between", "",
                          PHASE + "  done in 12m00s", "", "Checking the cluster from each node (3 nodes)...",
                          "Checking the cluster from each node (3 nodes)...  done in 5s", ""]
    assert LOG_EVERY == 300


def test_the_nodes_counted():
    callback = step_callback(tty=False)
    callback._print(PHASE, step=True)
    through(callback, "node1", "node2", "node3")
    assert callback._counted() == (3, 3)
    through(callback, "node1", task=Task(run_once=True))  # its one result: every node through it
    assert callback._counted() == (3, 3)
    callback._task_starts(Task())
    callback._count(Result("node2"), out=True)  # failed or unreachable: the play goes on without it
    callback._count(Result("node1"))
    assert callback._counted() == (1, 2)


def test_a_step_ends_with_its_play_or_its_run():
    callback = step_callback(tty=False)
    callback._print(PHASE, step=True)

    class Play(object):
        hosts, order = ["node1"], None
    callback.v2_playbook_on_play_start(Play())
    assert callback._step is None and callback._seen == set()
    assert callback._display.shown[-1][0] == PHASE + "  done in 0s"

    class Stats(object):
        failures, dark = {"node2": 1}, {}
    callback._print(PHASE, step=True)
    callback.v2_playbook_on_stats(Stats())
    assert callback._display.shown[-1][0] == PHASE  # a run that failed: no time said


def test_the_task_banner_keeps_the_step_line_above_it():
    """The default callback's task header (a failure shown in full) goes through the step's wrapper, put back after."""
    callback = step_callback(tty=True)
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        callback._print(PHASE, step=True)
        hooked = callback._display.__dict__["display"]
        callback._blank = True
        callback._display.columns = 80
        with pytest.raises(Exception):  # (the default callback's banner needs a real run: only the wrapper matters)
            CallbackModule._print_task_banner(callback, Task())
        assert callback._display.__dict__["display"] is hooked
    finally:
        sys.stdout = real


def test_the_step_flag_is_the_documented_one():
    assert STEP == "cassandra_output_step"


@pytest.mark.playbook
def test_a_step_in_a_log(tmp_path):
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: A step
      ansible.builtin.debug:
        msg: Comparing the config with the inventory (2 nodes)...
      run_once: true
      vars:
        cassandra_output: true
        cassandra_output_gap: true
        cassandra_output_step: true
    - name: Some work
      ansible.builtin.command: /bin/true
      changed_when: false
    - name: A note
      ansible.builtin.debug:
        msg: WARNING  a note
      run_once: true
      vars:
        cassandra_output: true
- hosts: all
  gather_facts: false
  tasks:
    - name: Another step
      ansible.builtin.debug:
        msg: Checking the cluster from each node (2 nodes)...
      run_once: true
      vars:
        cassandra_output: true
        cassandra_output_step: true
    - name: The end
      ansible.builtin.debug:
        msg: ["CHECK  apply_config  c  nothing to apply"]
      run_once: true
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    lines = [line for line in output.splitlines() if not line.startswith(("====", "[WARNING]", "[DEPRECATION"))]
    assert lines[:9] == ["Comparing the config with the inventory (2 nodes)...", "", "WARNING  a note", "",
                         re.sub(r"\d+s$", "0s", lines[4]), "", "Checking the cluster from each node (2 nodes)...",
                         re.sub(r"\d+s$", "0s", lines[7]), ""], output
    assert re.match(r"Comparing the config with the inventory \(2 nodes\)\.\.\.  done in \d+s$", lines[4]), output
    assert re.match(r"Checking the cluster from each node \(2 nodes\)\.\.\.  done in \d+s$", lines[7]), output


@pytest.mark.playbook
def test_a_step_on_a_terminal(tmp_path):
    """A real run on a terminal: the step's line under the output, its count and time, done in at the end; the log
    file without the lines going on."""
    pty = pytest.importorskip("pty")
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: A step
      ansible.builtin.debug:
        msg: Comparing the config with the inventory (2 nodes)...
      run_once: true
      vars:
        cassandra_output: true
        cassandra_output_gap: true
        cassandra_output_step: true
    - name: Some work
      ansible.builtin.command: sleep 1
      changed_when: false
    - name: The end
      ansible.builtin.debug:
        msg: ["CHECK  apply_config  c  nothing to apply"]
      run_once: true
      vars:
        cassandra_output: true
"""
    (tmp_path / "playbook.yml").write_text(playbook)
    env = playbook_command([], dict(ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops", ANSIBLE_NOCOLOR="1",
                                    ANSIBLE_LOG_PATH=str(tmp_path / "run.log")), ["ANSIBLE_CONFIG"])[1]
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.chdir(str(tmp_path))
        os.execve(sys.executable, [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i",
                                   "node1,node2,", "-c", "local", "-e", "ansible_python_interpreter=%s" % sys.executable,
                                   "playbook.yml"], env)
    output, deadline = b"", time.time() + 120
    while True:
        assert time.time() < deadline, output.decode(errors="replace")
        ready, dummy, dummy = select.select([fd], [], [], 0.5)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        output += data
    os.waitpid(pid, 0)
    raw = output.decode(errors="replace").replace("\r\n", "\n")
    assert re.search(r"Comparing the config with the inventory \(2 nodes\)\.\.\.  \d/2  \ds", raw), raw  # going on
    rows = [row for row in screen(raw) if row.strip() and not row.startswith("====")]
    done = [row for row in rows if row.startswith("Comparing")]
    assert len(done) == 1 and re.match(r"Comparing .*\.\.\.  done in \d+s$", done[0]), rows
    assert rows.index(done[0]) == len(rows) - 2 and rows[-1].startswith("CHECK  "), rows
    assert not any(re.search(r"\d/2  \ds", row) for row in rows), rows  # nothing left of the lines going on
    with open(str(tmp_path / "run.log"), encoding="utf-8") as f:
        log = f.read()
    assert re.search(r"Comparing .*\.\.\.  done in \d+s", log) and not re.search(r"\d/2  \ds", log), log


def test_a_step_on_a_terminal_a_line_left_open_a_worker_and_the_exit():
    """A line Ansible leaves open (no newline: a prompt) holds the step's line; a worker forked during the step
    prints as Ansible does (never the step's line, never its lock); at the exit the step's line is cleared; the
    log file gets the step's start."""
    callback = step_callback(tty=True)
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        callback._print(PHASE, step=True)
        assert callback._display.shown[-1] == (PHASE, None, True)  # the log file: its start
        callback._display.display("Answer: ", None, False, False, False, False)  # (positional, as Display's)
        through(callback, "node1", "node2", "node3")
        assert out.getvalue().endswith("Answer: ")  # nothing drawn after it
        callback._display.display("yes")
        assert screen(out.getvalue())[-1] == PHASE + "  3/3  0s"
        callback._pid = -1  # as in a forked worker
        before = out.getvalue()
        callback._display.display("a worker's line")
        assert out.getvalue() == before + "a worker's line\n"
        callback._pid = os.getpid()
        callback._footer_at_exit()
    finally:
        sys.stdout = real
    assert not any(r.endswith("0s") for r in screen(out.getvalue()))
