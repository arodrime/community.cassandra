from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# decommission_node and topology end with what is left to do, paste-ready:
# empty the removed nodes (reset_node), drop them from the inventory (a
# re-import read from a node that stays, --check --diff first), commit only in
# a git work tree. The expressions are read from the playbooks and rendered.

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

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def task(playbook, play, name):
    with open(os.path.join(TOP, "playbooks", playbook), encoding="utf-8") as f:
        found = next(p for p in yaml.safe_load(f) if p.get("name") == play)
    return next(t for t in found["tasks"] if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_decommission_node_says_reset_then_reimport(tmp_path, monkeypatch):
    inventories = tmp_path / "inventories"
    inventories.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    say = task("decommission_node.yml", "Summary", "Say what is left to do")
    assert "not ansible_check_mode" in say["when"]
    # the re-import only for an inventory import_cluster wrote (its <cluster group>.yml there, its first line)
    hosts_file = inventories / "my_cluster.yml"
    for content, imported in (("# Written by community.cassandra.import_cluster for my_cluster: the next import replaces"
                               " it. Your own settings: other files.\n", True), ("all:\n", False), (None, False)):
        if content is None:
            hosts_file.unlink()
        else:
            hosts_file.write_text(content)
        assert render(say["vars"]["_imported"], _stays="node1", _hosts_file=str(hosts_file)) is imported, content
    assert render(say["vars"]["_imported"], _stays="node1", _hosts_file="") is False  # -i host1,host2: no dir
    variables = dict(say["vars"], ansible_play_hosts_all=["node3"], ansible_inventory_sources=[str(inventories)],
                     hostvars={"node1": {"ansible_host": "10.0.0.1", "inventory_dir": str(inventories)}},
                     _stays="node1", _imported=True)
    msg = render(say["ansible.builtin.debug"]["msg"], **variables)
    assert msg.splitlines() == [
        "TO DO",
        "  1. empty node3 before reusing the host (its data is left in place):",
        "     ansible-playbook -i inventories community.cassandra.reset_node -e cassandra_target_nodes=node3",
        "  2. drop node3 from the inventory: re-import the cluster, its changes shown first (-i 10.0.0.1, leaves the"
        " inventory out: add your connection options, e.g. -u, when it sets them):",
        "     ansible-playbook -i 10.0.0.1, community.cassandra.import_cluster -e import_cluster_force=true --check --diff",
        "  3. then write them:",
        "     ansible-playbook -i 10.0.0.1, community.cassandra.import_cluster -e import_cluster_force=true",
        "  4. commit it:",
        "     git add inventories && git commit -m 'Inventory: node3 removed' -- inventories"]


def test_topology_done_then_the_same_todo(tmp_path, monkeypatch):
    inventories = tmp_path / "inventories"
    inventories.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    say = task("topology.yml", "Say what is left to do", "Say what is left to do")
    plan = {"add": [], "remove": ["node3"], "gone": [], "silent": ["node9"], "seeds": {"step": False}}
    hostvars = {"node1": {"cassandra_topology_plan": plan, "inventory_dir": str(inventories),
                          "_cassandra_preflight": {"cassandra_cluster_name": "my_cluster"}},
                "node2": {"ansible_host": "10.0.0.2", "_cassandra_preflight_running": True}}  # running: the re-import's
    variables = dict(say["vars"], ansible_play_hosts_all=["node1", "node2"], ansible_inventory_sources=[str(inventories)],
                     hostvars=hostvars, _imported=True)
    msg = render(say["ansible.builtin.debug"]["msg"], **variables)
    assert msg.splitlines() == [
        "DONE  topology  my_cluster  ring = inventory: removed node3",
        "",
        "TO DO",
        "  1. empty node3 before reusing the host (its data is left in place):",
        "     ansible-playbook -i inventories community.cassandra.reset_node -e cassandra_target_nodes=node3",
        "  2. drop node3, node9 from the inventory: re-import the cluster, its changes shown first (-i 10.0.0.2, leaves the"
        " inventory out: add your connection options, e.g. -u, when it sets them):",
        "     ansible-playbook -i 10.0.0.2, community.cassandra.import_cluster -e import_cluster_force=true --check --diff",
        "  3. then write them:",
        "     ansible-playbook -i 10.0.0.2, community.cassandra.import_cluster -e import_cluster_force=true"]


def test_reset_node_takes_a_host_marked_absent():
    with open(os.path.join(TOP, "roles", "cassandra_service", "tasks", "reset_node_plan.yml"), encoding="utf-8") as f:
        check = yaml.safe_load(f)[0]
    that = check["ansible.builtin.assert"]["that"]
    groups = {"my_cluster": ["node1", "node2", "node3"]}
    for host, ok in (("node3", True), ("other", False)):
        variables = {"inventory_hostname": host, "groups": groups}
        templar = Templar(loader=DataLoader(), variables=variables)
        # the cluster's group, as lookup('community.cassandra.cassandra_hosts') finds it
        expr = that.replace("lookup('community.cassandra.cassandra_hosts')", "'my_cluster'")
        assert templar.template(trust_as_template("{{ %s }}" % expr)) is ok
