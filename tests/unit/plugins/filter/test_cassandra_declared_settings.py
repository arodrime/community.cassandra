from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The cassandra.yaml settings commented out in stock (or absent from it) that have a variable: rendered per series,
# read back by import_cluster (in one unit whatever the node writes it in), and the self-check passing on a node
# that sets all of them.

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    _load_role, _render, cassandra_config_import)
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import canonical_quantity, same_settings
from ansible_collections.community.cassandra.tests.unit.plugins.filter.test_cassandra_import_check import (
    check, edit, seeded, stock)

FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}


def rendered(series, name="cassandra.yaml", **changes):
    env, ctx, dummy = _load_role(series, FACTS)
    ctx.update(changes)
    return "\n".join(_render(env, series, name, ctx)[1])


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


@pytest.mark.parametrize("series, var, value, key, read", ZEROS)
def test_zero_is_written_and_read_back(series, var, value, key, read):
    assert settings(series, **{var: value})[key] == read
    files = dict((n, rendered(series, n, **{var: value})) for n in _load_role(series, FACTS)[2])
    out = cassandra_config_import(files, series, FACTS)
    assert out["vars"][var] == 0 and out["hand_edits"] == []


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_variables_read_back_by_the_import(series):
    # all of the series' at once (an import reads the whole files)
    values = dict((var, value) for s, var, value, dummy, dummy2 in CASES if s == series)
    files = dict((n, rendered(series, n, **values)) for n in _load_role(series, FACTS)[2])
    out = cassandra_config_import(files, series, FACTS)
    assert dict((k, out["vars"].get(k)) for k in values) == values
    assert "cassandra_extra_settings" not in out["vars"]
    assert out["hand_edits"] == []


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

def node_50():
    files = seeded(stock("50x"))
    y = files["cassandra.yaml"]
    for old, new in [
            ("# heap_dump_path: /var/lib/cassandra/heapdump", "heap_dump_path: /srv/dumps"),
            ("# cdc_raw_directory: /var/lib/cassandra/cdc_raw", "cdc_raw_directory: /srv/cdc"),
            ("# disk_access_mode: mmap_index_only", "disk_access_mode: mmap_index_only"),
            ("# file_cache_size: 512MiB", "file_cache_size: 1GiB"),
            ("# memtable_heap_space: 2048MiB", "memtable_heap_space: 2048MiB"),
            ("# memtable_offheap_space: 2048MiB", "memtable_offheap_space: 4096MiB"),
            ("# memtable_flush_writers: 2", "memtable_flush_writers: 4"),
            ("# concurrent_compactors: 1", "concurrent_compactors: 4"),
            ("#sstable:\n#  selected_format: big", "sstable:\n  selected_format: bti"),
            ("# column_index_size: 4KiB", "column_index_size: 16KiB"),
            ("# stream_entire_sstables: true", "stream_entire_sstables: false"),
            ("# stream_throughput_outbound: 24MiB/s", "stream_throughput_outbound: 209715200B/s"),
            ("# inter_dc_stream_throughput_outbound: 24MiB/s", "inter_dc_stream_throughput_outbound: 100000000B/s"),
            ("# entire_sstable_stream_throughput_outbound: 24MiB/s", "entire_sstable_stream_throughput_outbound: 48MiB/s"),
            ("# entire_sstable_inter_dc_stream_throughput_outbound: 24MiB/s",
             "entire_sstable_inter_dc_stream_throughput_outbound: 24MiB/s"),
            ("# streaming_connections_per_host: 1", "streaming_connections_per_host: 2"),
            ("# internode_streaming_tcp_user_timeout: 300000ms", "internode_streaming_tcp_user_timeout: 5m"),
            ("# roles_update_interval: 2000ms", "roles_update_interval: 2s"),
            ("# permissions_update_interval: 2000ms", "permissions_update_interval: 3000ms"),
            ("# credentials_update_interval: 2000ms", "credentials_update_interval: 4000ms"),
            ("# partition_size_warn_threshold:", "partition_size_warn_threshold: 100MiB"),
            ("# partition_size_fail_threshold:", "partition_size_fail_threshold: 1GiB"),
            ("# gc_log_threshold: 200ms", "gc_log_threshold: 300ms"),
            ("# gc_warn_threshold: 1000ms", "gc_warn_threshold: 2s"),
            # set by hand near the top: read by its key wherever it is
            ("hinted_handoff_enabled: true", "auto_bootstrap: false\nhinted_handoff_enabled: true")]:
        y = edit(y, old, new)
    files["cassandra.yaml"] = y
    files["cassandra-env.sh"] = edit(files["cassandra-env.sh"], 'CASSANDRA_HEAPDUMP_DIR="$CASSANDRA_LOG_DIR"',
                                     'CASSANDRA_HEAPDUMP_DIR="/srv/dumps"')
    return files


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


def test_import_reads_every_declared_setting_50():
    out = cassandra_config_import(node_50(), "50x", FACTS, "", "/var/lib/cassandra")
    assert dict((k, out["vars"].get(k)) for k in EXPECTED_50) == EXPECTED_50
    assert "cassandra_extra_settings" not in out["vars"]
    assert out["hand_edits"] == []


def test_self_check_passes_on_a_node_with_every_declared_setting_50():
    assert check(node_50(), "50x")["differences"] == []


def node_4x(series):
    files = seeded(stock(series))
    y = files["cassandra.yaml"]
    new = series == "41x"
    for old, line in [
            ("# cdc_raw_directory: /var/lib/cassandra/cdc_raw", "cdc_raw_directory: /srv/cdc"),
            ("# memtable_heap_space: 2048MiB" if new else "# memtable_heap_space_in_mb: 2048",
             "memtable_heap_space: 2GiB" if new else "memtable_heap_space_in_mb: 2048"),
            ("# memtable_flush_writers: 2" if new else "#memtable_flush_writers: 2", "memtable_flush_writers: 4"),
            ("# concurrent_compactors: 1" if new else "#concurrent_compactors: 1", "concurrent_compactors: 4"),
            ("# stream_throughput_outbound: 24MiB/s" if new else "# stream_throughput_outbound_megabits_per_sec: 200",
             "stream_throughput_outbound: 24MiB/s" if new else "stream_throughput_outbound_megabits_per_sec: 400"),
            ("# roles_update_interval: 2000ms" if new else "# roles_update_interval_in_ms: 2000",
             "roles_update_interval: 2s" if new else "roles_update_interval_in_ms: 2000"),
            ("# gc_warn_threshold: 1000ms" if new else "# gc_warn_threshold_in_ms: 1000",
             "gc_warn_threshold: 2000ms" if new else "gc_warn_threshold_in_ms: 2000"),
            # no stock line in 4.x: anywhere
            ("hinted_handoff_enabled: true", "disk_access_mode: mmap_index_only\nhinted_handoff_enabled: true\n"
                                             "auto_bootstrap: true")]:
        y = edit(y, old, line)
    files["cassandra.yaml"] = y
    files["cassandra-env.sh"] = edit(files["cassandra-env.sh"], "# set jvm HeapDumpPath with CASSANDRA_HEAPDUMP_DIR",
                                     'CASSANDRA_HEAPDUMP_DIR="${CASSANDRA_HEAPDUMP_DIR:-/srv/dumps}"'
                                     "  # set jvm HeapDumpPath with it")
    return files


@pytest.mark.parametrize("series", ["40x", "41x"])
def test_import_and_self_check_4x(series):
    out = cassandra_config_import(node_4x(series), series, FACTS, "", "/var/lib/cassandra")
    v = out["vars"]
    assert v["cassandra_cdc_raw_dir"] == "/srv/cdc"
    assert v["cassandra_disk_access_mode"] == "mmap_index_only"
    assert v["cassandra_heap_dump_dir"] == "/srv/dumps"
    assert "cassandra_auto_bootstrap" not in v  # true: Cassandra's default, left out
    if series == "41x":
        assert (v["cassandra_memtable_heap_space"], v["cassandra_roles_update_interval"]) == ("2048MiB", "2000ms")
    else:
        assert (v["cassandra_stream_throughput_outbound_megabits_per_sec"], v["cassandra_gc_warn_threshold_in_ms"]) \
            == (400, 2000)
    assert "cassandra_extra_settings" not in v
    assert out["hand_edits"] == []
    assert "cassandra.yaml: auto_bootstrap: true, Cassandra's default, left out" in out["normalized"]
    assert check(node_4x(series), series)["differences"] == []


@pytest.mark.parametrize("series", ["40x", "41x"])
def test_heap_dump_dir_shell_value_on_4x(series):
    files = seeded(stock(series))
    files["cassandra-env.sh"] = edit(files["cassandra-env.sh"], "# set jvm HeapDumpPath with CASSANDRA_HEAPDUMP_DIR",
                                     'CASSANDRA_HEAPDUMP_DIR="${CASSANDRA_HEAPDUMP_DIR:-$CASSANDRA_LOG_DIR}"'
                                     "  # set jvm HeapDumpPath with it")
    out = cassandra_config_import(files, series, FACTS, "", "/var/lib/cassandra")
    assert out["vars"]["cassandra_heap_dump_dir"] == "$CASSANDRA_LOG_DIR"  # a shell value, as the variable takes it
    assert check(files, series)["differences"] == []


@pytest.mark.parametrize("env_dir, yaml_line", [
    ("/srv/dumps", None),  # cassandra-env.sh only: the role writes heap_dump_path, not compared
    (None, "heap_dump_path: /var/log/cassandra"),  # cassandra.yaml only: stays, not compared
    ("/srv/dumps", "heap_dump_path: /srv/other"),
])
def test_heap_dump_dir_from_cassandra_env_sh_50(env_dir, yaml_line):
    files = seeded(stock("50x"))
    if env_dir:
        files["cassandra-env.sh"] = edit(files["cassandra-env.sh"], 'CASSANDRA_HEAPDUMP_DIR="$CASSANDRA_LOG_DIR"',
                                         'CASSANDRA_HEAPDUMP_DIR="%s"' % env_dir)
    if yaml_line:
        files["cassandra.yaml"] = edit(files["cassandra.yaml"], "# heap_dump_path: /var/lib/cassandra/heapdump", yaml_line)
    out = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert out["vars"].get("cassandra_heap_dump_dir") == env_dir
    assert "cassandra_extra_settings" not in out["vars"]
    assert out["hand_edits"] == []
    if yaml_line:
        assert "cassandra.yaml: %s, not compared (the -XX:HeapDumpPath cassandra-env.sh gives wins)" % yaml_line \
            in out["normalized"]
    assert check(files, "50x")["differences"] == []


@pytest.mark.parametrize("line, value", [
    ("auto_bootstrap: true  # the default", None), ("auto_bootstrap: 'true'", None), ('auto_bootstrap: "true"', None),
    ("auto_bootstrap: True", None), ("auto_bootstrap: yes", None),
    ("auto_bootstrap: false", False), ("auto_bootstrap: false  # by hand", False), ("auto_bootstrap: no", False),
])
def test_auto_bootstrap_however_written(line, value):
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = edit(files["cassandra.yaml"], "hinted_handoff_enabled: true",
                                   line + "\nhinted_handoff_enabled: true")
    out = cassandra_config_import(files, "41x", FACTS, "", "/var/lib/cassandra")
    assert out["vars"].get("cassandra_auto_bootstrap") == value  # never false for a node at the default
    assert out["hand_edits"] == []
    assert check(files, "41x")["differences"] == []


@pytest.mark.parametrize("value", ["mmap  # by hand", "'standard'"])
def test_disk_access_mode_on_4x_however_written(value):
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = edit(files["cassandra.yaml"], "hinted_handoff_enabled: true",
                                   "disk_access_mode: %s\nhinted_handoff_enabled: true" % value)
    out = cassandra_config_import(files, "41x", FACTS, "", "/var/lib/cassandra")
    assert out["vars"]["cassandra_disk_access_mode"] == value.split()[0].strip("'")
    assert out["hand_edits"] == []
    assert check(files, "41x")["differences"] == []


@pytest.mark.parametrize("block", [
    "sstable:\n  selected_format: bti  # moved here",
    "sstable:\n    selected_format: 'bti'",
    "sstable: {selected_format: bti}",
])
def test_sstable_format_in_any_layout(block):
    files = seeded(stock("50x"))
    files["cassandra.yaml"] += "\n" + block + "\n"
    out = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert out["vars"]["cassandra_sstable_format"] == "bti"
    assert "cassandra_extra_settings" not in out["vars"]
    assert check(files, "50x")["differences"] == []


def test_declared_settings_next_to_extra_settings():
    for series, line in (("41x", "disk_access_mode: mmap_index_only\nauto_bootstrap: false"), ("50x", "auto_bootstrap: false")):
        files = seeded(stock(series))
        files["cassandra.yaml"] += line + "\nblock_for_peers_in_remote_dcs: true\n"
        out = cassandra_config_import(files, series, FACTS, "", "/var/lib/cassandra")
        assert out["vars"]["cassandra_auto_bootstrap"] is False
        assert out["vars"]["cassandra_extra_settings"] == {"block_for_peers_in_remote_dcs": True}
        assert out["hand_edits"] == []
        assert check(files, series)["differences"] == []


def test_cdc_raw_dir_of_a_tarball_node():
    # CDC on, cdc_raw_directory left out: under the node's storage dir, where the role then looks for it
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = edit(files["cassandra.yaml"], "cdc_enabled: false", "cdc_enabled: true")
    for key, sub in (("hints_directory", "hints"), ("commitlog_directory", "commitlog"),
                     ("saved_caches_directory", "saved_caches")):  # all set: no other reason for the storage dir
        files["cassandra.yaml"] = edit(files["cassandra.yaml"], "# %s: /var/lib/cassandra/%s" % (key, sub),
                                       "%s: /opt/c/data/%s" % (key, sub))
    files["cassandra.yaml"] = edit(files["cassandra.yaml"], "# data_file_directories:\n#     - /var/lib/cassandra/data",
                                   "data_file_directories:\n    - /opt/c/data/data")
    out = cassandra_config_import(files, "41x", FACTS, "", "/opt/c/data")
    assert out["vars"]["cassandra_config_storage_dir"] == "/opt/c/data"
    assert "cassandra_cdc_raw_dir" not in out["vars"]
