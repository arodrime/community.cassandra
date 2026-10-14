from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A re-import keeps how the inventory there reaches its hosts: one reached without ansible_host (imported with
# import_cluster_set_ansible_host=false, or taken out by hand) gets none again, unless the option is given; the
# hosts file it writes then is the one there (a re-import over a full import changes nothing).

import os

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_import_kept_ansible_host, cassandra_inventory_generated, cassandra_inventory_layout)

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")
NAMES = {"10.0.0.1": "node1", "10.0.0.2": "node2", "10.0.0.3": "node3"}


def node(i, **more):
    n = {"name": "node%d" % i, "address": "10.0.0.%d" % i, "ansible_host": "10.0.0.%d" % i, "dc": "dc1", "rack": "r1",
         "read": False, "vars": {}, "notes": ["reached at node%d.lan, its hosts file has its ring address 10.0.0.%d"
                                              " (import_cluster_set_ansible_host: false leaves ansible_host out)" % (i, i)]}
    n.update(more)
    return n


def hosts_file(entries, header=True):
    data = {"all": {"children": {"prod": {"children": {"prod_dc1": {"children": {"prod_dc1_r1": {"hosts": entries}}}}}}}}
    text = yaml.safe_dump(data)
    return cassandra_inventory_generated(text, "prod") if header else text


def kept(nodes, read):
    return [n["ansible_host"] for n in cassandra_import_kept_ansible_host(nodes, read, "prod", NAMES)]


def test_hosts_reached_without_ansible_host_stay_so():
    read = {"prod.yml": hosts_file({"node1": {}, "node2": None})}
    out = cassandra_import_kept_ansible_host([node(1), node(2)], read, "prod", NAMES)
    assert [n["ansible_host"] for n in out] == ["", ""]
    assert [n["notes"] for n in out] == [[], []]  # its address is not said either: nothing replaces it
    # a node new to the inventory (found in the ring): as its other hosts, none
    assert kept([node(1), node(3)], read) == ["", ""]


def test_hosts_with_ansible_host_keep_it_updated():
    read = {"prod.yml": hosts_file({"node1": {"ansible_host": "10.0.0.99"}, "node2": {}})}
    # node1's address comes from the ring again; node2 stays without; node3 new: some host has one, so it gets one
    assert kept([node(1), node(2), node(3)], read) == ["10.0.0.1", "", "10.0.0.3"]


def test_ansible_host_in_host_vars_counts():
    read = {"prod.yml": hosts_file({"node1": {}, "node2": {}}),
            "host_vars/node1.yml": "ansible_host: node1.lan\n", "host_vars/node2/conn.yml": "ansible_port: 2222\n"}
    assert kept([node(1), node(2)], read) == ["10.0.0.1", ""]
    # a vaulted host_vars file not read (no password): it may set it, the import's default stays
    read["host_vars/node2/vault.yml"] = "$ANSIBLE_VAULT;1.1;AES256\n6162\n"
    assert kept([node(1), node(2)], read) == ["10.0.0.1", "10.0.0.2"]


def test_a_first_import_or_another_clusters_file_changes_nothing():
    assert kept([node(1)], {}) == ["10.0.0.1"]
    other = cassandra_inventory_generated(yaml.safe_dump({"all": {"children": {"other": {"hosts": {"node1": {}}}}}}), "other")
    assert kept([node(1)], {"prod.yml": other}) == ["10.0.0.1"]
    # without the import's first line, a file holding this cluster's group alone is its own
    assert kept([node(1)], {"prod.yml": hosts_file({"node1": {}}, header=False)}) == [""]
    assert kept([node(1)], {"prod.yml": "not: [yaml"}) == ["10.0.0.1"]


def hosts_text(nodes):
    """The hosts file the playbook writes from the layout (Write the hosts file)."""
    layout = cassandra_inventory_layout(nodes, "prod")
    return cassandra_inventory_generated(yaml.safe_dump(layout["hosts"], indent=2, sort_keys=False), layout["cluster_group"])


def test_a_reimport_writes_the_hosts_file_there_again():
    # first import with import_cluster_set_ansible_host=false: no ansible_host; the re-import (default true) writes
    # the same file
    first = hosts_text([node(i, ansible_host="") for i in (1, 2)])
    again = cassandra_import_kept_ansible_host([node(1), node(2)], {"prod.yml": first}, "prod", NAMES)
    assert "ansible_host" not in first
    assert hosts_text(again) == first
    # without it, the addresses would be written
    assert "ansible_host: 10.0.0.1" in hosts_text([node(1), node(2)])


def test_the_playbook_applies_it_unless_the_option_is_given():
    with open(PLAYBOOK, encoding="utf-8") as f:
        play = next(p for p in yaml.safe_load(f) if p["name"] == "Write the inventory")
    names = [t["name"] for t in play["tasks"]]
    step = play["tasks"][names.index("Keep the hosts the inventory reaches without ansible_host so")]
    assert step["when"] == "import_cluster_set_ansible_host is not defined"
    assert "cassandra_import_kept_ansible_host(import_cluster_existing_text" in step["ansible.builtin.set_fact"]["_nodes"]
    # once the files there are read, before the layout the hosts file is written from
    assert names.index("Keep the files already there") < names.index(step["name"]) < names.index(
        "Lay out again over your own variables") < names.index("Write the hosts file")
