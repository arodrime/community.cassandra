from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_operation confirm.yml, run by ansible-playbook on two local hosts
# the way the playbooks include it, answered through a terminal (a pty).

import os
import re
import select
import subprocess
import time

import pytest

from ansible_collections.community.cassandra.tests.unit.helpers import LOCAL, playbook_command

pty = pytest.importorskip("pty")

pytestmark = pytest.mark.playbook

PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Confirm
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_operation
        tasks_from: confirm.yml
      vars:
        cassandra_confirm_prompt: "Remove node7 from the ring?"

    - name: Go on
      ansible.builtin.debug:
        msg: went on
"""
PROMPTS = (b"Answer yes or no", b"Please answer yes or no")


def command(tmp_path, *extra):
    playbook = tmp_path / "confirm.yml"
    playbook.write_text(PLAYBOOK)
    return playbook_command(["-i", "node1,node2,"] + LOCAL + [str(playbook)] + list(extra), dict(ANSIBLE_HOST_KEY_CHECKING="0"))


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
    assert re.search(r"Remove node7 from the ring\? Answer yes or no:", output)  # the question's line ends with ':'
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
    assert "STOPPED  at your request, nothing changed" in output
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
    assert "STOPPED  no yes or no after 3 answers, nothing changed" in output
    assert seen == 3
    assert went_on(output) == 0


def test_empty_then_n(tmp_path):
    rc, output, seen = run_in_terminal(tmp_path, ["", "n"])
    assert rc != 0
    assert "Please answer yes or no" in output
    assert "STOPPED  at your request, nothing changed" in output
    assert "No terminal" not in output
    assert went_on(output) == 0


def test_no_terminal_fails_at_once(tmp_path):
    argv, env = command(tmp_path)
    run = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode != 0
    assert "No terminal to answer the confirmation on, nothing changed. The question:" in run.stdout
    assert "Remove node7 from the ring?" in run.stdout  # (what it would have asked)
    assert "-e cassandra_operation_confirm=false" in run.stdout
    assert "Please answer yes or no" not in run.stdout
    assert went_on(run.stdout) == 0


def test_the_question_names_the_cluster(tmp_path):
    """(2026-11-26) every confirmation names the cluster, on a line of its own above the question: the shared task
    says it (the banner's name, else cassandra_cluster_name), no caller can forget it."""
    import yaml
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_operation", "tasks", "confirm_ask.yml")
    with open(path, encoding="utf-8") as f:
        ask = next(t for t in yaml.safe_load(f) if t["name"] == "Confirm the operation")

    def prompt(**variables):
        variables = dict(dict(_cassandra_confirm_tries=1, cassandra_confirm_prompt="Remove node7?", hostvars={"localhost": {}}),
                         **variables)
        variables.update((k, render_with(v, variables)) for k, v in ask["vars"].items() if k.startswith("_"))
        return render_with(ask["ansible.builtin.pause"]["prompt"], variables)

    assert prompt(cassandra_cluster_name="test_cluster") == "Cluster: test_cluster\nRemove node7? Answer yes or no"
    assert prompt(cassandra_cluster_name="test_cluster", hostvars={"localhost": {"_cassandra_cluster_shown": "other"}}) == (
        "Cluster: other\nRemove node7? Answer yes or no")  # the banner's (the group the run is on)
    assert prompt() == "Remove node7? Answer yes or no"  # no name known: none said
    assert prompt(_cassandra_confirm_tries=2, cassandra_cluster_name="test_cluster") == "Please answer yes or no"
    # the variable that skips it, the caller's (cassandra_config: cassandra_config_confirm)
    # and what its stops say was left as it was (cassandra_config: other roles of the play may have run before)
    argv, env = command(tmp_path, "-e", "_cassandra_confirm_skip=cassandra_config_confirm",
                        "-e", "_cassandra_confirm_unchanged='config not written'")
    run = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    assert "-e cassandra_config_confirm=false" in run.stdout and "Cluster:" not in run.stdout, run.stdout
    assert "No terminal to answer the confirmation on, config not written." in run.stdout, run.stdout
    assert "nothing changed" not in run.stdout, run.stdout


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
        name: community.cassandra.cassandra_operation
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
    assert '"msg": "DECOMMISSION NODE  remove node7\\n' in run.stdout
    assert run.stdout.count('DECOMMISSION NODE  remove node7\\n') == 1
    assert '--check: nothing will be changed' in run.stdout
    assert 'WARNING  replication: orders keeps 2 replicas' in run.stdout
    assert '(A real run would also warn about: not inside tmux or screen.)' in run.stdout
    assert "No terminal" not in run.stdout


def test_screen_with_confirm_false_is_printed_and_goes_on(tmp_path):
    run = screen_run(tmp_path, "-e", "cassandra_operation_confirm=false")
    assert run.returncode == 0, run.stdout
    assert 'cassandra_operation_confirm is false: no question, the run goes on.' in run.stdout
    assert run.stdout.count('DECOMMISSION NODE  remove node7\\n') == 1 and "Answer yes" not in run.stdout
    assert 'WARNING  not inside tmux or screen' in run.stdout


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
    assert output.count('DECOMMISSION NODE  remove node7\\n') == 1
    assert 'WARNING  replication: orders keeps 2 replicas' in output
    assert re.search(r"Confirm the operation\]\r?\nRemove node7\? Answer yes or no:", output)
    assert "STOPPED  decommission_node  at your request, nothing changed" in output
    assert "WAY OUT  run it again" in output and "community.cassandra.decommission_node" in output


def test_screen_is_printed_before_a_run_without_terminal_stops(tmp_path):
    run = screen_run(tmp_path)
    assert run.returncode != 0
    assert run.stdout.count('DECOMMISSION NODE  remove node7\\n') == 1
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
        msg: ["DONE  decommission_node", "", "TODO", "  1. delete node7 from the inventory"]
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
    output = read_until(fd, output, lambda out: b"TODO" in out and out.rstrip().endswith(b"====="), time.time() + 120).decode()
    os.waitpid(pid, 0)
    # the screen, the question, the answer, the progress and the recap: one blank line between two blocks
    rules = [re.sub(r"^([-=])\1{3,}$", lambda m: m.group(1) * 4, re.sub(r" -{4,}$", " ----", line))
             for line in on_screen(output.strip())]
    assert rules == [
        "Reset of node5:",
        "",
        "====",  # the plan: a heavy rule above its title, a light one for each blank line inside
        "",
        "DECOMMISSION NODE  remove node7",
        "",
        "----",
        "",
        "WARNING  replication: orders keeps 2 replicas",  # the warnings together, one under the other
        "WARNING  not inside tmux or screen: if this SSH session drops, the run stops",
        "         (the operation itself goes on, unwatched; run it inside tmux or",
        "         screen)",
        "",
        "----",  # above the question
        "",
        "[community.cassandra.cassandra_operation : Confirm the operation]",
        "Remove node7? Answer yes or no:",
        "yes",  # pause clears it once read: shown again
        "",
        "[1/1] node7 decommission  LEAVING",  # each node's progress: its own block
        "[1/1] node7 decommission  DECOMMISSIONED",
        "",
        "====",  # the recap: heavy rules around it
        "",
        "DONE  decommission_node",
        "",
        "----",
        "",
        "TODO",
        "  1. delete node7 from the inventory",
        "",
        "====",
    ], output


def test_the_stop_after_a_no_names_the_cluster_and_the_command_again():
    """(2026-11-26, rig screens) "Stopped at your request, nothing changed." said neither the cluster nor how to go on."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_confirm import cassandra_confirm_stop
    assert cassandra_confirm_stop("at your request", "apply_config", "test_cluster", inventory=["/w/inv/hosts.yml"],
                                  cwd="/w") == [
        "STOPPED  apply_config  test_cluster  at your request, nothing changed",
        "WAY OUT  run it again (with the other -e options you gave), answer yes:",
        "  ansible-playbook -i inv/hosts.yml community.cassandra.apply_config"]
    # (audit A1) the run's --limit kept: never a whole-cluster command after a run on some nodes
    assert cassandra_confirm_stop("at your request", "rolling_restart", "test_cluster", limit="n1,n3")[-1] == (
        "  ansible-playbook community.cassandra.rolling_restart --limit n1,n3")
    # the caller's own command (stop_rack: its rack), as it built it
    assert cassandra_confirm_stop("at your request", "stop_rack", "test_cluster", limit="n1",
                                  again="ansible-playbook community.cassandra.stop_rack -e x=y")[-1] == (
        "  ansible-playbook community.cassandra.stop_rack -e x=y")
    # a role's question (cassandra_config): no playbook to name
    assert cassandra_confirm_stop("no yes or no after 3 answers", "", "test_cluster", "config not written") == [
        "STOPPED  test_cluster  no yes or no after 3 answers, config not written"]


def test_the_stop_passes_the_limit_the_caller_s_command_and_a_resume():
    """(audit A1, B2, C) confirm_ask.yml gives the filter the run's --limit and the caller's own command; a resume
    says nothing changed in this run (the interrupted one did)."""
    import yaml
    from ansible_collections.community.cassandra.tests.unit.helpers import render_with
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_operation", "tasks", "confirm_ask.yml")
    with open(path, encoding="utf-8") as f:
        stop = next(t for t in yaml.safe_load(f) if t["name"] == "Stop at the operator's request")

    def rendered(name, **variables):
        variables = dict(dict(hostvars={"localhost": {}}, _cassandra_run_command_args={"inventory": ["inv.yml"]},
                              cassandra_screen={"operation": "apply_config"}), **variables)
        for key in ("_operation", "_cluster"):
            variables[key] = render_with(stop["vars"][key], variables)
        return render_with(stop["vars"][name], variables)

    assert rendered("_stop", ansible_limit="n3") == {
        "inventory": ["inv.yml"], "operation": "apply_config", "cluster": "", "limit": "n3", "again": ""}
    assert rendered("_stop", _cassandra_confirm_again="X")["again"] == "X"
    assert rendered("_stop", _cassandra_confirm_operation="topology")["operation"] == "topology"
    assert rendered("_unchanged") == "nothing changed"
    assert rendered("_unchanged", cassandra_rolling_resume=True) == "nothing changed in this run"
    assert rendered("_unchanged", _cassandra_confirm_unchanged="config not written") == "config not written"


def test_every_question_s_way_out_names_its_playbook():
    """(audit B2, B3) stop_rack and upgrade give their own command (their -e options in it); topology's question and
    cassandra_config's within create_cluster and add_node name their playbook."""
    import yaml
    top = os.path.join(os.path.dirname(__file__), "..", "..", "..")

    def text(*parts):
        with open(os.path.join(top, *parts), encoding="utf-8") as f:
            return f.read()
    for playbook, words in (("stop_rack.yml", ("'stop_rack' |", "cassandra_target_rack", "limit=ansible_limit")),
                            ("upgrade.yml", ("'upgrade' |", "cassandra_upgrade_phase=", "limit=ansible_limit"))):
        again = [v["_cassandra_confirm_again"] for v in _vars(yaml.safe_load(text("playbooks", playbook)))
                 if "_cassandra_confirm_again" in v]
        assert len(again) == 1 and all(w in again[0] for w in words), (playbook, again)
    assert "_cassandra_confirm_operation: topology" in text("playbooks", "topology.yml")
    assert "_cassandra_confirm_operation: create_cluster" in text("playbooks", "create_cluster.yml")
    assert "_cassandra_confirm_operation: add_node" in text("roles", "cassandra_operation", "tasks", "action_add_prepare.yml")


def _vars(node):
    if isinstance(node, dict):
        if isinstance(node.get("vars"), dict):
            yield node["vars"]
        for value in node.values():
            yield from _vars(value)
    elif isinstance(node, list):
        for item in node:
            yield from _vars(item)
