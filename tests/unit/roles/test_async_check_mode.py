from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Ansible refuses async under --check ("check mode and async cannot be used on
# same task"): every async task of cassandra_service is skipped in check mode,
# by its own when or its block's.

import glob
import os

import pytest
import yaml

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks")
# their --check (upgrade, cleanup) is not covered by this test yet
NOT_IN_CHECK_MODE = {"action_upgradesstables.yml", "cleanup_batch.yml"}


def conditions(task):
    when = task.get("when", [])
    return " ".join(when if isinstance(when, list) else [str(when)])


def async_tasks(items, inherited=""):
    for t in items or []:
        cond = inherited + " " + conditions(t)
        if "async" in t:
            yield t, cond
        for key in ("block", "rescue", "always"):
            for found in async_tasks(t.get(key), cond):
                yield found


FILES = sorted(os.path.basename(p) for p in glob.glob(os.path.join(TASKS, "*.yml")))


@pytest.mark.parametrize("name", [f for f in FILES if f not in NOT_IN_CHECK_MODE])
def test_async_tasks_are_skipped_in_check_mode(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        items = yaml.safe_load(f)
    for task, cond in async_tasks(items):
        assert "not ansible_check_mode" in cond, "%s: %s" % (name, task.get("name"))


def test_decommission_is_one_of_them():
    with open(os.path.join(TASKS, "action_decommission.yml"), encoding="utf-8") as f:
        assert [t["name"] for t, _ in async_tasks(yaml.safe_load(f))] == ["Decommission the node"]
