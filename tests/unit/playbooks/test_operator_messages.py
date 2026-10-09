from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The operator messages are marked for the ops callback (task variable cassandra_output: true): unmarked, a
# debug task prints nothing under it.

import glob
import os

import pytest
import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
DEBUG = ("debug", "ansible.builtin.debug")

MARKED = [
    ("playbooks/add_datacenter.yml", "Say what is left to do"),
    ("playbooks/add_datacenter.yml", "Say the cluster is not checked (--check)"),
    ("playbooks/start_rack.yml", "Say the cluster is not checked (--check)"),
    ("playbooks/remove_datacenter.yml", "Say what is left to do"),
    ("playbooks/remove_dead_node.yml", "Say what is left to do"),
    ("playbooks/upgrade.yml", "Say what is left to do"),
    ("playbooks/upgrade.yml", "Say what the next phase is"),
    ("roles/cassandra_change_report/tasks/main.yml", "Show changes ({{ cassandra_change_report_role }})"),
    ("roles/cassandra_config/tasks/main.yml", "Warn about a variable under its former name"),
    ("roles/cassandra_config/tasks/main.yml", "Say when Cassandra is newer than the templates"),
    ("roles/cassandra_install/tasks/dsbulk.yml", "Say dsbulk is not downloaded (offline)"),
    ("roles/cassandra_install/tasks/install.yml", "Say python3.11 is missing (cqlsh only)"),
    ("roles/cassandra_install/tasks/install.yml", "Say jemalloc is missing (optional)"),
    ("roles/cassandra_install/tasks/packages.yml", "Say what would be installed (--check)"),
    ("roles/cassandra_linux/tasks/os.yml", "Say the time sync service is missing (offline)"),
    ("roles/cassandra_linux/tasks/os.yml", "Say which data directories are not tuned"),
    ("roles/cassandra_medusa/tasks/main.yml", "Warn about variables under their former names"),
    ("roles/cassandra_medusa/tasks/main.yml", "Say python3.11 is not offered yet (--check)"),
    ("roles/cassandra_medusa/tasks/main.yml", "Say another medusa comes first in the PATH"),
    ("roles/cassandra_repository/tasks/repository.yml", "Say the repository could not be checked"),
    ("roles/cassandra_service/tasks/action_move.yml", "Say which receiving nodes are not checked"),
    ("roles/cassandra_service/tasks/main.yml", "Warn that a Cassandra started by the init script keeps running"),
]


def tasks(path):
    with open(os.path.join(TOP, path), encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    todo = []
    for item in data:  # a play (or an imported playbook), or a task of a role
        if "hosts" in item or "import_playbook" in item or "ansible.builtin.import_playbook" in item:
            todo += [t for k in ("pre_tasks", "tasks", "post_tasks", "handlers") for t in item.get(k) or []]
        else:
            todo.append(item)
    while todo:
        t = todo.pop(0)
        todo += [c for k in ("block", "rescue", "always") for c in t.get(k) or []]
        yield t


@pytest.mark.parametrize("path, name", MARKED)
def test_marked(path, name):
    found = [t for t in tasks(path) if t.get("name") == name]
    assert found, (path, name)
    for t in found:
        assert (t.get("vars") or {}).get("cassandra_output") is True, (path, name)


def test_every_what_is_left_and_next_phase_message_marked():
    # what the operator does next: never left to the default callback only
    for path in sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml"))):
        for t in tasks(os.path.relpath(path, TOP)):
            if any(a in t for a in DEBUG) and str(t.get("name", "")).startswith(("Say what is left", "Say what the next")):
                assert (t.get("vars") or {}).get("cassandra_output") is True, (path, t["name"])


def test_marking_keeps_the_task_variables():
    # vars given by a YAML alias: merged with the marker, not replaced by a second vars key
    t = next(t for t in tasks("roles/cassandra_repository/tasks/repository.yml")
             if t.get("name") == "Say the repository could not be checked")
    assert {"_results", "_codes", "_shown", "cassandra_output"} <= set(t["vars"])
