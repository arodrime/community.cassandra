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
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import PARAGRAPH as P

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
                     _stays="node1", _imported=True, cassandra_hosts="my_cluster",
                     groups={"all": ["node1", "node3", "other1"], "my_cluster": ["node1", "node3"],
                             "my_cluster_dc1": ["node1", "node3"]})
    msg = render(say["ansible.builtin.debug"]["msg"], **variables)
    assert msg.splitlines() == [
        "TO DO",
        "  1. empty node3 before reusing the host (its data is left in place):",
        "     ansible-playbook -i inventories community.cassandra.reset_node -e cassandra_hosts=my_cluster"
        " -e cassandra_target_nodes=node3",
        P, "  2. drop node3 from the inventory: re-import the cluster with its inventory, the changes shown first:",
        "     ansible-playbook -i inventories community.cassandra.import_cluster -e cassandra_hosts=my_cluster"
        " -e import_cluster_force=true --check --diff",
        P, "  3. then write them:",
        "     ansible-playbook -i inventories community.cassandra.import_cluster -e cassandra_hosts=my_cluster"
        " -e import_cluster_force=true",
        P, "  4. commit it:",
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
    # the inventory's only cluster: the import takes it without -e cassandra_hosts
    variables = dict(say["vars"], ansible_play_hosts_all=["node1", "node2"], ansible_inventory_sources=[str(inventories)],
                     hostvars=hostvars, _imported=True, groups={"all": ["node1", "node2", "node3", "node9"],
                                                                "my_cluster": ["node1", "node2", "node3", "node9"],
                                                                "my_cluster_dc1": ["node1", "node2", "node3", "node9"]})
    msg = render(say["ansible.builtin.debug"]["msg"], **variables)
    assert msg.splitlines() == [
        "DONE  topology  my_cluster  ring = inventory: removed node3",
        "",
        "TO DO",
        "  1. empty node3 before reusing the host (its data is left in place):",
        "     ansible-playbook -i inventories community.cassandra.reset_node -e cassandra_target_nodes=node3",
        P, "  2. drop node3, node9 from the inventory: re-import the cluster with its inventory, the changes shown first:",
        "     ansible-playbook -i inventories community.cassandra.import_cluster -e import_cluster_force=true --check --diff",
        P, "  3. then write them:",
        "     ansible-playbook -i inventories community.cassandra.import_cluster -e import_cluster_force=true"]


def test_reset_node_takes_a_host_marked_absent():
    with open(os.path.join(TOP, "roles", "cassandra_service", "tasks", "reset_node_plan.yml"), encoding="utf-8") as f:
        check = yaml.safe_load(f)[0]
    that = check["ansible.builtin.assert"]["that"]
    groups = {"my_cluster": ["node1", "node2", "node3"], "my_cluster_dc1": ["node1", "node2", "node3"]}
    for host, ok in (("node3", True), ("other", False)):
        variables = {"inventory_hostname": host, "groups": groups}
        templar = Templar(loader=DataLoader(), variables=variables)
        # the cluster's group, as lookup('community.cassandra.cassandra_hosts') finds it
        expr = that.replace("lookup('community.cassandra.cassandra_hosts')", "'my_cluster'")
        assert templar.template(trust_as_template("{{ %s }}" % expr)) is ok


def test_no_dash_i_when_the_inventory_is_ansible_cfgs(tmp_path, monkeypatch):
    """Run with ansible.cfg's inventory: the reset_node command has no -i (ansible.cfg finds it)."""
    inventories = tmp_path / "inventories"
    inventories.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    config = "lookup('ansible.builtin.config', 'DEFAULT_HOST_LIST')"
    for playbook, play in (("decommission_node.yml", "Summary"), ("topology.yml", "Say what is left to do")):
        say = task(playbook, play, "Say what is left to do")
        template = say["ansible.builtin.debug"]["msg"]
        assert template.count(config) == 1, playbook  # the shared rule (cassandra_command's)
        plan = {"add": [], "remove": ["node3"], "gone": [], "silent": [], "seeds": {"step": False}}
        hostvars = {"node1": {"cassandra_topology_plan": plan, "inventory_dir": str(inventories), "ansible_host": "10.0.0.1",
                              "_cassandra_preflight": {"cassandra_cluster_name": "my_cluster"}}}
        variables = dict(say["vars"], ansible_inventory_sources=[str(inventories)], hostvars=hostvars,
                         _imported=True, _ansible_cfg_inventory=[str(inventories)],
                         groups={"all": ["node1", "node3"], "my_cluster": ["node1", "node3"],
                                 "my_cluster_dc1": ["node1", "node3"]},
                         ansible_play_hosts_all=["node3"] if playbook == "decommission_node.yml" else ["node1"])
        if playbook == "decommission_node.yml":
            variables["_stays"] = "node1"  # (from the inventory: its lookup)
        msg = render(template.replace(config, "_ansible_cfg_inventory"), **variables)
        assert "     ansible-playbook community.cassandra.reset_node -e cassandra_target_nodes=node3" in msg.splitlines()
        assert "     ansible-playbook community.cassandra.import_cluster -e import_cluster_force=true" in msg.splitlines()
        assert "-i inventories" not in msg


def test_the_reimport_reads_the_inventory():
    # its connection variables and logins (secrets.yml, vaulted) come with it: no -i <node>, no JMX login to give
    for playbook, play in (("decommission_node.yml", "Summary"), ("topology.yml", "Say what is left to do")):
        say = task(playbook, play, "Say what is left to do")
        assert "jmx_user" not in say["ansible.builtin.debug"]["msg"], playbook
        assert "reimport_hosts=_reimport_hosts" in say["ansible.builtin.debug"]["msg"], playbook
        expr = say["vars"]["_reimport_hosts"].replace("lookup('community.cassandra.cassandra_hosts')", "'prod'")

        def hosts(env="", **variables):
            variables.setdefault("groups", {"all": ["n1", "n2", "o1"], "prod": ["n1", "n2"], "prod_dc1": ["n1", "n2"]})
            variables.setdefault("ansible_inventory_sources", [TOP])  # a dir that exists
            return render(expr.replace("lookup('ansible.builtin.env', 'CASSANDRA_CLUSTER')", repr(env)), **variables)

        assert hosts() == "prod"  # another cluster in the inventory: named
        assert hosts(groups={"all": ["n1", "n2"], "prod": ["n1", "n2"],
                             "prod_dc1": ["n1", "n2"]}) == ""  # its only cluster
        assert hosts(env="prod") == ""  # CASSANDRA_CLUSTER names it
        assert hosts(env="prod", ansible_inventory_sources=["n1,"]) == "prod"  # -i n1,: CASSANDRA_CLUSTER not read
        assert hosts(cassandra_hosts="prod") == "prod"


def test_reimport_hosts_filter():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_reimport_hosts
    assert cassandra_reimport_hosts("prod", others=3) == "prod"
    assert cassandra_reimport_hosts("prod", others=0) == ""
    assert cassandra_reimport_hosts("prod", env_cluster="prod", others=3) == ""
    assert cassandra_reimport_hosts("prod", env_cluster="prod", files=False, others=3) == "prod"
    assert cassandra_reimport_hosts("prod", hosts="prod", others=0) == "prod"
