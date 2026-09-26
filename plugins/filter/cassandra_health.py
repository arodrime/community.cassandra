# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_health_problems: the problems found by the cluster health check
(roles/cassandra_service/tasks/cluster_health.yml), as readable sentences."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


def _nodes(cluster_status):
    return [n for dc in cluster_status.values() for n in dc.get("nodes", [])]


def _error(result):
    msg = result.get("msg", "unreachable")
    stderr = (result.get("stderr") or "").strip()
    return "%s (%s)" % (msg, stderr.splitlines()[-1]) if stderr and stderr not in msg else msg


def cassandra_health_problems(views, expected, node, gossip=None, binary=None, netstats=None, schema=None, ports=None):
    """views: [{'from': host, 'result': cassandra_status result}]; the others
    are this node's registered results (ports: a wait_for loop over
    {'name', 'host', 'port'} items). Returns a list of problems."""
    problems = []
    for view in views:
        result = view["result"]
        if not result.get("cluster_status"):
            problems.append("nodetool status failed on %s: %s" % (view["from"], _error(result)))
            continue
        nodes = _nodes(result["cluster_status"])
        for n in nodes:
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
        if netstats.get("rc") != 0:
            problems.append("nodetool netstats failed on %s: %s" % (node, (netstats.get("stderr") or netstats.get("stdout") or "").strip()))
        elif "Not sending any streams" not in netstats.get("stdout", ""):
            problems.append("streams in progress on %s (nodetool netstats)" % node)
    if schema is not None and schema.get("failed"):
        problems.append("schema disagreement: %s" % schema.get("msg", ""))
    return problems


class FilterModule(object):
    def filters(self):
        return {"cassandra_health_problems": cassandra_health_problems}
