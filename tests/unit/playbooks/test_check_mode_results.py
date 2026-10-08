from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Under --check a rolling operation changed nothing: its results and its
# change report say what a real run would do ("would restart"), never
# "restarted" or "restart done in 12s".

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
TASKS = os.path.join(TOP, "roles", "cassandra_service", "tasks")


def load(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def defaults():
    with open(os.path.join(TOP, "roles", "cassandra_service", "defaults", "main.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def find(tasks, name):
    for t in tasks:
        if t.get("name") == name:
            return t
        for key in ("block", "rescue"):
            found = find(t.get(key) or [], name)
            if found:
                return found
    return None


@pytest.mark.parametrize("action, check, expected", [
    ("restart", True, "would restart (--check: nothing was changed)"),
    ("reboot", True, "would reboot (--check: nothing was changed)"),
    ("apply_config", True, "would apply the config (--check: nothing was changed)"),
    ("restart", False, "restart done in 12s"),
    ("cleanup", True, "would clean up (--check: nothing was changed)"),
    ("cleanup_after_move", True, "would clean up after the move (--check: nothing was changed)"),
    ("add", True, "would add (--check: nothing was changed)"),
    ("add", False, "add done in 12s"),
])
def test_node_result(action, check, expected):
    task = find(load("node_operation.yml"), "Record the result")
    result = render(task["ansible.builtin.set_fact"]["cassandra_op_result"], cassandra_service_node_action=action,
                    ansible_check_mode=check, _cassandra_op_start=0, now=lambda: _Now(12),
                    _cassandra_service_would=defaults()["_cassandra_service_would"])
    assert result == expected


@pytest.mark.parametrize("check", [True, False])
def test_node_result_add_again(check):
    # add_node run again: a node that joined in an earlier run is neither added nor would be
    task = find(load("node_operation.yml"), "Record the result")
    result = render(task["ansible.builtin.set_fact"]["cassandra_op_result"], cassandra_service_node_action="add",
                    cassandra_new_node_state="joined", ansible_check_mode=check, _cassandra_op_start=0,
                    now=lambda: _Now(12), _cassandra_service_would=defaults()["_cassandra_service_would"])
    assert result == "already in the ring, nothing to add"


@pytest.mark.parametrize("check, expected", [
    (True, "would restart with its rack (--check: nothing was changed)"),
    (False, "restart done with its rack in 30s"),
])
def test_rack_result(check, expected):
    task = find(load("restart_batch.yml"), "Record the result")
    assert render(task["ansible.builtin.set_fact"]["cassandra_op_result"], ansible_check_mode=check,
                  _cassandra_restart_start=0, now=lambda: _Now(30)) == expected


@pytest.mark.parametrize("check, expected", [(True, "would restart"), (False, "restarted")])
def test_change_report(check, expected):
    task = find(load("main.yml"), "Report")
    variables = dict(task["vars"], cassandra_service_state="restarted", ansible_check_mode=check,
                     cassandra_service_unit=_Unchanged(), cassandra_service_enabled=True,
                     _cassandra_service_before={"unit": "", "active": "active", "enabled": "enabled"})
    items = render(task["vars"]["cassandra_change_report_items"], **variables)
    assert [i["after"] for i in items if i["item"] == "cassandra"] == [expected]


class _Now(object):
    def __init__(self, seconds):
        self.seconds = seconds

    def timestamp(self):
        return self.seconds


class _Unchanged(dict):
    """A registered result that did not change (the 'changed' test reads it)."""
    def __init__(self):
        super(_Unchanged, self).__init__(changed=False)
