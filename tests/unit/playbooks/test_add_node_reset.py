from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node's reset, on by default (reset_node_plan.yml with _cassandra_node_reset_auto):
# the facts read on the node (directories found, du, system.peers files, the unit, the
# JVM) as the check sees them, and the plan it makes. The expressions are read from the
# task file and rendered by Ansible.

import os
import warnings

import pytest
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

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service")
with open(os.path.join(ROLE, "tasks", "reset_node_plan.yml"), encoding="utf-8") as f:
    TASKS = dict((t["name"], t) for t in yaml.safe_load(f))
CHECK = TASKS["Work out whether add_node may reset it"]
PLAN = TASKS["Work out the reset of {{ inventory_hostname }}"]
REFUSE = TASKS["Refuse the reset of {{ inventory_hostname }}"]
DATA = "/var/lib/cassandra/data"


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    return value


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def facts(found=(), running=False, active="inactive", du="12884901888\ttotal", peers=(), cluster="Test Cluster",
          keyspaces=None, ring_problems=(), auto=True, enabled="enabled", dir_problems=(), skipped=None):
    v = {
        "inventory_hostname": "node7", "cassandra_cluster_name": "my_cluster", "_cassandra_node_reset_auto": auto,
        "_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": "rack_b"},
        "cassandra_node_reset_unit": {"status": {"ActiveState": active, "UnitFileState": enabled, "LoadState": "loaded"}},
        "cassandra_node_reset_jvm": {"rc": 0 if running else 1},
        "cassandra_node_reset_du": {"stdout_lines": ["1\t/x", du]},
        "cassandra_node_reset_peers": {"files": [{"path": p} for p in peers]},
        "cassandra_node_reset_found": {"files": [{"path": p} for p in found], "skipped_paths": skipped or {}},
        "_cassandra_node_reset_listed": {"cluster_name": cluster},
        "_cassandra_node_reset_dirs": {"problems": list(dir_problems), "dirs": [
            {"path": DATA, "real": DATA, "kinds": ["data"], "from": ["inventory"]},
            {"path": "/var/lib/cassandra/commitlog", "real": "/var/lib/cassandra/commitlog", "kinds": ["commitlog"],
             "from": ["inventory"]}]},
        "_cassandra_node_reset_ring": {"problems": list(ring_problems), "info": []},
        "cassandra_node_reset_sysv": {},
    }
    if keyspaces is not None:
        v["cassandra_keyspaces"] = keyspaces
    v.update(trust(CHECK["vars"]))
    if auto:
        v["_cassandra_node_reset_check"] = render(CHECK["ansible.builtin.set_fact"]["_cassandra_node_reset_check"], v)
    v.update(trust(PLAN["vars"]))
    v["_cassandra_node_reset_plan"] = dict((k, render(t, v) if isinstance(t, str) else t)
                                           for k, t in PLAN["ansible.builtin.set_fact"]["_cassandra_node_reset_plan"].items())
    return v


STOCK = [DATA + "/system", DATA + "/system_schema", "/var/lib/cassandra/commitlog/CommitLog-7-1.log"]


def test_stock_node_down_is_reset():
    v = facts(found=STOCK)
    plan = v["_cassandra_node_reset_plan"]
    assert plan["problems"] == []
    assert plan["line"] == ("node7 (dc1/rack_b): has data (12.0 GiB, cluster 'Test Cluster', not in any ring, down)"
                            " — will be reset")
    assert plan["refusal"] == ""
    assert plan["stop"] is False and plan["disable"] is True and len(plan["delete"]) == 3


def test_node_without_data_needs_nothing():
    v = facts(found=[DATA + "/lost+found"], ring_problems=["no other node of the cluster answered"])
    plan = v["_cassandra_node_reset_plan"]
    assert plan["problems"] == [] and plan["line"] == "" and plan["delete"] == []
    assert plan["stop"] is False and plan["disable"] is False


def test_node_that_seems_empty_but_can_not_be_read_refused():
    # nothing found, but a directory or the live cassandra.yaml could not be read: data may hide there
    unreadable = "the live /etc/cassandra/cassandra.yaml can not be read: its directories are unknown"
    plan = facts(dir_problems=[unreadable, "data directory from inventory, /data: not a mount point"],
                 skipped={DATA: "Permission denied"})["_cassandra_node_reset_plan"]
    assert plan["problems"] == [DATA + ": can not be read (without root?)", unreadable]
    # a path the reset would refuse does not matter on an empty node
    assert facts(dir_problems=["data directory from inventory, /data: not a mount point"])["_cassandra_node_reset_plan"][
        "problems"] == []


@pytest.mark.parametrize("running, active", [(True, "inactive"), (False, "active"), (True, "active")])
def test_running_node_refused(running, active):
    v = facts(found=STOCK, running=running, active=active)
    plan = v["_cassandra_node_reset_plan"]
    assert plan["problems"][0].startswith("Cassandra runs on it: add_node resets only a node where Cassandra is down")
    assert plan["stop"] is False  # never stopped by add_node's reset
    assert plan["refusal"].startswith("node7 (dc1/rack_b): has data (12.0 GiB, cluster 'Test Cluster', not in any ring,"
                                      " running): not reset automatically")
    assert render(REFUSE["ansible.builtin.fail"]["msg"], dict(v, **trust(REFUSE["vars"]))) == plan["refusal"]


def test_user_keyspace_of_test_cluster_refused():
    plan = facts(found=STOCK + [DATA + "/shop"])["_cassandra_node_reset_plan"]
    assert plan["problems"] == ["it holds user keyspaces (shop): only a failed bootstrap of this cluster may"]


def test_failed_bootstrap_of_this_cluster_reset():
    plan = facts(found=STOCK + [DATA + "/shop"], cluster="my_cluster", keyspaces={"shop": {"rf": 3}},
                 peers=[DATA + "/system/peers_v2-c4325fbb8e5e3bafbd070f9250ed818e/nb-1-big-Data.db"])["_cassandra_node_reset_plan"]
    assert plan["problems"] == []
    assert "user keyspaces shop (a failed bootstrap of this cluster)" in plan["line"]


def test_member_of_another_ring_refused():
    plan = facts(found=STOCK, peers=[DATA + "/system/peers_v2-c4325fbb8e5e3bafbd070f9250ed818e/nb-1-big-Data.db",
                                     DATA + "/system/local-7ad54392bcdd35a684174e047860b377/nb-1-big-Data.db"])["_cassandra_node_reset_plan"]
    assert plan["problems"] == ["its system.peers lists other nodes: a member of another 'Test Cluster' ring"]
    # only system.local: alone
    plan = facts(found=STOCK, peers=[DATA + "/system/local-7ad54392bcdd35a684174e047860b377/nb-1-big-Data.db"])["_cassandra_node_reset_plan"]
    assert plan["problems"] == []


def test_another_cluster_refused():
    plan = facts(found=STOCK, cluster="billing")["_cassandra_node_reset_plan"]
    assert plan["problems"] == ["its data is of cluster 'billing', neither 'my_cluster' nor the package's stock 'Test Cluster'"]


def test_seen_in_the_ring_refused():
    seen = "node1 sees 10.100.100.7 in its ring (UN, host ID id-7): node7 is a member of the cluster, never reset it"
    plan = facts(found=STOCK, ring_problems=[seen])["_cassandra_node_reset_plan"]
    assert plan["problems"] == [seen]  # once: the check has the ring's problems
    assert "in the ring of this cluster" in plan["refusal"]


def test_reset_node_playbook_unchanged():
    # reset_node / replace_node: no add_node check, a running node may be stopped
    v = facts(found=STOCK, running=True, active="active", auto=False)
    plan = v["_cassandra_node_reset_plan"]
    assert plan["problems"] == [] and plan["stop"] is True and plan["line"] == "" and plan["refusal"] == ""


def test_topology_reads_the_keyspaces_before_the_reset_of_the_hosts_to_add():
    # the reset checks the user keyspaces of a host to add against the cluster's (else refused: unknown)
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "topology.yml"), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    names = [t.get("name") for p in plays for t in p.get("tasks", [])]
    assert names.index("Read the keyspaces for the hosts to add") < names.index("Check the hosts to add")
    assert names.index("Share the keyspaces with the hosts to add") < names.index("Check the hosts to add")
