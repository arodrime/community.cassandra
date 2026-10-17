from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The preflight's stop on a setting that differs between nodes.

from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import cassandra_differs


def test_a_setting_that_differs_says_where_to_fix_it():
    msg = cassandra_differs({"node1": "16", "node2": "16", "node3": "16", "node4": "256"}, "cassandra_num_tokens", "my_cluster")
    assert msg == ("cassandra_num_tokens is not the same on every node of my_cluster (node1..node3: 16; node4: 256):"
                   " every node needs the same. Set it once in group_vars/my_cluster, and remove it from host_vars/node4."
                   " Nothing was changed.")
    assert "{" not in msg and "_cassandra" not in msg


def test_the_seeds_that_differ_named_by_their_variable():
    msg = cassandra_differs({"node1": ["10.0.0.1", "10.0.0.2"], "node2": ["10.0.0.1", "10.0.0.2"], "node3": ["10.0.0.3"]},
                            "_cassandra_service_seeds", "my_cluster")
    assert msg.startswith("The seed list (cassandra_seeds) is not the same on every node of my_cluster"
                          " (node1, node2: 10.0.0.1, 10.0.0.2; node3: 10.0.0.3)")
    assert "_cassandra_service_seeds" not in msg and "[" not in msg and "host_vars/node3" in msg


def test_many_hosts_that_differ_are_cut():
    values = dict(("node%d" % i, "16" if i < 8 else "256") for i in range(1, 13))
    values["node1"] = None
    msg = cassandra_differs(values, "cassandra_num_tokens")
    assert "node1: (none)" in msg and "the cluster's group_vars" in msg and ", ..." in msg


def test_host_vars_in_natural_order():
    values = {"node1": "16", "node2": "16", "node3": "16", "node10": "8", "node9": "8"}
    assert "host_vars/node9, host_vars/node10." in cassandra_differs(values, "cassandra_num_tokens", "c")


def test_none_and_empty_differ_as_the_assert_sees_them():
    msg = cassandra_differs({"node1": None, "node2": "", "node3": ""}, "cassandra_initial_token", "c")
    assert msg.startswith('cassandra_initial_token is not the same on every node of c (node2, node3: ""; node1: (none)):')
