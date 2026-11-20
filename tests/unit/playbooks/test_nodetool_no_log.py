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
                for found in _tasks(task[key], hidden):
                    yield found
        yield task, hidden


def test_every_nodetool_with_its_password_hides_its_command():
    shown = []
    for path in sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml")) + glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml"))):
        with open(path) as f:
            data = yaml.safe_load(f) or []
        for task, hidden in _tasks(data):
            own = dict((k, v) for k, v in task.items() if k not in ("block", "rescue", "always", "tasks", "pre_tasks",
                                                                     "post_tasks", "handlers"))
            if "_cassandra_service_nodetool" in yaml.safe_dump(own) and not hidden:
                shown.append("%s: %s" % (os.path.relpath(path, TOP), task.get("name")))
    assert shown == []
