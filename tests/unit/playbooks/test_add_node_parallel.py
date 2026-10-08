from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node prepares every new node at once (repository, install, OS, config,
# firewall, Medusa), nothing started, then starts and bootstraps them one at a
# time without preparing them again; replace_node (action_replace.yml) still
# prepares its node itself.

import os

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
TASKS = os.path.join(TOP, "roles", "cassandra_service", "tasks")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def included(task):
    for key in ("ansible.builtin.include_role", "ansible.builtin.include_tasks", "ansible.builtin.import_tasks"):
        if key in task:
            value = task[key]
            return value.get("tasks_from") or value.get("name") if isinstance(value, dict) else value
    return None


PLAYS = load("playbooks", "add_node.yml")
NAMES = [p.get("name") for p in PLAYS]


def test_prepared_all_at_once_before_the_starts_one_at_a_time():
    prepare = PLAYS[NAMES.index("Prepare the new nodes, all at once")]
    add = PLAYS[NAMES.index("Add the new nodes, one at a time")]
    assert NAMES.index("Prepare the new nodes, all at once") < NAMES.index("Add the new nodes, one at a time")
    assert "serial" not in prepare and prepare["any_errors_fatal"] is True
    assert prepare["hosts"] == add["hosts"] == "{{ cassandra_new_nodes }}"
    assert "action_add_prepare.yml" in [included(t) for t in walk(prepare["tasks"])]
    assert add["serial"] == 1
    assert add["tasks"][0]["vars"]["_cassandra_add_prepared"] is True


def test_the_prepare_starts_nothing():
    prepare = load("roles", "cassandra_service", "tasks", "action_add_prepare.yml")
    roles = [included(t) for t in prepare]
    assert roles == ["community.cassandra.cassandra_repository", "community.cassandra.cassandra_install",
                     "community.cassandra.cassandra_linux", "community.cassandra.cassandra_config",
                     "community.cassandra.cassandra_firewall", "community.cassandra.cassandra_medusa"]
    for t in walk(prepare):
        assert "ansible.builtin.systemd_service" not in t and "ansible.builtin.service" not in t


def test_the_add_prepares_unless_prepared():
    add = load("roles", "cassandra_service", "tasks", "action_add.yml")
    first = add[0]
    assert included(first) == "action_add_prepare.yml"
    assert first["when"] == "not _cassandra_add_prepared | default(false) | bool"
    # the start comes after it, the roles only through it
    assert [included(t) for t in add[1:] if included(t) and "cassandra_" in included(t)] == []
    replace = load("roles", "cassandra_service", "tasks", "action_replace.yml")
    assert "action_add.yml" in [included(t) for t in replace]
    assert "_cassandra_add_prepared" not in str(replace)


def test_a_new_node_does_not_start_at_boot_before_its_turn():
    prepare = PLAYS[NAMES.index("Prepare the new nodes, all at once")]
    tasks = dict((t["name"], t) for t in walk(prepare["tasks"]))
    disable = tasks["Keep Cassandra from starting at boot until its turn"]
    assert disable["ansible.builtin.systemd_service"] == {"name": "cassandra", "enabled": False}
    assert "cassandra_new_node_state == 'new'" in disable["when"]  # a node in the ring keeps its setting
