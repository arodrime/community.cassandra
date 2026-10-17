# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_ring_report: the cassandra_status module's cluster_status as
readable lines for the status playbook: a header line (the cluster, the node
the ring is seen from), the nodes per datacenter as nodetool status shows them
(the ones not UN marked), a summary table of its racks and the datacenter,
where the inventory and the ring differ, and last the verdict (the nodes down,
joining, leaving or moving; under it, a WARNING line if the ring and the
inventory differ). Blank lines between the blocks."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
import socket

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import compressed_address as _ip

_STATES = [("U", "up"), ("D", "down"), ("J", "joining"), ("L", "leaving"), ("M", "moving")]
_IP = re.compile(r"^[0-9.]+$|:")
_DIFFER = "WARNING - ring and inventory differ"  # the verdict's second line, when they do


def _resolved(name):
    try:
        return [info[4][0] for info in socket.getaddrinfo(name, None)]
    except (socket.error, UnicodeError):
        return []


def hosts_by_address(inventory, ring):
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
    """["rack1", "3 nodes", "2 up, 1 down", "1.5 GiB", " (1 unknown)"]: a row of the summary table."""
    counts = _counts(nodes)
    sizes = [out.parse_size(n["load"]) for n in nodes]
    unknown = sizes.count(None)
    states = ", ".join("%d %s" % (counts[word], word) for dummy, word in _STATES if counts[word] or word == "up")
    return [name, out.plural(len(nodes), "node"), states, out.size(sum(x for x in sizes if x is not None)),
            " (%d unknown)" % unknown if unknown else ""]


def _summaries(rows):
    """The summary rows, in columns, the loads aligned to the right."""
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return ["  %s   %s   %s   load %s%s" % (
        name.ljust(widths[0]), count.ljust(widths[1]), states.ljust(widths[2]), load.rjust(widths[3]), unknown)
        for name, count, states, load, unknown in rows]


def _header(cluster, seen_from, matches, members, total):
    """"my_cluster  (seen from node1, ring = inventory, 3 nodes)"."""
    where = ["seen from %s" % seen_from] if seen_from else []
    where.append(("ring = %s" % members) if matches else "ring and %s differ" % members)
    where.append(out.plural(total, "node"))
    return "  ".join(x for x in [cluster, "(%s)" % ", ".join(where)] if x)


def _verdict(nodes, differ, unreached=()):
    """The verdict lines: "ALL 3 NODES UP" or "1 NODE DOWN: node2 (dc1/rack_b)",
    then the nodes joining, leaving or moving; under it, a ring that differs
    from the inventory as a warning line of its own (_DIFFER). nodes: (name,
    datacenter, node) of each node of the ring. unreached: the hosts Ansible
    did not reach: "1 NODE NOT REACHED: node3" (except the ones down), never
    ALL UP then (the ring's view, gossip, can be late)."""
    down = ["%s (%s/%s)" % (name, dc, n["rack"]) for name, dc, n in nodes if n["status"] == "D"]
    unreached = [h for h in unreached if h not in [name.rstrip("?") for name, dc, n in nodes if n["status"] == "D"]]
    parts = [("%s DOWN: %s" % (out.plural(len(down), "NODE", "NODES"), ", ".join(down)))] if down else []
    if unreached:
        parts.append("%s NOT REACHED: %s" % (out.plural(len(unreached), "NODE", "NODES"), ", ".join(unreached)))
    parts = parts or ["ALL %s UP" % out.plural(len(nodes), "NODE", "NODES")]
    for letter, word in _STATES[2:]:
        moving = [name for name, dc, n in nodes if n["status"] == "U" and n["state"] == letter]
        if moving:
            parts.append("%d %s: %s" % (len(moving), word.upper(), ", ".join(moving)))
    return [u" \u2014 ".join(parts)] + ([_DIFFER] if differ else [])


def _taken_for(owner, elsewhere, nodes, inventory, unreachable, places):
    """{ring address: host}: an unreachable host no address of the ring
    matched (no facts, no ansible_host) and the one ring address no host
    has: taken for each other, when the host's place (dc, rack), if known,
    is that node's. Never a guess between several."""
    matched = set(owner[_ip(n["address"])] for dc, n in nodes if _ip(n["address"]) in owner)
    lost = [h for h in inventory if h in unreachable and h not in matched]
    unknown = [(dc, n) for dc, n in nodes if _ip(n["address"]) not in owner and _ip(n["address"]) not in elsewhere]
    if len(lost) != 1 or len(unknown) != 1:
        return {}
    dc, n = unknown[0]
    place = (places or {}).get(lost[0])
    if place and list(place) != [dc, n["rack"]]:
        return {}
    return {_ip(n["address"]): lost[0]}


def _elsewhere(outside):
    """{host: [addresses]} of the other hosts of the inventory -> {address: host}, by address only."""
    elsewhere = {}
    for host in outside or {}:
        for address in outside[host]:
            if address and _IP.search(address):
                elsewhere.setdefault(_ip(address), host)
    return elsewhere


def _shown_name(n, owner, guessed):
    """A ring node, {address: host}, the guesses -> the host it is ("host?" when guessed), None for none."""
    return (owner.get(_ip(n["address"])) + "?") if _ip(n["address"]) in guessed else owner.get(_ip(n["address"]))


def _sort_nodes(cluster_status, owner, elsewhere, guessed, absent):
    """The ring, {address: host} of the run's hosts and of the other hosts, the guesses, the hosts marked absent
    -> (in_ring: the run's hosts in the ring, stray, known: in another host of the inventory, leaving: marked
    absent, their "address (dc, UN)" texts; named: (name, datacenter, node) of each node, for the verdict)."""
    in_ring, stray, known, leaving, named = set(), [], [], [], []
    for dc in sorted(cluster_status):
        for n in cluster_status[dc].get("nodes", []):
            host = owner.get(_ip(n["address"]))
            if host is None and elsewhere.get(_ip(n["address"])) in (absent or []):
                leaving.append("%s = %s (%s, %s%s)" % (n["address"], elsewhere[_ip(n["address"])], dc, n["status"], n["state"]))
            elif host is None and _ip(n["address"]) in elsewhere:
                known.append("%s = %s (%s, %s%s)" % (n["address"], elsewhere[_ip(n["address"])], dc, n["status"], n["state"]))
            elif host is None:
                stray.append("%s (%s, %s%s)" % (n["address"], dc, n["status"], n["state"]))
            else:
                in_ring.add(host)
            named.append((_shown_name(n, owner, guessed) or elsewhere.get(_ip(n["address"])) or n["address"], dc, n))
    return in_ring, stray, known, leaving, named


def _dc_block(dc, nodes, owner, guessed):
    """A datacenter, its ring nodes, {address: host}, the guesses -> its lines: the nodes as nodetool status shows
    them (the ones not UN marked), then the summary of its racks and of the datacenter."""
    rows = [["--", "Address", "Load", "Tokens", "Owns", "Host ID", "Rack", "Inventory", ""]]
    for n in nodes:
        mark = "" if n["status"] + n["state"] == "UN" else out.ARROW + " " + (
            "down" if n["status"] == "D" else dict(_STATES).get(n["state"], n["status"] + n["state"]))
        rows.append([n["status"] + n["state"], n["address"], n["load"] or "?", n["tokens"] or "",
                     n["owns"] or "", n["host_id"], n["rack"], _shown_name(n, owner, guessed) or "-", mark])
    lines = ["", "Datacenter: %s" % dc, ""]
    lines.extend(" " + line for line in _table(rows))
    racks = sorted(set(n["rack"] for n in nodes))
    summary = [_summary(rack, [n for n in nodes if n["rack"] == rack]) for rack in racks] if len(racks) > 1 else []
    return lines + [""] + _summaries(summary + [_summary(dc, nodes)])


def _differences(missing, known, leaving, stray, guessed, unreachable, run, outside):
    """Where the run's hosts and the ring differ -> the notes, a line each."""
    notes = ["%s not reached by Ansible: taken for %s, the only node of the ring no host has (its ansible_host or"
             " cassandra_listen_address in the inventory would say for sure)" % (host, address)
             for address, host in guessed.items()]
    if missing:
        notes.append("In %s, not in the ring: " % run + ", ".join(
            h + (" (unreachable)" if h in unreachable else "") for h in missing))
    if known:
        notes.append("In the ring and the inventory, not in %s: " % run + ", ".join(known))
    if leaving:
        notes.append("Marked cassandra_node_state: absent, still in the ring (the playbook topology removes them): "
                     + ", ".join(leaving))
    if stray:
        notes.append("In the ring, not in %s%s: " % (run, "" if outside is None else " nor found elsewhere in the inventory")
                     + ", ".join(stray))
    return notes


def cassandra_ring_report(cluster_status, inventory, unreachable=None, limited=False, group="the inventory",
                          outside=None, absent=None, cluster="", seen_from="", places=None):
    """cluster_status: the cassandra_status module's. inventory: {host:
    [addresses and names it is known by]} of the hosts of the run. unreachable:
    the hosts Ansible could not reach. limited: the play runs on part of the
    group (--limit). group: the group of the run, as the report names it.
    outside: {host: [addresses]} of the other hosts of the inventory, matched
    by address only (no facts, no name resolution for them). absent: the
    hosts of outside marked cassandra_node_state: absent (topology removes
    them from the ring). cluster: the cluster's name, seen_from: the node
    the ring was read from, for the header line (none without either).
    places: {host: [dc, rack]} of the hosts, from the inventory: an
    unreachable host matched by no address is taken for the one ring node
    no host has in its place (marked "host?"). Returns the report as a list
    of lines."""
    cluster_status = cluster_status or {}
    ring = [(dc, n) for dc in cluster_status for n in cluster_status[dc].get("nodes", [])]
    owner = hosts_by_address(inventory, [_ip(n["address"]) for dc, n in ring])
    elsewhere = _elsewhere(outside)
    unreachable = set(unreachable or [])
    guessed = _taken_for(owner, elsewhere, ring, inventory, unreachable, places)
    owner.update(guessed)
    in_ring, stray, known, leaving, named = _sort_nodes(cluster_status, owner, elsewhere, guessed, absent)
    lines = []
    for dc in sorted(cluster_status):
        lines.extend(_dc_block(dc, cluster_status[dc].get("nodes", []), owner, guessed))
    missing = [h for h in inventory if h not in in_ring]
    run = "this run (--limit)" if limited else group
    notes = _differences(missing, known, leaving, stray, guessed, unreachable, run, outside)
    matches = not missing and not known and not leaving and not stray and not guessed  # a guess is no match
    members = "inventory" if run == group else run
    if cluster or seen_from:
        lines.insert(0, _header(cluster, seen_from, matches, members, len(named)))
    elif matches:
        notes.append("The ring and %s match (%s)" % (run, out.plural(len(inventory), "node")))
    if notes:
        lines.extend([""] + notes)
    if named:
        # a ring node that is another host of the inventory: expected under --limit or a sub-group, not a problem
        lines.extend([""] + _verdict(named, bool(missing or stray or leaving), [h for h in inventory if h in unreachable]))
    return lines[1:] if lines[:1] == [""] else lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_ring_report": cassandra_ring_report}
