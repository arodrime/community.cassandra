from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The size guardrail of the Jinja strings: under 300 characters, the logic in a filter (readable, tested in Python).
# The templates the rc1 fixes grew past it (the start_rack rescue 319, stop_rack's refusal 519 and start command 379,
# the recap's results 797), held under it.

import os

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
LIMIT = 300


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def task(playbook, name):
    return next(t for p in load("playbooks", playbook) for t in p.get("tasks", []) for t in [t] + t.get("rescue", [])
                if t.get("name") == name)


def test_the_rack_and_recap_templates_are_under_the_guardrail():
    templates = {
        "start_rack rescue": task("start_rack.yml", "Stop on a node of the rack that did not start")[
            "ansible.builtin.fail"]["msg"],
        "stop_rack refusal": task("stop_rack.yml", "Refuse when a keyspace would lose more than one replica")[
            "ansible.builtin.assert"]["fail_msg"],
        "stop_rack start command": task("stop_rack.yml", "Say how to start it again")["vars"]["_start"],
        "recap results": load("roles", "cassandra_operation", "vars", "main.yml")["_cassandra_op_results"]}
    assert dict((k, len(v)) for k, v in templates.items() if len(v) > LIMIT) == {}
