from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import difflib
import re
import time

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

GIB = 1024 ** 3
P = out.PARAGRAPH  # a blank line between two paragraphs of a block
MIB = 1024 ** 2
ARROW = u"←"


# --- sizes, rates, durations ---

@pytest.mark.parametrize("count, scale, text", [
    (0, None, "0 B"),
    (512, None, "512 B"),
    (1023, None, "1023 B"),
    (1024, None, "1.0 KiB"),
    (2048, None, "2.0 KiB"),
    (1023.97 * 1024, None, "1.0 MiB"),  # not 1024.0 KiB
    (260.6 * MIB, None, "260.6 MiB"),  # alone: the unit it reads >= 1 in
    (1000 * MIB, None, "1000.0 MiB"),
    (1024 * MIB, None, "1.0 GiB"),
    (41.2 * GIB, None, "41.2 GiB"),
    (1023.99 * GIB, None, "1.0 TiB"),
    (5 * 1024 * GIB, None, "5.0 TiB"),
    (0, 100 * GIB, "0.0 GiB"),
    (0.3 * GIB, 100 * GIB, "0.3 GiB"),  # the unit of the scale
    (52 * 1024, 100 * GIB, "<0.1 GiB"),  # never 0.0 for a non-zero size
    (5 * GIB, 0, "5.0 GiB"),  # no scale: its own unit
    (-5, None, "0 B"),
])
def test_size(count, scale, text):
    assert out.size(count, scale) == text


@pytest.mark.parametrize("done, total, text", [
    (305.1 * MIB, 329.8 * MIB, "305.1/329.8 MiB"),  # was "0.3 GiB/329.8 MiB": the unit total reads naturally in
    (200 * MIB, 200 * MIB, "200.0/200.0 MiB"),  # done = total
    (0, 329.8 * MIB, "0.0/329.8 MiB"),  # done = 0
    (0, 512, "0/512 B"),
    (300, 512, "300/512 B"),
    (512, 900 * 1024, "0.5/900.0 KiB"),
    (52.2 * GIB, 100 * GIB, "52.2/100.0 GiB"),
    (52 * 1024, 100 * GIB, "<0.1/100.0 GiB"),
    (900 * GIB, 1000 * GIB, "900.0/1000.0 GiB"),  # under 1 TiB: GiB
    (0.5 * 1024 * GIB, 2.5 * 1024 * GIB, "0.5/2.5 TiB"),
    (1023.96 * MIB, 1023.98 * MIB, "1.0/1.0 GiB"),  # 1024.0 MiB reads 1.0 GiB, done in that unit too
    (329.79 * MIB, 329.8 * MIB, "329.8/329.8 MiB"),
    (340 * MIB, 329.8 * MIB, "329.8/329.8 MiB"),  # netstats counted more than the total: never shown above it
    (10, 0, "0/0 B"),
    (-1, 100, "0/100 B"),
])
def test_amount(done, total, text):
    assert out.amount(done, total) == text


def test_amount_never_shows_done_above_total():
    for total in [1, 999, 1024, 1500, 329.8 * MIB, 1023.99 * MIB, 99.95 * GIB, 3 * 1024 * GIB]:
        for done in [0, 1, total * 0.5, total * 0.999, total - 1, total, total + 1, total * 1.5]:
            done_text, total_text = out.amount(done, total).split()[0].split("/")
            assert done_text == "<0.1" or float(done_text) <= float(total_text), (done, total)
            assert out.amount(done, total).split()[1] == out.size(total).split()[1]  # the unit of the total alone


def test_sizes_and_parse_size():
    assert out.sizes(40.1 * GIB, 98 * GIB) == ("40.1 GiB", "98.0 GiB")
    assert out.sizes(305.1 * MIB, 329.8 * MIB) == ("305.1 MiB", "329.8 MiB")
    assert out.sizes(700 * MIB, 2 * 1024 * GIB) == ("<0.1 TiB", "2.0 TiB")
    assert out.amount(1 * GIB, 2 * GIB, " / ") == "1.0 / 2.0 GiB"
    assert out.parse_size("94.39 MiB") == 94.39 * MIB
    assert out.parse_size("1,5 GiB") == 1.5 * GIB
    assert out.parse_size("100 bytes") == 100
    assert out.parse_size("3 KB") == 3072
    assert out.parse_size("?") is None and out.parse_size(None) is None


def test_rate_duration_count_plural():
    assert out.rate(89 * MIB) == "89 MiB/s"
    assert out.rate(3.2 * MIB) == "3.2 MiB/s"
    assert out.rate(1.1 * GIB) == "1.1 GiB/s"
    assert out.rate(2048) == "2 KiB/s" and out.rate(10) == "10 B/s"
    assert [out.duration(s) for s in (45, 560, 690, 3 * 3600 + 60, 2 * 86400 + 4 * 3600)] == \
        ["45s", "9m20s", "11m30s", "3h01m", "2d04h"]
    assert out.duration(560, short=True) == "9m" and out.duration(-5) == "0s"
    assert out.count(1240) == "1 240"
    assert out.plural(1, "node") == "1 node" and out.plural(3, "node") == "3 nodes"
    assert out.plural(2, "copy", "copies") == "2 copies"


def test_clock_today_and_another_day():
    now = time.mktime((2026, 10, 8, 12, 0, 0, 0, 0, -1))
    assert out.clock(now + 3600, now).startswith("13:00 ")
    assert out.clock(now + 2 * 86400, now).startswith("2026-10-10 12:00 ")


# --- node lists (Q3) ---

@pytest.mark.parametrize("names, text", [
    ([], ""),
    (["node1"], "node1"),
    (["node1", "node2"], "node1, node2"),  # 2 listed
    (["node1", "node2", "node3"], "node1..node3"),  # a range from 3
    (["node3", "node1", "node2", "node5"], "node1..node3, node5"),  # sorted, a gap breaks the range
    (["node1", "node2", "node4", "node5"], "node1, node2, node4, node5"),  # never node1..node5
    (["node1", "node2", "node3", "node5", "node7", "node8"], "6 nodes: node1..node3, node5, node7, node8"),
    (["node%d" % i for i in range(1, 6)], "node1..node5"),  # 5: no count
    (["node9", "node10", "node11"], "node9..node11"),
    (["node09", "node10", "node11"], "node09..node11"),
    (["node008", "node009", "node010"], "node008..node010"),
    (["n1.dc1", "n2.dc1", "n3.dc1"], "n1.dc1..n3.dc1"),
    (["dc1-n1", "dc1-n2", "dc2-n3"], "dc1-n1, dc1-n2, dc2-n3"),  # another number changes
    (["a", "b", "c"], "a, b, c"),
    (["node1", "node1", "node2"], "node1, node2"),
])
def test_nodes(names, text):
    assert out.nodes(names) == text


def test_nodes_order_and_full():
    assert out.nodes(["node3", "node1", "node2"], keep_order=True) == "node3, node1, node2"
    assert out.nodes(["node4", "node5", "node6", "node1"], keep_order=True) == "node4..node6, node1"
    names = ["node%d" % i for i in range(1, 8)]
    assert out.nodes(names, full=True) == ", ".join(names)
    assert out.full_list(["node10", "node2"]) == "node2, node10"


# --- secrets ---

def test_mask_secret_shown():
    assert out.mask("keystore_password: changeit") == "keystore_password: ****"
    assert out.mask("-Djavax.net.ssl.keyStorePassword=abc -Xmx8G") == "-Djavax.net.ssl.keyStorePassword=****"
    assert out.mask("concurrent_reads: 32") == "concurrent_reads: 32"
    assert out.secret("cassandra_jmx_password", "x") and not out.secret("cassandra_jmx_password_file", "/etc/x")
    assert out.secret("opts", "-Dtruststore_password=y") and not out.secret("cassandra_jmx_password", "")
    assert out.secret("server_encryption_options", {"keystore_password": "x"})
    assert out.shown("keystore_password", "x") == "****"


SECRETS = [  # (key, value): each one hidden on screen and filed in secrets.yml
    ("cassandra_jmx_password", "x"), ("ldap_auth_token", "t"), ("cassandra_repository_pw", "p"),
    ("svc_pass", "p"), ("pw", "p"), ("pass", "p"), ("internode_ca_key", "k"), ("jmx_kspw", "k"), ("jmx_tspw", "k"),
    ("cassandra_medusa_s3_access_key_id", "AKIA"), ("tde", {"key_material": "m"}), ("opts", "-Dkeystore_pw=k"),
    ("opts", "nodetool -u u -pw secret1 status"), ("cmd", "cqlsh -u u -p secret2 host"),
    ("settings", {"auth": {"ca_key": "k"}}), ("settings", "{\"password\": \"x\"}"), ("tokens", ["-Dauth_token=x"]),
    ("cassandra_cql_password", 1234)]
NOT_SECRETS = [("cassandra_jmx_password_file", "/etc/x"), ("cassandra_jmx_password", ""),
               ("cassandra_jmx_password", None), ("concurrent_reads", 32), ("opts", "-Xmx8G -p 9042"),
               ("compass", "x"), ("bypass", "x"), ("passive_mode", "x"), ("settings", {"keystore": "conf/.keystore"}),
               ("opts", "-Dcom.sun.management.jmxremote.password.file=/etc/cassandra/conf/jmx/my_jmx.password")]


def test_one_rule_for_secrets():
    # what the output hides is what import_cluster files in secrets.yml: nothing hidden on screen in clear there
    for key, value in SECRETS:
        assert out.hidden(key, value) and out.secret(key, value), (key, value)
        assert out.shown(key, value) == "****"
    for key, value in NOT_SECRETS:
        assert not out.hidden(key, value) and not out.secret(key, value), (key, value)
    assert out.secret is out.hidden
    assert [out.shown("k", v) for v in ("text", 32, True, None, ["a"], {"b": 1})] == \
        ["text", "32", "true", "null", '["a"]', '{"b": 1}']


# --- a setting and its value per node ---

def test_setting_lines_all_and_differs():
    assert out.setting_lines("concurrent_reads", {"node1": 32, "node2": 32}) == ["concurrent_reads: 32   all"]
    lines = out.setting_lines("concurrent_reads", {"node1": 32, "node2": 32, "node3": 64, "node4": 32})
    assert lines == ["concurrent_reads: 32   node1, node2, node4",
                     "                  64   node3                 %s differs" % ARROW]


def test_setting_lines_not_all_when_nodes_missing():
    # node3 of the run has no value: not "all"
    assert out.setting_lines("x", {"node1": 1, "node2": 1}, all_nodes=["node1", "node2", "node3"]) == \
        ["x: 1   node1, node2"]


def test_setting_lines_expected_notes_where_and_secrets():
    lines = out.setting_lines("heap", {"n1": "8G", "n2": "8G", "n3": "16G"}, expected="16G",
                              where={"8G": "host_vars"}, notes={"16G": "set by hand"})
    assert lines == ["heap: 8G    n1, n2   in host_vars  %s differs" % ARROW,
                     "      16G   n3       (collection value)  (set by hand)"]
    assert out.setting_lines("heap", {"n1": "8G"}, where="group_vars/all.yml") == ["heap: 8G   all   in group_vars/all.yml"]
    lines = out.setting_lines("keystore_password", {"n1": "s3cret1", "n2": "s3cret2"})
    assert "s3cret" not in " ".join(lines) and lines[0].startswith("keystore_password: ****   n1")
    assert out.setting_lines("x", {}) == []


def test_settings_lines_one_column_each_across_settings():
    # the names, values and nodes aligned across the settings, a heading between them, a long name on its own line
    entries = ["cassandra.yaml",
               out.setting_rows("max_hint_window", {"n1": "3h", "n2": "3h", "n3": "6h"}),
               "jvm-server.options",
               ("-Ddemo.drift=1", [("present", "n2", ARROW + " differs (not in the inventory)")]),
               ("client_encryption_options.keystore_password", [("****", "n1, n2", "")])]
    assert out.settings_lines(entries) == [
        "cassandra.yaml",
        "  max_hint_window: 3h        n1, n2",
        "                   6h        n3       %s differs" % ARROW,
        "jvm-server.options",
        "  -Ddemo.drift=1:  present   n2       %s differs (not in the inventory)" % ARROW,
        "  client_encryption_options.keystore_password:",
        "                   ****      n1, n2"]
    # a value longer than the column pushes its nodes only
    assert out.settings_lines([("a", [("x" * 35, "n1", "")]), ("b", [("y", "n2", "")])], max_value=10) == [
        "  a: %s  n1" % ("x" * 35), "  b: y          n2"]
    assert out.settings_lines(["only a heading"]) == ["only a heading"] and out.settings_lines([]) == []
    # a long node list pushes its own note only
    many = ", ".join("node%d" % i for i in range(1, 40, 2))
    assert out.settings_lines([("x", [("1", many, "(inventory)"), ("2", "node2", ARROW + " differs")])]) == [
        "  x: 1   %s   (inventory)" % many, "     2   node2                            %s differs" % ARROW]


def test_by_nodes():
    lines = out.by_nodes([("node1", ["a", "b"]), ("node2", ["a", "c"]), ("node3", ["a", "c"])])
    assert lines == ["all", "  a", "node1", "  b", "node2, node3", "  c"]


# --- plan ---

def test_plan_order_warnings_above_the_question():
    lines = out.plan("decommission_node", "my_cluster", "2 nodes, one at a time",
                     steps=[{"node": "node7", "dc": "dc1", "rack": "rack_a", "text": "load 47.1 GiB -> node1,node4"},
                            {"node": "node8", "dc": "dc1", "rack": "rack_b", "text": "load 48.1 GiB"}],
                     facts=[["dc1 after", "6 nodes"], ["ring now", "8 UN"], "a plain fact"],
                     warnings=["not inside tmux/screen"], question="Decommission node7, node8? (yes/no)",
                     version="4.1.5")
    assert lines == [
        "PLAN  decommission_node  my_cluster (Cassandra 4.1.5)  2 nodes, one at a time",
        "  1.  node7  dc1/rack_a  load 47.1 GiB -> node1,node4",
        "  2.  node8  dc1/rack_b  load 48.1 GiB",
        "",  # a rule under the ops callback, PARAGRAPH a blank line (each fact a paragraph)
        "dc1 after:  6 nodes",
        P,
        "ring now:   8 UN",
        P,
        "a plain fact",
        "",
        "WARNING - not inside tmux/screen",
        "",
        "Decommission node7, node8? (yes/no)",
    ]


def test_plan_check_and_refused():
    lines = out.plan("cleanup", "c1", steps=["node1"], question="Go?", check=True)
    assert lines == ["PLAN  cleanup  c1", "  1.  node1", "", "--check: nothing will be changed"]
    assert out.plan("decommission_node", "c1", verdict="REFUSED", facts=["dc1 after: 2 nodes"]) == \
        ["REFUSED  decommission_node  c1", "", "dc1 after: 2 nodes"]
    # an empty block: no blank line for it, never two in a row
    assert out.plan("cleanup", "c1", steps=["node1"], warnings=["w", " "], question="Go?") == \
        ["PLAN  cleanup  c1", "  1.  node1", "", "WARNING - w", "", "Go?"]
    assert out.plan("cleanup", "c1", facts=["f"], warnings=["w"], check=True) == \
        ["PLAN  cleanup  c1", "", "f", "", "WARNING - w", "", "--check: nothing will be changed"]


# --- the prepare of new nodes ---

JAVA = {"managed": True, "package": "openjdk-17-jre-headless", "version": "17.0.12+7-1~deb12u1", "home": "",
        "series": "17"}
CONFIG = [{"item": "/etc/cassandra/%s" % f, "before": "current", "after": "%d line(s) changed" % n}
          for f, n in (("cassandra.yaml", 40), ("jvm-server.options", 3), ("cassandra-rackdc.properties", 2))]


def prepared(name, **kw):
    """A node as prepare_lines.yml keeps it (_cassandra_prepare_done): new, Cassandra installed now."""
    node = {"name": name, "ran": ["cassandra_install", "cassandra_linux", "cassandra_config"], "repository": "ok",
            "packages": [{"item": "package cassandra", "before": "absent", "after": "5.0.9"}],
            "cassandra": "5.0.9", "java": JAVA, "os": 2,
            "disks": {"name": name, "kb": "4", "changed": False, "brief": ["sdb (ssd): read-ahead 4 KB"], "warnings": []},
            "config": CONFIG, "firewall": "off", "medusa": "off", "seeds": "10.0.0.1,10.0.0.2"}
    node.update(kw)
    return node


def test_prepare_lines_one_line_per_step_nodes_grouped():
    assert out.prepare_lines([prepared("node4"), prepared("node5", packages=[], os=0)]) == [
        "node4, node5  repository  ok (already set)",
        "node4         install     cassandra 5.0.9 (installed now)",
        "node5         install     cassandra 5.0.9 (already installed)",
        "node4, node5  java        17.0.12 (already installed)",
        "node4         os tuning   2 changes",
        "node5         os tuning   ok (already set)",
        "node4, node5  data disks  read-ahead 4 KB already set",
        "node4, node5  config      3 files written",
        "node4, node5  firewall    off (cassandra_manage_firewall: false)",
        "node4, node5  medusa      off (cassandra_medusa_enabled: false)",
        "node4, node5  seeds       joins with 10.0.0.1, 10.0.0.2"]


def test_prepare_lines_check_mode_says_would():
    lines = out.prepare_lines([prepared("node4", repository="set", java=dict(JAVA, version=""),
                                        packages=[{"item": "package cassandra", "before": "absent", "after": "5.0.9"},
                                                  {"item": "package openjdk-17-jre-headless", "before": "absent",
                                                   "after": "17.0.13+11-1~deb12u1"}],
                                        disks=dict(prepared("node4")["disks"], changed=True),
                                        firewall=[{"item": "port 7000", "before": "closed", "after": "open"}],
                                        medusa=[{"item": "Medusa", "before": "absent", "after": "0.24.1"}])],
                              check=True)
    assert lines == [
        "node4  repository  would be set",
        "node4  install     cassandra 5.0.9 (would be installed)",
        "node4  java        17.0.13 (would be installed)",
        "node4  os tuning   would make 2 changes",
        "node4  data disks  read-ahead 4 KB would be set",
        "node4  config      would write 3 files",
        "node4  firewall    would make 1 change",
        "node4  medusa      would install Medusa 0.24.1",
        "node4  seeds       would join with 10.0.0.1, 10.0.0.2"]
    assert not [line for line in lines if "now" in line or "written" in line]


def test_prepare_lines_steps_off_or_not_managed():
    node = prepared("node7", repository="off", java={"managed": False}, disks={}, config=[], firewall="not managed",
                    ran=["cassandra_install", "cassandra_config", "cassandra_medusa"],
                    medusa=[{"item": "Medusa", "before": "absent", "after": "0.24.1"},
                            {"item": "/etc/profile.d/cassandra-medusa.sh", "before": "absent", "after": "PATH"}],
                    seeds="", packages=[{"item": "package cassandra", "before": "absent", "after": "5.0.9"},
                                        {"item": "package libjemalloc2", "before": "absent", "after": "5.3.0-1"}])
    assert out.prepare_lines([node]) == [
        "node7  repository  not managed (cassandra_repository_manage: false)",
        "node7  install     cassandra 5.0.9 (installed now; also libjemalloc2)",
        "node7  java        not managed (cassandra_install_java: false)",
        "node7  os tuning   not managed (cassandra_linux_manage: false)",  # no data disks line then
        "node7  config      ok (unchanged)",
        "node7  firewall    not managed (cassandra_firewall_manage: false)",
        "node7  medusa      Medusa 0.24.1 installed now (+1 change)"]  # no seeds: create_cluster's


def test_prepare_lines_other_outcomes():
    node = prepared("node8", repository="packages", java=dict(JAVA, home="/opt/java/jdk-17.0.12", version="17.0.12"),
                    disks={"name": "node8", "kb": "", "changed": False, "brief": [], "warnings": ["sdb: no scheduler"]},
                    config=[{"item": "config files", "before": "Cassandra not installed yet", "after": "8 files templated"}],
                    cassandra="", packages=[{"item": "package cassandra", "before": "absent",
                                             "after": "not in configured repos yet"},
                                            {"item": "package libjemalloc2", "before": "absent", "after": "5.3.0-1"}])
    assert out.prepare_lines([node], check=True)[:6] == [
        "node8  repository  none (cassandra_install_method: packages)",
        "node8  install     cassandra (would be installed: not in configured repos yet; also libjemalloc2)",
        "node8  java        17.0.12 from /opt/java/jdk-17.0.12",
        "node8  os tuning   would make 2 changes",
        "node8  data disks  nothing to tune (1 warning below)",
        "node8  config      would write the files (cassandra not installed yet)"]
    perms = [{"item": "/etc/cassandra/cassandra.yaml owner", "before": "root", "after": "cassandra"}]
    assert "node8  config      2 files written, 1 other change" in out.prepare_lines(
        [prepared("node8", config=CONFIG[:2] + perms)])
    assert "node8  config      1 other change" in out.prepare_lines([prepared("node8", config=perms)])


def test_prepare_lines_a_pinned_java_installed_now():
    node = prepared("node4", packages=[{"item": "package cassandra", "before": "absent", "after": "5.0.9"},
                                       {"item": "package openjdk-17-jre-headless=17.0.12*", "before": "absent",
                                        "after": "not in configured repos yet"}])  # what the role looks up as is
    lines = out.prepare_lines([node])
    assert "node4  install     cassandra 5.0.9 (installed now)" in lines  # not "also openjdk-..."
    assert "node4  java        17.0.12 (installed now)" in lines


def test_prepare_lines_disks_not_tuned_yet_point_below():
    node = prepared("node4", disks=dict(prepared("node4")["disks"], info=["sdc: not tuned yet (LVM deferred)"]))
    assert "node4  data disks  read-ahead 4 KB already set (see below)" in out.prepare_lines([node])
    node = prepared("node4", disks=dict(prepared("node4")["disks"], changed="on"))  # a flag as Ansible may give it
    assert "node4  data disks  read-ahead 4 KB set now" in out.prepare_lines([node])


def test_prepare_lines_many_nodes_ranges_and_nothing():
    lines = out.prepare_lines([prepared("node%d" % i) for i in range(1, 8)])
    assert lines[0] == "node1..node7  repository  ok (already set)"  # no "7 nodes: " on every line
    assert out.prepare_lines([]) == [] and out.prepare_lines(None) == []
    assert out.prepare_lines([{"name": "node1"}]) == []  # a node that ran no step


def test_left_system_warning_once_with_its_nodes():
    hosts = [{"name": n, "left": ["/var/lib/cassandra/data"], "dirs": ["/data/cassandra"]} for n in ("node4", "node5")]
    lines = out.left_system_warning(hosts + [{"name": "node6", "left": [], "dirs": ["/data/cassandra"]}])
    assert max(len(line) for line in lines[1:]) <= 110  # wrapped, under the WARNING line (its colour)
    assert all(line.startswith("  ") for line in lines[1:])
    text = " ".join(line.strip() for line in lines[1:])
    assert lines[0] == ("WARNING - node4, node5: an old system keyspace in /var/lib/cassandra/data, not one of the data"
                        " directories the config uses (/data/cassandra)")
    assert text.startswith("Cassandra does not read it: the node starts empty")  # why: ignored for the run
    # the way out: its keyspace directories, not the whole directory; never another instance's
    assert "if no other Cassandra on node4, node5 uses it: delete by hand the keyspace directories in" in text
    assert "reset_node does not" in text and "keeping" not in text
    assert "already initialized" not in " ".join(lines)
    # a data directory of the config under it: kept
    inside = out.left_system_warning([{"name": "node4", "left": ["/var/lib/cassandra/data"],
                                       "dirs": ["/var/lib/cassandra/data/d1"]}])
    assert "keeping /var/lib/cassandra/data/d1." in " ".join(line.strip() for line in inside)  # what the hostile suite checks an add never says
    assert out.left_system_warning([{"name": "node6", "left": []}]) == []


# --- progress (Q2) ---

NOW = time.mktime((2026, 10, 8, 13, 24, 0, 0, 0, -1))


def test_progress_line_going_with_peers_always():
    lines = out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=52.2 * GIB, total=100 * GIB,
                              speed=89 * MIB, now=NOW, start=NOW - 600,
                              peers=[{"name": "node1", "way": "from", "done": 18 * GIB, "total": 34 * GIB},
                                     {"name": "node3", "way": "from", "done": 4.2 * GIB, "total": 17 * GIB,
                                      "stalled": True},
                                     {"name": "node2", "way": "from", "done": 3 * GIB, "total": 3 * GIB}])
    eta = time.strftime("%H:%M", time.localtime(NOW + 47.8 * GIB / (89 * MIB)))
    assert lines == [
        "[1/2] node5 bootstrap  JOINING  [#####-----]  52%%  52.2/100.0 GiB  89 MiB/s  ETA %s (9m)  10m" % eta,
        "      from node1  18.0/34.0 GiB   52%           from node2    3.0/3.0 GiB  100%",
        "      from node3   4.2/17.0 GiB   24% STALLED"]


def _peers(count, way="to", stalled=(), slow=()):
    """count peers of 7.2 GiB each, 30% to 59% done, the stalled ones at 0, the slow ones at 12%."""
    peers = []
    for i in range(1, count + 1):
        part = 0.0 if i in stalled else 0.12 if i in slow else 0.30 + (i * 7 % 30) / 100.0
        peers.append({"name": "node%d" % i, "way": way, "done": 7.2 * GIB * part, "total": 7.2 * GIB,
                      "stalled": i in stalled})
    return peers


def test_peer_lines_always_columns_aligned():
    assert out.peer_lines([]) == out.peer_lines(None) == []
    assert out.peer_lines(_peers(1)) == ["      to node1  2.7/7.2 GiB  37%"]
    assert out.peer_lines(_peers(3), width=100) == [
        "      to node1  2.7/7.2 GiB  37%   to node2  3.2/7.2 GiB  44%   to node3  3.7/7.2 GiB  51%"]
    # by name (node10 after node9), the names, amounts and percents in columns across the block
    lines = out.peer_lines(_peers(14), width=100)
    assert lines[:2] == [
        "      to node1   2.7/7.2 GiB  37%   to node2   3.2/7.2 GiB  44%   to node3   3.7/7.2 GiB  51%",
        "      to node4   4.2/7.2 GiB  58%   to node5   2.5/7.2 GiB  35%   to node6   3.0/7.2 GiB  41%"]
    assert lines[3] == "      to node10  2.9/7.2 GiB  40%   to node11  3.4/7.2 GiB  46%   to node12  3.9/7.2 GiB  54%"
    assert len(lines) == 5 and all(len(line) <= 100 for line in lines)


def test_peer_lines_fit_the_width():
    wide, narrow = out.peer_lines(_peers(14), width=200), out.peer_lines(_peers(14), width=60)
    assert len(wide) == 4 and wide[0].count(" to ") == 4  # 4 columns at most
    assert len(narrow) == 14 and all(len(line) <= 60 for line in narrow)  # one when two do not fit
    assert len(out.peer_lines(_peers(14), width=80)) == 7  # two
    # cells of 27 and gaps of 3 after the indent of 6: 3 columns from 93 on, the line ending with its last cell
    assert len(max(out.peer_lines(_peers(14)), key=len)) == 6 + 3 * 27 + 2 * 3
    assert len(out.peer_lines(_peers(14), width=93)) == 5 and len(out.peer_lines(_peers(14), width=92)) == 7
    assert max(len(line) for line in out.peer_lines(_peers(14))) <= out.PEER_WIDTH  # no width: PEER_WIDTH


def test_peer_lines_stalled_in_place_and_ways():
    lines = out.peer_lines(_peers(3, stalled=(2,)), width=100)
    assert lines == ["      to node1  2.7/7.2 GiB  37%           to node2  0.0/7.2 GiB   0% STALLED",
                     "      to node3  3.7/7.2 GiB  51%"]
    # receiving: from; both ways in one block (a move), the way column as wide as "from"
    assert out.peer_lines(_peers(2, way="from"), width=100) == [
        "      from node1  2.7/7.2 GiB  37%   from node2  3.2/7.2 GiB  44%"]
    both = out.peer_lines([dict(p, way="to") for p in _peers(1)] + [dict(p, way="from") for p in _peers(1)])
    assert both == ["      from node1  2.7/7.2 GiB  37%   to   node1  2.7/7.2 GiB  37%"]


def test_peer_lines_above_30_summed_up():
    # 30: every one detailed; 31: one line sums them up
    assert len(out.peer_lines(_peers(30), width=100)) == 10
    assert out.peer_lines(_peers(31), width=100) == [
        "      31 peers: 31 ok (avg 44%); slowest: node30 30%, node13 31%, node26 32%"]
    lines = out.peer_lines(_peers(90, stalled=(5,), slow=(31, 44)), width=110)
    assert lines == [
        "      90 peers: 87 ok (avg 44%), 1 STALLED, 2 behind; slowest: node5 0% STALLED, node31 12%, node44 12%",
        "      to node5   0.0/7.2 GiB   0% STALLED   to node31  0.9/7.2 GiB  12%",
        "      to node44  0.9/7.2 GiB  12%"]
    done = [dict(p, done=p["total"]) for p in _peers(40)]
    assert out.peer_lines(done) == ["      40 peers: 40 done"]
    done[0]["done"] = 0  # the slowest: the ones not done only
    assert out.peer_lines(done) == ["      40 peers: 39 done, 1 ok (avg 0%); slowest: node1 0%"]
    stuck = [dict(p, stalled=True, done=0) for p in _peers(31)]
    assert out.peer_lines(stuck, width=100)[0] == \
        "      31 peers: 31 STALLED; slowest: node1 0% STALLED, node2 0% STALLED, node3 0% STALLED"


def test_peer_lines_summed_up_done_apart_and_never_behind_at_the_start():
    # the ones done are not in the average: 20 at 30% next to 40 done are ok, not behind
    peers = [dict(p, done=p["total"] if i < 40 else 0.3 * p["total"]) for i, p in enumerate(_peers(60))]
    assert out.peer_lines(peers, width=100) == [
        "      60 peers: 40 done, 20 ok (avg 30%); slowest: node41 30%, node42 30%, node43 30%"]
    # the start of a stream: most at 0%, none behind before the average means something
    peers = [dict(p, done=0.5 * p["total"] if i < 2 else 0) for i, p in enumerate(_peers(100))]
    assert out.peer_lines(peers, width=100) == [
        "      100 peers: 100 ok (avg 1%); slowest: node3 0%, node4 0%, node5 0%"]
    # once it does: below half of it, behind
    peers = [dict(p, done=0.4 * p["total"] if i else 0.1 * p["total"]) for i, p in enumerate(_peers(40))]
    assert out.peer_lines(peers, width=100) == [
        "      40 peers: 39 ok (avg 40%), 1 behind; slowest: node1 10%, node2 40%, node3 40%",
        "      to node1  0.7/7.2 GiB  10%"]


def test_peer_lines_summed_up_stalled_first_and_details_capped():
    # a peer stalled at 58% is the slowest, before one at 30%
    peers = _peers(40)
    peers[3]["stalled"] = True
    lines = out.peer_lines(peers, width=100)
    assert lines[0] == "      40 peers: 39 ok (avg 44%), 1 STALLED; slowest: node4 58% STALLED, node30 30%, node13 31%"
    assert lines[1:] == ["      to node4  4.2/7.2 GiB  58% STALLED"]
    # at most PEERS_DETAILED under it, the stalled first (here further on than the slow ones), then by name;
    # the others counted
    peers = _peers(60, stalled=range(41, 61), slow=range(1, 6))
    for p in peers[40:]:
        p["done"] = 0.2 * p["total"]
    lines = out.peer_lines(peers, width=100)
    assert lines[0].startswith("      60 peers: 35 ok (avg 44%), 20 STALLED, 5 behind; slowest: node41 20% STALLED")
    detailed = [w for line in lines[1:-1] for w in line.split() if w.startswith("node")]
    assert detailed == ["node%d" % i for i in range(41, 41 + out.PEERS_DETAILED)]
    assert lines[-1] == "      +13 more: 8 STALLED, 5 behind"
    assert out.peer_lines(_peers(60, stalled=range(1, 61)), width=100)[-1] == "      +48 more: 48 STALLED"


def test_peer_lines_behind_its_bounds():
    def behind(parts):  # [percent] -> the ones behind
        peers = [dict(p, done=p["total"] * part / 100.0) for p, part in zip(_peers(len(parts)), parts)]
        return [w for line in out.peer_lines(peers, width=100)[1:] for w in line.split() if w.startswith("node")]
    # an average of 10: below 5 behind; of 9: none
    assert behind([4] + [10] * 35 + [16]) == ["node1"]
    assert behind([4] + [9] * 36) == []
    # at half the average: not behind, below it: behind
    assert behind([20] + [40] * 39 + [60]) == []
    assert behind([19] + [40] * 39 + [61]) == ["node1"]


def test_peer_lines_summary_in_the_width():
    peers = [dict(p, name="cassandra-node-%d.dc1.example.com" % i, way=w)
             for i, p in enumerate(_peers(20, stalled=(1, 2, 3))) for w in ("to", "from")]
    for width in (60, 80, 100, 200):  # (a cell wider than the width stays whole)
        assert len(out.peer_lines(peers, width=width)[0]) <= width, width
    assert out.peer_lines(peers, width=80)[0] == "      40 peers: 34 ok (avg 45%), 6 STALLED"
    assert "; slowest: from cassandra-node-0.dc1.example.com 0% STALLED, " in out.peer_lines(peers, width=200)[0]
    assert out.peer_lines(_peers(90, stalled=(5,), slow=(31, 44)), width=100)[0] == \
        "      90 peers: 87 ok (avg 44%), 1 STALLED, 2 behind; slowest: node5 0% STALLED, node31 12%"


def test_peer_lines_summed_up_both_ways_named_with_their_way():
    both = [dict(p, way=w) for p in _peers(20) for w in ("to", "from")]
    assert out.peer_lines(both, width=100) == [
        "      40 peers: 40 ok (avg 44%); slowest: from node13 31%, to node13 31%, from node9 33%"]


def test_progress_line_stalled_only_from_the_warning_threshold():
    """A bootstrap a minute without progress (a whole SSTable on its way): no STALLED (its colour: test_ops)."""
    peers = [{"name": "node1", "way": "from", "done": 0.2 * GIB, "total": 2.3 * GIB, "idle": 60}]

    def line(idle, **kwargs):
        return out.progress_line(1, 1, "node4", "bootstrap", "JOINING", done=0.2 * GIB, total=2.3 * GIB, now=NOW,
                                 start=NOW - 120, idle=idle, limit=1800, warn=300, peers=kwargs.get("peers", peers))
    assert line(60) == ["[1/1] node4 bootstrap  JOINING  [----------]  8%  0.2/2.3 GiB  no progress for 1m  2m",
                        "      from node1  0.2/2.3 GiB  8% no progress for 1m"]
    assert "no progress for 4m  " in line(299)[0] and "STALLED" not in line(299)[0]
    assert "STALLED 5m/30m" in line(300)[0]
    stalled = [dict(peers[0], idle=300, stalled=True)]
    assert line(300, peers=stalled)[1] == "      from node1  0.2/2.3 GiB  8% STALLED"
    assert line(0, peers=[dict(peers[0], idle=30)])[1] == "      from node1  0.2/2.3 GiB  8%"


def test_progress_line_says_the_disk_growing_while_netstats_is_still():
    def line(**kwargs):
        return out.progress_line(1, 1, "node4", "bootstrap", "JOINING", done=0.2 * GIB, total=2.3 * GIB, now=NOW,
                                 start=NOW - 600, limit=1800, warn=300, **kwargs)[0]
    # netstats still for 5 minutes, the disk growing (idle: since its last growth)
    assert line(idle=0, disk=3.2 * GIB, frozen=300) == \
        "[1/1] node4 bootstrap  JOINING  [----------]  8%  0.2/2.3 GiB  receiving (disk +3.2 GiB in 5m)  10m"
    # netstats still for less than a minute: nothing to say; the disk quiet too for a minute: no progress
    assert "receiving" not in line(idle=0, disk=GIB, frozen=30)
    assert "receiving" not in line(idle=90, disk=GIB, frozen=400) and "no progress for 1m" in line(idle=90, disk=GIB, frozen=400)


def test_progress_line_small_totals_one_unit():
    """A decommission of a few hundred MiB: done and total in the total's unit, on both lines."""
    lines = out.progress_line(2, 3, "node4", "decommission", "LEAVING", done=305.1 * MIB, total=329.8 * MIB,
                              now=NOW, start=NOW - 60,
                              peers=[{"name": "node1", "way": "to", "done": 190 * MIB, "total": 200 * MIB},
                                     {"name": "node2", "way": "to", "done": 115.1 * MIB, "total": 129.8 * MIB},
                                     {"name": "node3", "way": "to", "done": 40 * 1024, "total": 900 * 1024}])
    assert lines == [
        "[2/3] node4 decommission  LEAVING  [#########-]  92%  305.1/329.8 MiB  1m",
        "      to node1  190.0/200.0 MiB  95%   to node2  115.1/129.8 MiB  88%",
        "      to node3   40.0/900.0 KiB   4%"]


def test_progress_line_waiting_stalled_done():
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", now=NOW, start=NOW - 32) == \
        ["[1/2] node5 bootstrap  JOINING  waiting for streams  32s"]
    # no stream yet (a bootstrap's ring delay): not called stalled, the time without progress shown after a minute
    assert out.progress_line(1, 2, "node5", "bootstrap", mode="JOINING", now=33, start=0, idle=33, limit=3600) == \
        ["[1/2] node5 bootstrap  JOINING  waiting for streams  33s"]
    assert out.progress_line(1, 2, "node5", "bootstrap", mode="JOINING", now=120, start=0, idle=120, limit=3600) == \
        ["[1/2] node5 bootstrap  JOINING  waiting for streams  no progress 2m/1h00m  2m"]
    # data left, a check or two without a byte: no news; a minute: said as progress; from the warning
    # threshold: STALLED and the limit
    assert "progress" not in out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB,
                                               now=NOW, start=NOW - 900, idle=50, limit=1800, warn=300)[0]
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB, now=NOW,
                             start=NOW - 900, idle=240, limit=1800, warn=300) == \
        ["[1/2] node5 bootstrap  JOINING  [###-------]  37%  37.5/100.0 GiB  no progress for 4m  15m"]
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB, now=NOW,
                             start=NOW - 900, idle=360, limit=1800, warn=300) == \
        ["[1/2] node5 bootstrap  JOINING  STALLED 6m/30m  37%  37.5/100.0 GiB  15m"]
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB, now=NOW,
                             start=NOW - 1800, idle=1800, limit=1800, warn=300, status="stalled") == \
        ["[1/2] node5 bootstrap  JOINING  STALLED 30m/30m  37%  37.5/100.0 GiB  30m"]
    # no warning threshold given: STALLED only at the limit
    assert "STALLED" not in out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=GIB, total=2 * GIB,
                                              now=NOW, start=NOW - 900, idle=899, limit=900)[0]
    assert out.progress_line(1, 2, "node5", "bootstrap", "JOINING", done=37.5 * GIB, total=100 * GIB, now=NOW,
                             start=NOW - 900, status="jmx_refused") == \
        ["[1/2] node5 bootstrap  JOINING  JMX LOGIN REFUSED  37%  37.5/100.0 GiB  15m"]
    # all sent, the end not there yet (index builds): finishing, quiet for a while
    assert out.progress_line(2, 3, "node4", "decommission", "LEAVING", done=GIB, total=GIB, now=NOW, start=NOW - 600,
                             idle=300, limit=3600)[0].endswith("all sent, finishing  no progress 5m/1h00m  10m")
    assert out.progress_line(0, 0, "node5", "bootstrap", done=10 * GIB, total=10 * GIB, now=NOW, start=NOW - 60,
                             status="done") == ["node5 bootstrap  done  10.0/10.0 GiB  1m"]
    assert out.progress_line(2, 3, "node4", "decommission", "LEAVING", done=1 * GIB, total=1 * GIB, speed=MIB,
                             now=NOW, start=NOW)[0].endswith("all sent, finishing  0s")


# --- recap (Q4) ---

def restarts(times):
    return [{"node": "node%d" % (i + 1), "outcome": "restarted", "seconds": t} for i, t in enumerate(times)]


def test_recap_grouped_with_the_slow_node_apart():
    lines = out.recap("rolling_restart", "my_cluster", restarts([109, 112, 400, 115, 118, 110]), seconds=690)
    assert lines == ["DONE  rolling_restart  my_cluster  6 restarted (11m30s)",
                     "  node1, node2, node4..node6  restarted  (1m49s..1m58s each)",
                     "  node3                       restarted  6m40s  %s slow (median 1m53s)" % ARROW]


def test_recap_no_outlier_under_the_factor_and_one_node():
    assert out.recap("rolling_restart", "c", restarts([100, 140]))[1:] == ["  node1, node2  restarted  (1m40s..2m20s each)"]
    assert out.recap("rolling_restart", "c", restarts([100]))[1:] == ["  node1  restarted  1m40s"]


def test_recap_failed_and_skipped():
    lines = out.recap("rolling_restart", "my_cluster", restarts([100]) + [
        {"node": "node2", "status": "failed", "reason": "not UN after 10m"},
        {"node": "node3", "status": "skipped", "reason": "after the failure"},
        {"node": "node4", "status": "skipped", "reason": "after the failure"}])
    assert lines == ["FAILED  rolling_restart  my_cluster  1 restarted, 1 failed, 2 not touched",
                     "  node1         restarted                        1m40s",
                     "  node2         FAILED",
                     "    not UN after 10m",
                     "  node3, node4  not touched (after the failure)"]


def test_recap_columns_not_stretched_by_a_long_reason():
    reason = "Cluster not healthy, nothing more is done: " + "x" * 300
    lines = out.recap("rolling_restart", "c", restarts([96]) + [{"node": "node2", "status": "failed", "reason": reason}])
    assert lines[1:] == ["  node1  restarted  1m36s", "  node2  FAILED", "    " + reason]


def test_recap_check_mode_says_would_and_nothing_changed():
    lines = out.recap("apply_config", "my_cluster", [{"node": "node%d" % i, "outcome": "applied, restarted",
                                                      "seconds": 3} for i in range(1, 5)], check=True, seconds=20,
                      extra=["  cassandra.yaml"])
    assert lines == ["CHECK  apply_config  my_cluster  would apply, restart on 4 nodes   nothing was changed",
                     "  node1..node4  would apply, restart",
                     "",
                     "  cassandra.yaml"]
    assert out.would("restarted") == "would restart" and out.would("frobbed") == "would be frobbed"
    assert out.recap("x", "c", []) == ["NOTHING TO DO  x  c  nothing to do"]


# --- what changed ---

def test_perm_lines():
    assert out.perm_lines([
        {"file": "cassandra.yaml", "before": {"owner": "root", "group": "cassandra", "mode": "0640"},
         "after": {"owner": "cassandra", "group": "cassandra", "mode": "0640"}},
        {"file": "same.sh", "before": {"owner": "a", "group": "b", "mode": "0644"},
         "after": {"owner": "a", "group": "b", "mode": "0644"}}]) == \
        ["  cassandra.yaml  owner/group/mode  root/cassandra 0640 -> cassandra/cassandra 0640"]


def test_diff_lines_changed_only_nested_and_masked():
    before = {"concurrent_reads": 32, "num_tokens": 16,
              "server_encryption_options": {"keystore_password": "old", "protocol": "TLS"}}
    after = {"concurrent_reads": 64, "num_tokens": 16, "new_one": True,
             "server_encryption_options": {"keystore_password": "new", "protocol": "TLS"}}
    lines = out.diff_lines(before, after)
    assert lines == ["  - concurrent_reads: 32", "  + concurrent_reads: 64", "  + new_one: true",
                     "  server_encryption_options:", "  -   keystore_password: ****", "  +   keystore_password: ****"]
    assert "old" not in "".join(lines) and "new\n" not in "\n".join(lines)


def test_changed_lines_masks_and_drops_headers():
    diff = "--- a\n+++ b\n@@ -1 +1 @@\n context\n-keystore_password: old\n+keystore_password: new\n-x: 1\n+x: 2\n"
    assert out.changed_lines(diff) == ["  -keystore_password: ****", "  +keystore_password: ****", "  -x: 1", "  +x: 2"]


# --- what is left to do (Q8) ---

def test_command_full_and_minimal():
    assert out.command("cleanup", "inventory.yml", "my_cluster", ["node1", "node2"],
                       {"cassandra_cleanup_jobs": 2}) == \
        "ansible-playbook -i inventory.yml community.cassandra.cleanup -e cassandra_hosts=my_cluster" \
        " --limit node1,node2 -e cassandra_cleanup_jobs=2"
    assert out.command("status") == "ansible-playbook community.cassandra.status"  # ansible.cfg's inventory
    assert out.command("topology", ["/srv/inv/a.yml", "/srv/inv/b.yml"], cwd="/srv") == \
        "ansible-playbook -i inv/a.yml -i inv/b.yml community.cassandra.topology"
    assert out.command("x", extra={"msg": "two words"}) == \
        """ansible-playbook community.cassandra.x -e '{"msg": "two words"}'"""
    assert out.command("x", options=["--vault-id", "prod@file"], extra=["-e a=1"]) == \
        "ansible-playbook --vault-id prod@file community.cassandra.x -e a=1"


def test_todo():
    assert out.todo([]) == []
    assert out.todo(["remove node7 from the inventory", None,
                     {"text": "cleanup the nodes that gave data", "command": "ansible-playbook x"}]) == [
        "TO DO", "  1. remove node7 from the inventory", P, "  2. cleanup the nodes that gave data:",
        "     ansible-playbook x"]


def test_inventory_steps_with_and_without_git(tmp_path):
    plain = tmp_path / "plain"
    (plain / "inv").mkdir(parents=True)
    hosts = plain / "inv" / "hosts.yml"
    hosts.write_text("all: {}\n")
    assert not out.in_git_work_tree(str(hosts))
    assert out.inventory_steps(str(hosts), cwd=str(plain)) == ["Review the inventory: inv/hosts.yml"]
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "inv").mkdir()
    hosts = repo / "inv" / "hosts.yml"
    hosts.write_text("all: {}\n")
    assert out.in_git_work_tree(str(hosts)) and out.in_git_work_tree(str(repo / "inv"))
    steps = out.inventory_steps(str(hosts), message="Remove node7", cwd=str(repo))
    assert steps == ["Review the inventory: inv/hosts.yml",
                     {"text": "commit it", "command": "git add inv/hosts.yml && git commit -m 'Remove node7'"}]
    assert out.inventory_steps("x.yml", in_git=False) == ["Review the inventory: x.yml"]


@pytest.mark.parametrize("text, masked", [
    ('"keystore_password": "x"', '"keystore_password": ****'),
    ("{'keystore_password': 'x', 'a': 1}", "{'keystore_password': ****, 'a': 1}"),
    ("sse_c_key = x", "sse_c_key = ****"),
    ("access_key: x", "access_key: ****"),
    ("password: >-\n  x\n  y\nnext: 1", "password: ****\n  ****\n  ****\nnext: 1"),
    ("cqlsh -u a -p x", "cqlsh -u a -p ****"),
    ("nodetool -pw x status", "nodetool -pw **** status"),
    ("--password x", "--password ****"),
    # an unquoted value to the end of the line: a comma or a brace is part of it
    ("keystore_password: abc,def", "keystore_password: ****"),
    ("keystore_password: ab}cd", "keystore_password: ****"),
    ("password: abc, def", "password: ****"),
    ("-Dssl.keyStorePassword=ab,cd -Dx=1", "-Dssl.keyStorePassword=****"),
    ("{password: abc, user: u}", "{password: ****, user: u}"),
    # a brace in the value or Jinja on the line: still to the end of the line; every secret of a flow mapping
    ("keystore_password: P@ss{1,2}word", "keystore_password: ****"),
    ("-Dcassandra.jmx.password=a{b}c,d", "-Dcassandra.jmx.password=****"),
    ("msg: {{ x }} password=hunter2,xyz", "msg: {{ x }} password=****"),
    ("{password: abc, secret: b, user: u}", "{password: ****, secret: ****, user: u}"),
    ('{"a": {"password": "x"}, "b": 1}', '{"a": {"password": ****}, "b": 1}'),
])
def test_mask_more_forms(text, masked):
    assert out.mask(text) == masked


def test_changed_lines_mask_a_value_with_a_comma():
    assert out.changed_lines(["+keystore_password: ab,cd"]) == ["  +keystore_password: ****"]


def test_hidden_values_in_lists_dicts_and_a_dict_becoming_a_value():
    assert out.shown("opts", ["a", ["-Dpassword=x"]]) == "****"
    assert out.shown("env", {"CASSANDRA_PASS": "x"}) == "****"
    assert out.shown("ldap_bind_pw", "x") == "****" and out.shown("num_tokens", 16) == "16"
    lines = out.diff_lines({"ks": {"keystore_password": "old"}}, {"ks": "x"})
    assert "x" not in " ".join(line.split(":")[-1] for line in lines) and "old" not in " ".join(lines)


@pytest.mark.parametrize("text", ["nodetool -p 7199 status", "mkdir -p /var/lib/cassandra", "ssh -p 2222 host",
                                  "credentials_validity_in_ms: 2000", "num_tokens: 16"])
def test_mask_leaves_ports_paths_and_settings(text):
    assert out.mask(text) == text


def test_mask_cqlsh_password_and_block_values():
    assert out.mask("cqlsh -u admin -p s3cr3t node1") == "cqlsh -u admin -p **** node1"
    assert out.mask("x_password: >-\n  line1\n\n  line2\nnext: 1") == "x_password: ****\n  ****\n\n  ****\nnext: 1"
    assert out.changed_lines("-  keystore_password: |\n-    s3cr3t\n+  other: 1\n") == \
        ["  -  keystore_password: ****", "  -    ****", "  +  other: 1"]
    assert out.diff_lines({}, {"a": 1}) == ["  + a: 1"] and out.diff_lines(None, {}) == []


# --- the TO DO once nodes left the ring ---

def test_removed_todo_reset_then_reimport_then_commit(tmp_path):
    inventories = tmp_path / "inventories"
    inventories.mkdir()
    (tmp_path / ".git").mkdir()
    lines = out.removed_todo(["node3"], "10.0.0.1", inventory=None, hosts=None, inventory_dir=str(inventories),
                             cwd=str(tmp_path))
    assert lines == [
        "TO DO",
        "  1. empty node3 before reusing the host (its data is left in place):",
        "     ansible-playbook community.cassandra.reset_node -e cassandra_target_nodes=node3",
        P, "  2. drop node3 from the inventory: re-import the cluster with its inventory, the changes shown first:",
        "     ansible-playbook community.cassandra.import_cluster -e import_cluster_force=true --check --diff",
        P, "  3. then write them:",
        "     ansible-playbook community.cassandra.import_cluster -e import_cluster_force=true",
        P, "  4. commit it:",
        "     git add inventories && git commit -m 'Inventory: node3 removed' -- inventories"]


def test_removed_todo_outside_git_another_dir_and_a_node_not_answering(tmp_path):
    prod = tmp_path / "inv" / "prod"
    prod.mkdir(parents=True)
    lines = out.removed_todo(["node3", "node4", "node9"], "node1", inventory=[str(prod)], hosts="orders",
                             inventory_dir=str(prod), cwd=str(tmp_path), unreachable=["node9"])
    assert lines == [
        "TO DO",
        "  1. empty node3, node4 before reusing the hosts (their data is left in place):",
        "     ansible-playbook -i inv/prod community.cassandra.reset_node -e cassandra_hosts=orders"
        " -e cassandra_target_nodes=node3,node4",
        P, "  2. drop node3, node4, node9 from the inventory: re-import the cluster with its inventory, the changes shown"
        " first:",
        "     ansible-playbook -i inv/prod community.cassandra.import_cluster -e cassandra_hosts=orders"
        " -e import_cluster_force=true -e import_cluster_dir=inv/prod --check --diff",
        P, "  3. then write them:",
        "     ansible-playbook -i inv/prod community.cassandra.import_cluster -e cassandra_hosts=orders"
        " -e import_cluster_force=true -e import_cluster_dir=inv/prod"]
    assert out.removed_todo([], "node1") == []
    # an inventory import_cluster did not write: removed by hand, no re-import
    assert out.removed_todo(["node3"], "node1", imported=False) == [
        "TO DO", "  1. empty node3 before reusing the host (its data is left in place):",
        "     ansible-playbook community.cassandra.reset_node -e cassandra_target_nodes=node3",
        P, "  2. remove node3 from the inventory (or leave it marked cassandra_node_state: absent)"]


def test_removed_todo_reimport_reads_the_inventory():
    # no -i <node>, nor JMX login: the inventory gives the connection and the logins (vaulted too)
    lines = out.removed_todo(["node3"], "node1", inventory=["inv"], hosts="", in_git=False, reimport_hosts="orders")
    assert lines[5] == "     ansible-playbook -i inv community.cassandra.import_cluster -e cassandra_hosts=orders" \
        " -e import_cluster_force=true --check --diff"
    text = "\n".join(out.removed_todo(["node3"], "node1", inventory=["inv"], in_git=False))
    assert "jmx" not in text and "-i node1," not in text
    # the run's -e cassandra_hosts, when no other is said
    assert "import_cluster -e cassandra_hosts=orders" in "\n".join(out.removed_todo(["node3"], "node1", hosts="orders"))


def test_seeds_todo_first_in_the_removed_todo():
    change = {"auto": [{"dc": "dc2"}], "new": "10.0.0.1, 10.0.0.7"}
    item = out.seeds_todo(change, "prod")
    assert item["command"] == 'cassandra_seeds: ["10.0.0.1", "10.0.0.7"]'
    assert out.seeds_todo({"auto": [], "new": "x"}) is None
    lines = out.removed_todo(["node3"], "node1", in_git=False, first=[item], imported=False)
    assert lines[1].startswith("  1. put the seeds the nodes now run with in the inventory (group_vars/prod/")
    assert lines[3] == P and lines[4].startswith("  2. empty node3")
    # nothing removed: the seeds alone
    assert out.removed_todo([], "node1", first=[item])[0] == "TO DO"
    assert out.removed_todo([], "node1", first=[None]) == []


# --- node groups, wrapping (a large cluster) ---

BIG_A = ["db1%d" % i for i in range(1, 7)] + ["db2%d" % i for i in range(1, 7)]  # 12
BIG_B = ["db07%d" % i for i in range(1, 7)]  # 6
BIG_C = ["db10%d" % i for i in range(1, 4)] + ["db111"]  # 4
BIG = BIG_A + BIG_B + BIG_C


def test_node_groups_are_the_partition_of_the_lists():
    sets = [BIG_A, BIG_B + BIG_C, [n for n in BIG_B if n != "db074"], BIG_A + ["db074"], BIG_C[:3]]
    groups = out.node_groups(sets, BIG)
    # db074 and db111 are in other lists than their neighbours: their names; db101..db103 a group of 3
    assert groups == [("G1", BIG_A), ("G2", ["db071", "db072", "db073", "db075", "db076"]),
                      ("G3", ["db101", "db102", "db103"])]
    assert out.who(BIG_B + BIG_C[:3], BIG, groups) == "G2 + G3 + db074"
    assert out.who(BIG_A + ["db074"], BIG, groups) == "G1 + db074"
    assert out.who(BIG, BIG, groups) == "all"
    assert out.who([n for n in BIG if n != "db111"], BIG, groups) == "all except db111"
    assert out.who(BIG_B + BIG_C, BIG, groups) == "all except G1"  # shorter than G2 + G3 + db074, db111
    assert out.who(["db101", "db074"], BIG, groups) == "db074, db101"
    singles = ["a1", "b2", "c3", "d4", "f6", "g7"]
    assert out.who(BIG_A + singles, BIG_A + BIG_B + singles + ["e5"], [("G1", BIG_A), ("G2", BIG_B)]) == \
        "all except (G2 + e5)"  # read as one
    # a list of one node splits nothing (a value per node would leave no group); the nodes in no list (not
    # read) make no group either
    assert out.node_groups([[n] for n in BIG] + [BIG_A, BIG_B + BIG_C], BIG) == [("G1", BIG_A), ("G2", BIG_B + BIG_C)]
    assert out.node_groups([[n] for n in BIG] + [BIG_A], BIG) == [("G1", BIG_A)]
    # under GROUPS_FROM nodes, or every node alike: none
    assert out.node_groups([BIG_A[:3]], BIG_A[:11]) == []
    assert out.node_groups([BIG], BIG) == []


def test_group_lines():
    groups = [("G1", BIG_A), ("G2", BIG_B)]
    assert out.group_lines(groups) == ["NODE GROUPS", "  G1 = db11..db16, db21..db26 (12 nodes)",
                                       "  G2 = db071..db076 (6 nodes)"]
    assert out.group_lines(groups, full=True, width=60) == [
        "NODE GROUPS",
        "  G1 = db11, db12, db13, db14, db15, db16, db21, db22, db23,",
        "       db24, db25, db26 (12 nodes)",
        "  G2 = db071, db072, db073, db074, db075, db076 (6 nodes)"]
    assert out.group_lines([]) == []


def test_wrap_list_never_cuts_a_name():
    text = "db11..db16, db21..db26, db071..db076, db101..db103"
    assert out.wrap_list(text, 30) == ["db11..db16, db21..db26,", "db071..db076, db101..db103"]
    assert out.wrap_list("G1 + G2 + db074", 6) == ["G1", "+ G2", "+ db074"]
    assert out.wrap_list("a-very-long-node-name.example.net", 10) == ["a-very-long-node-name.example.net"]
    assert out.wrap_list(text, 200) == [text]
    assert out.wrap_list("all except db006..db009, db101", 22) == ["all except", "db006..db009, db101"]
    assert out.wrap_list("all except (G2 + db074)", 15) == ["all except (G2", "+ db074)"]


def test_wrap_value_and_fit():
    assert out.wrap_value("short", 20) == ["short"]
    assert out.wrap_value("a b c d e f g h i j k l", 10) == ["a b c d e", "f g h i j", "k l"]
    assert out.wrap_value("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]  # a value may be cut, not a name
    lines = ["TO DO (1)", "  1. " + " ".join(["word"] * 20), "short"]
    assert out.fit(lines, 40) == ["TO DO (1)", "  1. word word word word word word word", "     word word word word word word word",
                                  "     word word word word word word", "short"]
    assert out.fit(["  " + "y" * 50], 20) == ["  " + "y" * 50]  # one long word: whole
    assert out.fit(lines, None) == lines


def test_settings_lines_in_a_width():
    entries = [("heap", [("31G", "G1", ""), ("16G", "G2 + db074", ARROW + " differs (group_vars/big_dc2)")]),
               ("listen_address", [("{{ ansible_facts['default_ipv4']['address'] }}", "all", "")]),
               ("seeds", [("db11, db071, db101", "db11..db16, db21..db26, db071..db076, db101..db103", "")])]
    assert out.settings_lines(entries, width=70) == [
        "  heap:           31G                  G1",
        "                  16G                  G2 + db074",
        "                                       %s differs (group_vars/big_dc2)" % ARROW,
        "  listen_address: {{ ansible_facts['default_ipv4']['address'] }}  all",
        "  seeds:          db11, db071, db101   db11..db16, db21..db26,",
        "                                       db071..db076, db101..db103"]
    # narrower: the long value goes on under its column, the note too
    narrow = out.settings_lines(entries, width=60)
    assert narrow[2:5] == ["%s%s differs (group_vars/big_dc2)" % (" " * 30, ARROW),  # moved left to fit
                           "  listen_address: {{ ansible_facts['default_ipv4']['add  all",
                           "                  ress'] }}"]
    assert all(len(line) <= 60 for line in narrow)
    # wide enough: a long value pushes its nodes, the notes after them
    wide = out.settings_lines(entries, width=200)
    assert wide[1] == "                  16G                  G2 + db074%s   %s differs (group_vars/big_dc2)" % (" " * 20, ARROW)
    assert wide[2] == "  listen_address: {{ ansible_facts['default_ipv4']['address'] }}  all"
    # no width: as before
    assert out.settings_lines(entries)[2] == "  listen_address: {{ ansible_facts['default_ipv4']['address'] }}  all"


def test_leaves():
    assert out.leaves("x", {"a": 1, "b": {"c": [1, 2], "d": {}}}) == [("x.a", 1), ("x.b.c", [1, 2]), ("x.b.d", {})]
    assert out.leaves("x", {}) == [("x", {})]
    assert out.leaves("x", "v") == [("x", "v")]


# --- laid out where printed (the help text) ---

def test_laid_out_paragraph():
    line = out.flow(4, "  Cassandra: series 41x, package 4.1.10, installed from repository (role default)")
    assert out.laid_out(line, 100) == (["  Cassandra: series 41x, package 4.1.10, installed from repository (role default)"], "")
    assert out.laid_out(line, 40) == (["  Cassandra: series 41x, package 4.1.10,", "    installed from repository (role",
                                       "    default)"], "")


def test_laid_out_key_and_its_column():
    line = out.flow(12, u"the hosts to add (comma-separated) (default:\xa0false)", "  -e x=y")
    assert out.laid_out(line, 100) == (["  -e x=y    the hosts to add (comma-separated) (default: false)"], "-e x=y")
    # wrapped under the column, "(default: false)" never broken
    assert out.laid_out(line, 48) == (["  -e x=y    the hosts to add (comma-separated)",
                                       "            (default: false)"], "-e x=y")


def test_laid_out_command():
    # one line when it fits; else cut between two arguments (an option and its value together, a quoted value
    # whole), " \\" at the end of each line but the last, the next ones 4 columns in
    command = ("ansible-playbook -i inventories/orders.yml community.cassandra.move_node"
               " -e '{\"cassandra_move_tokens\": {\"node1\": \"42\"}}' --check")
    line = out.command_flow("  ", command)
    assert out.is_command(line) and not out.is_command(out.flow(2, command))
    assert out.laid_out(line, 200) == (["  $ " + command], "")
    assert out.laid_out(line, 80) == (["  $ ansible-playbook -i inventories/orders.yml community.cassandra.move_node \\",
                                       "      -e '{\"cassandra_move_tokens\": {\"node1\": \"42\"}}' --check"], "")
    assert out.laid_out(line, 40) == (["  $ ansible-playbook \\", "      -i inventories/orders.yml \\",
                                       "      community.cassandra.move_node \\",
                                       "      -e '{\"cassandra_move_tokens\": {\"node1\": \"42\"}}' \\",
                                       "      --check"], "")  # an argument longer than the room: a line of its own
    assert out.command_words("a -e 'x y' --check -i 'h1,' p") == ["a", "-e 'x y'", "--check", "-i 'h1,'", "p"]
    # a backslash keeps the next character in the word (not in single quotes); an option is no option's value
    assert out.command_words("a -e x\\ y\tb -e \"q\\\" r\" 'c\\' -i -e z") == [
        "a", "-e x\\ y", "b", "-e \"q\\\" r\"", "'c\\'", "-i", "-e z"]


def test_laid_out_key_longer_than_its_column():
    line = out.flow(12, "what it does", "  -e cassandra_long_name=value")
    assert out.laid_out(line, 100) == (["  -e cassandra_long_name=value", "            what it does"],
                                       "-e cassandra_long_name=value")
    assert out.laid_out(out.flow(12, "", "  -e cassandra_long_name=value"), 100)[0] == ["  -e cassandra_long_name=value"]
    assert out.laid_out(out.flow(12, "", "  -e x"), 100)[0] == ["  -e x"]  # no trailing space


def test_laid_out_narrow_text_under_its_key():
    # less than NARROW columns right of the column: the text under the key, 4 columns in
    line = out.flow(50, "the cluster to run on (default: the one in the commands)", "    -e cassandra_hosts=<group>")
    assert out.laid_out(line, 60) == (["    -e cassandra_hosts=<group>",
                                       "        the cluster to run on (default: the one in the",
                                       "        commands)"], "-e cassandra_hosts=<group>")


def test_laid_out_leaves_other_lines_as_they_are():
    command = "      $ ansible-playbook -i inventories community.cassandra.add_node " + "-e x=y " * 20
    assert out.laid_out(command, 60) == ([command], "")
    assert out.laid_out("", 60) == ([""], "")


def test_flow_keeps_one_line():
    # a newline in a value of the inventory: a space, the line laid out as one
    line = out.flow(4, "  Cluster 'Multi\nline' (inventory group a)")
    assert "\n" not in line and out.laid_out(line, 100) == (["  Cluster 'Multi line' (inventory group a)"], "")
    assert "\n" not in out.flow(4, "x\r\ny", "  key\nz")


# --- a secret's value on several lines: every line of it masked ---

# a secret name in the key line's comment or inside the value, a YAML tag or anchor, the shell's $'...', an XML element
BLOCK_SECRETS_AFTER_AUDIT = [
    "  keystore_password: |  # PEM password: see vault\n    LEAK1\n    LEAK2\n  next: 1",
    "keystore_password: | # rotate the password: yearly\n  LEAK1\n  LEAK2\nnext: 1",
    "keystore_password:  # old_password: x\n    LEAK1\nnext: 1",
    "export KS_PASSWORD=\"LEAK0 # a password: x\nLEAK1\"\nnext=1",
    "keystore_password: !!str \"LEAK1\nLEAK2\"\nnext: 1",
    "keystore_password: &a 'LEAK1\nLEAK2'\nnext: 1",
    "export KS_PASSWORD=$'LEAK1\nLEAK2'\nnext=1",
    "JVM_OPTS=\"$JVM_OPTS -Da.password=x -Db.password='LEAK1\nLEAK2'\"\nnext=1",
    "<password>LEAK1</password>\n<next>1</next>",
    "<password><![CDATA[LEAK1]]></password>\n<next>1</next>",
    "JVM_OPTS=\"$JVM_OPTS -Dx.password:$x\n  LEAK1\"\nnext=1",  # $ after a colon, inside a shell quote
    "  <secretKey attr=\"1\">LEAK1</secretKey>\n<next>1</next>",
    "ldap_password=x\\\r\n  LEAK1\r\nnext=1",  # a CRLF line continued by its backslash
    "password: $abc\n  LEAK1\nother: 1",  # a YAML plain value starting with $: its more indented lines
    "password: $(x\n  LEAK1\nother: 1",
    "password: &a\n- LEAK1\nother: 1",  # an anchor alone: the list under the key
    "password: !!seq\n- LEAK1\nother: 1",
]


@pytest.mark.parametrize("text", [
    "keystore_password: |\n  LEAK1\n  LEAK2\nnext: 1",
    "keystore_password: >-\n  LEAK1\n\n  LEAK2\nnext: 1",
    "keystore_password: |+2\n    LEAK1\n  LEAK2\nnext: 1",
    "  keystore_password: LEAK1\n    LEAK2\n  next: 1",  # a plain value on more indented lines
    "keystore_password: \"LEAK1\nLEAK2\"\nnext: 1",  # a quote left open: its lines, whatever their indent
    "keystore_password: \"LEAK1\n  LEAK2\n  LEAK3\"\nnext: 1",
    "opts: {keystore_password: \"LEAK1, x\n  LEAK2\"}\nnext: 1",
    "keystore_password: \"LEAK1 \\\"\n  LEAK2 \\\n  LEAK3\"\nnext: 1",
    "keystore_password: 'LEAK1 it''s\n  LEAK2'\nnext: 1",
    "keystore_password: [LEAK1,\n  LEAK2]\nnext: 1",
    "keystore_password: {a: LEAK1,\n  b: \"LEAK2 }\"\n  }\nnext: 1",
    "opts: {a: 1, keystore_password: \"LEAK1\n  LEAK2\"}\nnext: 1",
    "opts: {keystore_password: x, other_password: \"LEAK1\n  LEAK2\"}\nnext: 1",  # the line's last secret
    "keystore_password:\n  LEAK1\n  LEAK2\nnext: 1",  # the value under the key
    "keystore_password:  # rotated\n  a: LEAK1\n  b: LEAK2\nnext: 1",
    "secrets:\n- LEAK1\n- LEAK2\nnext: 1",  # a list at the key's column
    "export KEYSTORE_PASSWORD=\"LEAK1\nLEAK2\"\nnext=1",  # cassandra-env.sh
    "KEYSTORE_PASSWORD='LEAK1\nLEAK2'\nnext=1",
    "-Djavax.net.ssl.keyStorePassword=\"LEAK1\nLEAK2\"\n-Xss256k",  # jvm options
    "keystore.password=LEAK1\\\n    LEAK2\\\\\\\n    LEAK3\nnext=1",  # a properties file's continued line
    "export KS_PASSWORD=x TS_PASSWORD=\"LEAK1\nLEAK2\"\nnext=1",  # the line's last secret
    "KEY_PASSWORD=\"LEAK1\"\\\nLEAK2\nnext=1",  # a quote closed, then a backslash
    "KEY_PASSWORD=\"LEAK1\nLEAK2\"\\\nLEAK3\nnext=1",
    "PASSWORDS=(LEAK1\n  LEAK2)\nnext=1",  # a shell array
    "opts: {keystore_password: a\n  LEAK1, x: 1}\nnext: 1",  # a flow mapping's plain value on the next line
    "opts: {keystore_password: [a,\n  LEAK1], x: 1}\nnext: 1",
] + BLOCK_SECRETS_AFTER_AUDIT)
def test_mask_every_line_of_a_secret_value(text):
    masked = out.mask(text)
    assert "LEAK" not in masked, masked
    assert masked.rsplit("\n", maxsplit=1)[-1] == text.rsplit("\n", maxsplit=1)[-1]  # the next setting shown
    assert len(masked.split("\n")) == len(text.split("\n"))


@pytest.mark.parametrize("text", [
    "- keystore_password: x\n  store_type: JCEKS",  # a list item's next key
    "keystore_password: \"x\"\n  next: 1",  # closed on its line
    "keystore_password: x\nnext: 1",
    "keystore_password: {a: x}\n  next: 1",
    "opts: {keystore_password: x,\n  next: 1}",
    "keystore_password=x\n  next=1",  # not YAML: an indented line is another setting
    "keystore_password=x\\\\\nnext=1",  # an even number of backslashes: not continued
    "keystore_password: [it's, x]\n  next: 1",  # a quote inside a word: not one that opens a value
    "# keystore_password: x\n# next: 1",
    "keystore_password: [x, y]  # see [1\n  next: 1",  # a comment: no bracket, no quote in it
    "JVM_OPTS=\"$JVM_OPTS -Djavax.net.ssl.trustStorePassword=\"\nJVM_OPTS=\"$JVM_OPTS -Dx=1\"",  # its quote closes
    "echo \"the password: \"\n  next",  # (inside a quote: not a YAML key)
    "# keystore_password: \"see the vault\nnum_tokens: 8",  # a comment goes on to no other line
    "opts: {keystore_password: a, x: 1,\n  next: 1}",
])
def test_mask_stops_where_the_secret_value_does(text):
    masked = out.mask(text).split("\n")
    assert masked[0] != text.split("\n")[0] or "x" not in masked[0]
    assert masked[1] == text.split("\n")[1]


def test_mask_lines_masks_a_value_line_without_its_key():
    lines = ["a: 1", "keystore_password: |", "  LEAK1", "  LEAK2", "b: 2"]
    assert out.mask_lines(lines) == ["a: 1", "keystore_password: ****", "  ****", "  ****", "b: 2"]
    assert out.mask_lines([]) == []


def test_mask_hunks_masks_each_side_of_each_hunk_as_a_block():
    lines = ["cassandra.yaml, line 3:", "  - keystore_password: x", "  + keystore_password: |", "  +   LEAK1",
             "cassandra.yaml, line 9:", "  +   LEAK2", "cassandra-env.sh, line 2:", "  + export X_PASSWORD=\"LEAK3",
             "  + LEAK4\"", "  - JVM_OPTS=1"]
    shown = out.mask_hunks(lines)
    assert "LEAK1" not in "\n".join(shown) and "LEAK3" not in "\n".join(shown) and "LEAK4" not in "\n".join(shown)
    assert shown[:4] == ["cassandra.yaml, line 3:", "  - keystore_password: ****", "  + keystore_password: ****",
                         "  +   ****"]
    assert shown[5] == "  +   LEAK2"  # (its key in another hunk: masked where the hunks are made, from the file)
    assert shown[9] == "  - JVM_OPTS=1"


@pytest.mark.parametrize("text", [
    "ldap.password=LEAK1 #x\\\n  LEAK2\nnext=1",  # (a # inside a properties value)
    "ldap_password: LEAK1\\\nLEAK2\nnext: 1",  # a properties file with key: value
    "keystore_password: \"LEAK1\\\nLEAK2\"\nnext: 1",
])
def test_mask_a_line_after_a_backslash(text):
    masked = out.mask(text)
    assert "LEAK" not in masked and masked.endswith("\nnext" + text.split("\nnext")[1])


def test_changed_lines_mask_each_side_with_its_context():
    lines = list(difflib.unified_diff(["a: 1", "keystore_password: |", "  OLDLEAK", "b: 2"],
                                      ["a: 1", "keystore_password: |", "  NEWLEAK", "b: 2"], lineterm=""))
    assert out.changed_lines(lines) == ["  -  ****", "  +  ****"]  # (its key a context line)
    old = ["keystore_password: \"OLD1", "  OLD2", "  END\"", "next: 1"]
    new = ["keystore_password: \"NEW1", "  SEC2", "  END\"", "next: 1"]
    shown = out.changed_lines(list(difflib.unified_diff(old, new, lineterm="")))
    assert not [line for line in shown if re.search(r"OLD|NEW|SEC|END", line)], shown
    # a hunk of its own: not masked with the one before
    old = ["keystore_password: x"] + ["  # c%d" % i for i in range(10)] + ["  parameters: 1"]
    new = ["keystore_password: y"] + ["  # c%d" % i for i in range(10)] + ["  parameters: 2"]
    assert out.changed_lines(list(difflib.unified_diff(old, new, lineterm="")))[-2:] == [
        "  -  parameters: 1", "  +  parameters: 2"]


def test_mask_a_long_word_is_quick():
    start = time.time()
    for text in ("a" * 4000, "password" * 500, "x_" * 2000 + "=1"):
        out.mask(text)
    assert time.time() - start < 5


def test_mask_an_element_s_text_only():
    assert out.mask("<password>LEAK1</password> x") == "<password>****</password> x"
    assert out.mask("<password></password>") == "<password></password>"
    assert out.mask("<password> LEAK1 </password>\r") == "<password>****</password>\r"
    for text in ("<password><![CDATA[LEAK1 a</b]]></password>", "<password><!-- c -->LEAK1</password>",
                 "<password>a<b/>LEAK1</password>", "<password>a<b>x</b>LEAK1</password>",
                 "<password><!-- </x> -->LEAK1</password>"):  # its text up to its own closing tag
        assert out.mask(text) == "<password>****</password>", text


def test_mask_a_line_of_many_secrets_is_quick():
    start = time.time()
    assert out.mask("<password>" + "<![CDATA[" * 30000).startswith("<password>****")  # (unclosed: to the end)
    assert "LEAK" not in out.mask("{" + ", ".join('"password%d": "LEAK"' % i for i in range(2000)) + "}")
    assert time.time() - start < 5


def test_seeds_reload_todo():
    assert out.seeds_reload_todo([], "cmd") is None
    item = out.seeds_reload_todo(["n1", "n3"], "cmd")
    assert item["command"] == "cmd"
    assert item["text"].startswith("reload the seed list on n1, n3: nodetool reloadseeds failed")
    assert "fix the file before any restart" in item["text"]


def test_a_node_range_longer_than_its_room_cut_after_its_dots():
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import wrap_list
    text = "db-node-1.example.net..db-node-3.example.net, db-node-5.example.net"
    assert wrap_list(text, 30) == ["db-node-1.example.net..", "db-node-3.example.net,", "db-node-5.example.net"]
    assert wrap_list(text, 50) == ["db-node-1.example.net..db-node-3.example.net,", "db-node-5.example.net"]
    assert wrap_list("node1..node3", 8) == ["node1..", "node3"]  # (no space added inside a range)
