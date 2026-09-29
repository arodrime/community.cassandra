from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Tasks that run on the controller (delegate_to: localhost, plays on localhost)
# must not become root there, whatever -b or an inventory ansible_become=true
# asks for the nodes: root-owned progress files and reports can then not be
# read by the controller-side lookups, which run as the user. The become
# keyword is beaten by an inventory ansible_become, so both are needed.

import glob
import os

import pytest
import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
CONTROLLER = ("localhost", "127.0.0.1")
BLOCK_KEYS = ("block", "rescue", "always")


def controller_tasks(tasks, become=None, var_become=None, on_controller=False, where=""):
    """(where, become, vars.ansible_become) of every controller-side task."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        b = task.get("become", become)
        vb = (task.get("vars") or {}).get("ansible_become", var_become)
        here = (on_controller or task.get("delegate_to") in CONTROLLER
                or task.get("connection") == "local" or "local_action" in task)
        name = "%s: %s" % (where, task.get("name", "?"))
        if any(k in task for k in BLOCK_KEYS):
            for key in BLOCK_KEYS:
                yield from controller_tasks(task.get(key), b, vb, here, name)
        elif here:
            yield name, b, vb


def scan(path):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    where = os.path.relpath(path, TOP)
    if any(isinstance(p, dict) and ("hosts" in p or "ansible.builtin.import_playbook" in p) for p in data):
        for play in data:
            if "hosts" not in play:
                continue  # import_playbook
            hosts = play["hosts"]
            local = (hosts in CONTROLLER or (isinstance(hosts, list) and set(hosts) <= set(CONTROLLER))
                     or play.get("connection") == "local")
            b = play.get("become")
            vb = (play.get("vars") or {}).get("ansible_become")
            if local:
                yield "%s: play %s" % (where, play.get("name", "?")), b, vb
            for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                yield from controller_tasks(play.get(key), b, vb, local, where)
    else:
        yield from controller_tasks(data, where=where)


FILES = sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml"))
               + glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml"))
               + glob.glob(os.path.join(TOP, "roles", "*", "handlers", "*.yml")))
FOUND = [found for path in FILES for found in scan(path)]


def test_there_are_controller_tasks():
    names = [where for where, _b, _vb in FOUND]
    assert any("progress.yml" in n for n in names)
    assert any("import_cluster.yml: play" in n for n in names)


@pytest.mark.parametrize("where, become, var_become", FOUND, ids=[f[0] for f in FOUND])
def test_controller_tasks_do_not_become(where, become, var_become):
    assert become is False, "%s: needs become: false" % where
    assert var_become is False, "%s: needs vars: ansible_become: false" % where


def test_scan_sees_playbooks_that_import_others(tmp_path):
    path = tmp_path / "pb.yml"
    path.write_text("- ansible.builtin.import_playbook: x\n"
                    "- {name: p, hosts: [localhost], tasks: [{name: t}]}\n"
                    "- {name: q, hosts: all, tasks: [{name: u, delegate_to: localhost}]}\n")
    assert [where.split(": ", 1)[1] for where, _b, _vb in scan(str(path))] == ["play p", "t", "u"]


def test_scan_sees_an_inherited_miss():
    tasks = [{"name": "b", "delegate_to": "localhost", "become": False,
              "block": [{"name": "t", "ansible.builtin.file": {}}]}]
    assert list(controller_tasks(tasks)) == [(": b: t", False, None)]
    assert len(list(controller_tasks([{"local_action": {}}, {"connection": "local"}]))) == 2
