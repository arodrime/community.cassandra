from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The summaries are lists (one line per node), not the text of a list: the
# template must be only the list, without the whitespace around the tags.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_operation_summary_is_a_list():
    msg = load("roles", "cassandra_service", "tasks", "summary.yml")[0]["ansible.builtin.debug"]["msg"]
    hostvars = {"n1": {"cassandra_op_result": "add done in 5s"}, "n2": {}}
    assert render(msg, cassandra_service_summary_hosts=["n1", "n2"], hostvars=hostvars, groups={"prod": ["n1", "n2"]}) == \
        ["n1: add done in 5s", "n2: not reached"]


def test_health_check_summary_is_a_list():
    play = load("playbooks", "health_check.yml")
    task = next(t for p in play for t in p.get("tasks", []) if t.get("name") == "Report")
    hostvars = {"n1": {"cassandra_health_problems": []}, "n2": {}}
    assert render(task["ansible.builtin.debug"]["msg"], ansible_play_hosts_all=["n1", "n2"], hostvars=hostvars) == \
        ["n1: OK", "n2: unreachable"]
