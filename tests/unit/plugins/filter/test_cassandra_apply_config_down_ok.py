from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# As apply_config renders it: tests/unit/playbooks/test_apply_config_permissions.py

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config_down_ok import cassandra_apply_config_down_ok

NODES = ["n1", "n2", "n3"]
HOSTVARS = {"n1": {"cassandra_apply_config_then": "start", "cassandra_apply_config_todo": True,
                   "_cassandra_preflight": {"ring_address": "10.0.0.1"}},
            "n2": {"cassandra_apply_config_then": "restart", "_cassandra_preflight": {"ring_address": "10.0.0.2"}},
            "n3": {"cassandra_apply_config_then": "write", "_cassandra_preflight": {"ring_address": "10.0.0.3"}}}


def test_one_at_a_time():
    # n1 was started before n2's turn: n2's restart waits for every other node up (but under --check)
    assert cassandra_apply_config_down_ok(NODES, HOSTVARS, "n2", "restart", False) == []
    assert cassandra_apply_config_down_ok(NODES, HOSTVARS, "n2", "restart", True) == ["10.0.0.1"]
    # n3 (written, left down): n1 is started already
    assert cassandra_apply_config_down_ok(NODES, HOSTVARS, "n3", "write", False) == []
    assert cassandra_apply_config_down_ok(NODES, HOSTVARS, "n1", "start", False) == ["10.0.0.3"]


def test_at_once():
    assert cassandra_apply_config_down_ok(NODES, HOSTVARS, "n2", "restart", False, batch=True) == ["10.0.0.1"]
    assert cassandra_apply_config_down_ok(NODES, HOSTVARS, "n2", "restart", True, batch=True) == ["10.0.0.1", "10.0.0.3"]
