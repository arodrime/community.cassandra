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


def conditions(task):
    when = task.get("when", [])
    return " ".join(when if isinstance(when, list) else [str(when)])


def async_tasks(items, inherited=""):
    for t in items or []:
        cond = inherited + " " + conditions(t)
        if "async" in t:
            yield t, cond
        for key in ("block", "rescue", "always"):
            yield from async_tasks(t.get(key), cond)


FILES = sorted(os.path.basename(p) for p in glob.glob(os.path.join(TASKS, "*.yml")))


@pytest.mark.parametrize("name", FILES)
def test_async_tasks_are_skipped_in_check_mode(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        items = yaml.safe_load(f)
    for task, cond in async_tasks(items):
        assert "not ansible_check_mode" in cond, "%s: %s" % (name, task.get("name"))


def test_decommission_is_one_of_them():
    with open(os.path.join(TASKS, "action_decommission.yml"), encoding="utf-8") as f:
        assert [t["name"] for t, unused in async_tasks(yaml.safe_load(f))] == ["Decommission the node"]


def all_tasks(items):
    for t in items or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from all_tasks(t.get(key))


@pytest.mark.parametrize("name", FILES)
def test_progress_file_written_only_outside_check_mode(name):
    # --check creates no progress file, and lineinfile fails on a missing one even in check mode
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        items = yaml.safe_load(f)
    for task in all_tasks(items):
        if "cassandra_progress_file" in str(task.get("ansible.builtin.lineinfile", {}).get("path", "")):
            assert "not ansible_check_mode" in conditions(task), "%s: %s" % (name, task.get("name"))


def test_progress_file_writers_are_found():
    found = []
    for name in FILES:
        with open(os.path.join(TASKS, name), encoding="utf-8") as f:
            items = yaml.safe_load(f)
        found += [name for t in all_tasks(items) if "cassandra_progress_file" in str(t.get("ansible.builtin.lineinfile", {}))]
    assert sorted(found) == ["cleanup_batch.yml", "node_operation.yml", "restart_batch.yml"]
