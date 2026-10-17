from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_clocks import cassandra_clock_nodes


def test_only_the_clocks_read_with_what_they_are_known_by():
    hostvars = {
        "n1": {"_cassandra_clock_read_ok": True, "_cassandra_clock": {"offset": 0.1}, "_cassandra_clock_samples": [],
               "cassandra_linux_timesync": "false", "ansible_host": "10.0.0.1",
               "_cassandra_preflight": {"ring_address": "192.168.0.1"}, "ansible_facts": {"all_ipv4_addresses": ["10.0.0.1"]}},
        "n2": {"_cassandra_clock_read_ok": False},
        "n3": {"_cassandra_clock_read_ok": "yes", "_cassandra_clock": {}, "_cassandra_clock_samples": [1],
               "cassandra_linux_manage": False, "cassandra_offline": "true"},
    }
    n1, n3 = cassandra_clock_nodes(["n1", "n2", "n3"], hostvars)
    assert (n1["node"], n1["manage"], n1["unmanaged_by"], n1["running"], n1["offline"]) == (
        "n1", False, "cassandra_linux_timesync", False, False)
    assert n1["addresses"] == ["n1", "10.0.0.1", "192.168.0.1", "10.0.0.1"]
    assert (n3["manage"], n3["unmanaged_by"], n3["offline"], n3["addresses"]) == (False, "cassandra_linux_manage", True,
                                                                                  ["n3", "n3", ""])
