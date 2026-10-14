from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node run again after an interrupted run (node4 joined meanwhile, node5
# still to add): every line says the same thing. The prepare names the new
# nodes only, the screen says node4 is only checked, the progress lines count
# the bootstraps ([1/1] node5), none followed for node4. Rendered by Ansible.

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
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


PLAYS = dict((p.get("name"), p) for p in load("playbooks", "add_node.yml"))
HOSTS = ["node4", "node5"]
HOSTVARS = {"node4": {"inventory_hostname": "node4", "cassandra_new_node_state": "joined"},
            "node5": {"inventory_hostname": "node5", "cassandra_new_node_state": "new"}}


def test_the_prepare_names_the_new_nodes_only():
    say = next(t for t in PLAYS["Prepare the new nodes, all at once"]["tasks"] if t.get("name") == "Say what the run does")
    new = render(say["vars"]["_new"], ansible_play_hosts_all=HOSTS, hostvars=HOSTVARS)
    assert new == ["node5"]
    assert render(say["ansible.builtin.debug"]["msg"], _new=new) == "Preparing the new nodes (node5)..."
    assert "_new | length > 0" in say["when"]  # nothing to prepare: no line


def test_the_progress_lines_count_the_bootstraps():
    add = next(t for t in PLAYS["Add the new nodes, one at a time"]["tasks"] if t.get("name") == "Add this node")
    variables = dict(add["vars"], ansible_play_hosts_all=HOSTS, hostvars=HOSTVARS)
    assert render(add["vars"]["_cassandra_stream_index"], inventory_hostname="node5", **variables) == 1
    assert render(add["vars"]["_cassandra_stream_steps"], inventory_hostname="node5", **variables) == 1
    hostvars = dict(HOSTVARS, node4={"inventory_hostname": "node4", "cassandra_new_node_state": "new"})
    variables = dict(add["vars"], ansible_play_hosts_all=HOSTS, hostvars=hostvars)
    assert render(add["vars"]["_cassandra_stream_index"], inventory_hostname="node5", **variables) == 2
    # a node already in the ring: no bootstrap followed ("[1/2] node4 bootstrap NORMAL done 0s")
    wait = next(t for t in load("roles", "cassandra_service", "tasks", "action_add.yml")
                if t.get("name") == "Wait for the bootstrap")
    joined = [c for c in wait["when"] if "joined" in c]
    assert joined and render("{{ %s }}" % joined[0], cassandra_new_node_state="joined") is False
    assert render("{{ %s }}" % joined[0], cassandra_new_node_state="new") is True


def test_the_screen_says_a_node_already_in_the_ring_is_only_checked():
    check = PLAYS["Check the new nodes"]
    screen = next(t for t in check["tasks"] if t.get("name") == "Show the plan and confirm the add")
    joined = render(check["vars"]["_joined"], ansible_play_hosts_all=HOSTS, hostvars=HOSTVARS)
    assert joined == ["node4"]
    line = [i for i in screen["vars"]["cassandra_screen"]["intro"] if "_joined" in i][0]
    assert render(line, _joined=joined, _new=["node5"], _joining=[]) == \
        "Already in the ring (an earlier run), only checked: node4."
    assert render(line, _joined=joined, _new=[], _joining=[]) == ""  # said by "No node to add" then
