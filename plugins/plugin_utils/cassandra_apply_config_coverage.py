# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What apply_config does with each variable of the roles: the coverage map.

apply_config makes the cluster match the inventory, and ignores no setting.
Every difference between the inventory and a node falls in one category,
shown in the plan (and under --check):

  A  live, no restart: applied by apply_config at the node's turn.
  B  needs a restart: applied by apply_config in the node's one restart (one
     restart per node whatever the reasons).
  C  a procedure of its own: apply_config stops before changing anything and
     gives the command (an upgrade, a node added or removed).
  D  impossible on a live cluster: refused, with why.
  -  not a node setting: an option of a run, a way to reach the nodes, a
     choice of the roles (what they manage, where they download from).

Some B variables are applied with no restart when a proof says the running
Cassandra already runs with them (the JVM's arguments, logback's scan, an
owner or mode read at start): the category is what a change needs at most.

COVERAGE: one row per variable class, (names, category, what apply_config
does with them); a name is a variable or a regular expression (^...$). A unit
test lists every variable of the roles' defaults and argument specs, and
fails when one has no row here: add it to the row of its class (or a new
row), with the category its change needs.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

LIVE, RESTART, PROCEDURE, IMPOSSIBLE, NOT_A_SETTING = "A", "B", "C", "D", "-"

CATEGORIES = {
    LIVE: "live, no restart",
    RESTART: "applied in the node's one restart",
    PROCEDURE: "a procedure of its own: apply_config stops before any change, with the command",
    IMPOSSIBLE: "refused on a live cluster",
    NOT_A_SETTING: "not a node setting",
}

COVERAGE = [
    # --- D: what a joined node keeps for life (cassandra_config refuses it: compare.yml, identity) ---
    (["cassandra_cluster_name", "cassandra_num_tokens", "cassandra_partitioner", "cassandra_endpoint_snitch",
      "cassandra_dc", "cassandra_rack", "cassandra_rackdc_dc", "cassandra_rackdc_rack", "cassandra_rackdc_extra_settings"],
     IMPOSSIBLE, "a joined node keeps it for life: refused (cassandra_config_force_identity_change for a documented"
                 " procedure)"),
    (["cassandra_initial_token"], IMPOSSIBLE,
     "read at a node's first start only: another token than the ring's for a joined node is refused (move_node moves"
     " it; the inventory then gets its new token, which apply_config writes)"),
    (["cassandra_storage_compatibility_mode"], RESTART,
     "5.0: one step at a time, never back (cassandra_config refuses the rest); restart"),

    # --- C: procedures of their own ---
    (["cassandra_version"], PROCEDURE,
     "another series than the one the node runs: an upgrade (upgrade -e cassandra_upgrade_phase=preflight)"),
    (["cassandra_package_version"], RESTART,
     "a newer release of the series: the package updated in the node's restart (stopped before any change when the"
     " release would not start with the node's settings, or is newer than the collection's notes:"
     " plugin_utils cassandra_patch_notes); another series, or an older release: stopped (C)"),
    (["cassandra_node_state"], PROCEDURE,
     "a host to add, or marked absent while still in the ring: topology (--check, then the real run)"),
    (["cassandra_seeds"], RESTART,
     "written in cassandra.yaml with the config (change_seeds applies a seed list live)"),

    # --- B: the config files cassandra_config writes (Cassandra reads them at start) ---
    ([r"^cassandra_jvm\d*_.*$", r"^cassandra_heap_.*$", "cassandra_max_direct_memory_size"], RESTART,
     "the JVM options files and cassandra-env.sh: restart (none when the JVM's arguments are proven the same)"),
    ([r"^cassandra_log_.*$", "cassandra_logback_root_extra_refs", "cassandra_debug_log_enabled"], RESTART,
     "logback.xml: restart (none when logback re-reads it, scan=\"true\")"),
    ([r"^cassandra_prometheus_exporter_(?!sources$).*$"], RESTART,
     "the Prometheus exporter's agent (read at start; its rules reloaded at its next scrape)"),
    ([r"^cassandra_jmx_(?!username$|password$|password_file$).*$", "cassandra_local_jmx"], RESTART,
     "JMX (cassandra-env.sh, jmxremote.access: read at start; jmxremote.password: read at each login, live)"),
    ([r"^cassandra_(audit|tde|client|server)_.*$", r"^cassandra_.*_(timeout|threshold|interval|size|validity)(_in_\w+|_mb)?$",
      r"^cassandra_.*_dir(s)?$", "cassandra_data_file_directories", "cassandra_extra_settings"], RESTART,
     "cassandra.yaml settings: restart"),
    ([r"^cassandra_(allocate_tokens_for_local_replication_factor|authenticator|authorizer|auto_bootstrap"
      r"|auto_hints_cleanup_enabled|auto_snapshot|auto_snapshot_ttl|batchlog_replay_throttle(_in_kb)?"
      r"|cdc_enabled|cidr_authorizer_class_name|column_index_cache_size(_in_kb)?|column_index_size(_in_kb)?"
      r"|commit_failure_policy|commitlog_disk_access_mode|commitlog_segment_size(_in_mb)?|commitlog_sync"
      r"|commitlog_sync_group_window(_in_ms)?|commitlog_sync_period(_in_ms)?|compaction_throughput(_mb_per_sec)?"
      r"|concurrent_\w+|counter_cache_save_period|crypto_provider_\w+|diagnostic_events_enabled|disk_access_mode"
      r"|disk_failure_policy|drop_compact_storage_enabled|dynamic_snitch_\w+|enable_\w+"
      r"|entire_sstable_(inter_dc_)?stream_throughput_outbound|hinted_handoff_enabled|hinted_handoff_throttle(_in_kb)?"
      r"|hints_flush_period(_in_ms)?|incremental_backups|index_summary_capacity|index_summary_resize_interval\w*"
      r"|inter_dc_stream_throughput_outbound\w*|inter_dc_tcp_nodelay|internode_\w+|key_cache_save_period"
      r"|legacy_ssl_storage_port_enabled|listen_address|materialized_views_enabled|max_hint_window(_in_ms)?"
      r"|max_hints_delivery_threads|max_hints_file_size(_in_mb)?|memtable_\w+|native_transport_\w+"
      r"|network_authorizer|prefer_local|prepared_statements_cache_size|repaired_data_tracking_\w+"
      r"|report_unconfirmed_repaired_data_mismatches|require_endpoint_verification|role_manager"
      r"|row_cache_save_period|rpc_address|rpc_keepalive|sasi_indexes_enabled|scripted_user_defined_functions_enabled"
      r"|seed_provider_class_name|snapshot_before_compaction|snapshot_links_per_second|ssl_storage_port"
      r"|sstable_format|sstable_preemptive_open_interval(_in_mb)?|start_native_transport|storage_port"
      r"|stream_entire_sstables|stream_throughput_outbound\w*|streaming_connections_per_host|streaming_stats_enabled"
      r"|trace_type_\w+|tracetype_\w+|transient_replication_enabled|trickle_fsync\w*|truststore(_password)?"
      r"|unlogged_batch_across_partitions_warn_threshold|user_defined_functions_enabled"
      r"|uuid_sstable_identifiers_enabled|windows_timer_interval)$"], RESTART, "cassandra.yaml settings: restart"),
    (["cassandra_config_template_overrides", "cassandra_config_template_snippets", "cassandra_config_normalize"],
     RESTART, "the config files as the role renders them: restart (none for the role's header line alone)"),
    (["cassandra_config_user", "cassandra_config_group", "cassandra_config_mode", "cassandra_config_public_mode",
      "cassandra_config_file_permissions"], LIVE,
     "the owner, group and mode of the config files: live (read at start, no restart for them)"),
    (["cassandra_conf_dir", "cassandra_rpm_conf_alternative", "cassandra_rpm_conf_alternative_priority"], RESTART,
     "where the config is (the RPM conf alternative): restart"),
    (["cassandra_cqlsh_credentials"], LIVE, "the cqlsh credentials files: not read by Cassandra, live"),

    # --- B: the systemd unit (read when Cassandra starts) ---
    (["cassandra_service_environment", "cassandra_service_user", "cassandra_service_group", "cassandra_user",
      "cassandra_group", r"^cassandra_service_limit_\w+$"], RESTART,
     "what the running Cassandra gets from its unit (environment, user, limits): compared with the running JVM's,"
     " a difference restarts the node, the unit written in that restart"),
    (["cassandra_service_restart", "cassandra_service_start_limit_burst", "cassandra_service_start_limit_interval",
      "cassandra_service_timeout_stop", "cassandra_service_tasks_max", "cassandra_service_drain_on_stop",
      "cassandra_service_drain_timeout", "cassandra_service_start", "cassandra_service_unit_manage"], RESTART,
     "systemd's handling of the unit: written at the node's next restart (said in the plan, no restart for it)"),

    # --- B: Java (the running JVM's) ---
    (["cassandra_java_version", "cassandra_java_home", "cassandra_java_link", "cassandra_java_package",
      "cassandra_java_tarball", "cassandra_java_tarball_checksum", "cassandra_java_tarball_dir", "cassandra_java_tarballs",
      "cassandra_java_versions", "cassandra_java_set_default"], RESTART,
     "another Java than the running one: installed, made the one Cassandra starts with, restart, checked"
     " (as update_java)"),

    # --- A: what Cassandra does not read, or reads at its next start only ---
    (["cassandra_install_tools", "cassandra_install_jemalloc", "cassandra_packages", "cassandra_package_hold",
      r"^cassandra_dsbulk_\w+$", r"^cassandra_cqlsh_python\w*$", "cassandra_cqlsh_source", "cassandra_install_java"],
     LIVE, "packages and tools (cassandra_install): live; jemalloc is loaded at the next start (said)"),
    (["cassandra_repository_manage", "cassandra_repository_key_url", "cassandra_repository_key_fingerprints",
      "cassandra_apt_keyring_path", "cassandra_rpm_key_path", "cassandra_install_method", "cassandra_install_url",
      "cassandra_install_username", "cassandra_install_password", "debian_packages"], LIVE,
     "the package repository (cassandra_repository): live"),
    (["cassandra_firewall_manage", "cassandra_firewall_enabled", "cassandra_firewall_port_sources", "open_ports",
      "cassandra_prometheus_exporter_sources"], LIVE, "the firewall (cassandra_firewall): live"),
    ([r"^cassandra_linux_\w+$", r"^cassandra_data_(?!dir$|file_directories$)\w+$", "cassandra_sysfs_block_root"], LIVE,
     "the OS settings (cassandra_linux): live; what is not done under a running Cassandra in its restart window"),
    ([r"^cassandra_medusa_\w+$"], LIVE, "Medusa: not read by Cassandra, live"),

    # --- not a node setting ---
    (["cassandra_config_confirm", "cassandra_config_backup", "cassandra_config_force_identity_change",
      "cassandra_config_keep_files", "cassandra_config_reload_seeds", "cassandra_config_storage_dir",
      "cassandra_offline", "cassandra_install_checksums", "cassandra_install_package_file", "cassandra_java_allow_unsupported",
      "cassandra_java_tarball_username", "cassandra_java_tarball_password", "cassandra_change_report_dir",
      "cassandra_import_kept", "cassandra_imported_host", "cassandra_settings_kept_on_purpose"], NOT_A_SETTING,
     "how the roles work (questions, backups, downloads, what is kept by hand), not a setting of the node"),
    (["cassandra_jmx_username", "cassandra_jmx_password", "cassandra_jmx_password_file", "cassandra_cql_username",
      "cassandra_cql_password", "cassandra_cqlsh_command"], NOT_A_SETTING,
     "how the playbooks log in (nodetool, cqlsh); the JMX users of the node are cassandra_jmx_users"),
    ([r"^cassandra_(rolling|stream|clock|move|token|upgrade_sstables|add_node|replace_node|create_cluster"
      r"|new_node|dead_node|decommission)_\w+$",
      "cassandra_operation_confirm", "cassandra_operation_drain", "cassandra_service_allow_new_seed",
      "cassandra_service_enabled", "cassandra_service_state", "cassandra_service_health_force",
      "cassandra_service_health_timeout", "cassandra_service_restart_on_change", "cassandra_service_wait_for_normal",
      "cassandra_service_wait_timeout"], NOT_A_SETTING, "options of the operations (how a run goes), not node settings"),
]


def _matches(pattern, name):
    return re.match(pattern, name) is not None if pattern.startswith("^") else pattern == name


def category(name):
    """A variable -> (its category, what apply_config does with it), from the first row naming it (an exact name
    before a pattern); None when no row has it."""
    for names, cat, what in COVERAGE:
        if name in names:
            return cat, what
    for names, cat, what in COVERAGE:
        if any(n.startswith("^") and _matches(n, name) for n in names):
            return cat, what
    return None
