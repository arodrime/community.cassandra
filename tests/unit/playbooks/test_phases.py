from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Phase lines: one per major phase of add_node, decommission_node, topology,
# reset_node, apply_config, rolling_restart, cleanup and import_cluster,
# marked for the ops callback (plain, its phase colour by the "..." end),
# their words in one place (module_utils cassandra_output PHASES).

import os
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.callback.ops import colour
from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def phases(*path):
    """The phase tasks of a playbook (every play) or a task file: their msg templates."""
    data = load(*path)
    tasks = [t for p in data for t in walk(p.get("tasks"))] if path[0] == "playbooks" else list(walk(data))
    return [t for t in tasks if "cassandra_phase" in str((t.get("ansible.builtin.debug") or {}).get("msg", ""))]


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_the_words():
    assert out.phase("check", count=5) == "Checking the cluster (5 nodes)..."
    assert out.phase("ring") == "Reading the ring and seeds..."
    assert out.phase("prepare", ["node5", "node6"]) == "Preparing the new nodes (node5, node6)..."
    assert out.phase("join", node="node5") == "Starting node5 and waiting for its bootstrap..."
    assert out.phase("decommission", node="node3") == "Decommissioning node3..."
    assert out.phase("wait_un") == "Waiting for every node to be UN..."
    assert out.phase("write_config", ["node2"]) == "Writing the config (node2)..."
    assert out.phase("restart", node="node2") == "Restarting node2..."
    assert out.phase("cleanup", ["node1", "node2", "node3", "node5"]) == "Cleaning up node1..node3, node5..."
    assert out.phase("no such phase") == ""
    # each one in the ops callback's phase colour, a line of its own
    for name in out.PHASES:
        line = out.phase(name, ["node1"], node="node1", what="x", count=1)
        assert line.endswith("...") and "\n" not in line and colour(line) is not None, name


@pytest.mark.parametrize("path, at_least", [
    (("playbooks", "preflight.yml"), 1), (("playbooks", "add_node.yml"), 2), (("playbooks", "decommission_node.yml"), 1),
    (("playbooks", "topology.yml"), 2), (("playbooks", "reset_node.yml"), 1), (("playbooks", "apply_config.yml"), 1),
    (("playbooks", "import_cluster.yml"), 1), (("roles", "cassandra_service", "tasks", "node_operation.yml"), 2),
    (("roles", "cassandra_service", "tasks", "cleanup_batch.yml"), 1),
    (("roles", "cassandra_service", "tasks", "restart_batch.yml"), 1),
    (("roles", "cassandra_service", "tasks", "action_apply_config.yml"), 1),
    (("roles", "cassandra_service", "tasks", "reset_node.yml"), 1)])
def test_marked_phase_lines(path, at_least):
    found = phases(*path)
    assert len(found) >= at_least, path
    for task in found:
        # an operator message, a block of its own
        assert task["vars"]["cassandra_output"] is True and task["vars"]["cassandra_output_gap"] is True, task["name"]


@pytest.mark.parametrize("action, extra, line", [
    ("add", {}, "Starting node5 and waiting for its bootstrap..."),
    ("add", {"cassandra_new_node_state": "joining"}, "Waiting for the bootstrap of node5 (started by an earlier run)..."),
    ("add", {"cassandra_new_node_state": "joined"}, ""),
    # add_datacenter: no bootstrap
    ("add", {"cassandra_extra_settings": {"auto_bootstrap": False}},
     "Starting node5, joining without streaming (auto_bootstrap false)..."),
    ("decommission", {}, "Decommissioning node5..."),
    ("decommission", {"cassandra_leaving_node": {"state": "leaving"}},
     "Waiting for the decommission of node5 (started by an earlier run)..."),
    ("decommission", {"cassandra_leaving_node": {"state": "decommissioned"}}, ""),
    ("restart", {}, "Restarting node5..."),
    ("apply_config", {}, "Writing the config (node5)..."),
    ("replace", {"cassandra_replace_address": "10.0.0.9"}, "Starting node5 and waiting while it replaces 10.0.0.9..."),
    ("update_java", {}, "Updating Java on node5...")])
def test_each_node_says_what_is_done_to_it(action, extra, line):
    say = next(t for t in phases("roles", "cassandra_service", "tasks", "node_operation.yml")
               if t["name"].startswith("Say what is done"))
    variables = dict(say["vars"], inventory_hostname="node5", cassandra_service_node_action=action, **extra)
    variables["_phase"] = render(say["vars"]["_phase"], **variables)
    assert render(say["ansible.builtin.debug"]["msg"], **variables).strip() == line
    assert "not ansible_check_mode" in say["when"]  # --check does none of it
