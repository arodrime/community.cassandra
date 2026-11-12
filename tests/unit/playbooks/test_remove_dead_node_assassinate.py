from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# remove_dead_node with assassinate: a second question of its own after the
# plan's, the operator types the dead node's address (without a terminal:
# -e cassandra_dead_node_assassinate_confirm=<its address>). The playbook's
# block, run for real by ansible-playbook on a local host.

import json
import os
import select
import subprocess
import sys
import time

import pytest
import yaml

pty = pytest.importorskip("pty")

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))

with open(os.path.join(TOP, "playbooks", "remove_dead_node.yml"), encoding="utf-8") as _f:
    REMOVE = next(p for p in yaml.safe_load(_f) if p.get("name") == "Remove the dead node")
NAMES = [t.get("name") for t in REMOVE["tasks"]]
CONFIRM = REMOVE["tasks"][NAMES.index("Confirm the assassinate")]

ADDRESS = "10.0.0.14"


def test_asked_after_the_plan_before_the_assassinate_only_on_a_real_run():
    assert NAMES.index("Show the plan and confirm") < NAMES.index("Confirm the assassinate") \
        < NAMES.index("Remove it (assassinate, from {{ ansible_play_hosts[0] }})")
    assert CONFIRM["when"] == ["_method == 'assassinate'", "not ansible_check_mode"]


def playbook(tmp_path, **variables):
    play = {"hosts": "all", "gather_facts": False,
            "vars": dict({"_method": "assassinate", "_dead_address": ADDRESS, "_dead_target": "node4",
                          "cassandra_operation_confirm": True}, **variables),
            "tasks": [CONFIRM, {"name": "Assassinate", "ansible.builtin.debug": {"msg": "WENT ON"}}]}
    (tmp_path / "confirm.yml").write_text(json.dumps([play]))
    (tmp_path / "hosts.ini").write_text("node1\n")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_STDOUT_CALLBACK="default",
               ANSIBLE_CALLBACK_RESULT_FORMAT="yaml")  # a msg as text on 2.16 too (its JSON escapes quotes)
    env.pop("ANSIBLE_CONFIG", None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.ini"),
            "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable, str(tmp_path / "confirm.yml")]
    return argv, env


def run(tmp_path, **variables):
    """Without a terminal (stdin not a tty): rc, output."""
    argv, env = playbook(tmp_path, **variables)
    proc = subprocess.run(argv, env=env, cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=300, check=False)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def run_typed(tmp_path, answer):
    """On a terminal (a pty), the answer typed at the question: rc, output."""
    argv, env = playbook(tmp_path)
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.chdir(str(tmp_path))
        os.execve(argv[0], argv, env)
    out, typed, deadline = b"", False, time.time() + 300
    while time.time() < deadline:
        ready = select.select([fd], [], [], 1)[0]
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # the run is over
            break
        if not chunk:
            break
        out += chunk
        if not typed and b"Type its address" in out:
            time.sleep(1)  # pause drops what was typed before it reads: an operator's pace
            os.write(fd, answer.encode() + b"\r")
            typed = True
    status = os.waitpid(pid, 0)[1]
    os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")


def assert_not_ready_to_copy(out):
    """A refusal never prints the answer to copy: typing the address is the confirmation."""
    assert "assassinate_confirm=10.0.0.14" not in out and "assassinate_confirm=node4" not in out, out


@pytest.mark.parametrize("given", [ADDRESS, "node4", " 10.0.0.14 "])
def test_given_without_a_terminal_goes_on(tmp_path, given):
    rc, out = run(tmp_path, cassandra_operation_confirm=False, cassandra_dead_node_assassinate_confirm=given)
    assert rc == 0 and "WENT ON" in out, out
    assert "Type its address" not in out, out


def test_another_address_given_stops(tmp_path):
    rc, out = run(tmp_path, cassandra_operation_confirm=False, cassandra_dead_node_assassinate_confirm="10.0.0.15")
    assert rc != 0 and "WENT ON" not in out, out
    assert "cassandra_dead_node_assassinate_confirm is 10.0.0.15, not the address of the dead node" in out, out
    assert "(-e \"cassandra_dead_node_assassinate_confirm=<the dead node's address>\"): nothing was changed." in out, out
    assert_not_ready_to_copy(out)


def test_first_question_skipped_does_not_skip_this_one(tmp_path):
    # cassandra_operation_confirm false alone: the address must be given
    rc, out = run(tmp_path, cassandra_operation_confirm=False)
    assert rc != 0 and "WENT ON" not in out, out
    assert "cassandra_operation_confirm is false: give the address of the node to assassinate instead" in out, out
    assert_not_ready_to_copy(out)


def test_no_terminal_to_type_it_on_stops(tmp_path):
    rc, out = run(tmp_path)
    assert rc != 0 and "WENT ON" not in out, out
    assert "Not the address of the dead node, stopped, nothing changed (without a terminal:" in out, out
    assert_not_ready_to_copy(out)


@pytest.mark.parametrize("answer, goes_on", [(ADDRESS, True), ("node4", True), ("yes", False), ("", False)])
def test_typed_on_a_terminal(tmp_path, answer, goes_on):
    rc, out = run_typed(tmp_path, answer)
    assert "DATA LOSS: assassinate takes 10.0.0.14 out of gossip without streaming" in out, out
    assert (rc == 0 and "WENT ON" in out) if goes_on else (rc != 0 and "WENT ON" not in out), out
