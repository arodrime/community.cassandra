from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node's reset, on by default (reset_node_plan.yml with _cassandra_node_reset_auto):
# the facts read on the node (directories found, du, system.peers files, the unit, the
# JVM) as the check sees them, and the plan it makes. The expressions are read from the
# task file and rendered by Ansible.

import os
import time
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


def _tasks(name):
    with open(os.path.join(ROLE, "tasks", name), encoding="utf-8") as f:
        return dict((t["name"], t) for t in yaml.safe_load(f))


TASKS = _tasks("reset_node_plan.yml")
KEEP = _tasks("node_standing_read.yml")["Keep what was read"]
STANDING = _tasks("node_standing.yml")["Work out where {{ inventory_hostname }} stands"]
CHECK = TASKS["Work out whether this node may be reset"]
PLAN = TASKS["Work out the reset of {{ inventory_hostname }}"]
REFUSE = TASKS["Refuse the reset of {{ inventory_hostname }}"]
DATA = "/var/lib/cassandra/data"
N1 = {"address": "10.0.0.1", "status": "U", "state": "N", "host_id": "id-1"}
SEEN = {"address": "10.0.0.7", "status": "U", "state": "N", "host_id": "id-7"}


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    return value


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def facts(found=(), running=False, active="inactive", du="12884901888\ttotal", peers=(), cluster="Test Cluster",
          keyspaces=None, ring="clear", auto=True, enabled="enabled", dir_problems=(), skipped=None, mark=None,
          given=None, view=None, own_view=None):
    """What the node reads (node_standing_read.yml), where it stands (node_standing.yml), then the reset's check and
    plan, all rendered from the task files. ring: what node1, the node of the cluster asked, sees: clear (not this
    node), seen (this node UN), none (no answer)."""
    status = {"clear": {"dc1": {"nodes": [N1]}}, "seen": {"dc1": {"nodes": [N1, SEEN]}}, "none": None}[ring]
    v = {
        "inventory_hostname": "node7", "cassandra_cluster_name": "my_cluster", "_cassandra_node_reset_auto": auto,
        "_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": "rack_b", "address": "10.0.0.7"},
        "groups": {"cassandra": ["node1", "node7"]},
        "hostvars": {"node1": {"_cassandra_preflight": {"address": "10.0.0.1"}},
                     "node7": {"_cassandra_preflight": {"address": "10.0.0.7"}}},
        "cassandra_node_reset_unit": {"status": {"ActiveState": active, "UnitFileState": enabled, "LoadState": "loaded"}},
        "cassandra_node_reset_jvm": {"rc": 0 if running else 1},
        "cassandra_node_reset_du": {"stdout_lines": ["1\t/x", du]},
        "cassandra_node_reset_peers": {"files": [{"path": p} for p in peers]},
        "cassandra_node_reset_found": {"files": [{"path": p} for p in found], "skipped_paths": skipped or {}},
        "cassandra_node_reset_live_stat": {"stat": {"exists": True}},
        "cassandra_node_reset_live": {"content": ""},
        "cassandra_node_standing_elsewhere": {"results": []},
        "_cassandra_node_standing_data_dirs": {"own": [DATA], "elsewhere": [], "problem": ""},
        "_cassandra_node_reset_listed": {"cluster_name": cluster, "addresses": [], "dirs": []},
        "_cassandra_node_reset_dirs": {"problems": list(dir_problems), "dirs": [
            {"path": DATA, "real": DATA, "kinds": ["data"], "from": ["inventory"]},
            {"path": "/var/lib/cassandra/commitlog", "real": "/var/lib/cassandra/commitlog", "kinds": ["commitlog"],
             "from": ["inventory"]}]},
        # the node of the cluster asked (the preflight did not read it): its answer
        "_cassandra_node_standing_from": ["node1"],
        "cassandra_node_reset_rings": {"results": [{"item": "node1", "cluster_status": status}]},
        # running: its own ring (alone), its host ID
        "cassandra_node_reset_own": {"cluster_status": {"dc1": {"nodes": [dict(SEEN)]}}} if running else {"skipped": True},
        "cassandra_node_standing_info": {"stdout": "ID                     : id-7\n", "stderr": ""} if running or active == "active"
        else {"skipped": True},
        "cassandra_node_reset_sysv": {},
        # add_node's mark of a bootstrap it started
        "cassandra_node_reset_bootstrap_mark": {"stat": {"exists": bool(mark), "mtime": time.time() - (
            mark if mark is not True and mark else 0)}},
        "_cassandra_service_bootstrap_mark_days": 7,
    }
    if own_view is not None:  # the preflight's read of this node's own ring
        v["_cassandra_preflight_view"] = own_view
    if view is not None:  # the preflight's read of node1's own ring (_cassandra_preflight_view)
        v["hostvars"]["node1"]["_cassandra_preflight_view"] = view
    if given is not None:  # -e cassandra_add_node_reset=...
        v["cassandra_add_node_reset"] = given
    if keyspaces is not None:
        v["cassandra_keyspaces"] = keyspaces
    v.update(trust(KEEP["vars"]))
    v["_cassandra_node_standing_read"] = dict((k, render(t, v)) for k, t in KEEP["ansible.builtin.set_fact"][
        "_cassandra_node_standing_read"].items())
    v.update(trust(STANDING["vars"]))
    v["cassandra_node_standing"] = render(STANDING["ansible.builtin.set_fact"]["cassandra_node_standing"], v)
    for name in ("_cassandra_node_reset_ring", "_cassandra_node_reset_check"):
        v[name] = render(CHECK["ansible.builtin.set_fact"][name], v)
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
    v = facts(found=[DATA + "/lost+found"], ring="none")
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


PEERS = [DATA + "/system/peers_v2-c4325fbb8e5e3bafbd070f9250ed818e/nb-1-big-Data.db"]


def test_failed_bootstrap_of_this_cluster_reset():
    plan = facts(found=STOCK + [DATA + "/shop"], cluster="my_cluster", keyspaces={"shop": {"rf": 3}}, peers=PEERS,
                 mark=True)["_cassandra_node_reset_plan"]
    assert plan["problems"] == []
    assert "user keyspaces shop (a failed bootstrap of this cluster)" in plan["line"]


def test_an_old_mark_is_not_trusted():
    """A mark older than a week: the node may have joined since (its end not seen): a former member."""
    for age, ok in ((3600, True), (8 * 86400, False)):
        plan = facts(found=STOCK + [DATA + "/shop"], cluster="my_cluster", keyspaces={"shop": {"rf": 3}}, peers=PEERS,
                     mark=age)["_cassandra_node_reset_plan"]
        assert (plan["problems"] == []) is ok, age


@pytest.mark.parametrize("given", [None, "auto", "yes", True, "true", "false"])
def test_former_member_of_this_cluster_reset_only_when_given(given):
    """Removed with removenode, put back in the inventory: no mark of an unfinished bootstrap."""
    v = facts(found=STOCK + [DATA + "/shop"], cluster="my_cluster", keyspaces={"shop": {"rf": 3}}, peers=PEERS,
              mark=False, given=given)
    plan = v["_cassandra_node_reset_plan"]
    if str(given).lower() in ("yes", "true"):
        assert plan["problems"] == [] and plan["refusal"] == ""
        assert plan["line"].endswith("user keyspaces shop (a former member of this cluster), not in any ring, down) —"
                                     " will be reset (-e cassandra_add_node_reset=true: the writes only it holds are"
                                     " lost; snapshot or copy its data first if in doubt)")
    else:
        assert plan["line"] == ""
        assert "a former member of this cluster (removed with removenode?)" in plan["refusal"]
        assert "-e cassandra_add_node_reset=true resets it" in plan["refusal"]
        assert render(REFUSE["ansible.builtin.fail"]["msg"], dict(v, **trust(REFUSE["vars"]))) == plan["refusal"]


def test_the_refusal_is_a_verdict():
    # the ops callback prints its msg as is, red, no task header nor fatal: dump
    assert REFUSE["vars"]["cassandra_output"] is True


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
    plan = facts(found=STOCK, ring="seen")["_cassandra_node_reset_plan"]
    assert plan["problems"] == [  # once: the check has the ring's problems
        "node1 sees 10.0.0.7 in its ring (UN, host ID id-7): node7 (dc1/rack_b) is a member of the cluster, never reset"
        " it. A node leaves with decommission_node (remove_dead_node when dead), then can be reset"]
    assert "in the ring of this cluster" in plan["refusal"]
    # reset_node: the same refusal
    assert facts(found=STOCK, ring="seen", auto=False)["_cassandra_node_reset_plan"]["problems"] == plan["problems"]


def test_where_it_stands_is_the_one_the_plan_decides_from():
    """The reset's decision is the state's (cassandra_node_standing): one reading, one state."""
    for kwargs, state in ((dict(found=STOCK), "failed_bootstrap"), (dict(), "blank"),
                          (dict(found=STOCK, ring="seen"), "address_conflict"),
                          (dict(found=STOCK, ring="none"), "unknown"),
                          (dict(found=STOCK, running=True, active="active"), "running_outside_ring"),
                          (dict(found=STOCK, cluster="billing"), "other_cluster"),
                          (dict(found=STOCK + [DATA + "/shop"], cluster="my_cluster", keyspaces={"shop": {}}),
                           "former_member")):
        assert facts(**kwargs)["cassandra_node_standing"]["state"] == state, kwargs


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


def test_the_ways_out_add_node_gives_a_former_member_go_through():
    """add_node refuses a former member of this cluster and offers reset_node, or -e cassandra_add_node_reset=true:
    both reset it (reset_node's own plan has no add_node check; the given reset lifts that one only)."""
    former = dict(found=STOCK + [DATA + "/shop"], cluster="my_cluster", keyspaces={"shop": {"rf": 3}}, peers=PEERS, mark=False)
    assert facts(**former)["_cassandra_node_reset_check"]["former"] is True
    plan = facts(auto=False, **former)["_cassandra_node_reset_plan"]  # reset_node
    assert plan["problems"] == [] and len(plan["delete"]) == 4
    plan = facts(given="true", **former)["_cassandra_node_reset_plan"]  # add_node -e cassandra_add_node_reset=true
    assert plan["problems"] == [] and plan["line"].endswith("snapshot or copy its data first if in doubt)")


def test_the_rings_the_preflight_read_are_used():
    """Each running node read its own ring in the preflight: its answer is that one, not asked again. A read the
    preflight skipped (not asked for) is asked here, as before."""
    seen = {"cluster_status": {"dc1": {"nodes": [N1, SEEN]}}}
    assert facts(found=STOCK, view=seen)["cassandra_node_standing"]["ring"]["in_ring"] == "UN"
    # the preflight read nothing there: the answer read here (clear)
    assert facts(found=STOCK, view={"skipped": True})["cassandra_node_standing"]["state"] == "failed_bootstrap"
    # read, and no answer: not asked again, no ring
    assert facts(found=STOCK, view={"failed": False})["cassandra_node_standing"]["state"] == "unknown"
    # the preflight's read is not asked again; one it skipped is
    loop = _tasks("node_standing_read.yml")["Read the ring other nodes of the cluster see"]
    for v, asked in (({"cluster_status": {}}, []), ({"skipped": True}, ["node1"]), (None, ["node1"])):
        hv = {"node1": {"inventory_hostname": "node1"}}
        if v is not None:
            hv["node1"]["_cassandra_preflight_view"] = v
        variables = dict(trust(loop["vars"]), hostvars=hv, _cassandra_node_standing_from=["node1"])
        assert render(loop["loop"], variables) == asked, v


def test_its_own_ring_only_when_its_jvm_runs():
    """The unit active, no JVM yet: no own ring (its preflight read, if any, is not this Cassandra's answer)."""
    ring = {"cluster_status": {"dc1": {"nodes": [N1, SEEN]}}}
    v = facts(found=STOCK, active="active", own_view=ring)
    assert v["_cassandra_node_standing_read"]["own"] is None
    assert v["cassandra_node_standing"]["state"] == "running_outside_ring"
    v = facts(found=STOCK, running=True, active="active", own_view=ring)
    assert v["_cassandra_node_standing_read"]["own"] == ring["cluster_status"]
