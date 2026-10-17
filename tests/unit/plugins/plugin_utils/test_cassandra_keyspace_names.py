from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_keyspace_names import is_system_keyspace


@pytest.mark.parametrize("name, system", [
    ("system", True), ("system_auth", True), ("system_schema", True), ("system_distributed", True),
    ("systemic", False), ("systems_inventory", False), ("orders", False)])
def test_system_keyspaces_by_name(name, system):
    assert is_system_keyspace(name) is system
