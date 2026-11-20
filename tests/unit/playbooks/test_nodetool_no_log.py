from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# nodetool with its JMX password on the command line (-pw, _cassandra_service_nodetool): every task that runs it hides
# its command (no_log), else -v (or a failure) prints the password.

import glob
import os

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _tasks(tasks, inherited=False):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        hidden = inherited or "no_log" in task
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers"):
            if isinstance(task.get(key), list):
                yield from _tasks(task[key], hidden)
        yield task, hidden


def test_every_nodetool_with_its_password_hides_its_command():
    shown = []
    for path in sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml")) + glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml"))):
        with open(path) as f:
            data = yaml.safe_load(f) or []
        for task, hidden in _tasks(data):
            nested = ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers")
            own = dict((k, v) for k, v in task.items() if k not in nested)
            if "_cassandra_service_nodetool" in yaml.safe_dump(own) and not hidden:
                shown.append("%s: %s" % (os.path.relpath(path, TOP), task.get("name")))
    assert shown == []


def test_a_hidden_nodetool_failure_is_said_with_its_password_masked():
    # (no_log hides the failure too: each such task fails not by itself, a stop after it says its rc and error, the
    # -pw value masked; an async one is followed by its own wait)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_failure import cassandra_nodetool_failure
    silent = []
    for path in sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml")) + glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml"))):
        with open(path) as f:
            data = yaml.safe_load(f) or []
        for task, hidden in _tasks(data):
            nested = ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers")
            own = dict((k, v) for k, v in task.items() if k not in nested)
            if "_cassandra_service_nodetool" not in yaml.safe_dump(own) or "async" in task:
                continue
            if "failed_when" not in task and "ignore_errors" not in task:
                silent.append("%s: %s" % (os.path.relpath(path, TOP), task.get("name")))
    assert silent == []
    assert cassandra_nodetool_failure({"rc": 0, "stdout": "ok"}, "n1", "version") == []
    assert cassandra_nodetool_failure({"skipped": True}, "n1", "version") == []
    failed = {"rc": 1, "stderr": "error: nodetool -h 127.0.0.1 -u cassandra -pw zorglub failed: Connection refused\n-- StackTrace --"}
    assert cassandra_nodetool_failure(failed, "n1", "snapshot") == [
        "n1: nodetool snapshot failed (rc 1): error: nodetool -h 127.0.0.1 -u cassandra -pw **** failed: Connection refused",
        "Way out: fix it (nodetool snapshot on n1 shows it), then run the same command again."]
    loop = {"results": [{"rc": 0, "item": "a"}, {"rc": 2, "stdout": "No space left on device", "item": "b"}]}
    assert cassandra_nodetool_failure(loop, "n1", "clearsnapshot")[0] == \
        "n1: nodetool clearsnapshot failed (rc 2): No space left on device"
    assert cassandra_nodetool_failure({"rc": 1}, "n1", "version")[0] == "n1: nodetool version failed (rc 1): no output"
    # each one these tasks register is said by a stop right after it
    for path, register in (("roles/cassandra_operation/tasks/version_verify.yml", "cassandra_version_verify"),
                           ("playbooks/upgrade.yml", "cassandra_upgrade_prepared"),
                           ("playbooks/upgrade.yml", "cassandra_upgrade_snapshots"),
                           ("playbooks/upgrade.yml", "cassandra_upgrade_snapshot"),
                           ("playbooks/upgrade.yml", "cassandra_upgrade_cleared"),
                           ("playbooks/remove_dead_node.yml", "cassandra_dead_force_run")):
        with open(os.path.join(TOP, path)) as f:
            tasks = [t for t, dummy in _tasks(yaml.safe_load(f))]
        for i, task in enumerate(tasks):
            if task.get("register") == register:
                after = tasks[i + 1]
                assert after["name"].startswith("Stop where nodetool"), (path, register)
                assert register in after["vars"]["_failed"] and "cassandra_nodetool_failure" in after["vars"]["_failed"]
