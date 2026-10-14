from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The cassandra.yaml settings that have a variable (some commented out in stock, some absent from it): refused in
# cassandra_extra_settings (a dotted key by its first part), refused for a series without the key, BTI refused
# before storage_compatibility_mode NONE, the directories created, auto_bootstrap false warned about by preflight, and
# the -e names check knowing them.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
ROLE = os.path.join(TOP, "roles", "cassandra_config")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


TASKS = load("roles", "cassandra_config", "tasks", "main.yml")
ROLE_VARS = load("roles", "cassandra_config", "vars", "main.yml")
DEFAULTS = load("roles", "cassandra_config", "defaults", "main.yml")


def task(name, tasks=None):
    todo = list(TASKS if tasks is None else tasks)
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def templar(**variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables)


def render(text, **variables):
    return templar(**variables).template(trust_as_template(text))


def series_vars(version):
    return dict(DEFAULTS, **dict(ROLE_VARS, cassandra_version=version, role_path=ROLE))


def run(name, version="50x", **changes):
    """(the assert passes, its message) of a cassandra_config assert; passes when its when is false."""
    t = task(name)
    variables = dict(series_vars(version), **changes)
    variables.update(t.get("vars", {}))
    variables["inventory_hostname"] = "node1"
    when = t.get("when", [])
    if not all(render("{{ %s }}" % w, **variables) for w in (when if isinstance(when, list) else [when])):
        return True, ""
    that = t["ansible.builtin.assert"]["that"]
    that = that if isinstance(that, list) else [that]
    passed = all(render("{{ %s }}" % x, **variables) for x in that)
    return passed, " ".join(str(render(t["ansible.builtin.assert"]["fail_msg"], **variables)).split())


# --- cassandra_extra_settings ----------------------------------------------

NOW_VARIABLES = ["auto_bootstrap", "disk_access_mode", "file_cache_size", "memtable_heap_space", "memtable_offheap_space",
                 "memtable_flush_writers", "concurrent_compactors", "stream_entire_sstables", "stream_throughput_outbound",
                 "inter_dc_stream_throughput_outbound", "entire_sstable_stream_throughput_outbound",
                 "entire_sstable_inter_dc_stream_throughput_outbound", "streaming_connections_per_host",
                 "internode_streaming_tcp_user_timeout", "roles_update_interval", "permissions_update_interval",
                 "credentials_update_interval", "gc_log_threshold", "gc_warn_threshold", "cdc_raw_directory",
                 "heap_dump_path", "sstable", "partition_size_warn_threshold", "partition_size_fail_threshold",
                 "column_index_size"]
NOW_VARIABLES_40 = ["auto_bootstrap", "disk_access_mode", "file_cache_size_in_mb", "memtable_heap_space_in_mb",
                    "memtable_offheap_space_in_mb", "memtable_flush_writers", "concurrent_compactors",
                    "stream_entire_sstables", "stream_throughput_outbound_megabits_per_sec",
                    "inter_dc_stream_throughput_outbound_megabits_per_sec", "streaming_connections_per_host",
                    "internode_streaming_tcp_user_timeout_in_ms", "roles_update_interval_in_ms",
                    "permissions_update_interval_in_ms", "credentials_update_interval_in_ms", "gc_log_threshold_in_ms",
                    "gc_warn_threshold_in_ms", "cdc_raw_directory"]


@pytest.mark.parametrize("version, key", [("50x", k) for k in NOW_VARIABLES] + [("40x", k) for k in NOW_VARIABLES_40]
                         + [("41x", "disk_access_mode"), ("41x", "auto_bootstrap")])
def test_extra_settings_refuse_the_keys_with_a_variable(version, key):
    passed, msg = run("Refuse extra settings the template already has", version, cassandra_extra_settings={key: "x"})
    assert passed is False
    assert msg.startswith("cassandra_extra_settings: %s (cassandra_" % key)
    assert msg.endswith(") already in cassandra.yaml, or the former name of a key whose variable is set (Cassandra"
                        " refuses both names): set them with their variable")


@pytest.mark.parametrize("key, var", [("heap_dump_path", "cassandra_heap_dump_dir"), ("cdc_raw_directory", "cassandra_cdc_raw_dir"),
                                      ("sstable", "cassandra_sstable_format"), ("auto_bootstrap", "cassandra_auto_bootstrap")])
def test_extra_settings_refusal_names_the_variable(key, var):
    msg = run("Refuse extra settings the template already has", cassandra_extra_settings={key: "x"})[1]
    assert msg.startswith("cassandra_extra_settings: %s (%s) already in cassandra.yaml" % (key, var))


@pytest.mark.parametrize("version, set_var, ok", [("41x", True, False), ("50x", True, False), ("41x", False, True),
                                                  ("40x", True, False)])
def test_former_name_next_to_its_variable_is_refused(version, set_var, ok):
    # Cassandra 4.1+ refuses a cassandra.yaml with both names; 4.0: the template's own key
    changes = {"cassandra_extra_settings": {"gc_warn_threshold_in_ms": 500}}
    if set_var:
        changes.update(cassandra_gc_warn_threshold="2000ms", cassandra_gc_warn_threshold_in_ms=2000)
    passed, msg = run("Refuse extra settings the template already has", version, **changes)
    assert passed is ok
    if not ok:  # the variable to set: on 4.0 its own *_in_ms one
        var = "cassandra_gc_warn_threshold_in_ms" if version == "40x" else "cassandra_gc_warn_threshold"
        assert msg.startswith("cassandra_extra_settings: gc_warn_threshold_in_ms (%s) already" % var)


@pytest.mark.parametrize("key", ["compaction_large_partition_warning_threshold", "compaction_large_partition_warning_threshold_mb"])
def test_former_names_of_the_partition_size_guardrail_are_refused_on_50(key):
    # (on 4.1 the template's own key, compaction_large_partition_warning_threshold, and its _mb former name)
    assert run("Refuse extra settings the template already has", "41x", cassandra_extra_settings={key: "50MiB"})[1] \
        .startswith("cassandra_extra_settings: %s (cassandra_compaction_large_partition_warning_threshold)" % key)
    changes = {"cassandra_extra_settings": {key: "100MiB"}}
    assert run("Refuse extra settings the template already has", **changes)[0] is True
    passed, msg = run("Refuse extra settings the template already has", cassandra_partition_size_warn_threshold="100MiB",
                      **changes)
    assert passed is False and msg.startswith("cassandra_extra_settings: %s (cassandra_partition_size_warn_threshold)" % key)


@pytest.mark.parametrize("version, value, ok", [("50x", "legacy", True), ("41x", "legacy", False), ("50x", "direct", False),
                                                ("40x", "mmap_index_only", True), ("41x", "", True), ("50x", None, True),
                                                ("41x", "mmap-index-only", False)])
def test_disk_access_mode_cassandra_starts_with(version, value, ok):
    assert run("Refuse a disk_access_mode Cassandra would not start with", version, cassandra_disk_access_mode=value)[0] is ok


@pytest.mark.parametrize("name", ["Refuse an SSTable format Cassandra would not start with", "Refuse an empty heap dump dir on 5.0"])
def test_5_0_checks_pass_on_4x_at_the_defaults(name):
    assert task(name)["when"] == "cassandra_version == '50x'"
    assert run(name, "41x", cassandra_heap_dump_dir="")[0] is True  # 4.x: empty is the stock value


def test_extra_settings_refuse_a_dotted_key_by_its_first_part():
    passed, msg = run("Refuse extra settings the template already has", cassandra_extra_settings={
        "sstable.selected_format": "bti", "commitlog_total_space": "8192MiB"})
    assert passed is False and "sstable.selected_format (cassandra_sstable_format) already in cassandra.yaml" in msg
    assert run("Refuse extra settings the template already has",
               cassandra_extra_settings={"commitlog_total_space": "8192MiB", "streaming_stats_enabled": False})[0] is True


# --- series ---------------------------------------------------------------------

@pytest.mark.parametrize("version, var, ok", [
    ("50x", "cassandra_sstable_format", True),
    ("41x", "cassandra_sstable_format", False),
    ("40x", "cassandra_partition_size_warn_threshold", False),
    ("41x", "cassandra_partition_size_fail_threshold", False),
    ("41x", "cassandra_entire_sstable_stream_throughput_outbound", True),
    ("40x", "cassandra_entire_sstable_stream_throughput_outbound", False),
    ("40x", "cassandra_entire_sstable_inter_dc_stream_throughput_outbound", False),
])
def test_settings_of_a_later_series_are_refused(version, var, ok):
    passed, msg = run("Refuse settings of a later Cassandra series", version,
                      cassandra_storage_compatibility_mode="NONE", **{var: "big" if "format" in var else "24MiB/s"})
    assert passed is ok
    if not ok:
        assert msg.startswith(var + ": not a setting of Cassandra " + version[0] + "." + version[1])


@pytest.mark.parametrize("version", ["40x", "41x", "50x"])
def test_series_check_passes_at_the_defaults(version):
    assert run("Refuse settings of a later Cassandra series", version)[0] is True


@pytest.mark.parametrize("fmt, mode, ok", [("bti", "NONE", True), ("bti", "UPGRADING", True), ("bti", "CASSANDRA_4", False),
                                           ("BTI", "NONE", False), ("trie", "NONE", False), ("big", "CASSANDRA_4", True),
                                           ("", "CASSANDRA_4", True), (None, "CASSANDRA_4", True)])
def test_sstable_format_cassandra_starts_with(fmt, mode, ok):
    # Cassandra 5.0: bti refused in CASSANDRA_4 only; format names are lower case
    passed, msg = run("Refuse an SSTable format Cassandra would not start with", cassandra_sstable_format=fmt,
                      cassandra_storage_compatibility_mode=mode)
    assert passed is ok
    if not ok:
        assert "bti needs cassandra_storage_compatibility_mode UPGRADING or NONE (it is %s)" % mode in msg


@pytest.mark.parametrize("value, ok", [("", False), (None, False), ("$CASSANDRA_LOG_DIR", True), ("/srv/dumps", True)])
def test_heap_dump_dir_not_empty_on_50(value, ok):
    assert run("Refuse an empty heap dump dir on 5.0", cassandra_heap_dump_dir=value)[0] is ok


# --- directories -----------------------------------------------------------

@pytest.mark.parametrize("changes, dirs", [
    ({}, []),
    ({"cassandra_cdc_enabled": True}, [["cdc_raw dir", ["/var/lib/cassandra/cdc_raw"]]]),
    ({"cassandra_cdc_enabled": True, "cassandra_config_storage_dir": "/opt/c/data/"}, [["cdc_raw dir", ["/opt/c/data/cdc_raw"]]]),
    ({"cassandra_cdc_raw_dir": "/srv/cdc"}, [["cdc_raw dir", ["/srv/cdc"]]]),
    ({"cassandra_heap_dump_dir": "/srv/dumps"}, [["heap dump dir", ["/srv/dumps"]]]),
    ({"cassandra_heap_dump_dir": "$CASSANDRA_HOME/dumps"}, []),  # the shell's: not created
])
def test_more_dirs(changes, dirs):
    assert render(ROLE_VARS["_cassandra_config_more_dirs"], **dict(series_vars("50x"), **changes)) == dirs
    # the same on 4.x (whose default heap dump dir is empty, 5.0's $CASSANDRA_LOG_DIR: neither created)
    assert render(ROLE_VARS["_cassandra_config_more_dirs"], **dict(series_vars("41x"), **changes)) == dirs


def test_more_dirs_are_read_created_and_checked():
    read = task("Read the node's config files and directories")["vars"]["_request"]
    variables = dict(series_vars("50x"), cassandra_cdc_raw_dir="/srv/cdc", cassandra_heap_dump_dir="/srv/dumps",
                     cassandra_config_keep_files=[], cassandra_jmx_users=[],
                     ansible_facts={"os_family": "Debian"})
    variables["cassandra_data_file_directories"] = ["/var/lib/cassandra/data"]
    assert {"/srv/cdc", "/srv/dumps"} <= set(render(read["dirs"], **variables))
    assert [["cdc_raw dir", "/srv/cdc"], ["heap dump dir", "/srv/dumps"]] == render(read["dir_stats"], **variables)[-2:]


# --- preflight ------------------------------------------------------------------

PREFLIGHT = next(p for p in load("playbooks", "preflight.yml") if p.get("name") == "Cassandra preflight checks")
SUSPICIOUS = task("Warn about sensitive or suspicious settings", PREFLIGHT["tasks"])
WHY = ("a node that loses its data restarts empty without streaming it back; remove the setting (default true) unless"
       " you know why it is there")


@pytest.mark.parametrize("hostvars, warned", [
    ({"n1": {}, "n2": {}}, ""),
    ({"n1": {"cassandra_auto_bootstrap": True}, "n2": {"cassandra_auto_bootstrap": None}}, ""),
    ({"n1": {"cassandra_auto_bootstrap": False}, "n2": {"cassandra_auto_bootstrap": "false"}, "n3": {}}, "n1, n2"),
])
def test_preflight_warns_about_auto_bootstrap_false(hostvars, warned):
    # a WARNING (note.yml), never a refusal
    assert SUSPICIOUS["ansible.builtin.include_role"]["tasks_from"] == "note.yml"
    variables = dict(SUSPICIOUS["vars"], ansible_play_hosts=list(hostvars), hostvars=hostvars)
    found = render(SUSPICIOUS["vars"]["_found"], **variables)
    assert found == (["WARNING  %s: auto_bootstrap: false \u2014 %s" % (warned, WHY)] if warned else [])
    assert render("{{ %s }}" % SUSPICIOUS["when"], **variables) is bool(warned)
    assert not [t for t in PREFLIGHT["tasks"] if "auto_bootstrap" in str(t.get("ansible.builtin.assert", ""))]


def test_add_datacenter_sets_auto_bootstrap_itself():
    plays = load("playbooks", "add_datacenter.yml")
    adds = [t for p in plays for t in p.get("tasks", []) if t.get("name") == "Add this node"]
    assert adds and adds[0]["vars"]["cassandra_auto_bootstrap"] is False
    assert "cassandra_extra_settings" not in adds[0]["vars"]


# --- -e names ------------------------------------------------------------------

@pytest.mark.parametrize("name", ["cassandra_auto_bootstrap", "cassandra_cdc_raw_dir", "cassandra_sstable_format",
                                  "cassandra_stream_throughput_outbound",
                                  "cassandra_stream_throughput_outbound_megabits_per_sec",
                                  "cassandra_partition_size_fail_threshold", "cassandra_gc_warn_threshold_in_ms"])
def test_extra_vars_check_knows_the_new_names(name):
    assert cassandra_unknown_extra_vars([name], []) == {}


def test_extra_vars_check_still_catches_a_typo():
    assert cassandra_unknown_extra_vars(["cassandra_stream_throughput_outbund"], []) == {
        "cassandra_stream_throughput_outbund": "cassandra_stream_throughput_outbound"}


# --- reset_node ------------------------------------------------------------------

RESET_PLAN = load("roles", "cassandra_service", "tasks", "node_standing_read.yml")  # (reset_node_plan.yml reads it)
RESET_DIRS = next(t for t in RESET_PLAN if t.get("name") == "Work out its Cassandra directories")["vars"]["_inventory"]


@pytest.mark.parametrize("changes, cdc_raw", [
    ({}, []),
    ({"cassandra_cdc_raw_dir": "/srv/cdc"}, ["/srv/cdc"]),
    ({"cassandra_extra_settings": {"cdc_raw_directory": "/srv/old"}}, ["/srv/old"]),  # an inventory from before the variable
    ({"cassandra_cdc_enabled": True}, ["/var/lib/cassandra/cdc_raw"]),
    ({"cassandra_cdc_enabled": True, "cassandra_config_storage_dir": "/opt/c/data/"}, ["/opt/c/data/cdc_raw"]),
])
def test_reset_node_empties_the_cdc_raw_dir(changes, cdc_raw):
    variables = dict(series_vars("50x"), cassandra_data_file_directories=["/var/lib/cassandra/data"],
                     _extra=changes.get("cassandra_extra_settings", {}), **changes)
    dirs = render(RESET_DIRS, **variables)
    assert [d for kind, d in dirs if kind == "cdc_raw"] == cdc_raw


def test_add_datacenter_rewrites_the_config_without_auto_bootstrap():
    # the last write rewrites the files: no auto_bootstrap false left
    plays = load("playbooks", "add_datacenter.yml")
    writes = [t for p in plays for t in p.get("tasks", []) if t.get("name") == "Write the config without auto_bootstrap"]
    assert writes and writes[0]["vars"]["cassandra_config_normalize"] is True
    assert "cassandra_auto_bootstrap" not in writes[0]["vars"]


@pytest.mark.parametrize("playbook, task_name", [("add_node.yml", "Prepare this node"), ("add_node.yml", "Add this node"),
                                                 ("replace_node.yml", "Replace")])
def test_add_and_replace_stream_whatever_the_inventory(playbook, task_name):
    # an inventory's cassandra_auto_bootstrap false (e.g. imported, only a warning) never reaches a node they add
    def walk(tasks):
        for t in tasks:
            yield t
            yield from walk(t.get("block", []))
    found = [t for p in load("playbooks", playbook) for t in walk(p.get("tasks", []))
             if t.get("name") == task_name and "ansible.builtin.include_role" in t]
    assert found and all(t["vars"]["cassandra_auto_bootstrap"] is True for t in found)


def test_auto_bootstrap_written_false_whatever_its_spelling():
    for value in (False, "no", 0, "false"):
        text = render(open(os.path.join(ROLE, "templates", "5.0", "cassandra.yaml.j2")).read().split("{% set _extra")[0][-400:],
                      cassandra_auto_bootstrap=value, cassandra_disk_access_mode="")
        assert "auto_bootstrap: false" in text, value
