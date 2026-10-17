from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file, roles_with_defaults


def test_a_role_file_is_a_copy_of_its_own():
    defaults = role_file("cassandra_config")
    assert "cassandra_cluster_name" in defaults
    defaults["cassandra_cluster_name"] = "changed by a caller"
    assert role_file("cassandra_config")["cassandra_cluster_name"] != "changed by a caller"
    assert "_cassandra_config_files" in role_file("cassandra_config", "vars")


def test_the_roles_with_defaults():
    roles = roles_with_defaults()
    assert roles == sorted(roles) and "cassandra_config" in roles and "cassandra_install" in roles
