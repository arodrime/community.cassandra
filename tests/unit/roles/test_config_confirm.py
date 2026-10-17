from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config's question on a node that is not blank: after the diffs it
# is about, once for the batch, its answer for every node of the batch.

import os
import re
import select
import sys
import time

import warnings

import pytest
import yaml

from ansible.plugins.loader import init_plugin_loader

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

from ansible_collections.community.cassandra.tests.unit.roles.test_config_preview import initialized_facts, render, task  # noqa: E402

pty = pytest.importorskip("pty")

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
DATA = "/data/cassandra/data"
STALE = "/var/lib/cassandra/data"  # the package's default: not one of the inventory's data dirs
# its data dirs empty, a system keyspace left in the package's default dir
STALE_ONLY = dict(cassandra_data_file_directories=[DATA], isdir=lambda d: d == STALE + "/system")


def test_a_system_keyspace_in_the_package_dir_still_counts():
    # a node whose inventory does not match it yet: the guard stays
    facts = initialized_facts(None, **STALE_ONLY)
    assert facts["_cassandra_config_initialized"] is True
    assert facts["_cassandra_config_initialized_why"] == STALE + "/system"


def test_the_question_comes_after_the_diffs_it_is_about():
    # "diffs above": printed by the ops callback too (marked), each node's under its name, when it asks
    shown = task("Show the config changes")
    asked = task("Show the config changes to confirm")
    assert "cassandra_output" not in (shown.get("vars") or {}) and asked["vars"]["cassandra_output"] is True
    preview = {"results": [{"stdout": "-a\n+b", "stdout_lines": ["-a", "+b"], "cassandra_config_file": "cassandra.yaml"},
                           {"stdout": "", "stdout_lines": [], "cassandra_config_file": "logback.xml"}]}
    variables = dict(inventory_hostname="n4", cassandra_config_preview=preview, _cassandra_config_kept=[])
    assert render(shown["ansible.builtin.debug"]["msg"], **variables) == ["-a", "+b"]
    assert render(asked["ansible.builtin.debug"]["msg"], **variables) == ["n4:", "-a", "+b"]
    for ask, check, says in ((True, False, (False, True)), (False, False, (True, False)), (True, True, (True, False))):
        variables = dict(_cassandra_config_changes=["cassandra.yaml"], _cassandra_config_ask=ask, ansible_check_mode=check)
        assert tuple(all(render("{{ %s }}" % c, **variables) for c in t["when"]) for t in (shown, asked)) == says


# cassandra_config's question, when it asks (nodes already in the ring):
# one prompt for the batch, its answer for each of its nodes

PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: What the role found on each node (read from the others through hostvars)
      ansible.builtin.set_fact:
        _cassandra_config_ask: true
        _cassandra_config_changes: [cassandra.yaml]
        _cassandra_config_initialized: true
        _cassandra_config_initialized_why: /var/lib/cassandra/data/system

    - name: Write the config
      ansible.builtin.include_tasks: confirm.yml

    - name: Go on
      ansible.builtin.debug:
        msg: went on
"""


def confirm_tasks():
    """The role's two tasks, as they are in tasks/main.yml."""
    ask, check = task("Confirm config changes"), task("Stop unless the changes were confirmed")
    assert ask["register"] == "cassandra_config_answer"
    return [ask, check]


def run_in_terminal(tmp_path, answer):
    (tmp_path / "confirm.yml").write_text(yaml.safe_dump(confirm_tasks()))
    (tmp_path / "playbook.yml").write_text(PLAYBOOK)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0",
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", "node1,node2,", "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable, str(tmp_path / "playbook.yml")]
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.chdir(str(tmp_path))
        os.execve(argv[0], argv, env)
    output, deadline, sent = b"", time.time() + 120, False
    while time.time() < deadline:
        ready, dummy, dummy = select.select([fd], [], [], 1)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:  # the run ended
                break
            if not data:
                break
            output += data
        if not sent and b"Type 'yes' to apply" in output:
            time.sleep(1)  # pause drops what was typed before it reads
            os.write(fd, answer.encode() + b"\r")
            sent = True
    rc = os.waitpid(pid, 0)[1]
    rc = os.waitstatus_to_exitcode(rc) if hasattr(os, "waitstatus_to_exitcode") else rc >> 8
    return rc, output.decode(errors="replace")


def test_the_answer_holds_for_every_node_of_the_batch(tmp_path):
    rc, output = run_in_terminal(tmp_path, "yes")
    assert rc == 0, output
    assert output.count("Type 'yes' to apply") == 1  # one question for the batch
    assert re.search(r"node1: cassandra.yaml\r?\n\s*node2: cassandra.yaml", output), output
    assert len(re.findall(r'"msg": "went on"', output)) == 2, output
    assert "not confirmed" not in output


def test_no_stops_every_node_of_the_batch(tmp_path):
    rc, output = run_in_terminal(tmp_path, "no")
    assert rc != 0
    assert sorted(set(re.findall(r"Config changes not confirmed on (node\d)", output))) == ["node1", "node2"], output
    assert "went on" not in output
