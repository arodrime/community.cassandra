from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The import of a large cluster (88 nodes, made up): its screen and report stay readable, node lists by groups,
# a dict value one row per sub-key, the hand edits counted per file, a value that follows the node names one
# template; and the inventory still gives each node its own values.

import os

import pytest

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4, cassandra_inventory_files, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import (
    _trusted, cassandra_inventory_host_vars)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import cassandra_import_report

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
WRITTEN = ["/p/inventories/big.yml", "/p/inventories"]
REPORT = "/p/reports/big/report.txt"
# three parts of the cluster, each its own heap, extra settings, Medusa bucket
PART_A = ["db%d%d" % (rack, i) for rack in range(1, 7) for i in range(1, 7)]  # 36: db11..db16, ..., db61..db66
PART_B = ["db%03d" % (rack * 10 + i) for rack in (7, 8, 9) for i in range(1, 7)] + \
    ["db%d" % (rack * 10 + i) for rack in (20, 21) for i in range(1, 7)]  # 30: db071..db076, ..., db211..db216
PART_C = ["db10%d" % i for i in range(1, 4)] + ["db11%d" % i for i in range(1, 10)] + \
    ["db%d" % i for i in range(121, 131)]  # 22: db101..db103, db111..db119, db121..db130
LOGBACK = ["logback.xml, line %d:" % (40 + 3 * i) for i in range(10)]
JVM11 = ["jvm11-server.options, line %d:" % (60 + i) for i in range(5)]


def _edits(name):
    """The hand edit lines of a node: 10 blocks in logback.xml everywhere, 5 in jvm11-server.options on part B,
    one setting on db111."""
    lines = []
    for i, head in enumerate(LOGBACK):
        lines += [head, '  - <logger name="org.example.l%d" level="INFO"/>' % i,
                  '  + <logger name="org.example.l%d" level="WARN"/>' % i, '  + <!-- tuned %d -->' % i]
    if name in PART_B:
        for i, head in enumerate(JVM11):
            lines += [head, "  + -XX:Example%d=1" % i, "  + -XX:Other%d=2" % i]
    if name == "db111":
        lines += ["cassandra.yaml, line 100:", "  - concurrent_writes: 32", "  + concurrent_writes: 48"]
    return lines


def fake_node(name):
    part = "a" if name in PART_A else "b" if name in PART_B else "c"
    dc = {"a": "dc1", "b": "dc2", "c": "dc3"}[part]
    rack = "r%s" % name[2:-1].lstrip("0")
    ip = "10.100.%d.%d" % ({"a": 1, "b": 2, "c": 3}[part], int(name[2:]))
    extra = {"streaming_connections_per_host": {"a": 2, "b": 1, "c": 1}[part],
             "sstable_preemptive_open_interval": "50MiB",
             "audit_logging_options": {"included_categories": "DDL,DCL", "audit_logs_dir": "/var/log/audit"}}
    if name == "db111":
        extra["streaming_connections_per_host"] = 6
    variables = {
        "cassandra_version": "41x", "cassandra_num_tokens": 16, "cassandra_seeds": ["db11", "db071", "db101"],
        "cassandra_listen_address": IPV4, "cassandra_jmx_password": "s3cret-%s" % part,
        "cassandra_max_heap_size": "8G" if name == "db111" else {"a": "31G", "b": "24G", "c": "16G"}[part],
        "cassandra_extra_settings": extra,
        "cassandra_linux_limits": {"nofile": 100000, "memlock": "unlimited", "nproc": 32768},
        "cassandra_config_file_permissions": {"cassandra.yaml": "0644" if part == "a" or name == "db074" else "0640",
                                              "jmxremote.password": "0400"},
        "cassandra_medusa_nodetool_host": "%s.example.net" % name,
        "cassandra_broadcast_rpc_address": ip,
        "cassandra_medusa_extra_settings": {"storage": {"bucket_name": "backups-%s" % ("a" if part == "a" else "b"),
                                                        "secret_access_key": "k-%s" % part},
                                            "cassandra": {"nodetool_ssl": "false"}},
    }
    return {"name": name, "address": ip, "ansible_host": ip, "hostname": name, "dc": dc, "rack": rack, "read": True,
            "vars": variables, "hand_edits": _edits(name), "normalized": [], "notes": [],
            "keep": {"cassandra_linux_manage": False} if part == "c" else {}}


def fake_cluster():
    return [fake_node(n) for n in PART_A + PART_B + PART_C]


def big_report(**kwargs):
    layout = cassandra_inventory_layout(fake_cluster(), "Big Cluster")
    files = [f["path"] for f in cassandra_inventory_files(layout)]
    changes = {"changed": ["big.yml"] + files, "removed": [], "existed": []}
    return cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, changes=changes,
                                   **kwargs)


def golden(name, lines):
    path = os.path.join(FIXTURES, name)
    if os.environ.get("CASSANDRA_GOLDEN_WRITE"):  # to write them again, after a wanted change
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    with open(path) as f:
        return f.read().split("\n")[:-1]


def test_screen_of_88_nodes():
    screen = big_report(screen=True, width=120)
    assert screen == golden("import_88_screen.txt", screen)
    assert max(len(line) for line in screen) <= 120
    text = "\n".join(screen)
    assert "s3cret" not in text and "k-a" not in text


@pytest.mark.parametrize("width", [80, 100, 200])
def test_screen_of_88_nodes_fits_the_terminal(width):
    for line in big_report(screen=True, width=width)[1:]:  # (the verdict line whole)
        assert len(line) <= width, line


def test_report_of_88_nodes():
    report = big_report(width=160)
    details = report.index("DETAILS")
    assert report[:details] == golden("import_88_report.txt", report[:details])
    text = "\n".join(report)
    assert "s3cret" not in text and "k-a" not in text and "k-b" not in text
    # DETAILS: every node by its name, its hand edits line by line
    assert all("  == %s" % n in report[details:] for n in PART_A + PART_B + PART_C)
    at = report.index("  == db111")
    assert '        + <logger name="org.example.l9" level="WARN"/>' in report[at:at + 60]
    assert "        + concurrent_writes: 48" in report[at:at + 60]


def effective(nodes):
    """Each node's variables as the written inventory gives them to Ansible (the import's files read back, merged
    as Ansible does, templates rendered for that host), and as they were read on the node: {node: (inventory's,
    node's)}."""
    layout = cassandra_inventory_layout(nodes, "Big Cluster")
    files = cassandra_inventory_files(layout)
    out = {}
    for n in nodes:
        magic = {"inventory_hostname": n["name"], "inventory_hostname_short": n["name"].split(".")[0],
                 "ansible_facts": {"default_ipv4": {"address": n["address"]}}}

        def rendered(variables):
            templar = Templar(loader=DataLoader(), variables=dict(magic, **_trusted(variables)))
            return dict((k, templar.template(_trusted(v))) for k, v in variables.items())
        written = rendered(cassandra_inventory_host_vars(files, layout["hosts"], n["name"]))
        # (the variables the inventory gives on top: the ring's dc and rack, what is left as it is on the node)
        extra = set(written) - set(n["vars"]) - {"cassandra_dc", "cassandra_rack", "cassandra_linux_manage",
                                                 "cassandra_imported_host"}
        assert not extra, (n["name"], extra)
        out[n["name"]] = (dict((k, written.get(k)) for k in n["vars"]), rendered(n["vars"]))
    return out


def test_inventory_gives_each_node_its_values():
    """The groups, flattening and templates change the screen only: each node still gets the values read on it."""
    for name, (written, read) in effective(fake_cluster()).items():
        assert written == read, name
    layout = cassandra_inventory_layout(fake_cluster(), "Big Cluster")
    assert layout["group_vars"]["big_cluster"]["cassandra_medusa_nodetool_host"] == \
        "{{ inventory_hostname }}.example.net"
    assert not any("cassandra_medusa_nodetool_host" in v for v in layout["host_vars"].values())
    # an address, the node's identity, stays its own (never from ansible_host, which another source may set)
    assert layout["host_vars"]["db11"]["cassandra_broadcast_rpc_address"] == "10.100.1.11"
    # nodes without the setting: the template is not theirs
    nodes = fake_cluster()
    for n in nodes[-3:]:
        del n["vars"]["cassandra_medusa_nodetool_host"]
    for name, (written, read) in effective(nodes).items():
        assert written == read, name


def test_one_node_off_the_rule_keeps_every_value_per_node():
    nodes = fake_cluster()
    nodes[5]["vars"]["cassandra_medusa_nodetool_host"] = "other.example.net"
    layout = cassandra_inventory_layout(nodes, "Big Cluster")
    assert "cassandra_medusa_nodetool_host" not in layout["templates"]
    assert layout["host_vars"]["db16"]["cassandra_medusa_nodetool_host"] == "other.example.net"
    assert layout["host_vars"]["db11"]["cassandra_medusa_nodetool_host"] == "db11.example.net"
    for name, (written, read) in effective(nodes).items():
        assert written == read, name
    # the screen: one line for the setting, not 88
    screen = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, screen=True, width=160)
    at = [i for i, line in enumerate(screen) if line.startswith("  cassandra_medusa_nodetool_host:")]
    assert len(at) == 1 and screen[at[0]].split(None, 1)[1].split("  ")[0] == "(a value per node)"
    assert " all " in screen[at[0]] and screen[at[0]].rstrip(")").split("(")[-1].startswith("host_vars")
    assert screen[at[0] + 1].startswith("  cassandra_")


def test_a_default_is_never_folded_into_the_value_per_node_row():
    nodes = fake_cluster()
    for i, n in enumerate(nodes):
        n["vars"]["cassandra_concurrent_writes"] = 32 + i
    del nodes[3]["vars"]["cassandra_concurrent_writes"]
    layout = cassandra_inventory_layout(nodes, "Big Cluster")
    screen = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, screen=True, width=160)
    assert "(collection's default)" in "\n".join(screen)
    del nodes[3]
    layout = cassandra_inventory_layout(nodes, "Big Cluster")
    screen = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, screen=True, width=160)
    assert [line for line in screen if line.startswith("  cassandra_concurrent_writes:")][0].split()[1:5] == [
        "(a", "value", "per", "node)"]


def test_jvm_moves_of_88_nodes_named_by_groups():
    # part B's cassandra-env.sh hand edits moved, the options its JVM does not use, part C's not moved
    nodes = fake_cluster()
    for n in nodes:
        if n["name"] in PART_B:
            n.update(moved=["cassandra-env.sh, line 290: -XX:+UseNUMA -> cassandra_jvm11_extra_options"],
                     rewritten=["cassandra-env.sh"],
                     cleanup=["cassandra-env.sh, line 291: -XX:+UseParNewGC: not an option of Java 11"])
        elif n["name"] in PART_C:
            n["not_moved"] = "the running JVM's arguments not read"
    layout = cassandra_inventory_layout(nodes, "Big Cluster")
    report = cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p", in_git=True, width=160)
    report = report[:report.index("DETAILS")]
    moved = [line for line in report if "UseNUMA" in line or "UseParNewGC" in line or "rewrites cassandra-env.sh" in line
             or "not moved to variables" in line]
    assert len(moved) >= 4
    for line in moved:
        assert len(line) <= 160 and "db071, db072" not in line and "db101, db102" not in line, line
    assert all(len(line) <= 100 for line in cassandra_import_report(layout, WRITTEN, REPORT, {}, True, cwd="/p",
                                                                    in_git=True, screen=True, width=100)[1:])
