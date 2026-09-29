from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_close_names

KNOWN = ["cassandra_hosts", "cassandra_new_nodes", "cassandra_target_dc", "cassandra_target_rack", "cassandra_status_from"]


def test_misspelt_names_found():
    assert cassandra_close_names(["cassandra_host", "casandra_hosts", "cassandra_new_node", "cassandra_hots"], KNOWN) == {
        "cassandra_host": "cassandra_hosts", "casandra_hosts": "cassandra_hosts",
        "cassandra_new_node": "cassandra_new_nodes", "cassandra_hots": "cassandra_hosts"}


def test_known_and_other_settings_left_alone():
    # the playbooks' own names, and the usual settings of an inventory
    names = KNOWN + ["cassandra_dc", "cassandra_rack", "cassandra_seeds", "cassandra_version", "cassandra_cluster_name",
                     "cassandra_jmx_port", "cassandra_status_raw_output", ""]
    assert cassandra_close_names(names, KNOWN) == {}
    assert cassandra_close_names(None, KNOWN) == {}


def test_role_variables_are_not_typos():
    # close to cassandra_replace_address, but a cassandra_config variable
    assert cassandra_close_names(["cassandra_rpc_address"], ["cassandra_replace_address"]) == {}
