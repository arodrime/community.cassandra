from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_seed_list import cassandra_seed_list


@pytest.mark.parametrize("seeds, expected", [
    ("10.100.100.1:7000,10.100.100.2", [{"host": "10.100.100.1", "port": 7000}, {"host": "10.100.100.2", "port": None}]),
    # the list form, spaces and empty entries as people write them
    (["10.100.100.1:7001", " Node2.Example.com "],
     [{"host": "10.100.100.1", "port": 7001}, {"host": "node2.example.com", "port": None}]),
    (" 10.100.100.1 , ,10.100.100.2,", [{"host": "10.100.100.1", "port": None}, {"host": "10.100.100.2", "port": None}]),
    # IPv6: a bare address keeps all its groups, a port needs brackets
    ("fe80::1", [{"host": "fe80::1", "port": None}]),
    ("::1", [{"host": "::1", "port": None}]),
    ("fe80::1%eth0", [{"host": "fe80::1%eth0", "port": None}]),
    ("[fe80::1]:7000", [{"host": "fe80::1", "port": 7000}]),
    ("[fe80::1]", [{"host": "fe80::1", "port": None}]),
    ("FE80::A", [{"host": "fe80::a", "port": None}]),
    (["10.100.100.1,10.100.100.2:7001"], [{"host": "10.100.100.1", "port": None}, {"host": "10.100.100.2", "port": 7001}]),
    ("10.100.100.1:,[fe80::1]:", [{"host": "10.100.100.1", "port": None}, {"host": "fe80::1", "port": None}]),
    ("", []),
    ([], []),
    (None, []),
])
def test_seed_list(seeds, expected):
    assert cassandra_seed_list(seeds) == expected


@pytest.mark.parametrize("seed", ["10.100.100.1:abc", "node1:7000:x", "fe80::1::2", "[fe80::1]:x", "10.100.100.1:0", "10.100.100.1:65536",
                                  7000, {"host": "10.100.100.1"}])
def test_invalid_seed(seed):
    with pytest.raises(AnsibleFilterError):
        cassandra_seed_list(seed)
