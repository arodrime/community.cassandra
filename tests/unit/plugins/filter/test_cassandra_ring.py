from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import socket

import pytest

from ansible_collections.community.cassandra.plugins.filter import cassandra_ring
from ansible_collections.community.cassandra.plugins.filter.cassandra_ring import cassandra_ring_report
from ansible_collections.community.cassandra.plugins.modules.cassandra_status import cluster_up_down

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "modules", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return cluster_up_down(f.read())


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Names resolve only through this table, never through the real DNS."""
    names = {"node9.example": ["10.0.0.9"]}

    def getaddrinfo(name, port):
        if name not in names:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in names[name]]
    monkeypatch.setattr(cassandra_ring.socket, "getaddrinfo", getaddrinfo)


def node(address, status="U", state="N", load="1.0 GiB", rack="r1"):
    return {"address": address, "status": status, "state": state, "load": load, "tokens": "16",
            "owns": "33.3%", "host_id": "id-" + address, "rack": rack}


def ring(*nodes, **dcs):
    status = {"dc1": {"nodes": list(nodes)}} if nodes else {}
    status.update((dc, {"nodes": n}) for dc, n in dcs.items())
    return status


def test_matching_ring_and_inventory():
    lines = cassandra_ring_report(ring(node("10.0.0.1"), node("10.0.0.2", load="512.0 MiB")),
                                  {"n1": ["n1", "10.0.0.1"], "n2": ["10.0.0.2"]})
    assert lines == [
        "Datacenter: dc1",
        "  --  Address   Load       Tokens  Owns   Host ID      Rack  Inventory",
        "  UN  10.0.0.1  1.0 GiB    16      33.3%  id-10.0.0.1  r1    n1",
        "  UN  10.0.0.2  512.0 MiB  16      33.3%  id-10.0.0.2  r1    n2",
        "  dc1: 2 node(s), 2 up, 0 down; load 1.50 GiB",
        "The ring and the inventory match (2 node(s))",
    ]


def test_summary_counts_every_state_and_unknown_load():
    lines = cassandra_ring_report(
        ring(node("10.0.0.1"), node("10.0.0.2", "D", load="?"), node("10.0.0.3", state="J", load="100 KiB"),
             node("10.0.0.4", state="L"), node("10.0.0.5", "D", "L", load="1,5 GiB"), node("10.0.0.6", state="M")),
        dict(("n%d" % i, ["10.0.0.%d" % i]) for i in range(1, 7)))
    assert lines[-2] == "  dc1: 6 node(s), 4 up, 2 down, 1 joining, 2 leaving, 1 moving; load 4.50 GiB (1 unknown)"


def test_mismatches_both_ways():
    lines = cassandra_ring_report(ring(node("10.0.0.1"), node("10.0.0.7", "D")),
                                  {"n1": ["10.0.0.1"], "n2": ["10.0.0.2"], "n3": ["n3"]}, unreachable=["n3"])
    assert "  DN  10.0.0.7  1.0 GiB  16      33.3%  id-10.0.0.7  r1    -" in lines
    assert lines[-2:] == ["In the inventory, not in the ring: n2, n3 (unreachable)",
                          "In the ring, not in the inventory: 10.0.0.7 (dc1, DN)"]


def test_unreachable_host_found_by_its_resolved_name():
    lines = cassandra_ring_report(ring(node("10.0.0.9", "D")), {"node9.example": ["node9.example", ""]},
                                  unreachable=["node9.example"])
    assert lines[1:3] == ["  --  Address   Load     Tokens  Owns   Host ID      Rack  Inventory",
                          "  DN  10.0.0.9  1.0 GiB  16      33.3%  id-10.0.0.9  r1    node9.example"]
    assert lines[-1] == "The ring and the inventory match (1 node(s))"


def test_multi_dc_fixture_grouped_by_dc():
    lines = cassandra_ring_report(fixture("nodetool_status_multi_dc.txt"),
                                  {"a": ["10.0.0.1"], "b": ["10.0.1.1"]})
    assert lines == [
        "Datacenter: datacenter1",
        "  --  Address   Load     Tokens  Owns   Host ID                               Rack   Inventory",
        "  UN  10.0.0.1  1.0 GiB  16      50.0%  aaaaaaaa-1111-1111-1111-111111111111  rack1  a",
        "  datacenter1: 1 node(s), 1 up, 0 down; load 1.00 GiB",
        "Datacenter: datacenter2",
        "  --  Address   Load     Tokens     Owns   Host ID                               Rack   Inventory",
        "  UN  10.0.1.1  2.0 GiB  123456789  50.0%  bbbbbbbb-2222-2222-2222-222222222222  rack2  b",
        "  datacenter2: 1 node(s), 1 up, 0 down; load 2.00 GiB",
        "The ring and the inventory match (2 node(s))",
    ]


def test_token_per_node_fixture_down_node_unknown_load():
    lines = cassandra_ring_report(fixture("nodetool_status_token_per_node.txt"),
                                  {"a": ["10.100.100.136"], "b": ["10.100.100.137"]})
    assert "  datacenter1: 2 node(s), 1 up, 1 down; load 648.19 GiB (1 unknown)" in lines


def test_small_loads_and_empty_ring():
    assert cassandra_ring_report(ring(node("10.0.0.1", load="512 bytes")), {"n1": ["10.0.0.1"]})[-2] \
        == "  dc1: 1 node(s), 1 up, 0 down; load 512 bytes"
    assert cassandra_ring_report({}, {"n1": ["10.0.0.1"]}) == ["In the inventory, not in the ring: n1"]


def test_ipv6_ring_address_matches_the_compressed_facts():
    lines = cassandra_ring_report(ring(node("2001:db8:0:0:0:0:0:1")), {"a": ["a", "2001:db8::1", "fe80::1%eth0"]})
    assert lines[2].split()[-1] == "a"
    assert lines[-1] == "The ring and the inventory match (1 node(s))"


def test_explicit_address_wins_first_host_wins_and_ips_never_resolved(monkeypatch):
    asked = []
    real = cassandra_ring.socket.getaddrinfo

    def spy(name, port):
        asked.append(name)
        return real(name, port)
    monkeypatch.setattr(cassandra_ring.socket, "getaddrinfo", spy)
    # node9.example resolves to 10.0.0.9, but n1 is known by that address;
    # 10.0.0.7 is nobody's, so the unmatched names are resolved
    lines = cassandra_ring_report(ring(node("10.0.0.9"), node("10.0.0.5"), node("10.0.0.7")),
                                  {"n1": ["10.0.0.9", "fe80::1"], "node9.example": ["node9.example"],
                                   "n2": ["10.0.0.5"], "n3": ["10.0.0.5"]})
    assert [line.split()[-1] for line in lines[2:4]] == ["n1", "n2"]
    assert asked == ["node9.example"]


def test_no_dns_when_every_ring_address_is_known(monkeypatch):
    def fail(name, port):
        raise AssertionError("resolved " + name)
    monkeypatch.setattr(cassandra_ring.socket, "getaddrinfo", fail)
    lines = cassandra_ring_report(ring(node("10.0.0.1")), {"n1": ["n1", "10.0.0.1"], "n2": ["n2"]})
    assert lines[-1] == "In the inventory, not in the ring: n2"


def test_limited_run_labels_the_other_ring_nodes():
    lines = cassandra_ring_report(ring(node("10.0.0.1"), node("10.0.0.2")), {"n1": ["10.0.0.1"]}, limited=True)
    assert lines[-1] == "In the ring, not in this run (--limit): 10.0.0.2 (dc1, UN)"


def test_sub_group_named_and_other_inventory_hosts_told_apart():
    # cassandra_hosts=dc1_nodes, a sub-group: a ring node that is another host of the inventory is named as such
    lines = cassandra_ring_report(
        ring(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.9", "D")), {"n1": ["10.0.0.1"], "n3": ["10.0.0.3"]},
        group="group dc1_nodes", outside={"n2": ["n2", "10.0.0.2"], "other": ["other", ""]})
    assert "  UN  10.0.0.2  1.0 GiB  16      33.3%  id-10.0.0.2  r1    -" in lines
    assert lines[-3:] == ["In group dc1_nodes, not in the ring: n3",
                          "In the ring and the inventory, not in group dc1_nodes: 10.0.0.2 = n2 (dc1, UN)",
                          "In the ring, not in group dc1_nodes nor found elsewhere in the inventory: 10.0.0.9 (dc1, DN)"]
    lines = cassandra_ring_report(ring(node("10.0.0.1")), {"n1": ["10.0.0.1"]}, group="group dc1_nodes", outside={})
    assert lines[-1] == "The ring and group dc1_nodes match (1 node(s))"


def test_other_hosts_matched_by_address_only(monkeypatch):
    # no resolution of the names of the hosts outside the run (a big inventory would be slow)
    monkeypatch.setattr(cassandra_ring.socket, "getaddrinfo", lambda name, port: pytest.fail("resolved " + name))
    lines = cassandra_ring_report(ring(node("10.0.0.1"), node("2001:db8:0:0:0:0:0:2")), {"n1": ["10.0.0.1"]},
                                  outside={"n2": ["n2.example", "2001:db8::2"]}, limited=True)
    assert lines[-1] == "In the ring and the inventory, not in this run (--limit): 2001:db8:0:0:0:0:0:2 = n2 (dc1, UN)"


def test_hosts_marked_absent_still_in_the_ring():
    outside = {"n3": ["n3", "10.0.0.3"], "m1": ["m1", "10.0.0.9"]}
    lines = cassandra_ring_report(ring(node("10.0.0.1"), node("10.0.0.3", state="L"), node("10.0.0.9")),
                                  {"n1": ["10.0.0.1"]}, group="group prod", outside=outside, absent=["n3"])
    assert lines[-2:] == [
        "In the ring and the inventory, not in group prod: 10.0.0.9 = m1 (dc1, UN)",
        "Marked cassandra_node_state: absent, still in the ring (the playbook topology removes them): 10.0.0.3 = n3 (dc1, UL)"]
    # gone from the ring: the ring and the group match
    lines = cassandra_ring_report(ring(node("10.0.0.1")), {"n1": ["10.0.0.1"]}, group="group prod",
                                  outside=outside, absent=["n3"])
    assert lines[-1] == "The ring and group prod match (1 node(s))"
