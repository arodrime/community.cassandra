# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_ring_report: the cassandra_status module's cluster_status as
readable lines for the status playbook: a verdict line, the nodes per
datacenter as nodetool status shows them (the ones not UN marked), a summary
line per rack and per datacenter, and where the inventory and the ring
differ."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ipaddress
import re
import socket

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

_STATES = [("U", "up"), ("D", "down"), ("J", "joining"), ("L", "leaving"), ("M", "moving")]
_IP = re.compile(r"^[0-9.]+$|:")


def _bytes(load):
    """nodetool's Load (FileUtils.stringifyFileSize, a comma as decimal mark in some locales) in bytes."""
    return out.parse_size(load)


def _ip(address):
    """One spelling per address: nodetool prints IPv6 uncompressed
    (2001:db8:0:0:0:0:0:1), the facts and the resolver compressed."""
    try:
        return ipaddress.ip_address(u"%s" % address.split("%")[0]).compressed
    except ValueError:
        return address


def _resolved(name):
    try:
        return [info[4][0] for info in socket.getaddrinfo(name, None)]
    except (socket.error, UnicodeError):
        return []


def _host_of(inventory, ring):
    """ring address -> inventory host: every address a host is known by, then,
    only while some ring address has no host, what the names of the hosts
    still unmatched resolve to on the controller (a host Ansible can't reach
    has no facts)."""
    owner = {}
    for host in inventory:
        for address in inventory[host]:
            if address:
                owner.setdefault(_ip(address), host)
    matched = set(owner[a] for a in ring if a in owner)
    for host in inventory:
        if host in matched or all(a in owner for a in ring):
            continue
        for name in inventory[host]:
            if name and not _IP.search(name):
                for address in _resolved(name):
                    owner.setdefault(_ip(address), host)
    return owner


def _table(rows):
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]


def _counts(nodes):
    counts = dict((word, 0) for dummy, word in _STATES)
    for n in nodes:
        for letter, word in _STATES:
            # U/D: the status (first letter); J/L/M: the state (second)
            if letter == (n["status"] if letter in "UD" else n["state"]):
                counts[word] += 1
    return counts


def _summary(name, nodes):
    """"rack1: 3 nodes, 2 up, 1 down; load 1.5 GiB (1 unknown)"."""
    counts = _counts(nodes)
    sizes = [_bytes(n["load"]) for n in nodes]
    unknown = sizes.count(None)
    states = ", ".join("%d %s" % (counts[word], word) for dummy, word in _STATES if counts[word] or word in ("up", "down"))
    return "%s: %s, %s; load %s%s" % (name, out.plural(len(nodes), "node"), states,
                                      out.size(sum(x for x in sizes if x is not None)),
                                      " (%d unknown)" % unknown if unknown else "")


def _verdict(cluster_status, cluster, seen_from, matches, members):
    """"my_cluster  1 DOWN  (seen from node1, ring = inventory, 3 nodes)"; OK
    when every node is UN."""
    nodes = [n for dc in cluster_status.values() for n in dc.get("nodes", [])]
    counts = _counts(nodes)
    problems = ["%d %s" % (counts[word], word.upper()) for dummy, word in _STATES if word != "up" and counts[word]]
    where = ["seen from %s" % seen_from] if seen_from else []
    where.append(("ring = %s" % members) if matches else "ring and %s differ" % members)
    where.append(out.plural(len(nodes), "node"))
    return "  ".join(x for x in [cluster, ", ".join(problems) or "OK", "(%s)" % ", ".join(where)] if x)


def cassandra_ring_report(cluster_status, inventory, unreachable=None, limited=False, group="the inventory",
                          outside=None, absent=None, cluster="", seen_from=""):
    """cluster_status: the cassandra_status module's. inventory: {host:
    [addresses and names it is known by]} of the hosts of the run. unreachable:
    the hosts Ansible could not reach. limited: the play runs on part of the
    group (--limit). group: the group of the run, as the report names it.
    outside: {host: [addresses]} of the other hosts of the inventory, matched
    by address only (no facts, no name resolution for them). absent: the
    hosts of outside marked cassandra_node_state: absent (topology removes
    them from the ring). cluster: the cluster's name, seen_from: the node
    the ring was read from, for the verdict line (none without either).
    Returns the report as a list of lines."""
    cluster_status = cluster_status or {}
    owner = _host_of(inventory, [_ip(n["address"]) for dc in cluster_status
                                 for n in cluster_status[dc].get("nodes", [])])
    elsewhere = {}
    for host in outside or {}:
        for address in outside[host]:
            if address and _IP.search(address):
                elsewhere.setdefault(_ip(address), host)
    lines = []
    in_ring = set()
    stray = []
    known = []
    leaving = []
    for dc in sorted(cluster_status):
        nodes = cluster_status[dc].get("nodes", [])
        rows = [["--", "Address", "Load", "Tokens", "Owns", "Host ID", "Rack", "Inventory", ""]]
        for n in nodes:
            host = owner.get(_ip(n["address"]))
            if host is None and elsewhere.get(_ip(n["address"])) in (absent or []):
                leaving.append("%s = %s (%s, %s%s)" % (n["address"], elsewhere[_ip(n["address"])], dc, n["status"], n["state"]))
            elif host is None and _ip(n["address"]) in elsewhere:
                known.append("%s = %s (%s, %s%s)" % (n["address"], elsewhere[_ip(n["address"])], dc, n["status"], n["state"]))
            elif host is None:
                stray.append("%s (%s, %s%s)" % (n["address"], dc, n["status"], n["state"]))
            else:
                in_ring.add(host)
            mark = "" if n["status"] + n["state"] == "UN" else out.ARROW + " " + (
                "down" if n["status"] == "D" else dict(_STATES).get(n["state"], n["status"] + n["state"]))
            rows.append([n["status"] + n["state"], n["address"], n["load"] or "?", n["tokens"] or "",
                         n["owns"] or "", n["host_id"], n["rack"], host or "-", mark])
        lines.append("Datacenter: %s" % dc)
        lines.extend("  " + line for line in _table(rows))
        racks = sorted(set(n["rack"] for n in nodes))
        if len(racks) > 1:
            lines.extend("  " + _summary(rack, [n for n in nodes if n["rack"] == rack]) for rack in racks)
        lines.append("  " + _summary(dc, nodes))
    missing = [h for h in inventory if h not in in_ring]
    unreachable = set(unreachable or [])
    run = "this run (--limit)" if limited else group
    if missing:
        lines.append("In %s, not in the ring: " % run + ", ".join(
            h + (" (unreachable)" if h in unreachable else "") for h in missing))
    if known:
        lines.append("In the ring and the inventory, not in %s: " % run + ", ".join(known))
    if leaving:
        lines.append("Marked cassandra_node_state: absent, still in the ring (the playbook topology removes them): "
                     + ", ".join(leaving))
    if stray:
        lines.append("In the ring, not in %s%s: " % (run, "" if outside is None else " nor found elsewhere in the inventory")
                     + ", ".join(stray))
    matches = not missing and not stray and not known and not leaving
    if matches and not (cluster or seen_from):  # else said by the verdict
        lines.append("The ring and %s match (%s)" % (run, out.plural(len(inventory), "node")))
    if cluster or seen_from:
        lines.insert(0, _verdict(cluster_status, cluster, seen_from, matches, "inventory" if run == group else run))
    return lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_ring_report": cassandra_ring_report}
