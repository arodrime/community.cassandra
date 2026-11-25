from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config on many nodes is bound by the controller: before each task, Ansible builds each node's variables,
# its facts copied in (seconds per task with a real server's packages and services in them), and each read of
# hostvars[node] builds that node's again. So: no package_facts or service_facts on the way of the operations
# (the packages and the unit read by name), and no task added without a thought for its cost (a ceiling).

import glob
import os

import yaml

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
TASKS_MAX = 2003  # +1: apply_config reads every node once (its nodes, its plan; 2 tasks less on each node)


def tasks(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        inner = [t for key in ("block", "rescue", "always") for t in item.get(key) or []]
        if inner:
            yield from tasks(inner)
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


def test_every_named_read_of_packages_is_in_english():
    # cassandra_installed_packages knows a name not installed by rpm's and dpkg-query's English words
    reads = [task for path, found in task_files() for task in found
             if "cassandra_package_query" in str(task.get("ansible.builtin.command"))]
    assert len(reads) == 7
    assert all((task.get("environment") or {}).get("LC_ALL") == "C" for task in reads)


def test_the_preflight_reads_whether_cassandra_runs_from_its_unit():
    from ansible_collections.community.cassandra.tests.unit.helpers import render
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "tasks", "node_facts.yml"), encoding="utf-8") as f:
        found = list(tasks(yaml.safe_load(f)))
    expr = next(t for t in found if t.get("name") == "Is Cassandra running here")["ansible.builtin.set_fact"]
    for read, running in (({"rc": 0, "stdout": "SubState=running"}, True), ({"rc": 0, "stdout": "SubState=dead"}, False),
                          ({"rc": 0, "stdout": "SubState=failed"}, False), ({"rc": 1, "stdout": ""}, False)):
        assert str(render(expr["_cassandra_preflight_running"], cassandra_node_facts_unit=read)) == str(running)


def apply_config_plays():
    with open(os.path.join(ROOT, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
        return dict((p["name"], p) for p in yaml.safe_load(f) if "hosts" in p)


def test_the_plays_that_write_read_no_other_node_per_node():
    # each node of these plays reads what apply_config noted of every node once (its view, the nodes each one
    # may find down), never every node's variables: read on each node, that cost grew with the square of the nodes
    plays = apply_config_plays()
    for name in ("Apply the OS settings, on these nodes at once", "Apply the config where nothing is restarted, on these nodes at once",
                 "Apply the config, one node at a time"):
        text = yaml.safe_dump(plays[name]["tasks"]).replace("hostvars['localhost']", "")
        assert "hostvars" not in text, name
        assert "cassandra_apply_config_down_ok(" not in text, name


def test_the_cluster_nodes_and_the_view_are_read_once_per_run():
    find = apply_config_plays()["Find what each node needs"]["tasks"]
    names = [t.get("name") for t in find]
    memo = find[names.index("Note the cluster's nodes for the run")]
    assert memo["run_once"] is True and "memo=true" in memo["ansible.builtin.set_fact"]["_cassandra_nodes_memo"]
    assert names.index("Note the cluster's nodes for the run") < names.index("Say what the run does")
    for name in ("Note what the run needs of every node", "Note the other nodes each node may find down"):
        assert find[names.index(name)]["run_once"] is True
    # the refusal reads the view, not every node's variables
    refused = find[names.index("Note whether restarts are refused while a node is not running")]
    assert "hostvars" not in yaml.safe_dump(refused)
    # the health checks read the ring's names from the run's
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "tasks", "cluster_health_read.yml"), encoding="utf-8") as f:
        assert "_cassandra_run_addresses" in f.read()


def test_the_view_is_noted_before_any_task_reads_it():
    find = apply_config_plays()["Find what each node needs"]["tasks"]
    names = [t.get("name") for t in find]
    first = names.index("Note what the run needs of every node")
    readers = [i for i, t in enumerate(find) if "_cassandra_apply_config_view" in yaml.safe_dump(t) and i != first]
    assert readers and min(readers) > first
    assert names.index("Note what this node needs") < first  # (the plan it reads)
