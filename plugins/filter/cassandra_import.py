# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters behind the import_cluster playbook.

cassandra_ring_nodes: cassandra_status module's cluster_status -> list of nodes.
cassandra_config_import: a node's config files -> cassandra_config variables,
    plus what the role would still change (hand edits / normalized lines).
cassandra_jmx_files: cassandra-env.sh, the JVM options files, the JVM's
    arguments -> the remote JMX password and access files the JVM reads.
cassandra_inventory_layout: every node's variables -> inventory groups,
    group_vars, host_vars, drift between nodes and the report.
cassandra_inventory_layout_over: the same, over the user's own variables
    files of the inventory dir (a value of theirs kept or written over).
cassandra_inventory_user_files: the inventory dir's vars files -> the user's
    own that apply to this cluster's hosts.
cassandra_inventory_files: that layout -> the files to write, passwords apart,
    variables grouped by subject.
cassandra_inventory_generated: a file's content -> the same with the import's
    first line (naming the cluster).
cassandra_inventory_leftovers: the inventory dir's files -> the ones of an
    earlier import to remove, keep or replace, and the conflicts with others.
cassandra_inventory_same_secret: a vaulted file, a content -> whether writing
    it again would only change its encryption.
cassandra_inventory_own_values: the inventory dir's vars files -> values an
    earlier import of this cluster wrote (the CQL login).
cassandra_import_kept_ansible_host: the import's nodes -> the same, without the
    ansible_host the inventory already there does without.
cassandra_config_ignored_vars: variable names, series -> the cassandra_config
    variables among them that series' templates don't use (e.g. 4.0 names after
    an upgrade to 4.1).
cassandra_unit_environment: systemctl's Environment of a unit -> dict.
cassandra_import_error: a failed task's result -> why it failed, without the
    values (the import's no_log tasks hold passwords).
cassandra_ring_names: getent hosts on a given node -> the name to reach each
    node found in the ring by.
cassandra_leading_comments: a file's text -> its lines before the first line
    of data (what tells an earlier import's file).
cassandra_inventory_file_heads: the inventory dir's files as read -> what of
    each one tells whose it is (no value of a variables file).
cassandra_import_group, cassandra_import_group_error: -e import_group -> the
    cluster's group, or why it is not an Ansible group name.
cassandra_import_known_group: the group an earlier import wrote for the given
    nodes.
cassandra_import_other_cluster: the hosts of a <cluster group>.yml there
    already when none of them is a node of this import.
cassandra_import_kept_last: the inventory dir's files -> the parts the last
    import kept as each node had them.
cassandra_import_keep_parts: import_keep_hand_edits -> the parts it names.

The code is in plugins/plugin_utils, by subject (each module imports from the ones before it only):
cassandra_import_common: the constants every import module shares, and the error rules below;
cassandra_import_read: a node's config file read against the role's template;
cassandra_import_files: the files the import writes, and an earlier import's;
cassandra_import_jvm: the JVM options files, JMX and logback.xml;
cassandra_import_exporter: the Prometheus exporter;
cassandra_import_config: cassandra_config_import, which puts those together;
cassandra_import_layout: every node's variables -> groups, group_vars, host_vars.

Their unexpected errors do not quote the error message, which may show a value
read from the config (a password): its type and where it happened only.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import cassandra_import_error
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_files import (
    cassandra_import_kept_ansible_host, cassandra_import_kept_last, cassandra_inventory_file_heads, cassandra_inventory_files,
    cassandra_inventory_generated, cassandra_inventory_leftovers, cassandra_inventory_own_values,
    cassandra_inventory_same_secret, keep_parts_option, leading_comments)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_jvm import cassandra_jmx_files
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_config import (
    cassandra_config_ignored_vars, cassandra_config_import, cassandra_unit_environment)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_layout import (
    cassandra_import_group, cassandra_import_group_error, cassandra_import_known_group, cassandra_import_other_cluster,
    cassandra_inventory_layout, cassandra_inventory_layout_over, cassandra_inventory_user_files, cassandra_ring_names,
    cassandra_ring_nodes)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_ring_nodes": cassandra_ring_nodes,
            "cassandra_import_group": cassandra_import_group,
            "cassandra_import_group_error": cassandra_import_group_error,
            "cassandra_import_known_group": cassandra_import_known_group,
            "cassandra_import_other_cluster": cassandra_import_other_cluster,
            "cassandra_config_import": cassandra_config_import,
            "cassandra_jmx_files": cassandra_jmx_files,
            "cassandra_inventory_layout": cassandra_inventory_layout,
            "cassandra_import_kept_last": cassandra_import_kept_last,
            "cassandra_import_keep_parts": keep_parts_option,
            "cassandra_inventory_layout_over": cassandra_inventory_layout_over,
            "cassandra_inventory_user_files": cassandra_inventory_user_files,
            "cassandra_inventory_files": cassandra_inventory_files,
            "cassandra_inventory_generated": cassandra_inventory_generated,
            "cassandra_inventory_leftovers": cassandra_inventory_leftovers,
            "cassandra_inventory_same_secret": cassandra_inventory_same_secret,
            "cassandra_inventory_own_values": cassandra_inventory_own_values,
            "cassandra_import_kept_ansible_host": cassandra_import_kept_ansible_host,
            "cassandra_config_ignored_vars": cassandra_config_ignored_vars,
            "cassandra_unit_environment": cassandra_unit_environment,
            "cassandra_import_error": cassandra_import_error,
            "cassandra_ring_names": cassandra_ring_names,
            "cassandra_leading_comments": leading_comments,
            "cassandra_inventory_file_heads": cassandra_inventory_file_heads,
        }
