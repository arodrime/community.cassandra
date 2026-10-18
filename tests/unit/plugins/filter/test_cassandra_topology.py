from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_seed_change: the seed list change_seeds applies, from the live lists and cassandra_seeds.

from ansible_collections.community.cassandra.plugins.filter.cassandra_topology import FilterModule, cassandra_seed_change
from ansible_collections.community.cassandra.plugins.filter.cassandra_topology import cassandra_seed_host, cassandra_seed_port


def host(i, absent=False, dc="dc1", rack="r1", **extra):
    h = {"name": "node%d" % i, "absent": absent, "address": "10.0.0.%d" % i, "dc": dc, "rack": rack, "seed": False,
         "reachable": True, "running": True, "state": None, "refused": "", "single": False, "token": "", "reset": None}
    h.update(extra)
    return h


def seeded(i, live="10.0.0.1,10.0.0.2", **extra):
    """A host with the names a seed entry may give it and the seed list of its cassandra.yaml."""
    extra.setdefault("names", ["node%d" % i, "10.0.0.%d" % i])
    return host(i, live=live, **extra)


def test_seed_change_replaces_a_seed():
    hosts = [seeded(1), seeded(2, absent=True, rack="r2"), seeded(3), seeded(5, live="", rack="r2")]
    change = cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.5:7000"])
    assert change["new"] == "10.0.0.1,10.0.0.5:7000"
    assert change["old"] == ["10.0.0.1", "10.0.0.2"]
    assert change["old_hosts"] == ["node1", "node2"]
    assert change["removed"] == ["node2 (dc1/r2)"] and change["added"] == ["node5 (dc1/r2)"]
    assert change["differ"] == ["node1", "node3"]  # node5 has no cassandra.yaml yet, node2 leaves
    assert change["step"] and change["problems"] == []


def test_seed_change_nothing_to_do_and_a_rewrite():
    hosts = [seeded(1), seeded(2), seeded(3)]
    assert cassandra_seed_change(hosts, "10.0.0.1,10.0.0.2")["step"] is False
    assert cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.2"])["step"] is False
    # the same seeds, written another way on one node: rewritten there
    hosts[2]["live"] = "node1,node2"
    change = cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.2"])
    assert change["removed"] == [] and change["added"] == [] and change["differ"] == ["node3"] and change["step"]


def test_seed_change_gives_a_datacenter_left_with_no_seed_its_own():
    # dc2's only seed leaves the list: dc2 keeps nodes, so a seed of its own is picked there (up nodes only,
    # 2 or 3 a datacenter), applied live, said in a warning; none up there: refused
    lives = "10.0.0.1,10.0.0.4"
    hosts = [seeded(1, live=lives, up=True), seeded(2, live=lives, up=True),
             seeded(4, dc="dc2", live=lives, absent=True), seeded(5, dc="dc2", live=lives, up=True),
             seeded(6, dc="dc2", live=lives, up=True, rack="r2"), seeded(7, dc="dc2", live=lives)]
    change = cassandra_seed_change(hosts, ["10.0.0.1"])
    assert change["problems"] == []
    assert change["auto"] == [{"dc": "dc2", "hosts": ["node5", "node6"], "labels": ["node5 (dc2/r1)", "node6 (dc2/r2)"],
                               "entries": ["10.0.0.5", "10.0.0.6"]}]
    assert change["new"] == "10.0.0.1,10.0.0.5,10.0.0.6" and change["wanted"] == "10.0.0.1"
    assert change["step"] and change["added"] == ["node5 (dc2/r1)", "node6 (dc2/r2)"]
    assert change["warnings"] == [
        "dc2 would be left with no seed (node4 leaves the seed list): node5 (dc2/r1), node6 (dc2/r2) made its seeds,"
        " live only: put them in cassandra_seeds (apply_config would put the inventory's list back)"]
    # the next run, the same inventory: the same seeds kept, nothing to apply, still said
    for h in hosts:
        h["live"] = change["new"]
    again = cassandra_seed_change(hosts, ["10.0.0.1"])
    assert again["new"] == change["new"] and not again["step"]
    assert again["warnings"] == [
        "cassandra_seeds has no node of dc2: node5 (dc2/r1), node6 (dc2/r2) kept as its seeds, live only: put them in"
        " cassandra_seeds (apply_config would put the inventory's list back)"]
    # a rerun with node5 (picked before) down: replaced by node7, node6 kept; only node5 said to leave
    hosts[5]["up"] = True
    hosts[3]["up"] = False
    swap = cassandra_seed_change(hosts, ["10.0.0.1"])
    assert swap["new"] == "10.0.0.1,10.0.0.6,10.0.0.7"
    assert swap["warnings"] == [
        "dc2 would be left with no seed (node5 leaves the seed list): node6 (dc2/r2) kept as its seed; node7 (dc2/r1)"
        " made its seed, live only: put them in cassandra_seeds (apply_config would put the inventory's list back)"]
    hosts[5]["up"] = False
    hosts[3]["up"] = True
    # by name when cassandra_seeds names them, its port kept
    assert cassandra_seed_change(hosts, ["node1:7000"])["new"] == "node1:7000,node5:7000,node6:7000"
    # names the inventory does not have (FQDNs the nodes resolve): the address, never an inventory alias
    for h in hosts:
        h["names"].append("%s.example.net" % h["name"])
    assert cassandra_seed_change(hosts, ["node1.example.net"])["new"] == "node1.example.net,10.0.0.5,10.0.0.6"
    # a node of dc2 in the list: nothing picked
    assert cassandra_seed_change(hosts, ["10.0.0.1", "10.0.0.7"])["auto"] == []
    # none up in dc2: refused
    for h in hosts[3:]:
        h["up"] = False
    for h in hosts:
        h["live"] = lives
    assert cassandra_seed_change(hosts, ["10.0.0.1"])["problems"] == [
        "dc2 would be left with no seed (node4 leaves the seed list) and none of its nodes is up to be one: put a node"
        " of dc2 in cassandra_seeds."]
    # dc2 removed for good: no node left there, nothing to seed
    for h in hosts[3:]:
        h["absent"] = True
    assert cassandra_seed_change(hosts, ["10.0.0.1"])["problems"] == []
    assert cassandra_seed_change(hosts, [])["problems"][0].startswith("cassandra_seeds is empty")


def test_seed_change_reads_ipv6_entries():
    # an IPv6 seed, alone, with its port in brackets, or bracketed alone: matched to its host (not cut at a colon)
    v6 = ["2001:db8::1", "2001:db8::2", "2001:db8::4"]
    hosts = [seeded(1, address=v6[0], names=["node1", v6[0]], live="2001:db8::1,[2001:db8::4]:7000", up=True),
             seeded(2, address=v6[1], names=["node2", v6[1]], live="2001:db8::1,[2001:db8::4]:7000", up=True),
             seeded(4, address=v6[2], names=["node4", v6[2]], live="2001:db8::1,[2001:db8::4]:7000", dc="dc2", absent=True),
             seeded(5, address="2001:db8::5", names=["node5", "2001:db8::5"], live="", dc="dc2", up=True)]
    for entry in ("2001:db8::1", "[2001:db8::1]:7000", "[2001:db8::1]"):
        change = cassandra_seed_change(hosts, [entry, "2001:db8::5"])
        assert change["old_hosts"] == ["node1", "node4"] and change["removed"] == ["node4 (dc2/r1)"], entry
        assert change["added"] == ["node5 (dc2/r1)"] and change["auto"] == [], entry
    # the guard of a datacenter left with no seed sees the IPv6 seed leave: dc2 gets seeds of its own
    change = cassandra_seed_change(hosts, ["2001:db8::1"])
    assert change["removed"] == ["node4 (dc2/r1)"] and change["auto"][0]["hosts"] == ["node5"]
    assert change["new"] == "2001:db8::1,2001:db8::5"
    # written with the port the wanted entries carry, in brackets
    change = cassandra_seed_change(hosts, ["[2001:db8::1]:7000"])
    assert change["auto"][0]["entries"] == ["[2001:db8::5]:7000"] and change["new"] == "[2001:db8::1]:7000,[2001:db8::5]:7000"
    # a seed of another datacenter wanted with no port: as is
    assert cassandra_seed_change(hosts, ["2001:db8::1", "10.0.0.9:7000"])["auto"][0]["entries"] == ["2001:db8::5"]


def test_seed_change_with_a_host_of_no_datacenter():
    hosts = [seeded(1, up=True), seeded(2, dc=None, absent=True), seeded(3, dc=None, up=True)]
    change = cassandra_seed_change(hosts, ["10.0.0.1"])  # node2 leaves the list: no crash on its missing dc
    assert change["removed"] == ["node2 (?/r1)"] and change["problems"] == []


def test_seed_host_and_port():
    # what the roles compare a seed entry by (seed_facts.yml) and probe (the wait for the other seeds)
    for entry, host, port in (("10.0.0.1", "10.0.0.1", None), ("10.0.0.1:7000", "10.0.0.1", "7000"), ("node1", "node1", None),
                              ("node1:7000", "node1", "7000"), (" 10.0.0.1:7000 ", "10.0.0.1", "7000"),
                              ("2001:db8::4", "2001:db8::4", None), ("[2001:db8::4]:7000", "2001:db8::4", "7000"),
                              ("[2001:db8::4]", "2001:db8::4", None), ("::1", "::1", None)):
        assert (cassandra_seed_host(entry), cassandra_seed_port(entry)) == (host, port), entry
    assert set(FilterModule().filters()) >= {"cassandra_seed_host", "cassandra_seed_port"}
