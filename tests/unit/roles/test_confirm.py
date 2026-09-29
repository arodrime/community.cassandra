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


def test_yes_goes_on(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, [" Yes "])
    assert rc == 0, output
    assert went_on(output) == 2
    assert seen == 1
    assert re.search(r"Remove node7 from the ring\?\r?\nAnswer yes to go on, no to stop", output)


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
