from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The cassandra.yaml settings that have a variable (some commented out in stock, some absent from it): refused in
# cassandra_extra_settings (a dotted key by its first part), refused for a series without the key, BTI refused
# before storage_compatibility_mode NONE, the directories created, auto_bootstrap false warned about by preflight, and
# the -e names check knowing them.

import os
import re

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_yaml_properties as known
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

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


TASKS = load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "main.yml"))
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
                 "column_index_size", "native_transport_max_frame_size", "auto_snapshot_ttl", "streaming_stats_enabled"]
NOW_VARIABLES_40 = ["auto_bootstrap", "disk_access_mode", "file_cache_size_in_mb", "memtable_heap_space_in_mb",
                    "memtable_offheap_space_in_mb", "memtable_flush_writers", "concurrent_compactors",
                    "stream_entire_sstables", "stream_throughput_outbound_megabits_per_sec",
                    "inter_dc_stream_throughput_outbound_megabits_per_sec", "streaming_connections_per_host",
                    "internode_streaming_tcp_user_timeout_in_ms", "roles_update_interval_in_ms",
                    "permissions_update_interval_in_ms", "credentials_update_interval_in_ms", "gc_log_threshold_in_ms",
                    "gc_warn_threshold_in_ms", "cdc_raw_directory", "native_transport_max_frame_size_in_mb"]


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


@pytest.mark.parametrize("version", ["41x", "50x"])
def test_former_frame_size_name_next_to_its_variable_is_refused(version):
    changes = {"cassandra_extra_settings": {"native_transport_max_frame_size_in_mb": 32}}
    assert run("Refuse extra settings the template already has", version, **changes)[0] is True
    passed, msg = run("Refuse extra settings the template already has", version,
                      cassandra_native_transport_max_frame_size="32MiB", **changes)
    assert passed is False and msg.startswith(
        "cassandra_extra_settings: native_transport_max_frame_size_in_mb (cassandra_native_transport_max_frame_size)")


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
               cassandra_extra_settings={"commitlog_total_space": "8192MiB",
                                         "internode_application_send_queue_capacity": "8MiB"})[0] is True


# --- series ---------------------------------------------------------------------

SINCE = "Refuse variables of keys the release does not know"
NOT_INSTALLED = {"cassandra_config_pkg_conf": {"stat": {"exists": False}}}  # (the task runs with read.yml too)
# set values of the variables of _cassandra_config_since
SET = {"cassandra_sstable_format": "big", "cassandra_streaming_stats_enabled": False,
       "cassandra_auto_snapshot_ttl": "30d", "cassandra_partition_size_warn_threshold": "100MiB",
       "cassandra_partition_size_fail_threshold": "1GiB"}


def since(version, var, **changes):
    return run(SINCE, version, cassandra_storage_compatibility_mode="NONE",
               **dict(NOT_INSTALLED, **dict({var: SET.get(var, "24MiB/s")}, **changes)))


@pytest.mark.parametrize("version, var, ok", [
    ("50x", "cassandra_sstable_format", True),
    ("41x", "cassandra_sstable_format", False),
    ("40x", "cassandra_partition_size_warn_threshold", False),
    ("41x", "cassandra_partition_size_fail_threshold", False),
    ("41x", "cassandra_entire_sstable_stream_throughput_outbound", True),
    ("40x", "cassandra_entire_sstable_stream_throughput_outbound", False),
    ("40x", "cassandra_entire_sstable_inter_dc_stream_throughput_outbound", False),
    ("41x", "cassandra_auto_snapshot_ttl", True),
    ("40x", "cassandra_auto_snapshot_ttl", False),
    ("50x", "cassandra_streaming_stats_enabled", True),
    ("41x", "cassandra_streaming_stats_enabled", True),  # the latest 4.1 release known
    ("40x", "cassandra_streaming_stats_enabled", False),
])
def test_settings_of_a_later_series_are_refused(version, var, ok):
    passed, msg = since(version, var)
    assert passed is ok
    if not ok:
        series = version[0] + "." + version[1]
        key, minimum = ROLE_VARS["_cassandra_config_since"][var]
        assert msg == ("node1: %s: Cassandra %s (the latest %s release known) does not know %s, it came with %s. That"
                       " needs cassandra_version %sx or later: set it, or leave the variable empty; nothing is changed."
                       % (var, ROLE_VARS["_cassandra_config_template_versions"][series], series, key, minimum,
                          minimum[:3].replace(".", "")))


@pytest.mark.parametrize("version", ["40x", "41x", "50x"])
def test_series_check_passes_at_the_defaults(version):
    assert run(SINCE, version, **NOT_INSTALLED)[0] is True


UPGRADE_THE_NODE = "Upgrade the node, or leave the variable empty; nothing is changed."
PIN = "Pin 4.1.1 or later in cassandra_package_version, or leave the variable empty; nothing is changed."


@pytest.mark.parametrize("changes, ok, release, fix", [
    ({"_cassandra_config_package": "4.1.0"}, False, "4.1.0 (installed)", UPGRADE_THE_NODE),
    ({"_cassandra_config_package": "4.1.1"}, True, "", ""),
    ({"_cassandra_config_package": "4.1.12-1"}, True, "", ""),
    # nothing installed yet: the pin
    ({"cassandra_package_version": "4.1.0"}, False, "4.1.0 (cassandra_package_version)", PIN),
    ({"cassandra_package_version": "4.1.0*"}, False, "4.1.0 (cassandra_package_version)", PIN),
    ({"cassandra_package_version": "1:4.1.0-1"}, False, "4.1.0 (cassandra_package_version)", PIN),  # an epoch
    ({"cassandra_package_version": "4.1.2"}, True, "", ""),
    ({"cassandra_package_version": "4.1.0", "_cassandra_config_package": "4.1.3"}, True, "", ""),  # installed first
    ({"_cassandra_config_package": "4.0.21"}, True, "", ""),  # another series installed (upgrade preview): the series'
    ({"cassandra_package_version": "4.0.20"}, True, "", ""),  # pinned to another series: the series' latest
    ({}, True, "", ""),  # the latest 4.1 release known
    ({"cassandra_streaming_stats_enabled": "", "_cassandra_config_package": "4.1.0"}, True, "", ""),  # empty: no line
    ({"cassandra_streaming_stats_enabled": None, "_cassandra_config_package": "4.1.0"}, True, "", ""),
    # the upgrade's preview: the release it installs (the pin) before the installed one
    ({"_cassandra_config_package": "4.1.0", "cassandra_package_version": "4.1.5",
      "_cassandra_config_target_preview": True}, True, "", ""),
    ({"_cassandra_config_package": "4.1.5", "cassandra_package_version": "4.1.0",
      "_cassandra_config_target_preview": True}, False, "4.1.0 (cassandra_package_version)", PIN),
    ({"_cassandra_config_package": "4.1.0", "_cassandra_config_target_preview": True}, False, "4.1.0 (installed)",
     UPGRADE_THE_NODE),  # (nothing pinned)
])
def test_a_patch_release_minimum(changes, ok, release, fix):
    passed, msg = since("41x", "cassandra_streaming_stats_enabled", **changes)
    assert passed is ok
    if not ok:
        assert msg == ("node1: cassandra_streaming_stats_enabled: Cassandra %s does not know streaming_stats_enabled,"
                       " it came with 4.1.1. %s" % (release, fix))


def test_several_variables_in_one_message():
    passed, msg = since("41x", "cassandra_streaming_stats_enabled", _cassandra_config_package="4.1.0",
                        cassandra_sstable_format="big")
    assert passed is False
    assert msg == ("node1: cassandra_streaming_stats_enabled: Cassandra 4.1.0 (installed) does not know"
                   " streaming_stats_enabled, it came with 4.1.1; cassandra_sstable_format: Cassandra 4.1.0 (installed)"
                   " does not know sstable.selected_format, it came with 5.0. That needs cassandra_version 50x or later:"
                   " set it, or leave the variables empty; nothing is changed.")  # a later series: cassandra_version
    # pinned: the latest release one of them came with
    msg = since("41x", "cassandra_streaming_stats_enabled", cassandra_package_version="4.1.0",
                cassandra_key_cache_size="100MiB")[1]
    assert msg == PIN.join(["node1: cassandra_streaming_stats_enabled: Cassandra 4.1.0 (cassandra_package_version) does"
                            " not know streaming_stats_enabled, it came with 4.1.1. ", ""])


def test_the_check_runs_with_the_installed_release_and_before_install():
    # read.yml (Cassandra installed: its release read), and main.yml when nothing is installed yet (--check)
    imports = [t for t in load(*"roles cassandra_config tasks main.yml".split()) if "since.yml" in str(t)]
    assert imports and imports[0]["when"] == "not cassandra_config_pkg_conf.stat.exists"
    assert [t for t in load(*"roles cassandra_config tasks read.yml".split()) if "since.yml" in str(t)]
    # an operator message (the ops callback prints it as is: it names the host, said once)
    assert task(SINCE)["vars"]["cassandra_output"] is True


def _first_release(key, series):
    """The first release of series that accepts key (cassandra_yaml_properties): the series ("4.1") when all do."""
    if key in known.ALL[series]:
        return series
    ranges = known.SOME[series].get(key)
    return "%s.%d" % (series, ranges[0][0]) if ranges else None


# The variables whose minimum is a patch release: the keys a series got after its first release (SOME)
PATCH_MINIMUMS = sorted(var for var, (key, minimum) in ROLE_VARS["_cassandra_config_since"].items()
                        if key.split(".")[0] in known.SOME.get(minimum[:3], {}))


def test_minimums_match_the_releases_lists():
    assert PATCH_MINIMUMS == ["cassandra_streaming_stats_enabled"]  # (today: the one)
    for var, (key, minimum) in ROLE_VARS["_cassandra_config_since"].items():
        assert _first_release(key.split(".")[0], minimum[:3]) == minimum, var
        earlier = {"4.1": "4.0", "5.0": "4.1"}.get(minimum[:3])
        if minimum.count(".") == 1 and earlier:  # a series' first release: none of the series before knows it
            top = key.split(".")[0]
            assert top not in known.ALL[earlier] and top not in known.SOME[earlier], var


def _variable_keys(series):
    """{key: variables} of the top-level keys the series' cassandra.yaml template writes through an optional variable
    (empty by default: one an inventory sets, or not)."""
    with open(os.path.join(ROLE, "templates", series, "cassandra.yaml.j2"), encoding="utf-8") as f:
        lines = f.read().split("\n")
    out = {}
    for line in lines:
        m = re.match(r"^(?:\{\{[^}]*\}\}|#)?\s?([a-z0-9_]+):", line)
        if m:
            for var in re.findall(r"cassandra_\w+", line):
                if DEFAULTS.get(var, "x") in ("", None):
                    out.setdefault(m.group(1), set()).add(var)
    return out


@pytest.mark.parametrize("series, earlier", [("4.1", "4.0"), ("5.0", "4.1")])
def test_every_key_a_series_added_has_a_minimum(series, earlier):
    """The other way: a key the series' template writes through a variable that the series before does not know has a
    _cassandra_config_since entry, unless the series before has its own variable for it (its former name, e.g.
    cassandra_file_cache_size_in_mb: the variable of the other naming is ignored there)."""
    with open(os.path.join(ROLE, "templates", earlier, "cassandra.yaml.j2"), encoding="utf-8") as f:
        before = set(re.findall(r"cassandra_\w+", f.read()))
    since = ROLE_VARS["_cassandra_config_since"]
    missing = []
    for key, variables in sorted(_variable_keys(series).items()):
        if key in known.ALL[earlier] or key in known.SOME[earlier]:
            continue
        for var in sorted(variables):
            own = [v for v in before if v == var or v.startswith(var + "_")]
            if not own and var not in since:
                missing.append(var)
            elif var in since:
                assert since[var][0].split(".")[0] == key, var
    assert missing == []


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
HEALTH = next(p for p in load("playbooks", "health_check.yml") if p.get("name") == "Cluster health")


@pytest.mark.parametrize("hostvars, warned", [
    ({"n1": {}, "n2": {}}, ""),
    ({"n1": {"cassandra_auto_bootstrap": True}, "n2": {"cassandra_auto_bootstrap": None}}, ""),
    ({"n1": {"cassandra_auto_bootstrap": False}, "n2": {"cassandra_auto_bootstrap": "false"}, "n3": {}}, "n1, n2"),
])
def test_health_check_says_auto_bootstrap_false_in_its_settings(hostvars, warned):
    # the cluster's posture: health_check's SETTINGS TO REVIEW block (two lines), not preflight's, never a refusal
    report = task("Report", HEALTH["tasks"])
    variables = dict(report["vars"], ansible_play_hosts=list(hostvars), hostvars=hostvars, _checked=list(hostvars))
    found = render(report["vars"]["_settings"], **variables)
    assert found == (["auto_bootstrap false on %s: a node that loses its data would restart empty, without streaming it"
                      " back" % warned, "  remove cassandra_auto_bootstrap (the default is true); kept on purpose: list"
                      " auto_bootstrap_false in cassandra_settings_kept_on_purpose"] if warned else [])
    assert not [t for t in PREFLIGHT["tasks"] if "suspicious" in str(t)]
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
                                  "cassandra_partition_size_fail_threshold", "cassandra_gc_warn_threshold_in_ms",
                                  "cassandra_native_transport_max_frame_size_in_mb", "cassandra_audit_max_log_size"])
def test_extra_vars_check_knows_the_new_names(name):
    assert cassandra_unknown_extra_vars([name], []) == {}


def test_extra_vars_check_still_catches_a_typo():
    assert cassandra_unknown_extra_vars(["cassandra_stream_throughput_outbund"], []) == {
        "cassandra_stream_throughput_outbund": "cassandra_stream_throughput_outbound"}


# --- reset_node ------------------------------------------------------------------

RESET_PLAN = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "node_standing_read.yml"))  # (reset_node_plan.yml reads it)
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


def test_the_data_dirs_the_node_uses_rendered_whole():
    # the list of the inventory's dirs is the filter's input as a whole (a filter binds tighter than +)
    task = next(t for t in RESET_PLAN if t.get("name") == "Work out its Cassandra directories")
    variables = dict(series_vars("50x"), cassandra_data_file_directories=["/srv/a", "/srv/b"], _live="",
                     _extra={"local_system_data_file_directory": "/srv/sys"}, cassandra_conf_dir="/etc/cassandra")
    dirs = render(task["ansible.builtin.set_fact"]["_cassandra_node_standing_data_dirs"], **variables)
    assert dirs["own"] == ["/srv/a", "/srv/b", "/srv/sys"] and "/var/lib/cassandra/data" in dirs["elsewhere"]


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


@pytest.mark.parametrize("ring, members_warned", [
    (None, ""),  # the ring not read: a replace flag is not judged
    ([("10.0.0.1", "U")], "n1"),  # n2 holds the flag but is not in the ring yet (a replacement staged: replace_node's own)
    ([("10.0.0.1", "U"), ("10.0.0.2", "D")], "n1"),  # n2 down at its address: maybe a replacement of it
    ([("10.0.0.1", "U"), ("10.0.0.2", "U")], "n1, n2"),  # both up in the ring: both judged
])
def test_health_check_judges_a_replace_flag_on_the_members_of_the_ring(ring, members_warned):
    # a member: up in the ring at its address (not a node whose check merely ran)
    hostvars = {"n1": {"inventory_hostname": "n1", "ansible_host": "10.0.0.1", "cassandra_jvm_extra_options": ["-Dcassandra.replace_address=10.0.0.7"]},
                "n2": {"inventory_hostname": "n2", "ansible_host": "10.0.0.2", "cassandra_jvm_extra_options": ["-Dcassandra.replace_address=10.0.0.7"]}}
    if ring is not None:
        hostvars["n1"]["cassandra_health_self"] = {"cluster_status": {"dc1": {"nodes": [{"address": a, "status": s} for a, s in ring]}}}
    report = task("Report", HEALTH["tasks"])
    checked = [] if ring is None else ["n1", "n2"]
    variables = dict(report["vars"], ansible_play_hosts=["n1", "n2"], hostvars=hostvars, _checked=checked)
    found = render(report["vars"]["_settings"], **variables)
    assert [line.split(" on ")[1].split(":")[0] for line in found[::2]] == ([members_warned] if members_warned else [])
