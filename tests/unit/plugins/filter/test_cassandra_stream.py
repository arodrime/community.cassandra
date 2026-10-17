from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import cassandra_host_addresses


def test_host_addresses():
    hostvars = {"n1": {"ansible_host": "10.0.0.1", "cassandra_listen_address": "10.1.0.1"},
                "n2": {"ansible_facts": {"default_ipv4": {"address": "10.0.0.2"}}, "cassandra_listen_address": "localhost"},
                "10.0.0.3": {}}
    assert cassandra_host_addresses(["n1", "n2", "10.0.0.3"], hostvars) == {
        "10.1.0.1": "n1", "10.0.0.1": "n1", "n1": "n1", "10.0.0.2": "n2", "n2": "n2", "10.0.0.3": "10.0.0.3"}

    class Broken(dict):
        def get(self, key, default=None):
            raise ValueError("an undefined variable")
    assert cassandra_host_addresses(["n4"], {"n4": Broken()}) == {"n4": "n4"}


def test_host_addresses_leave_out_a_template_hostvars_could_not_render():
    # an unreachable host (no facts): hostvars gives its listen address template as it is
    hostvars = {"n3": {"cassandra_listen_address": "{{ ansible_facts['default_ipv4']['address'] }}"}}
    assert cassandra_host_addresses(["n3"], hostvars) == {"n3": "n3"}


def test_host_addresses_what_a_host_listens_on_before_another_ones_ansible_host():
    # n3's ansible_host (stale, or 127.0.0.1 for ccm-like nodes) is n1's ring address: n1's, whatever the order
    hostvars = {"n3": {"ansible_host": "10.0.0.1", "_cassandra_preflight": {"ring_address": "10.0.0.3"}},
                "n1": {"_cassandra_preflight": {"ring_address": "10.0.0.1"}}}
    names = cassandra_host_addresses(["n3", "n1"], hostvars)
    assert names["10.0.0.1"] == "n1" and names["10.0.0.3"] == "n3"
