from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the cluster's group (-e import_group, else the one an earlier import wrote for the given nodes,
# else its cluster name's), and import_force never writing over another cluster's files (two clusters of one name,
# Test Cluster by default, get one group).

import pytest
import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    GENERATED, cassandra_import_group, cassandra_import_known_group, cassandra_import_other_cluster,
    cassandra_inventory_files, cassandra_inventory_layout, cassandra_inventory_layout_over)
from ansible_collections.community.cassandra.tests.unit.playbooks.test_import_cluster_unsupported import (
    PLAYS, render)
from ansible_collections.community.cassandra.tests.unit.task_files import find_play, find_task

FIND = find_play(PLAYS, "Find the cluster from the given nodes")
WRITE = find_play(PLAYS, "Write the inventory")
CHECK = find_task(FIND, "Check the group asked for")


LAYOUT = find_task(WRITE, "Lay out groups and variables")
OTHER = find_task(WRITE, "Stop rather than overwrite another cluster's inventory")


def node(name, address, dc="dc1", rack="r1"):
    return {"name": name, "address": address, "hostname": name, "dc": dc, "rack": rack, "read": True,
            "vars": {"cassandra_cluster_name": "Test Cluster"}, "hand_edits": [], "normalized": [], "notes": []}


ORDERS = [node("h-n1", "10.0.1.1"), node("h-n2", "10.0.1.2")]
BILLING = [node("h-n7", "10.0.2.7"), node("h-n8", "10.0.2.8")]


def hosts_text(nodes, group=""):
    layout = cassandra_inventory_layout(nodes, "Test Cluster", group)
    return GENERATED % layout["cluster_group"] + "\n" + yaml.safe_dump(layout["hosts"])


# --- the group ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("given, group", [("billing", "billing"), ("Billing_EU", "billing_eu"), ("_x1", "x1")])
def test_import_group_is_the_clusters_group(given, group):
    assert cassandra_import_group(given) == group
    layout = cassandra_inventory_layout(BILLING, "Test Cluster", given)
    assert layout["cluster_group"] == group
    children = layout["hosts"]["all"]["children"]
    assert list(children) == [group] and list(children[group]["children"]) == [group + "_dc1"]
    assert sorted(f["path"] for f in cassandra_inventory_files(layout) if f["path"].startswith("group_vars/" + group + "/"))


def test_without_import_group_the_cluster_names_group():
    assert cassandra_inventory_layout(BILLING, "Test Cluster")["cluster_group"] == "test_cluster"


@pytest.mark.parametrize("given", ["1x", "a-b", "a b", "all", "ungrouped", "", "x.y"])
def test_a_name_that_is_no_group_is_refused(given):
    with pytest.raises(AnsibleFilterError, match="not an Ansible group name"):
        cassandra_import_group(given)


@pytest.mark.parametrize("given, ok", [("billing", True), ("Billing_EU", True), ("1x", False), ("a-b", False),
                                       ("all", False), ("ALL", False)])
def test_the_playbook_checks_the_name_first_with_the_filters_rule(given, ok):
    # (one rule: the filter's, the playbook says its error before any change)
    assert "cassandra_import_group_error" in CHECK["vars"]["_error"]
    variables = dict(CHECK["vars"], import_group=given)
    assert render("{{ %s }}" % CHECK["ansible.builtin.assert"]["that"], **variables) is ok
    if not ok:
        assert render(CHECK["ansible.builtin.assert"]["fail_msg"], **variables) == (
            "import_group=%s: not an Ansible group name: letters, digits and _, not a digit first (nor all,"
            " ungrouped). Nothing was written." % given)


def test_the_layout_over_your_files_keeps_the_group():
    layout = cassandra_inventory_layout_over(BILLING, "Test Cluster", [], "/p/inv", "billing")
    assert layout["cluster_group"] == "billing"


# --- a re-import finds the group again ---------------------------------------------------------------------------

def test_an_earlier_import_s_group_is_found_again():
    texts = {"billing": hosts_text(BILLING, "billing"), "billing_dc1": "", "test_cluster": hosts_text(ORDERS)}
    assert cassandra_import_known_group(["billing", "billing_dc1", "billing_dc1_r1"], texts) == "billing"
    # a group whose hosts file another tool wrote, or none: the cluster name's
    assert cassandra_import_known_group(["billing"], {"billing": "all:\n  hosts: {h-n7: {}}\n"}) == ""
    assert cassandra_import_known_group(["ungrouped"], {}) == ""


def test_the_playbook_takes_the_group_found_again(tmp_path):
    (tmp_path / "billing.yml").write_text(hosts_text(BILLING, "billing"))
    hostvars = {"h-n7": {"group_names": ["billing", "billing_dc1", "billing_dc1_r1"]}}
    variables = dict(LAYOUT["vars"], _given=["h-n7"], hostvars=hostvars, import_dir=str(tmp_path))
    assert render(LAYOUT["vars"]["_group"], **variables) == "billing"
    assert render(LAYOUT["vars"]["_group"], **dict(variables, import_group="other")) == "other"
    hostvars["h-n7"]["group_names"] = ["ungrouped"]  # (-i <node>,: a new cluster)
    assert render(LAYOUT["vars"]["_group"], **variables) == ""


# --- import_force never writes over another cluster ---------------------------------------------------------------

def test_two_clusters_of_one_name_collide_and_the_second_is_refused():
    first = hosts_text(ORDERS)
    layout = cassandra_inventory_layout(BILLING, "Test Cluster")
    assert layout["cluster_group"] == "test_cluster"  # (the same group: the collision)
    assert cassandra_import_other_cluster(first, layout) == ["h-n1", "h-n2"]
    variables = dict(OTHER["vars"], _layout=layout, _dir="/p/inv", _hosts_file="test_cluster.yml",
                     _other=["h-n1", "h-n2"])
    msg = " ".join(render(OTHER["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert msg == ("/p/inv/test_cluster.yml is another cluster's: none of its hosts (h-n1, h-n2) is a node of this one,"
                   " though both are named Test Cluster. Import this one under a group of its own: -e"
                   " import_group=<another name> (then -e cassandra_hosts=<that name> for the playbooks). Nothing was"
                   " written.")
    # under its own group: no collision
    assert cassandra_inventory_layout(BILLING, "Test Cluster", "billing")["cluster_group"] == "billing"


@pytest.mark.parametrize("now", [
    ORDERS,  # the same nodes
    ORDERS + [node("h-n3", "10.0.1.3")],  # a node added since
    [ORDERS[0]],  # one removed since
    [dict(ORDERS[0], name="10.0.1.1"), dict(ORDERS[1], name="10.0.1.2")],  # named by address now
])
def test_a_re_import_of_the_same_cluster_passes(now):
    assert cassandra_import_other_cluster(hosts_text(ORDERS), cassandra_inventory_layout(now, "Test Cluster")) == []


def test_a_re_import_matches_an_alias_by_its_ansible_host():
    # the file has aliases (zorglub-a, zorglub-b) with ansible_host; this import names the nodes by address: the same
    text = GENERATED % "test_cluster" + "\n" + yaml.safe_dump({"all": {"children": {"test_cluster": {"hosts": {
        "zorglub-a": {"ansible_host": "10.0.1.1"}, "zorglub-b": {"ansible_host": "10.0.1.2"}}}}}})
    now = [dict(ORDERS[0], name="10.0.1.1"), dict(ORDERS[1], name="10.0.1.2")]
    assert cassandra_import_other_cluster(text, cassandra_inventory_layout(now, "Test Cluster")) == []
    # this import's nodes with an ansible_host of their own, the file's host named by it: the same
    layout = cassandra_inventory_layout(BILLING, "Test Cluster")
    layout["nodes"] = [dict(n, ansible_host="zorglub-a") for n in layout["nodes"]]
    assert cassandra_import_other_cluster(text, layout) == []
    # neither name, address nor ansible_host in common: another cluster's
    assert cassandra_import_other_cluster(text, cassandra_inventory_layout(BILLING, "Test Cluster")) == [
        "zorglub-a", "zorglub-b"]


def test_the_stop_runs_on_an_existing_file_only(tmp_path):
    (tmp_path / "test_cluster.yml").write_text(hosts_text(ORDERS))
    layout = cassandra_inventory_layout(BILLING, "Test Cluster")
    variables = dict(OTHER["vars"], _layout=layout, _dir=str(tmp_path), _hosts_file="test_cluster.yml")
    assert render("{{ _other }}", **variables) == ["h-n1", "h-n2"]
    assert OTHER["when"] == "import_cluster_hosts_stat.stat.exists"
