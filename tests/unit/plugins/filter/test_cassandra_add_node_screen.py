from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_add_node_screen import (
    cassandra_add_node_blocks, cassandra_add_node_reset_refusals)


def test_a_refusal_says_the_check_and_the_other_problems():
    hostvars = {"n4": {"_cassandra_preflight": {"cassandra_dc": "dc1"},
                       "_cassandra_node_reset_plan": {"problems": ["holds data", "a path not checked"]},
                       "_cassandra_node_reset_check": {"problems": ["holds data"], "why": "a former member", "way": "w",
                                                       "holds": "3.0 GiB", "former": True}},
                "n5": {"_cassandra_node_reset_plan": {"problems": []}}}
    assert cassandra_add_node_reset_refusals(["n4", "n5"], hostvars) == [
        {"name": "n4", "title": "n4 (dc1/?)", "holds": "3.0 GiB", "why": "a former member; reset refused: a path not checked",
         "way": "w", "former": False}]


def test_a_block_per_new_node():
    hostvars = {"n4": {"_cassandra_preflight": {"address": "10.0.0.4"}, "cassandra_medusa_enabled": "true",
                       "cassandra_medusa_fqdn_domain": "int.example", "ansible_facts": {"hostname": "node4"},
                       "_cassandra_node_reset_plan": {"line": "reset: nothing to delete"}}}
    estimate = [{"host": "n4", "bytes": 2 * 1024 ** 3, "basis": "the ring's load"}, {"host": "n5", "bytes": 1, "basis": "x"}]
    assert cassandra_add_node_blocks(["n4"], hostvars, estimate) == [{"title": "n4  10.0.0.4", "lines": [
        "about 2.0 GiB to stream (the ring's load)", "Medusa fqdn node4.int.example", "reset: nothing to delete"]}]
