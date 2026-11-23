from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config on many nodes is bound by the controller: before each task, Ansible builds each node's variables,
# its facts copied in (seconds per task with a real server's packages and services in them), and each read of
# hostvars[node] builds that node's again. So: no package_facts or service_facts on the way of the operations
# (the packages and the unit read by name), and no task added without a thought for its cost (a ceiling).

import glob
import os

import yaml

from ansible_collections.community.cassandra.tests.unit.task_files import find_play, find_task

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
# the task files that still run package_facts or service_facts, and when: none on the preflight or apply_config's
# compare (only shrinks)
FACTS_LEFT = {
    "roles/cassandra_config/tasks/apply.yml": "a config written while Cassandra runs, a seed list to reload",
    "roles/cassandra_config/tasks/node_state.yml": "a node whose init is not systemd",
    "roles/cassandra_operation/tasks/action_restart.yml": "apply_config's restart of a node moving to another release",
    "roles/cassandra_operation/tasks/apply_config_window.yml": "the same, its package update failed",
    "roles/cassandra_operation/tasks/new_node_read.yml": "a new node (add_node, replace_node)",
    "roles/cassandra_operation/tasks/seeds_apply.yml": "a seed list written (change_seeds, topology)",
}
FACTS_MODULES = ("ansible.builtin.package_facts", "ansible.builtin.service_facts", "package_facts", "service_facts")
# the tasks of the playbooks and roles (each task of a block, the block not counted): a task more costs every node
# a build of its variables, run or skipped. Raise it with the reason in the commit, or lower it
TASKS_MAX = 1929


def tasks(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        inner = [t for key in ("block", "rescue", "always") for t in item.get(key) or []]
        if inner:
            for task in tasks(inner):
                yield task
        else:
            yield item


def task_files():
    paths = glob.glob(os.path.join(ROOT, "roles", "*", "tasks", "*.yml")) + glob.glob(os.path.join(ROOT, "playbooks", "*.yml"))
    for path in sorted(paths):
        with open(path, encoding="utf-8") as f:
            content = yaml.safe_load(f) or []
        plays = [p for p in content if isinstance(p, dict) and "hosts" in p]
        items = [t for p in plays for key in ("pre_tasks", "tasks", "post_tasks", "handlers") for t in p.get(key) or []]
        yield os.path.relpath(path, ROOT), list(tasks(items if plays else content))


def test_no_package_or_service_facts_on_the_operations_way():
    found = sorted(set(path for path, found in task_files() for task in found if any(m in task for m in FACTS_MODULES)))
    assert found == sorted(FACTS_LEFT)


def test_task_count_ceiling():
    count = sum(len(found) for path, found in task_files())
    assert count <= TASKS_MAX, "%d tasks, %d allowed" % (count, TASKS_MAX)


def test_the_recap_reads_the_nodes_once():
    # the recap and the report read every node's results: kept as facts first (a fact beats the play's variable of
    # the same name), each read building every node's variables once, not at each use
    with open(os.path.join(ROOT, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
        summary = find_play(yaml.safe_load(f), "Summary")
    read = find_task(summary, "Read what each node got")
    kept = read["ansible.builtin.set_fact"]
    assert read["run_once"] is True and set(kept) <= set(summary["vars"])
    assert all(value == "{{ %s }}" % name for name, value in kept.items())
    names = [t["name"] for t in summary["tasks"]]
    assert names.index("Read what each node got") < names.index("Print the changes") < names.index("Write the report of the run")
