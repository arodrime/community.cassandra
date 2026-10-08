from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node and move_node print the cleanup command to run afterwards: both
# options at their defaults, then what else they take. The task's msg is read
# from the playbook and rendered by Ansible.

import os
import warnings

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

PLAYBOOKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks")

OPTIONS = [
    "",
    "  cassandra_cleanup_mode: sequential (one node at a time, default) | rack (a rack at a time)",
    "                          | dc (a datacenter at a time) | all (every node at once: heavy I/O)",
    "  cassandra_cleanup_jobs: compaction threads per node, default 2 (0 = all compaction threads)",
]


def hint(playbook, **variables):
    with open(os.path.join(PLAYBOOKS, playbook), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    play = [p for p in plays if any(t.get("name") == "Say which cleanups to run" for t in p.get("tasks", []))][0]
    task = [t for t in play["tasks"] if t["name"] == "Say which cleanups to run"][0]
    values = {"ansible_inventory_sources": ["inventory/prod.yml"], "_targets": ["node1", "node4"],
              "cassandra_cleanup_jobs": play["vars"]["cassandra_cleanup_jobs"],
              "_hosts_option": trust_as_template(task["vars"]["_hosts_option"])}
    values.update(variables)
    templar = Templar(loader=DataLoader(), variables=values)
    msg = task["ansible.builtin.debug"]["msg"]
    if isinstance(msg, str):  # one multi-line string (plain text under any callback)
        assert (task.get("vars") or {}).get("cassandra_output") is True
        templar.available_variables = dict(values, _nl="\n")
        return templar.template(trust_as_template(msg)).split("\n")
    return [templar.template(trust_as_template(line)) for line in msg]


def test_add_node_prints_both_options_at_their_defaults():
    assert hint("add_node.yml", _scope="dc1: the new nodes' racks only") == [
        "Once the cluster is fine, clean up the nodes that handed data over (dc1: the new nodes' racks only):",
        "  ansible-playbook -i inventory/prod.yml community.cassandra.cleanup --limit node1,node4"
        " -e cassandra_cleanup_mode=sequential -e cassandra_cleanup_jobs=2",
    ] + OPTIONS


def test_move_node_prints_both_options_and_the_group_given():
    assert hint("move_node.yml", _move_record="/tmp/moves.txt", cassandra_hosts="orders") == [
        "Once the cluster is fine, clean up the nodes that lost ranges (listed in /tmp/moves.txt until a move_node"
        " run with cassandra_move_cleanup cleans them up):",
        "  ansible-playbook -i inventory/prod.yml community.cassandra.cleanup -e cassandra_hosts=orders"
        " --limit node1,node4 -e cassandra_cleanup_mode=sequential -e cassandra_cleanup_jobs=2",
    ] + OPTIONS
