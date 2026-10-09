from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service confirm.yml, run by ansible-playbook on two local hosts
# the way the playbooks include it, answered through a terminal (a pty).

import os
import re
import select
import subprocess
import sys
import time

import pytest

pty = pytest.importorskip("pty")

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))

PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Confirm
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_service
        tasks_from: confirm.yml
      vars:
        cassandra_confirm_prompt: "Remove node7 from the ring?"

    - name: Go on
      ansible.builtin.debug:
        msg: went on
"""
PROMPTS = (b"Answer yes to go on, no to stop", b"Please answer yes or no")


def command(tmp_path, *extra):
    playbook = tmp_path / "confirm.yml"
    playbook.write_text(PLAYBOOK)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0",
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()",
            "-i", "node1,node2,", "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable, str(playbook)]
    return argv + list(extra), env


def prompts(output):
    return sum(output.count(p) for p in PROMPTS)


def read_until(fd, output, done, deadline):
    while not done(output):
        if time.time() > deadline:
            raise AssertionError("timed out:\n" + output.decode(errors="replace"))
        ready, dummy, dummy = select.select([fd], [], [], 1)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:  # the run ended
                return output
            if not data:
                return output
            output += data
    return output


def run_in_terminal(tmp_path, answers):
    """Answers each prompt in turn; returns (rc, output, prompts seen)."""
    argv, env = command(tmp_path)
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.execve(argv[0], argv, env)
    output, deadline, seen = b"", time.time() + 120, 0
    for answer in answers:
        output = read_until(fd, output, lambda out: prompts(out) > seen, deadline)
        seen = prompts(output)
        # pause drops what was typed before it reads: an operator's pace
        time.sleep(1)
        os.write(fd, answer.encode() + b"\r")
    output = read_until(fd, output, lambda out: b"PLAY RECAP" in out, deadline)
    while True:  # the rest, up to the end of the run
        ready, dummy, dummy = select.select([fd], [], [], 5)
        try:
            data = os.read(fd, 4096) if ready else b""
        except OSError:
            data = b""
        if not data:
            break
        output += data
    rc = os.waitpid(pid, 0)[1]
    return os.waitstatus_to_exitcode(rc) if hasattr(os, "waitstatus_to_exitcode") else rc >> 8, output.decode(), prompts(output)


def went_on(output):
    return len(re.findall(r'"msg": "went on"', output))


def task_hosts(output, name):
    """The hosts each run of the task named name printed a line for."""
    runs = re.findall(r"TASK \[[^\]]*: %s\][^\n]*\n(.*?)(?=TASK \[|PLAY RECAP)" % re.escape(name), output, re.S)
    return [re.findall(r"(?:ok|changed|skipping|fatal): \[(node\d)\]", run) for run in runs]


def test_yes_goes_on(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, [" Yes "])
    assert rc == 0, output
    assert went_on(output) == 2
    assert seen == 1
    assert re.search(r"Remove node7 from the ring\?\r?\nAnswer yes to go on, no to stop", output)
    # controller bookkeeping: one line per step, the facts on both hosts (both went on)
    for name in ("Start the count of answers", "Count the answers", "Confirm the operation", "Read the answer"):
        assert task_hosts(output, name) == [["node1"]], (name, output)


def test_y_goes_on(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, ["y"])
    assert rc == 0, output
    assert went_on(output) == 2


def test_no_stops(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, ["no"])
    assert rc != 0
    assert "Stopped at your request, nothing changed." in output
    assert went_on(output) == 0


def test_typo_then_yes(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, ["yse", "yes"])
    assert rc == 0, output
    assert "Please answer yes or no" in output
    assert went_on(output) == 2
    assert task_hosts(output, "Count the answers") == [["node1"], ["node1"]], output


def test_three_typos_stop(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, ["yse", "ok", "sure"])
    assert rc != 0
    assert "No yes or no after 3 answers, stopped, nothing changed." in output
    assert seen == 3
    assert went_on(output) == 0


def test_empty_then_n(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, ["", "n"])
    assert rc != 0
    assert "Please answer yes or no" in output
    assert "Stopped at your request, nothing changed." in output
    assert "No terminal" not in output
    assert went_on(output) == 0


def test_no_terminal_fails_at_once(tmp_path):
    argv, env = command(tmp_path)
    run = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode != 0
    assert "No terminal to answer the confirmation on" in run.stdout
    assert "-e cassandra_operation_confirm=false" in run.stdout
    assert "Please answer yes or no" not in run.stdout
    assert went_on(run.stdout) == 0


def test_no_terminal_and_confirm_false_goes_on(tmp_path):
    argv, env = command(tmp_path, "-e", "cassandra_operation_confirm=false")
    run = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == 0, run.stdout
    assert went_on(run.stdout) == 2


def test_check_mode_asks_nothing(tmp_path):
    # --check changes nothing: no question, even without a terminal
    argv, env = command(tmp_path, "--check")
    run = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == 0, run.stdout
    assert "No terminal" not in run.stdout and "Answer yes" not in run.stdout
    assert went_on(run.stdout) == 2


SCREEN_PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Screen
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_service
        tasks_from: screen.yml
      vars:
        cassandra_screen_question: Remove node7?
        cassandra_screen_session_warning: true
        cassandra_screen:
          operation: decommission_node
          summary: remove node7
          warnings:
            - label: replication
              text: orders keeps 2 replicas
"""


def screen_run(tmp_path, *extra):
    argv, env = command(tmp_path, *extra)
    (tmp_path / "confirm.yml").write_text(SCREEN_PLAYBOOK)  # in place of the plain one
    env = dict(env, TMUX="", STY="")  # the session warning shows
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)


def test_screen_under_check_is_printed_once_without_question(tmp_path):
    run = screen_run(tmp_path, "--check")
    assert run.returncode == 0, run.stdout
    # one string (plain text under callback_result_format=yaml), not a list of lines
    assert '"msg": "decommission_node: remove node7\\n' in run.stdout
    assert run.stdout.count('decommission_node: remove node7\\n') == 1
    assert '--check: nothing will be changed (the plan only, no question).' in run.stdout
    assert 'WARNING - replication: orders keeps 2 replicas' in run.stdout
    assert '(A real run would also warn about: session.)' in run.stdout
    assert "No terminal" not in run.stdout


def test_screen_with_confirm_false_is_printed_and_goes_on(tmp_path):
    run = screen_run(tmp_path, "-e", "cassandra_operation_confirm=false")
    assert run.returncode == 0, run.stdout
    assert 'cassandra_operation_confirm is false: no question, the run goes on.' in run.stdout
    assert run.stdout.count('decommission_node: remove node7\\n') == 1 and "Answer yes" not in run.stdout
    assert 'WARNING - session: this run is not inside tmux or screen' in run.stdout


def test_screen_then_the_question(tmp_path):
    argv, env = command(tmp_path)
    (tmp_path / "confirm.yml").write_text(SCREEN_PLAYBOOK)  # in place of the plain one
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.execve(argv[0], argv, env)
    output = read_until(fd, b"", lambda out: prompts(out) > 0, time.time() + 120)
    time.sleep(1)
    os.write(fd, b"no\r")
    output = read_until(fd, output, lambda out: b"PLAY RECAP" in out, time.time() + 120).decode()
    os.waitpid(pid, 0)
    # the screen printed once (in the logs too), then the question alone
    assert output.count('decommission_node: remove node7\\n') == 1
    assert 'WARNING - replication: orders keeps 2 replicas' in output
    assert re.search(r"Confirm the operation\]\r?\nRemove node7\?\r?\nAnswer yes to go on, no to stop", output)
    assert "Stopped at your request, nothing changed." in output


def test_screen_is_printed_before_a_run_without_terminal_stops(tmp_path):
    run = screen_run(tmp_path)
    assert run.returncode != 0
    assert run.stdout.count('decommission_node: remove node7\\n') == 1
    assert "No terminal to answer the confirmation on" in run.stdout


OPS_PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Before
      ansible.builtin.debug:
        msg: "Reset of node5:"
      run_once: true
      vars:
        cassandra_output: true
""" + SCREEN_PLAYBOOK.split("  tasks:\n", 1)[1] + """
    - name: Start following it
      ansible.builtin.set_fact:
        _started: true
      vars:
        cassandra_output_gap: true

    - name: Progress
      ansible.builtin.debug:
        msg: "[1/1] node7 decommission  {{ item }}"
      loop: [LEAVING, DECOMMISSIONED]
      run_once: true
      vars:
        cassandra_output: true

    - name: Recap
      ansible.builtin.debug:
        msg: ["DONE  decommission_node", "", "TO DO", "  1. delete node7 from the inventory"]
      run_once: true
      vars:
        cassandra_output: true
        cassandra_output_gap: true
"""


def on_screen(output):
    """The lines as a terminal shows them: what follows the last carriage return, no escape sequences."""
    return [re.sub(r"(\x1b\[?)+K?", "", line.rstrip("\r").split("\r")[-1]) for line in output.split("\n")]


def test_ops_callback_blocks_one_blank_line_apart(tmp_path):
    argv, env = command(tmp_path)
    (tmp_path / "confirm.yml").write_text(OPS_PLAYBOOK)
    env = dict(env, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops", TMUX="", STY="")
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.execve(argv[0], argv, env)
    output = read_until(fd, b"", lambda out: prompts(out) > 0, time.time() + 120)
    time.sleep(1)
    os.write(fd, b"yes\r")
    output = read_until(fd, output, lambda out: b"TO DO" in out and out.rstrip().endswith(b"====="), time.time() + 120).decode()
    os.waitpid(pid, 0)
    # the screen, the question, the answer, the progress and the recap: one blank line between two blocks
    rules = [re.sub(r"^([-=])\1{3,}$", lambda m: m.group(1) * 4, re.sub(r" -{4,}$", " ----", line))
             for line in on_screen(output.strip())]
    assert rules == [
        "Reset of node5:",
        "====",  # a screen: a heavy rule above, a light one between its blocks
        "decommission_node: remove node7",
        "----",
        "WARNING - replication: orders keeps 2 replicas",
        "WARNING - session: this run is not inside tmux or screen: if the SSH session to this machine drops,",
        "  the run stops (the operation itself goes on, unwatched). Run it inside tmux or screen.",
        "----",  # above the question
        "[community.cassandra.cassandra_service : Confirm the operation]",
        "Remove node7?",
        "Answer yes to go on, no to stop:",
        "yes",  # pause clears it once read: shown again
        "",
        "---- [1/1] node7 decommission ----",  # each node's progress: its own block
        "[1/1] node7 decommission  LEAVING",
        "[1/1] node7 decommission  DECOMMISSIONED",
        "====",  # the recap: heavy rules around it
        "DONE  decommission_node",
        "----",
        "TO DO",
        "  1. delete node7 from the inventory",
        "====",
    ], output
