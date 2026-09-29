from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_netstats import parse_netstats, table_of

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "modules", "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


def test_not_streaming():
    mode, lines, sessions = parse_netstats(load_fixture("nodetool_netstats_normal.txt"))
    assert (mode, lines, sessions) == ("NORMAL", [], [])


def test_4x_sizes_with_bytes():
    mode, lines, sessions = parse_netstats(load_fixture("nodetool_netstats_joining.txt"))
    assert mode == "JOINING"
    assert sessions == [{
        "operation": "Bootstrap", "plan_id": "9a3f2c10-6b1e-11ef-8b1a-3d7c1c0a1b2c", "peer": "10.0.0.1",
        "direction": "receiving", "files_total": 12, "bytes_total": 104857600, "files_done": 3, "bytes_done": 26214400,
        "files": [{"path": "/var/lib/cassandra/data/ks/t-1/nb-1-big-Data.db", "table": "", "done": 8738133,
                   "total": 8738133}]}]


def test_5x_plain_sizes_ports_and_both_directions():
    out = """Mode: LEAVING
Unbootstrap 0d5f4a20-7c2e-11ef-9c2b-4e8d2d1b2c3d
    /10.0.0.2:7000
        Sending 4 files, 4000 total. Already sent 1 files (25.00%), 1000 total (25.00%)
            /data/cassandra/data/orders/items-5a1c3b2e8d9f4a6b7c8d9e0f1a2b3c4d/nb-7-big-Data.db 1000/1000 bytes (100%) sent to idx:0/10.0.0.2:7000
            /data/cassandra/data/orders/items-5a1c3b2e8d9f4a6b7c8d9e0f1a2b3c4d/nb-8-big-Data.db 10/2000 bytes (0%) sent to idx:0/10.0.0.2:7000
    /10.0.0.3:7000 (using /10.0.1.3:7000)
        Receiving 1 files, 3 GiB total. Already received 0 files (0.00%), 1.5 GiB total (50.00%)
        Sending 2 files, 500 total. Already sent 2 files (100.00%), 500 total (100.00%)
Read Repair Statistics:
Attempted: 0
"""
    mode, lines, sessions = parse_netstats(out)
    assert mode == "LEAVING"
    assert len(lines) == 8
    assert [(s["operation"], s["peer"], s["direction"], s["bytes_done"], s["bytes_total"]) for s in sessions] == [
        ("Unbootstrap", "10.0.0.2", "sending", 1000, 4000),
        ("Unbootstrap", "10.0.0.3", "receiving", 3 * 1024 ** 3 // 2, 3 * 1024 ** 3),
        ("Unbootstrap", "10.0.0.3", "sending", 500, 500)]
    assert [(f["table"], f["done"], f["total"]) for f in sessions[0]["files"]] == [
        ("orders.items", 1000, 1000), ("orders.items", 10, 2000)]


def test_several_plans():
    out = """Mode: NORMAL
Repair 11111111-2222-3333-4444-555555555555
    /10.0.0.2:7000
        Receiving 1 files, 10 total. Already received 0 files (0.00%), 0 total (0.00%)
Restore replica count 66666666-7777-8888-9999-000000000000
    /10.0.0.4:7000
        Receiving 2 files, 20 total. Already received 1 files (50.00%), 10 total (50.00%)
"""
    sessions = parse_netstats(out)[2]
    assert [(s["operation"], s["plan_id"][:4], s["peer"]) for s in sessions] == [
        ("Repair", "1111", "10.0.0.2"), ("Restore replica count", "6666", "10.0.0.4")]


def test_down_node():
    assert parse_netstats("") == ("", [], [])


def test_table_of():
    assert table_of("/var/lib/cassandra/data/ks/tbl-0123456789abcdef0123456789abcdef/nb-1-big-Data.db") == "ks.tbl"
    assert table_of("/var/lib/cassandra/data/ks/my-table-0123456789abcdef0123456789abcdef/nb-1-big-Index.db") == "ks.my-table"
    assert table_of("ks/tbl-0") == "ks.tbl"
    assert table_of("nb-1-big-Data.db") == ""


def test_real_50_bootstrap_receiving():
    mode, lines, sessions = parse_netstats(load_fixture("nodetool_netstats_50_bootstrap_receiving.txt"))
    assert mode == "JOINING"
    assert [(s["peer"], s["direction"], s["files_done"], s["files_total"], s["bytes_done"], s["bytes_total"])
            for s in sessions] == [("172.29.0.4", "receiving", 1, 2, 10872197, 158151511),
                                   ("172.29.0.3", "receiving", 1, 2, 10558319, 137813054)]
    assert [(f["table"], f["done"], f["total"]) for f in sessions[0]["files"]] == [
        ("keyspace1.standard1", 10864965, 158144279), ("e2e.t", 7232, 7232)]


def test_real_50_bootstrap_sending():
    mode, lines, sessions = parse_netstats(load_fixture("nodetool_netstats_50_bootstrap_sending.txt"))
    assert mode == "NORMAL"
    assert [(s["operation"], s["peer"], s["direction"], s["bytes_done"]) for s in sessions] == [
        ("Bootstrap", "172.29.0.5", "sending", 8789056)]
    assert [f["table"] for f in sessions[0]["files"]] == ["e2e.t", "keyspace1.standard1"]


def test_real_40_and_41_bootstrap_receiving():
    # 4.0.x and 4.1.11: "N bytes" sizes, received files named keyspace/table-N, entire SSTable
    # components (system_auth) under their full path
    for name, files_done, bytes_done, tables in (
            ("40_bootstrap_receiving_early", 0, 25632576, {"ks.items"}),
            ("40_bootstrap_receiving_late", 8, 47859601, {"ks.items", "system_auth.roles"}),
            ("41_bootstrap_receiving_early", 0, 7049187, {"ks.items", "ks.orders"}),
            ("41_bootstrap_receiving_late", 3, 47132521, {"ks.items", "ks.orders"})):
        mode, lines, sessions = parse_netstats(load_fixture("nodetool_netstats_%s.txt" % name))
        assert mode == "JOINING"
        assert [(s["operation"], s["peer"], s["direction"], s["files_done"], s["bytes_done"]) for s in sessions] == [
            ("Bootstrap", "192.168.0.2", "receiving", files_done, bytes_done)]
        assert set(f["table"] for f in sessions[0]["files"]) == tables


def test_real_40_and_41_bootstrap_sending():
    for name, peer_total, bytes_done in (("40", 95740912, 42624463), ("41", 96183376, 89245977)):
        mode, lines, sessions = parse_netstats(load_fixture("nodetool_netstats_%s_bootstrap_sending.txt" % name))
        assert mode == "NORMAL"
        assert [(s["peer"], s["direction"], s["bytes_total"], s["bytes_done"]) for s in sessions] == [
            ("192.168.0.3", "sending", peer_total, bytes_done)]
        assert set(f["table"] for f in sessions[0]["files"]) <= {"ks.items", "ks.orders"}
