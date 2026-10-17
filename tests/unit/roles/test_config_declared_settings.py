from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The cassandra.yaml settings commented out in stock (or absent from it) that have a variable: rendered per series,
# and the same amount in another unit taken as the same setting.

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import canonical_quantity, same_settings
from ansible_collections.community.cassandra.tests.unit.roles.role_templates import load_role, render_file

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}


def rendered(series, name="cassandra.yaml", **changes):
    env, ctx, dummy = load_role(series, FACTS)
    ctx.update(changes)
    return "\n".join(render_file(env, series, name, ctx)[1])


def settings(series, **changes):
    return yaml.safe_load(rendered(series, **changes))


# --- the templates ----------------------------------------------------------

# variable, value, the key and the value Cassandra reads, per series
ALL_SERIES = [
    ("cassandra_cdc_raw_dir", "/srv/cdc", "cdc_raw_directory", "/srv/cdc"),
    ("cassandra_disk_access_mode", "mmap_index_only", "disk_access_mode", "mmap_index_only"),
    ("cassandra_memtable_flush_writers", 4, "memtable_flush_writers", 4),
    ("cassandra_concurrent_compactors", 4, "concurrent_compactors", 4),
    ("cassandra_stream_entire_sstables", False, "stream_entire_sstables", False),
    ("cassandra_streaming_connections_per_host", 2, "streaming_connections_per_host", 2),
    ("cassandra_auto_bootstrap", False, "auto_bootstrap", False),
]
SINCE_41 = [
    ("cassandra_file_cache_size", "1024MiB", "file_cache_size", "1024MiB"),
    ("cassandra_memtable_heap_space", "2048MiB", "memtable_heap_space", "2048MiB"),
    ("cassandra_memtable_offheap_space", "4096MiB", "memtable_offheap_space", "4096MiB"),
    ("cassandra_stream_throughput_outbound", "200MiB/s", "stream_throughput_outbound", "200MiB/s"),
    ("cassandra_inter_dc_stream_throughput_outbound", "100MiB/s", "inter_dc_stream_throughput_outbound", "100MiB/s"),
    ("cassandra_entire_sstable_stream_throughput_outbound", "48MiB/s", "entire_sstable_stream_throughput_outbound",
     "48MiB/s"),
    ("cassandra_entire_sstable_inter_dc_stream_throughput_outbound", "24MiB/s",
     "entire_sstable_inter_dc_stream_throughput_outbound", "24MiB/s"),
    ("cassandra_internode_streaming_tcp_user_timeout", "300000ms", "internode_streaming_tcp_user_timeout", "300000ms"),
    ("cassandra_roles_update_interval", "2000ms", "roles_update_interval", "2000ms"),
    ("cassandra_permissions_update_interval", "3000ms", "permissions_update_interval", "3000ms"),
    ("cassandra_credentials_update_interval", "4000ms", "credentials_update_interval", "4000ms"),
    ("cassandra_gc_log_threshold", "300ms", "gc_log_threshold", "300ms"),
    ("cassandra_gc_warn_threshold", "2000ms", "gc_warn_threshold", "2000ms"),
]
ONLY_50 = [
    ("cassandra_partition_size_warn_threshold", "100MiB", "partition_size_warn_threshold", "100MiB"),
    ("cassandra_partition_size_fail_threshold", "1024MiB", "partition_size_fail_threshold", "1024MiB"),
    ("cassandra_column_index_size", "16KiB", "column_index_size", "16KiB"),
    ("cassandra_heap_dump_dir", "/srv/dumps", "heap_dump_path", "/srv/dumps"),
]
ONLY_40 = [
    ("cassandra_file_cache_size_in_mb", 1024, "file_cache_size_in_mb", 1024),
    ("cassandra_memtable_heap_space_in_mb", 2048, "memtable_heap_space_in_mb", 2048),
    ("cassandra_memtable_offheap_space_in_mb", 4096, "memtable_offheap_space_in_mb", 4096),
    ("cassandra_stream_throughput_outbound_megabits_per_sec", 400, "stream_throughput_outbound_megabits_per_sec", 400),
    ("cassandra_inter_dc_stream_throughput_outbound_megabits_per_sec", 100,
     "inter_dc_stream_throughput_outbound_megabits_per_sec", 100),
    ("cassandra_internode_streaming_tcp_user_timeout_in_ms", 300000, "internode_streaming_tcp_user_timeout_in_ms", 300000),
    ("cassandra_roles_update_interval_in_ms", 2000, "roles_update_interval_in_ms", 2000),
    ("cassandra_permissions_update_interval_in_ms", 3000, "permissions_update_interval_in_ms", 3000),
    ("cassandra_credentials_update_interval_in_ms", 4000, "credentials_update_interval_in_ms", 4000),
    ("cassandra_gc_log_threshold_in_ms", 300, "gc_log_threshold_in_ms", 300),
    ("cassandra_gc_warn_threshold_in_ms", 2000, "gc_warn_threshold_in_ms", 2000),
]
# 0 is a value (unthrottled, switched off), not "unset"
ZEROS = [
    ("40x", "cassandra_stream_throughput_outbound_megabits_per_sec", 0, "stream_throughput_outbound_megabits_per_sec", 0),
    ("40x", "cassandra_gc_warn_threshold_in_ms", 0, "gc_warn_threshold_in_ms", 0),
    ("40x", "cassandra_internode_streaming_tcp_user_timeout_in_ms", 0, "internode_streaming_tcp_user_timeout_in_ms", 0),
    ("41x", "cassandra_memtable_flush_writers", 0, "memtable_flush_writers", 0),
    ("50x", "cassandra_streaming_connections_per_host", 0, "streaming_connections_per_host", 0),
]
CASES = ([(s,) + c for s in ("40x", "41x", "50x") for c in ALL_SERIES] + [(s,) + c for s in ("41x", "50x") for c in SINCE_41]
         + [("50x",) + c for c in ONLY_50] + [("40x",) + c for c in ONLY_40])


@pytest.mark.parametrize("series, var, value, key, read", CASES)
def test_variable_writes_its_key(series, var, value, key, read):
    assert key not in settings(series)  # commented out (or absent) at the default: Cassandra's own
    assert key not in settings(series, **{var: None})  # null in the inventory: the same
    assert settings(series, **{var: value})[key] == read


def test_sstable_format_section():
    assert "sstable" not in settings("50x")
    assert settings("50x", cassandra_sstable_format="bti")["sstable"] == {"selected_format": "bti"}


@pytest.mark.parametrize("series", ["40x", "41x"])
def test_heap_dump_dir_on_4x(series):
    env_sh = rendered(series, "cassandra-env.sh")
    assert "# set jvm HeapDumpPath with CASSANDRA_HEAPDUMP_DIR" in env_sh  # stock: none
    env_sh = rendered(series, "cassandra-env.sh", cassandra_heap_dump_dir="/srv/dumps")
    assert 'CASSANDRA_HEAPDUMP_DIR="${CASSANDRA_HEAPDUMP_DIR:-/srv/dumps}"' in env_sh  # the environment's still wins
    assert "heap_dump_path" not in settings(series, cassandra_heap_dump_dir="/srv/dumps")  # 5.0's key


def test_heap_dump_dir_on_50_only_a_path_goes_to_cassandra_yaml():
    assert "heap_dump_path" not in settings("50x")  # $CASSANDRA_LOG_DIR: the shell's, not a path
    assert "heap_dump_path" not in settings("50x", cassandra_heap_dump_dir="$CASSANDRA_HOME/dumps")
    assert 'CASSANDRA_HEAPDUMP_DIR="/srv/dumps"' in rendered("50x", "cassandra-env.sh", cassandra_heap_dump_dir="/srv/dumps")


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_declared_tail_renders_nothing_at_the_defaults(series):
    assert "Settings the stock file has no line for" not in rendered(series)
    text = rendered(series, cassandra_auto_bootstrap=False)
    assert "# Settings the stock file has no line for (their variables)\nauto_bootstrap: false" in text


# --- units ----------------------------------------------------------------------

@pytest.mark.parametrize("key, node, written", [
    ("stream_throughput_outbound", "209715200B/s", "200MiB/s"),
    ("stream_throughput_outbound", "100000000B/s", "100000000B/s"),  # not whole in KiB/s: as it is
    ("stream_throughput_outbound", "25600KiB/s", "25MiB/s"),
    ("roles_update_interval", "2s", "2000ms"),
    ("internode_streaming_tcp_user_timeout", "5m", "300000ms"),
    ("gc_log_threshold", "1500us", "1500us"),
    ("partition_size_fail_threshold", "1GiB", "1024MiB"),
    ("column_index_size", "1MiB", "1024KiB"),
    ("roles_update_interval", "2000", "2000"),  # no unit: not one Cassandra 4.1+ reads, as it is
    ("compaction_throughput", "67108864B/s", "67108864B/s"),  # not one of these settings
])
def test_canonical_unit(key, node, written):
    assert canonical_quantity(key, node) == written


def test_same_amount_is_the_same_setting():
    role = rendered("50x", cassandra_stream_throughput_outbound="200MiB/s", cassandra_roles_update_interval="2000ms")
    node = role.replace("stream_throughput_outbound: 200MiB/s", "stream_throughput_outbound: 209715200B/s") \
               .replace("roles_update_interval: 2000ms", "roles_update_interval: 2s")
    assert node != role and same_settings("cassandra.yaml", node, role)
    assert not same_settings("cassandra.yaml", node.replace("209715200B/s", "209715201B/s"), role)


@pytest.mark.parametrize("line, same", [("auto_bootstrap: true\n", True), ("auto_bootstrap: yes\n", True),
                                        ("auto_bootstrap: false\n", False), ("heap_dump_path: /srv/x\n", True),
                                        ("block_for_peers_in_remote_dcs: true\n", False)])
def test_settings_left_out_of_the_comparison_50(line, same):
    # auto_bootstrap: true is the default (false stays: a node wiped later would rejoin without data);
    # heap_dump_path: the -XX:HeapDumpPath of cassandra-env.sh wins
    role = rendered("50x")
    assert same_settings("cassandra.yaml", role + line, role) is same


@pytest.mark.parametrize("series", ["40x", "41x"])
def test_heap_dump_path_compared_on_4x(series):
    # Cassandra 4.x refuses the key: a node with it is rewritten
    role = rendered(series)
    assert not same_settings("cassandra.yaml", role + "heap_dump_path: /srv/x\n", role)


# --- import and self-check of a node that sets all of them ---------------------------

EXPECTED_50 = {
    "cassandra_heap_dump_dir": "/srv/dumps", "cassandra_cdc_raw_dir": "/srv/cdc",
    "cassandra_disk_access_mode": "mmap_index_only", "cassandra_file_cache_size": "1024MiB",
    "cassandra_memtable_heap_space": "2048MiB", "cassandra_memtable_offheap_space": "4096MiB",
    "cassandra_memtable_flush_writers": 4, "cassandra_concurrent_compactors": 4, "cassandra_sstable_format": "bti",
    "cassandra_column_index_size": "16KiB", "cassandra_stream_entire_sstables": False,
    "cassandra_stream_throughput_outbound": "200MiB/s", "cassandra_inter_dc_stream_throughput_outbound": "100000000B/s",
    "cassandra_entire_sstable_stream_throughput_outbound": "48MiB/s",
    "cassandra_entire_sstable_inter_dc_stream_throughput_outbound": "24MiB/s",
    "cassandra_streaming_connections_per_host": 2, "cassandra_internode_streaming_tcp_user_timeout": "300000ms",
    "cassandra_roles_update_interval": "2000ms", "cassandra_permissions_update_interval": "3000ms",
    "cassandra_credentials_update_interval": "4000ms", "cassandra_partition_size_warn_threshold": "100MiB",
    "cassandra_partition_size_fail_threshold": "1024MiB", "cassandra_gc_log_threshold": "300ms",
    "cassandra_gc_warn_threshold": "2000ms", "cassandra_auto_bootstrap": False,
}
