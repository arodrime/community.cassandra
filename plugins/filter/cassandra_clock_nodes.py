# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_clock_nodes: the nodes whose clock was read (clocks_read.yml), as cassandra_clock_verdicts takes
them."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible.plugins.filter.core import to_bool  # the bool filter, as the expressions these replace used


def _flag(v, name, default):
    return to_bool(v.get(name, default))


def cassandra_clock_nodes(hosts, hostvars):
    """hosts: the play's hosts; hostvars. -> [{node, probe, samples, running, manage, unmanaged_by, offline,
    addresses}] for the hosts whose clock was read (_cassandra_clock_read_ok): what clock_probe.yml read
    (_cassandra_clock, _cassandra_clock_samples), whether Cassandra runs there (_cassandra_clock_running), whether
    cassandra_linux manages its time sync (cassandra_linux_manage and cassandra_linux_timesync, else which one is
    off), cassandra_offline, and the names and addresses other nodes may give it."""
    nodes = []
    for name in hosts:
        v = hostvars[name]
        if not _flag(v, "_cassandra_clock_read_ok", False):
            continue
        linux = _flag(v, "cassandra_linux_manage", True)
        nodes.append({
            "node": name, "probe": v["_cassandra_clock"], "samples": v["_cassandra_clock_samples"],
            "running": _flag(v, "_cassandra_clock_running", False),
            "manage": linux and _flag(v, "cassandra_linux_timesync", True),
            "unmanaged_by": "cassandra_linux_timesync" if linux else "cassandra_linux_manage",
            "offline": _flag(v, "cassandra_offline", False),
            "addresses": [name, v.get("ansible_host", name), (v.get("_cassandra_preflight") or {}).get("ring_address", "")]
            + (v.get("ansible_facts") or {}).get("all_ipv4_addresses", []),
        })
    return nodes


class FilterModule(object):
    def filters(self):
        return {"cassandra_clock_nodes": cassandra_clock_nodes}
