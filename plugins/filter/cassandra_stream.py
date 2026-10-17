# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The progress and the checks of streaming operations (bootstrap, decommission, rebuild, removenode, cleanup).

cassandra_host_addresses: the inventory names of the addresses netstats shows.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


def _host_var(hostvars, host, *path):
    """hostvars[host][path[0]][path[1]]... when it is a string, else "" (also
    for a template left as it is: hostvars gives that of a host without facts,
    e.g. "{{ ansible_facts['default_ipv4']['address'] }}" when Ansible did not
    reach it)."""
    try:
        value = hostvars[host]
        for key in path:
            value = value.get(key) or {}
    except Exception:  # pylint: disable=broad-except  # an undefined or broken template in that host's variables
        return ""
    return value if isinstance(value, str) and "{{" not in value and "{%" not in value else ""


def cassandra_host_addresses(hosts, hostvars):
    """{address: inventory name} of hosts, from what their variables say they
    broadcast or listen on, their ansible_host and default IPv4 fact (netstats
    names the other ends by address). A variable that can't be read is skipped.
    What a host broadcasts or listens on first, for every host, then ansible_host
    and the facts: one host's ansible_host never takes the address another one
    is in the ring by (127.0.0.1 for ccm-like nodes, a stale ansible_host).
    e.g. (["node1", "node2"], {node1: {cassandra_listen_address: "10.0.0.1"}, node2: {ansible_host: "10.0.0.2"}})
    -> {"10.0.0.1": "node1", "10.0.0.2": "node2", "node1": "node1", "node2": "node2"}."""
    names = {}
    for paths in ((("cassandra_extra_settings", "broadcast_address"), ("_cassandra_preflight", "ring_address"),
                   ("_cassandra_preflight", "address"), ("cassandra_listen_address",)),
                  (("ansible_host",), ("ansible_facts", "default_ipv4", "address"))):
        for host in hosts:
            for path in paths:
                address = _host_var(hostvars, host, *path)
                if address and address != "localhost":
                    names.setdefault(address, host)
    for host in hosts:
        names.setdefault(host, host)
    return names


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_host_addresses": cassandra_host_addresses,
        }
