from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster reading the ring: nodetool must read the JMX password file
# (cassandra's, 0400), a failure must say why, and the nodes found in the
# ring (add_host) must not hide the given ones.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_import_error

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name):
    return next(t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == name)


def test_nodetool_runs_with_become():
    # every play that runs nodetool, the ring read included
    for play in PLAYS:
        if "nodetool" in yaml.safe_dump(play) or "cassandra_status" in yaml.safe_dump(play):
            assert play.get("become") is True, play["name"]


def test_failure_shows_each_node_error():
    stop = task("Stop if no node answered")
    hostvars = {
        "n1": {"import_cluster_ring": {"failed": True, "msg": "Unable to determine Cassandra version: ",
                                       "stderr": "error: ******** (Permission denied)\n-- StackTrace --\n..."}},
        "n2": {},  # unreachable: the task never ran there
    }
    variables = {"import_cluster_given": ["n1", "n2"], "hostvars": hostvars}
    templar = Templar(loader=DataLoader(), variables=variables)
    errors = templar.template(trust_as_template(stop["vars"]["_errors"]))
    assert errors == ["n1: Unable to determine Cassandra version:  error: ******** (Permission denied)",
                      "n2: unreachable"]


def test_given_nodes_found_when_a_discovered_node_comes_first():
    # add_host puts the nodes found in the ring in groups['all'], maybe first;
    # only the given ones have import_cluster_given
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    hostvars = {"10.0.0.2": {}, "node1": {"import_cluster_given": ["node1"]}}
    templar = Templar(loader=DataLoader(), variables={"groups": {"all": ["10.0.0.2", "node1"]}, "hostvars": hostvars})
    assert templar.template(trust_as_template(write["vars"]["_given"])) == ["node1"]


def test_inventory_written_without_become():
    # run with -b for the nodes: sudo on the controller would fail (password) or write root's files
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    assert write.get("become") is False


def test_node_read_failure_is_reported_without_values():
    # the no_log step is rescued: its reason, kept for the report, shows no value
    block = task("Read this node's config")
    assert [t["name"] for t in block["block"]] == ["Check cassandra.yaml was read", "Turn them into cassandra_config variables"]
    keep = block["rescue"][0]["ansible.builtin.set_fact"]["import_cluster_error"]
    variables = {"ansible_failed_task": {"name": "Turn them into cassandra_config variables"},
                 "ansible_failed_result": {"failed": True, "msg": "templating failed: cassandra_config_import: "
                                           "cassandra.yaml, KeyError in _read_line(), line 3 (message hidden: it may "
                                           "show a value read from the nodes)"}}
    # the filter branch of _why (Templar here does not load the collection's filters)
    assert "cassandra_import_error" in block["rescue"][0]["vars"]["_why"]
    variables["_why"] = cassandra_import_error(variables["ansible_failed_result"])
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template(keep)) == (
        '"Turn them into cassandra_config variables" failed: cassandra_config_import: cassandra.yaml, KeyError in _read_line(), '
        'line 3 (message hidden: it may show a value read from the nodes)')
    # and the report gives it as the reason the node was not read
    match = next(t for t in task("Build the inventory")["block"] if t["name"] == "Match the ring with the hosts")
    hv = {"import_cluster_running": {"rc": 0}, "import_cluster_version": "4.1.12", "import_cluster_error": "boom"}
    templar = Templar(loader=DataLoader(), variables={"_hv": hv})
    assert templar.template(trust_as_template(match["vars"]["_node"]["reason"])) == "boom"


def test_node_read_needs_a_supported_series_and_its_cassandra_yaml():
    # a skipped loop registers results too: an unsupported version must not reach the filter
    block = task("Read this node's config")
    variables = {"import_cluster_series": "30x",
                 "import_cluster_slurp": {"skipped": True, "results": [{"skipped": True, "item": "cassandra.yaml"}]}}
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ " + block["when"] + " }}")) is False
    # without cassandra.yaml the role defaults (cluster name, seeds) would be imported
    check = block["block"][0]
    assert check["name"] == "Check cassandra.yaml was read"
    variables = {"import_cluster_conf_dir": "/etc/cassandra/conf", "import_cluster_slurp": {"results": [
        {"item": "cassandra-env.sh", "content": "eA=="},
        {"item": "cassandra.yaml", "failed": True, "msg": "file not found: /etc/cassandra/conf/cassandra.yaml"}]}}
    variables["_yaml"] = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(check["vars"]["_yaml"]))
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ " + check["ansible.builtin.assert"]["that"] + " }}")) is False
    assert templar.template(trust_as_template(check["ansible.builtin.assert"]["fail_msg"])) == (
        "file not found: /etc/cassandra/conf/cassandra.yaml")
    # the reason kept for the report is the assert's message
    rescue = {"ansible_failed_task": {"name": "Check cassandra.yaml was read"},
              "ansible_failed_result": {"failed": True, "msg": "file not found: /etc/cassandra/conf/cassandra.yaml"}}
    why = Templar(loader=DataLoader(), variables=rescue).template(trust_as_template(block["rescue"][0]["vars"]["_why"]))
    assert why == "file not found: /etc/cassandra/conf/cassandra.yaml"
