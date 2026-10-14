from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The CONVERGENCE section of an import (90 nodes, made up): how many settings every node shares, the others with
# their values and nodes, what to change where to give every node the value most have, how many nodes change and
# whether apply_config restarts them, the command to check it; the files kept as edited by hand, what differs from
# the template and which variable would carry it. Output only, secrets never shown.

import os

import pytest

from ansible_collections.community.cassandra.plugins.filter import cassandra_import_convergence as convergence
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4, cassandra_inventory_files, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_convergence import (
    _carry, _kind, cassandra_import_convergence)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
REPORT = "/p/reports/big/report.txt"
SECRETS = ("s3cret-dc1", "s3cret-dc3", "k-dc1", "k-dc3", "hunter2")
# 3 DCs of 3 racks of 10 nodes: cass001..cass030 in dc1, cass031..cass060 in dc2, cass061..cass090 in dc3
NAMES = ["cass%03d" % i for i in range(1, 91)]
OLD = NAMES[60:70]  # 256 tokens: kept for life
FEWER = NAMES[10:15]  # streaming_connections_per_host 1
JVM_KEPT = NAMES[30:50]  # jvm11-server.options kept by hand
UNREAD = "cass045"


def _dc(i):
    return "dc%d" % (i // 30 + 1)


def _rack(i):
    return "r%d" % (i % 30 // 10 + 1)


def fake_node(i):
    name = NAMES[i]
    dc = _dc(i)
    ip = "10.0.%d.%d" % (i // 30 + 1, i + 1)
    extra = {"streaming_connections_per_host": 1 if name in FEWER else 6 if name == "cass090" else 2,
             "sstable_preemptive_open_interval": "50MiB"}
    variables = {
        "cassandra_seeds": ["cass001", "cass031", "cass061"], "cassandra_num_tokens": 256 if name in OLD else 16,
        "cassandra_listen_address": IPV4, "cassandra_broadcast_rpc_address": ip,
        "cassandra_concurrent_reads": 64,
        "cassandra_heap_size": "12G" if dc == "dc2" and _rack(i) == "r1" else "16G",
        "cassandra_extra_settings": extra,
        "cassandra_jmx_password": "s3cret-%s" % ("dc3" if dc == "dc3" else "dc1"),
        "cassandra_config_file_permissions": {"cassandra.yaml": "0644" if name in ("cass002", "cass033", "cass077")
                                              else "0640"},
        "cassandra_data_file_directories": ["/data1", "/data2"] if name in ("cass088", "cass089") else ["/data"],
        "cassandra_linux_limits": {"nofile": 200000 if name == "cass007" else 100000},
        "cassandra_medusa_extra_settings": {"storage": {"secret_access_key": "k-%s" % ("dc3" if dc == "dc3" else "dc1"),
                                                        "bucket_name": "backups"}},
    }
    hand, keep = [], {}
    if name in JVM_KEPT:
        hand += ["jvm11-server.options, line 60:", "  + -XX:+UseNUMA"]
        keep["cassandra_config_keep_files"] = ["jvm11-server.options"]
    if name in ("cass061", "cass062", "cass063"):
        hand += ["logback.xml, line 40:", '  - <logger name="org.apache.cassandra" level="DEBUG"/>',
                 '  + <logger name="org.apache.cassandra" level="INFO"/>', "logback.xml, line 52:",
                 '  + <logger name="com.example.audit" level="WARN"/>']
        keep["cassandra_config_keep_files"] = ["logback.xml"]
    if name == "cass090":
        hand += ["cassandra-env.sh, line 300:", '  + JVM_OPTS="$JVM_OPTS -Dcassandra.example=1"',
                 "cassandra-env.sh, line 301:", "  + export KEYSTORE_PASSWORD=hunter2"]
        keep["cassandra_config_keep_files"] = ["cassandra-env.sh"]
    if name in ("cass088", "cass089"):
        keep["cassandra_linux_manage"] = False
    node = {"name": name, "address": ip, "ansible_host": ip, "hostname": name, "dc": dc, "rack": _rack(i),
            "read": name != UNREAD, "vars": variables, "hand_edits": hand, "normalized": [], "notes": [], "keep": keep}
    if name == UNREAD:
        node.update(vars={}, hand_edits=[], keep={}, reason="unreachable")
    return node


def layout():
    return cassandra_inventory_layout([fake_node(i) for i in range(90)], "Big")


def golden(name, lines):
    path = os.path.join(FIXTURES, name)
    if os.environ.get("CASSANDRA_GOLDEN_WRITE"):  # to write them again, after a wanted change
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    with open(path) as f:
        return f.read().split("\n")[:-1]


def section(**kwargs):
    return cassandra_import_convergence(layout(), {}, report_file=REPORT, cwd="/p", **kwargs)


def test_the_screen_of_90_nodes():
    screen = section(screen=True, width=120)
    assert screen == golden("convergence_90_screen.txt", screen)
    assert screen[0] == ("CONVERGENCE  4/11 settings common to all nodes, 7 differ, 3 files kept by hand"
                         " (1 node not read: not counted)")
    assert screen[1] == "  Next (5 of 7, the safest first, then the fewest nodes):"  # the top 5 actions
    assert len([line for line in screen if line.startswith("    cassandra_")]) == 5


@pytest.mark.parametrize("width", [80, 100, 200])
def test_the_screen_fits_the_terminal(width):
    for line in section(screen=True, width=width):
        assert len(line) <= width, line


def test_the_report_of_90_nodes():
    report = section(width=160)
    assert report == golden("convergence_90_report.txt", report)


@pytest.mark.parametrize("screen", [True, False])
def test_no_secret_shown(screen):
    text = "\n".join(section(screen=screen))
    for secret in SECRETS:
        assert secret not in text
    if not screen:
        assert "secret 1 (59 nodes)" in text and "KEYSTORE_PASSWORD=****" in text


def test_the_score():
    report = section()
    score = report[1].strip()
    # counted: every SETTINGS row but the addresses and what a node keeps for life (num_tokens)
    assert score.startswith("4/11 settings common to all nodes, 7 differ, 3 files kept by hand"), score
    assert "(1 node not read: not counted)" in score
    assert any("cassandra_broadcast_rpc_address" in line and "cassandra_num_tokens" in line for line in report)


def test_each_setting_its_values_action_and_check():
    report = section()
    text = "\n".join(report)
    # the values, most nodes first, the others with their nodes
    assert any(line.startswith("  cassandra_extra_settings.streaming_connections_per_host")
               and "2 (83 nodes)   1 (5: G6)   6 (1: cass090)" in line for line in report)
    # a node or two: --limit to them; many: the whole cluster
    assert "ansible-playbook community.cassandra.apply_config --limit cass007 --check --diff" in text
    assert "apply_config --limit cass011,cass012,cass013,cass014,cass015,cass090 --check --diff" in text
    # no restart for a mode, a restart for cassandra.yaml, unknown for what apply_config does not write
    assert "3 nodes change, no restart" in text and "6 nodes change, then restart" in text
    assert "1 node changes, no restart (cassandra_linux, applied live)" in text
    # what apply_config does not write: no check that would show nothing
    assert "30 nodes change, restart unknown (apply_config does not write it); check: none, apply_config does not" \
        in text
    # a data directory: a move first
    assert "moved by hand first, the node stopped" in text
    # a top-level variable: to the cluster's group_vars, removed below it
    flat = " ".join(text.split())
    assert ("cassandra_heap_size: 16G in group_vars/big/main.yml, removed from group_vars of 5 groups (big_dc1,"
            " big_dc2_r1, big_dc2_r2, big_dc2_r3, big_dc3)") in flat
    # a sub-key: set in the files that keep another value of it only
    assert "streaming_connections_per_host: 2 in cassandra_extra_settings in host_vars of 6 nodes" in flat
    # a secret: by its number, the value most nodes have
    assert "cassandra_jmx_password: secret 1 (its value in group_vars/big_dc1/) in group_vars/big/secrets.yml" in flat


def test_the_files_kept_by_hand():
    report = section()
    at = report.index("  KEPT BY HAND — cassandra_config_keep_files: left as their node has them")
    text = "\n".join(report[at:])
    assert "-XX:+UseNUMA   → cassandra_jvm11_extra_options" in text
    assert "→ cassandra_log_level_cassandra" in text and "com.example.audit" in text and "no variable" in text
    assert "→ cassandra_jvm_extra_options" in text
    assert "host_vars/cass090/" in text and "host_vars of 3 nodes (cass061, cass062, cass063)" in text


def test_the_restart_kinds():
    assert _kind("cassandra_concurrent_reads") == "restart"
    assert _kind("cassandra_heap_size") == "restart"
    assert _kind("cassandra_jvm11_extra_options") == "restart"
    assert _kind("cassandra_jmx_users") == "restart"
    assert _kind("cassandra_config_file_permissions") == "none"
    assert _kind("cassandra_seeds") == "seeds"
    assert _kind("cassandra_data_file_directories") == "move"
    assert _kind("cassandra_linux_limits") == "os"
    assert _kind("cassandra_jmx_password") == "login" and _kind("cassandra_cql_password") == "login"
    assert _kind("cassandra_server_keystore_password") == "keystore"
    assert _kind("cassandra_linux_timesync") == "restart"  # (synced in the restart window)
    assert _kind("cassandra_rpm_conf_alternative") == "restart"
    assert _kind("cassandra_internode_encryption") == "switch" and _kind("cassandra_authenticator") == "switch"
    assert _kind("cassandra_package_version") == "version" and _kind("cassandra_java_version") == "version"
    assert _kind("cassandra_medusa_extra_settings") == "unknown"


@pytest.mark.parametrize("name, line, carried", [
    ("cassandra.yaml", "concurrent_writes: 48", ("cassandra_concurrent_writes", "48")),
    ("cassandra.yaml", "hints_directory: /hints", ("cassandra_hints_dir", "/hints")),  # (its variable's own name)
    ("cassandra.yaml", "some_new_key: 1", ("cassandra_extra_settings", "{some_new_key: 1}")),
    ("cassandra.yaml", "# a comment", (None, None)),
    ("jvm-server.options", "-Dfoo=1", ("cassandra_jvm_extra_options", "[-Dfoo=1] (if no other line of the file sets it)")),
    ("jvm17-server.options", "-XX:+UseNUMA",
     ("cassandra_jvm17_extra_options", "[-XX:+UseNUMA] (if no other line of the file sets it)")),
    ("jvm-server.options", "-Xmx8G", ("cassandra_heap_size", "8G")),
    ("jvm-server.options", "-Xmn2G", ("cassandra_heap_newsize", "2G")),
    ("cassandra-env.sh", 'JVM_OPTS="$JVM_OPTS -Dx=1"', ("cassandra_jvm_extra_options", "[-Dx=1]")),
    ("cassandra-env.sh", 'JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=h1"', ("cassandra_jmx_rmi_hostname", "h1")),
    ("cassandra-env.sh", 'MAX_HEAP_SIZE="8G"', ("cassandra_heap_size", "8G")),
    ("cassandra-env.sh", 'JMX_PORT="7299"', ("cassandra_jmx_port", "7299")),
    ("cassandra-env.sh", "ulimit -n 1", (None, None)),
    ("cassandra-rackdc.properties", "prefer_local=true", ("cassandra_prefer_local", "true")),
    ("cassandra-rackdc.properties", "rack=r9", ("cassandra_rackdc_rack", "r9")),
    ("logback.xml", '<logger name="org.apache.cassandra" level="WARN"/>', ("cassandra_log_level_cassandra", "WARN")),
    ("logback.xml", '<root level="WARN">', ("cassandra_log_level", "WARN")),
    ("logback.xml", '<logger name="com.example" level="WARN"/>', (None, None)),
])
def test_the_variable_that_carries_a_hand_edit(name, line, carried):
    assert _carry(name, line) == carried


def test_one_group_already():
    nodes = [fake_node(i) for i in range(3)]
    for n in nodes:
        n["vars"].update(cassandra_extra_settings={"streaming_connections_per_host": 2},
                         cassandra_config_file_permissions={"cassandra.yaml": "0640"}, cassandra_num_tokens=16)
        n["vars"]["cassandra_linux_limits"] = {"nofile": 100000}
    lines = cassandra_import_convergence(cassandra_inventory_layout(nodes, "Small"), {}, screen=True, width=200)
    assert lines == ["CONVERGENCE  10/10 settings common to all nodes — one group, every setting in"
                     " group_vars/small/"]


def test_no_node_read():
    nodes = [dict(fake_node(0), read=False, vars={}, hand_edits=[], keep={})]
    assert cassandra_import_convergence(cassandra_inventory_layout(nodes, "Small"), {}) == []


def test_commands_for_a_shared_inventory_dir():
    text = "\n".join(section(inventory="/p/inventories", hosts="big"))
    assert ("ansible-playbook -i inventories community.cassandra.apply_config -e cassandra_hosts=big --limit cass007"
            " --check --diff") in text
    assert "community.cassandra.change_seeds" not in text  # (the seeds are common)


def test_the_seeds_by_change_seeds():
    nodes = [fake_node(i) for i in range(4)]
    nodes[3]["vars"]["cassandra_seeds"] = ["cass001"]
    small = cassandra_inventory_layout(nodes, "Small")
    text = "\n".join(cassandra_import_convergence(small, {}, width=200))
    assert "1 node changes, no restart (change_seeds applies it live); check:" \
        " ansible-playbook community.cassandra.change_seeds --check" in text
    screen = cassandra_import_convergence(small, {}, screen=True, width=200)
    assert ("  Check each one before it is applied: ansible-playbook community.cassandra.apply_config --check --diff,"
            " or ansible-playbook community.cassandra.change_seeds --check (the seeds)") in screen


def _small(nodes):
    return cassandra_inventory_layout(nodes, "Small")


def test_never_remove_from_a_file_of_yours():
    # group_vars/all: every cluster of the inventory dir reads it; group_vars/<cluster>/ wins over it anyway
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    nodes = [fake_node(i) for i in range(6)]
    for n in nodes[:2]:
        n["vars"]["cassandra_concurrent_reads"] = 32
    files = [{"path": "group_vars/all/tuning.yml", "content": "cassandra_concurrent_reads: 32\n"}]
    small = cassandra_inventory_layout_over(nodes, "Small", files)
    text = " ".join("\n".join(cassandra_import_convergence(small, {}, width=200)).split())
    assert "cassandra_concurrent_reads: 64 in group_vars/small/main.yml" in text
    assert "tuning.yml" not in text.split("cassandra_concurrent_reads: 64 in group_vars/small/main.yml")[1].split(";")[0]


def _flat(lines):
    return " ".join("\n".join(lines).split())


def test_a_file_of_yours_below_the_cluster_named_one_above_never():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    nodes = [fake_node(i) for i in range(6)]
    nodes[0]["vars"]["cassandra_concurrent_reads"] = 32
    files = [{"path": "host_vars/cass001/tuning.yml", "content": "cassandra_concurrent_reads: 32\n"}]
    text = _flat(cassandra_import_convergence(cassandra_inventory_layout_over(nodes, "Small", files), {}, width=200))
    assert "cassandra_concurrent_reads: 64 in group_vars/small/main.yml, removed from" in text
    assert "yours: host_vars/cass001/tuning.yml" in text
    # a sub-key a shared file of yours gives: the whole dict in the cluster's group, never that file
    nodes = [fake_node(i) for i in range(6)]
    for n in nodes[:2]:  # (as yours has it: left to it, not written again)
        n["vars"]["cassandra_extra_settings"] = {"streaming_connections_per_host": 1,
                                                 "sstable_preemptive_open_interval": "50MiB"}
    files = [{"path": "group_vars/all/tuning.yml", "content": "cassandra_extra_settings:\n"
              "  streaming_connections_per_host: 1\n  sstable_preemptive_open_interval: 50MiB\n"}]
    text = _flat(cassandra_import_convergence(cassandra_inventory_layout_over(nodes, "Small", files), {}, width=200))
    assert "in yours: group_vars/all" not in text
    assert "the whole cassandra_extra_settings in group_vars/small/main.yml, its other keys too" in text
    assert "on yours: group_vars/all/tuning.yml, which every cluster of the inventory reads" in text


def test_a_file_of_yours_the_import_wrote_over_named():
    # group_vars/small_dc1/a.yml sets 128, which the import overrides for every dc1 node: once their values are
    # removed it would win, so it is named too, and the check reaches every node (no --limit)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
    nodes = [fake_node(i) for i in range(40)]
    for n in nodes[:2]:
        n["vars"]["cassandra_concurrent_reads"] = 32
    files = [{"path": "group_vars/small_dc1/a.yml", "content": "cassandra_concurrent_reads: 128\n"}]
    text = _flat(cassandra_import_convergence(cassandra_inventory_layout_over(nodes, "Small", files), {}, width=250))
    assert "yours: group_vars/small_dc1/a.yml" in text
    assert "--limit cass001,cass002" not in text


def test_a_file_kept_by_hand_with_values_per_variant_stays_per_node():
    nodes = [fake_node(i) for i in range(4)]
    for n in nodes:
        n["hand_edits"] = ["jvm11-server.options, line 9:",
                           "  + -Xmx%s" % ("8G" if n["name"] in ("cass001", "cass002") else "16G")]
        n["keep"] = {"cassandra_config_keep_files": ["jvm11-server.options"]}
    text = _flat(cassandra_import_convergence(_small(nodes), {}, width=250))
    assert "set cassandra_heap_size in host_vars of 4 nodes" in text and "group_vars/small/;" not in text


def test_a_pre_release_is_not_the_newest():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_convergence import _version
    assert sorted(["4.0~beta1", "4.0", "4.1-rc2", "4.0.13", "(collection's default)"], key=_version) == [
        "(collection's default)", "4.0~beta1", "4.0", "4.0.13", "4.1-rc2"]
    assert sorted(["4.0~rc1", "4.0~beta2", "4.0~beta1", "1:4.0.11-2", "4.0.11-1"], key=_version) == [
        "4.0~beta1", "4.0~beta2", "4.0~rc1", "4.0.11-1", "1:4.0.11-2"]


def test_the_newest_version_never_down():
    nodes = [fake_node(i) for i in range(5)]
    for n in nodes:
        n["vars"]["cassandra_package_version"] = "4.1.10" if n["name"] in ("cass001", "cass002") else "4.1.5"
    text = _flat(cassandra_import_convergence(_small(nodes), {}, width=200))
    assert "\u2192 4.1.10 for all:" in text and "3 nodes change, an upgrade (upgrade, update_java), never down" in text


def test_the_dc_of_rackdc_not_counted_and_the_unmanaged_os_said():
    nodes = [fake_node(i) for i in range(4)]
    for n in nodes:
        n["vars"]["cassandra_rackdc_dc"] = "east" if n["name"] in ("cass001", "cass002") else "west"
    nodes[3]["vars"]["cassandra_linux_limits"] = {"nofile": 1}
    nodes[3]["keep"] = {"cassandra_linux_manage": False}
    text = _flat(cassandra_import_convergence(_small(nodes), {}, width=200))
    assert "cassandra_rackdc_dc" in text.split("SETTINGS THAT DIFFER", maxsplit=1)[0]  # (in "Not counted")
    assert "not on cass004 (cassandra_linux_manage: false: left as it is)" in text


def test_a_failed_self_check_first():
    nodes = [fake_node(i) for i in range(3)]
    for n in nodes:
        n["vars"].update(cassandra_extra_settings={"streaming_connections_per_host": 2},
                         cassandra_config_file_permissions={"cassandra.yaml": "0640"}, cassandra_num_tokens=16)
        n["vars"]["cassandra_linux_limits"] = {"nofile": 100000}
    small = _small(nodes)
    screen = cassandra_import_convergence(small, {}, screen=True, width=200, self_check_ok=False)
    assert screen == ["CONVERGENCE  10/10 settings common to all nodes",
                      "  The self-check first: the inventory does not reproduce the nodes yet"]
    report = cassandra_import_convergence(small, {}, width=200, self_check_ok=False)
    assert "one group" not in report[1] and report[2].startswith("  The self-check first (SELF-CHECK above)")


def test_a_rack_kept_by_hand_stays_the_node_s():
    nodes = [fake_node(i) for i in range(3)]
    nodes[0]["hand_edits"] = ["cassandra-rackdc.properties, line 3:", "  - rack=r1", "  + rack=rack-a"]
    nodes[0]["keep"] = {"cassandra_config_keep_files": ["cassandra-rackdc.properties"]}
    text = " ".join("\n".join(cassandra_import_convergence(_small(nodes), {}, width=200)).split())
    assert "set cassandra_rackdc_rack in host_vars/cass001/;" in text and "group_vars/small/ instead" not in text


def test_a_dict_empty_on_a_node_is_not_removed():
    nodes = [fake_node(i) for i in range(4)]
    nodes[0]["vars"]["cassandra_server_encryption"] = {}
    for n in nodes[1:]:
        n["vars"]["cassandra_server_encryption"] = {"keystore_password": "hunter2"}
    text = "\n".join(cassandra_import_convergence(_small(nodes), {}, width=200))
    assert "remove cassandra_server_encryption from" not in text and "hunter2" not in text
    assert "cassandra_server_encryption.keystore_password" in text


def test_a_value_on_lines_of_its_own_stays_on_its_line():
    nodes = [fake_node(i) for i in range(3)]
    nodes[0]["vars"]["cassandra_extra_settings"] = {"streaming_connections_per_host": "1\nSELF-CHECK FAILED  x"}
    for line in cassandra_import_convergence(_small(nodes), {}, width=200):
        assert not line.startswith("SELF-CHECK"), line


def test_a_tie_said():
    nodes = [fake_node(i) for i in range(4)]
    for n in nodes[:2]:
        n["vars"]["cassandra_concurrent_reads"] = 32
    text = "\n".join(cassandra_import_convergence(_small(nodes), {}, width=200))
    assert "\u2192 32 for all (a tie: or another of the values): cassandra_concurrent_reads: 32" in text


def test_a_yaml_block_secret_in_a_kept_edit_masked():
    nodes = [fake_node(i) for i in range(3)]
    nodes[0]["hand_edits"] = ["cassandra.yaml, line 12:", "  + keystore_password: |", "  +   abc444"]
    nodes[0]["keep"] = {"cassandra_config_keep_files": ["cassandra.yaml"]}
    text = "\n".join(cassandra_import_convergence(_small(nodes), {}, width=200))
    assert "abc444" not in text and "keystore_password: ****" in text


def test_an_undefined_value_is_raised(monkeypatch):
    from ansible.errors import AnsibleUndefinedVariable

    def undefined(*args):
        raise AnsibleUndefinedVariable("'cluster_group' is undefined")
    monkeypatch.setattr(convergence, "_sections", undefined)
    with pytest.raises(AnsibleUndefinedVariable):
        section()


def test_an_error_of_its_own_is_a_line(monkeypatch):
    def broken(*args):
        raise KeyError("hunter2")
    monkeypatch.setattr(convergence, "_sections", broken)
    lines = section(screen=True)
    assert lines == ["CONVERGENCE  not worked out (KeyError in cassandra_import_convergence; the inventory and the"
                     " rest of the report are not affected)"]


def test_in_the_report_and_on_the_screen():
    big = layout()
    files = [f["path"] for f in cassandra_inventory_files(big)]
    changes = {"changed": ["big.yml"] + files, "removed": [], "existed": []}
    common = dict(cwd="/p", in_git=True, changes=changes)
    screen = cassandra_import_report(big, ["/p/inventories/big.yml", "/p/inventories"], REPORT, {}, True, screen=True,
                                     width=120, convergence=section(screen=True, width=120), **common)
    at = next(i for i, line in enumerate(screen) if line.startswith("CONVERGENCE  "))
    assert screen[at - 1] == "" and screen[-1] == "full report: reports/big/report.txt" and screen[-2] == ""
    report = cassandra_import_report(big, ["/p/inventories/big.yml", "/p/inventories"], REPORT, {}, True,
                                     convergence=section(), **common)
    at = report.index("CONVERGENCE — towards one group, every setting in group_vars/big/")
    assert report.index("NOT READ — set in group_vars/all if they apply") > at
    # without it: as before
    assert not [line for line in cassandra_import_report(big, ["/p/inventories/big.yml", "/p/inventories"], REPORT,
                                                         {}, True, **common) if "CONVERGENCE" in line]
