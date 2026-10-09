from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node: the new nodes that fail to be prepared (a repository mirror down
# while python3.11 is installed, on two nodes at once) are said once: one
# verdict, the same cause once with its nodes, then what to check and the
# command to run again; no "(the playbook handles it)" line before it.

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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "add_node.yml")
with open(PLAYBOOK, encoding="utf-8") as f:
    PLAY = [p for p in yaml.safe_load(f) if p.get("name") == "Prepare the new nodes, all at once"][0]
BLOCK = [t for t in PLAY["tasks"] if "block" in t][0]

EPEL = ("Failed to download metadata for repo 'epel': Cannot download repomd.xml: Cannot download repodata/repomd.xml:"
        " All mirrors were tried")


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def render_all(value, **variables):
    if isinstance(value, dict):
        return dict((k, render_all(v, **variables)) for k, v in value.items())
    return render(value, **variables)


def test_the_rescue_says_the_failure_once():
    assert BLOCK["vars"]["cassandra_output_rescued"] is True
    # an earlier rescue's failure (the keyspaces' read on the first new node) must not count as this step's
    forget = BLOCK["block"][0]
    assert forget["ansible.builtin.set_fact"] == {"ansible_failed_result": {}, "ansible_failed_task": {}}
    names = [t["name"] for t in BLOCK["rescue"]]
    assert names == ["Record the failure", "Stop here"]  # no summary of its own: the verdict says it
    stop = BLOCK["rescue"][1]
    assert stop["run_once"] is True and stop["vars"]["cassandra_output"] is True


def test_two_nodes_one_repository_down(tmp_path):
    record = BLOCK["rescue"][0]
    hostvars = {}
    for host in ("node5", "node6"):
        variables = dict(record["vars"], inventory_hostname=host, ansible_facts={"os_family": "RedHat"},
                         ansible_failed_task={"name": "Install python3.11"},
                         ansible_failed_result={"msg": EPEL, "failures": [], "rc": 1, "changed": False})
        hostvars[host] = render_all(record["ansible.builtin.set_fact"], **variables)
    # any_errors_fatal: a node prepared fine comes to the rescue too, without a failure of its own
    hostvars["node7"] = render_all(record["ansible.builtin.set_fact"], **dict(
        record["vars"], inventory_hostname="node7", ansible_facts={"os_family": "RedHat"},
        ansible_failed_task={"name": "Install python3.11"}, ansible_failed_result={}))
    assert hostvars["node7"]["cassandra_op_result"] == "not prepared: stopped with the others (a node failed)"
    assert not hostvars["node7"]["_cassandra_add_prepare_failure"]  # left out of the report (select)
    assert hostvars["node5"]["cassandra_op_result"] == (
        "add FAILED while preparing it: Install python3.11: repository 'epel' unreachable (a repository or mirror"
        " problem on the node)")
    stop = BLOCK["rescue"][1]
    inventory = tmp_path / "inventories"
    inventory.mkdir()
    msg = render(stop["ansible.builtin.fail"]["msg"], **dict(
        stop["vars"], hostvars=hostvars, ansible_play_hosts_all=["node5", "node6", "node7"],
        _cassandra_preflight={"cassandra_cluster_name": "my_cluster"}, ansible_inventory_sources=[str(inventory)],
        cassandra_target_nodes="node5,node6"))
    assert msg.splitlines() == [
        "FAILED  add_node  my_cluster  node5, node6 could not be prepared: nothing was started",
        "  node5, node6  Install python3.11: repository 'epel' unreachable (a repository or mirror problem on the node)",
        "",
        "TO DO",
        "  1. check the repositories of node5, node6 (a mirror down or unreachable from them):",
        '     ansible -i %s node5,node6 -b -m ansible.builtin.command -a "dnf -q makecache"' % inventory,
        "  2. fix the repository or mirror on the node (or its proxy)",
        "  3. or, when cqlsh gets its Python another way, set cassandra_cqlsh_python_manage: false in the inventory:"
        " python3.11 only runs cqlsh on the new nodes (the reset check reads the keyspaces with cqlsh on a node of the"
        " cluster)",
        "  4. run it again once fixed (nothing was started):",
        "     ansible-playbook -i %s community.cassandra.add_node -e cassandra_target_nodes=node5,node6" % inventory]
