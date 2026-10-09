from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node and replace_node show what a reset deletes on their own screen
# (cassandra_reset_warnings): reset_node_plan.yml then prints nothing of its
# own, the details once; reset_node itself still prints them.

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


def shown(**variables):
    say = next(t for t in load("roles", "cassandra_service", "tasks", "reset_node_plan.yml")
               if t["name"] == "What the reset of {{ inventory_hostname }} would do")
    variables.setdefault("_p", {"delete": ["/var/lib/cassandra/data/system"]})
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(templar.template(trust_as_template("{{ %s }}" % w)) for w in say["when"])


def test_the_details_once():
    assert shown()  # reset_node: the details, then its question
    assert shown(_cassandra_node_reset_auto=True)  # add_node's before: printed, and again in the screen
    assert not shown(_cassandra_node_reset_auto=True, _cassandra_node_reset_on_screen=True)
    assert not shown(_cassandra_notes_deferred=True)  # topology: in its plan


def test_add_node_and_replace_node_show_it_on_their_screen():
    for playbook, name in (("add_node.yml", "Work out the reset of the new nodes (cassandra_add_node_reset)"),
                           ("replace_node.yml", None)):
        tasks = [t for p in load("playbooks", playbook) for t in p.get("tasks", [])
                 if (t.get("ansible.builtin.include_role") or {}).get("tasks_from") == "reset_node_plan.yml"]
        assert tasks and all(t["vars"]["_cassandra_node_reset_on_screen"] is True for t in tasks), playbook
        assert name is None or tasks[0]["name"] == name
