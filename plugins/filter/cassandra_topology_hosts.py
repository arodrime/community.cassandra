# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_topology_hosts: what the topology playbook knows of each host of the cluster's group, the input of
cassandra_topology_plan and cassandra_topology_screen."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible.plugins.filter.core import to_bool  # the bool filter, as the expressions these replace used


def _reset(plan):
    """The reset plan of a host when it does something (deletes, stops or disables), else None."""
    if plan is None:
        return None
    return plan if plan.get("delete") or to_bool(plan.get("stop")) or to_bool(plan.get("disable")) else None


def cassandra_topology_hosts(names, hostvars, play_hosts, absent, single):
    """names: the hosts of the cluster's group; hostvars; play_hosts: the hosts the play reached
    (ansible_play_hosts_all); absent: the hosts marked absent (to remove); single: one token per node.
    -> [{name, absent, reachable, address, dc, rack, seed, names, live, entry, running, state, refused, single,
    token, info, checks, reset}], from each host's preflight facts (_cassandra_preflight) and the checks run on it.
    A host marked absent is reachable when topology reached it (_cassandra_topology_reachable); its state is its
    decommission's (cassandra_leaving_node), a host to add its new node state."""
    out = []
    for name in names:
        v = hostvars[name]
        p = v.get("_cassandra_preflight") or {}
        gone = name in absent
        check = v.get("_cassandra_new_node_check") or {}
        out.append({
            "name": name, "absent": gone,
            "reachable": to_bool(v.get("_cassandra_topology_reachable", False)) if gone else name in play_hosts,
            "address": p.get("ring_address") or v.get("_cassandra_topology_address", ""),
            "dc": p.get("cassandra_dc", "?"), "rack": p.get("cassandra_rack", "?"),
            "seed": to_bool(v.get("_cassandra_service_is_seed", False)),
            "names": v.get("_cassandra_service_names", [name]), "live": p.get("live_seeds", ""),
            "entry": p.get("address", ""), "running": to_bool(v.get("_cassandra_preflight_running", False)),
            "state": (v.get("cassandra_leaving_node") or {}).get("state") if gone else v.get("cassandra_new_node_state"),
            "refused": v.get("_cassandra_topology_refused", ""), "single": single,
            "token": p.get("cassandra_initial_token", ""),
            "info": [] if gone else check.get("info", []), "checks": [] if gone else check.get("warnings", []),
            "reset": None if gone else _reset(v.get("_cassandra_node_reset_plan")),
        })
    return out


class FilterModule(object):
    def filters(self):
        return {"cassandra_topology_hosts": cassandra_topology_hosts}
