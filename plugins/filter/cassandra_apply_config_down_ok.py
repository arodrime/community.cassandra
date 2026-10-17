# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_apply_config_down_ok: the other nodes the health checks of apply_config let be down while a node is
done (their ring addresses)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible.plugins.filter.core import to_bool  # the bool filter, as the expressions these replace used


def cassandra_apply_config_down_ok(nodes, hostvars, host, then, check, batch=False):
    """nodes: the cluster's nodes, in the order apply_config does them; hostvars; host: the node done now; then: its
    cassandra_apply_config_then; check: --check; batch: the nodes done at once (its first play), else one at a time.
    -> the ring addresses of the other nodes planned (cassandra_apply_config_then) to be started (start) or left
    stopped (write), which may be down. One at a time, a start node only until its turn (or when it is not to do,
    or under --check: the nodes before it were not started), and none before a restart, which waits for every
    other node up (except under --check). At once: a write node not before a restart (except under --check)."""
    out = []
    for name in nodes:
        v = hostvars[name]
        if name == host or "cassandra_apply_config_then" not in v:
            continue
        other = v["cassandra_apply_config_then"]
        if batch:
            down = other == "start" or (other == "write" and (then != "restart" or check))
        else:
            later = other == "start" and (nodes.index(name) > nodes.index(host) or check
                                          or not to_bool(v.get("cassandra_apply_config_todo", False)))
            down = later or (other == "write" and then != "restart")
        if down:
            out.append(v["_cassandra_preflight"]["ring_address"])
    return out if batch or then != "restart" or check else []


class FilterModule(object):
    def filters(self):
        return {"cassandra_apply_config_down_ok": cassandra_apply_config_down_ok}
