# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_health_problems: the problems found by the cluster health check
(roles/cassandra_service/tasks/cluster_health.yml), as readable sentences.
cassandra_leaving_state: where a node given to decommission_node stands.
cassandra_removal_state: where a dead node given to remove_dead_node stands."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


def _nodes(cluster_status):
    return [n for dc in cluster_status.values() for n in dc.get("nodes", [])]


def _error(result):
    msg = result.get("msg", "unreachable")
    stderr = (result.get("stderr") or "").strip()
    return "%s (%s)" % (msg, stderr.splitlines()[-1]) if stderr and stderr not in msg else msg


def cassandra_health_problems(views, expected, node, gossip=None, binary=None, netstats=None, schema=None, ports=None,
                              down_ok=None, joining_ok=None, leaving_ok=None):
    """views: [{'from': host, 'result': cassandra_status result}]; the others
    are this node's registered results (ports: a wait_for loop over
    {'name', 'host', 'port'} items). down_ok: addresses expected down (a dead
    node being replaced). joining_ok: addresses expected up and joining (a
    bootstrap still running). leaving_ok: addresses expected up and leaving (a
    decommission still running). Returns a list of problems."""
    problems = []
    for view in views:
        result = view["result"]
        if not result.get("cluster_status"):
            problems.append("nodetool status failed on %s: %s" % (view["from"], _error(result)))
            continue
        nodes = _nodes(result["cluster_status"])
        for n in nodes:
            if n["status"] == "D" and n["address"] in (down_ok or []):
                continue
            if n["status"] + n["state"] == "UJ" and n["address"] in (joining_ok or []):
                continue
            if n["status"] + n["state"] == "UL" and n["address"] in (leaving_ok or []):
                continue
            if n["status"] != "U" or n["state"] != "N":
                problems.append("%s (%s) is %s%s, seen from %s" % (n["address"], n["rack"], n["status"], n["state"], view["from"]))
        if len(nodes) != int(expected):
            problems.append("the ring has %d nodes, the inventory %d (seen from %s): a node is missing from the ring, "
                            "or the inventory is incomplete" % (len(nodes), int(expected), view["from"]))
    for port in (ports or {}).get("results", []):
        if port.get("failed"):
            item = port["item"]
            problems.append("%s port %s is not answering on %s (%s)" % (item["name"], item["port"], node, item["host"]))
    if gossip is not None and not gossip.get("is_up"):
        problems.append("gossip is not running on %s" % node)
    if binary is not None and not binary.get("is_up"):
        problems.append("the native transport (CQL) is not running on %s" % node)
    if netstats is not None:
        if netstats.get("failed") or "streaming" not in netstats:
            problems.append("nodetool netstats failed on %s: %s" % (node, _error(netstats)))
        elif netstats["streaming"]:
            problems.append("streams in progress on %s (nodetool netstats)" % node)
    if schema is not None and schema.get("failed"):
        problems.append("schema disagreement: %s" % schema.get("msg", ""))
    return problems


def _ring_node(cluster_status, address):
    return next((n for n in _nodes(cluster_status or {}) if n["address"] == address), None)


def cassandra_leaving_state(netstats, ring, address):
    """netstats: this node's cassandra_netstats result; ring: cassandra_status
    result from a node that stays; address: this node's address in the ring.
    Returns {'state', 'in_ring', 'reason'}. state: 'normal' (not leaving: the
    usual checks decide), 'leaving' (a decommission in progress, to wait for;
    in_ring false once it has announced it left), 'decommissioned' (gone from
    the ring, Cassandra running or not: to stop and disable), 'failed' (an
    operator's job, reason says why)."""
    mode = (netstats or {}).get("mode") or ""
    if not (ring or {}).get("cluster_status"):
        return {"state": "normal", "in_ring": True, "reason": ""}  # the health check reports it
    seen = _ring_node(ring["cluster_status"], address)
    in_ring = seen is not None
    if mode == "LEAVING":
        return {"state": "leaving", "in_ring": in_ring, "reason": ""}
    if mode == "DECOMMISSIONED" and not in_ring:
        return {"state": "decommissioned", "in_ring": False, "reason": ""}
    if mode == "DECOMMISSIONED":
        return {"state": "failed", "in_ring": True, "reason": (
            "Mode DECOMMISSIONED but still %s%s in the ring: wait a minute (the other nodes drop it after about 30s)"
            " and run this again; if it stays, check nodetool status and gossipinfo on the other nodes."
            % (seen["status"], seen["state"]))}
    if mode == "DECOMMISSION_FAILED":
        return {"state": "failed", "in_ring": in_ring, "reason": (
            "its decommission failed (Mode DECOMMISSION_FAILED): see why in its system.log. Once fixed, nodetool"
            " decommission on it resumes it (the ranges already streamed are skipped), or restart Cassandra on it"
            " to cancel it (it comes back UN) and run decommission_node again.")}
    stopped = "Connection refused" in "%s %s" % ((netstats or {}).get("msg") or "", (netstats or {}).get("stderr") or "")
    down = [n for n in _nodes(ring["cluster_status"]) if n["status"] == "D"]
    if not in_ring and not mode and stopped and not down:
        # decommissioned and stopped since: what is left is to keep it stopped. Not
        # when a node is down: it may be this one, known by another address
        return {"state": "decommissioned", "in_ring": False, "reason": (
            "not in the ring and its nodetool does not answer (%s)" % _error(netstats or {}))}
    return {"state": "normal", "in_ring": in_ring, "reason": ""}


def cassandra_removal_state(ring, address, removal_status):
    """ring: cassandra_status result; address: the dead node's;
    removal_status: {host: stdout of nodetool removenode status on it} for
    every node of the run ('' when it did not answer). Only the node coordinating a
    removal says "Removing token"; the others see the node DL (gossip).
    Returns {'state', 'on'}. state: 'absent' (not in the ring: nothing to
    do), 'resume' (DL and one node, 'on', is removing it: wait for it),
    'busy' (a node is removing another node, several are removing, or other
    nodes are leaving), 'orphan' (DL but no node of the run is coordinating
    its removal: elsewhere, or its coordinator restarted), 'unknown' (no
    ring) or 'start'."""
    if not (ring or {}).get("cluster_status"):
        return {"state": "unknown", "on": ""}
    nodes = _nodes(ring["cluster_status"])
    dead = next((n for n in nodes if n["address"] == address), None)
    if dead is None:
        return {"state": "absent", "on": ""}
    others_leaving = [n for n in nodes if n["state"] == "L" and n["address"] != address]
    removing = sorted(h for h, out in (removal_status or {}).items() if "Removing token" in (out or ""))
    dl = dead["status"] + dead["state"] == "DL"
    if len(removing) == 1 and dl and not others_leaving:
        return {"state": "resume", "on": removing[0]}
    if removing:
        return {"state": "busy", "on": ", ".join(removing)}
    if dl:
        return {"state": "orphan", "on": ""}
    return {"state": "start", "on": ""}


class FilterModule(object):
    def filters(self):
        return {"cassandra_health_problems": cassandra_health_problems,
                "cassandra_leaving_state": cassandra_leaving_state,
                "cassandra_removal_state": cassandra_removal_state}
