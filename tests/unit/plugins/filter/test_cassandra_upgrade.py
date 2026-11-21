from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# upgrade.yml's filters: the nodes outside the run read from describecluster, the refusal of a config copy.

from ansible_collections.community.cassandra.plugins.filter.cassandra_upgrade import (
    cassandra_upgrade_copy_refusal, cassandra_upgrade_outside_nodes)

DESCRIBED = """Cluster Information:
\tName: c
Database versions:
\t4.1.12: [10.0.0.1:7000, 10.0.0.2:7000, [fd00::3]:7000]
\t4.0.21: [10.0.0.4:7000, fd00::5:7000]

Keyspaces:
\tsystem_traces -> Replication class: SimpleStrategy {replication_factor=2}
"""


def test_outside_nodes_by_ring_address_ipv6_too():
    run = [{"address": "10.0.0.1", "ring_address": "10.0.0.1"}, {"address": "node2", "ring_address": "10.0.0.2"}]
    assert cassandra_upgrade_outside_nodes(run, DESCRIBED) == [
        {"address": "fd00::3", "version": "4.1.12"}, {"address": "10.0.0.4", "version": "4.0.21"},
        {"address": "fd00::5", "version": "4.0.21"}]
    # the run's address counts too; the Keyspaces section is not read
    assert cassandra_upgrade_outside_nodes([{"address": "10.0.0.4"}, {"ring_address": "fd00::5"}, {}],
                                           DESCRIBED)[-1] == {"address": "fd00::3", "version": "4.1.12"}


def test_outside_nodes_when_none_is_read():
    assert cassandra_upgrade_outside_nodes(None, "") == []
    assert cassandra_upgrade_outside_nodes([], "Cluster Information:\n\tName: c\n") == []
    assert cassandra_upgrade_outside_nodes([], "Database versions:\n\tUNREACHABLE: [10.0.0.9:7000]\n") == []


def refusal(lines):
    return cassandra_upgrade_copy_refusal(lines, "n1", "/etc/cassandra", "/etc/cassandra.pre-upgrade-4.0.21", "4.0.21",
                                          "ansible-playbook community.cassandra.upgrade -e cassandra_upgrade_phase=prepare")


def test_copy_not_whole_cut_at_ten_lines():
    lines = ["missing f%d" % i for i in range(12)]
    assert refusal(lines).split("\n") == [
        "n1: the config copy /etc/cassandra.pre-upgrade-4.0.21 is not whole (/etc/cassandra has more, or other sizes)."
        " The snapshot pre-upgrade-4.0.21 is taken; no node was upgraded."] + ["  missing f%d" % i for i in range(10)] + [
        "  ... 2 more",
        "WAY OUT  remove /etc/cassandra.pre-upgrade-4.0.21 on n1 (rm -r), then run the prepare phase again:",
        "  ansible-playbook community.cassandra.upgrade -e cassandra_upgrade_phase=prepare"]
    assert "\n  ... 1 more\n" in refusal(lines[:11]) and "  ... " not in refusal(lines[:10])


def test_copy_unreadable():
    assert refusal(["unreadable copy x"]).startswith(
        "n1: the config copy /etc/cassandra.pre-upgrade-4.0.21 could not be read whole.")


def test_config_unreadable_way_out():
    # the config itself unreadable: removing the copy would not help, the way out is the config's access
    assert refusal(["size f", "unreadable config /etc/cassandra/x"]).split("\n") == [
        "n1: the config /etc/cassandra could not be read whole, the copy is not checked. The snapshot pre-upgrade-4.0.21"
        " is taken; no node was upgraded.",
        "  size f",
        "  unreadable config /etc/cassandra/x",
        "WAY OUT  make /etc/cassandra on n1 readable (the directory Cassandra reads: cassandra_conf_dir), then run the"
        " prepare phase again:",
        "  ansible-playbook community.cassandra.upgrade -e cassandra_upgrade_phase=prepare"]
