# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_ip: an address in one spelling (IPv6 compressed, as the facts
have it; nodetool prints it in full), anything else as is.

cassandra_ring_report: the cassandra_status module's cluster_status as
readable lines for the status playbook: first the verdict (the nodes down,
joining, leaving or moving), the header lines under it (the cluster, the node
the ring is seen from and when; a WARNING line if the ring and the inventory
differ), then the nodes per datacenter as nodetool status shows them (the
ones not UN marked; with names, a node by its name and address), a summary
table of its racks and the datacenter, and where the inventory and the ring
differ. Blank lines between the blocks.

cassandra_dead_address: remove_dead_node's target -> the ring address of the
    dead node.
cassandra_dead_node_refusal: remove_dead_node's target refused -> why.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
import socket

from ansible_collections.community.cassandra.plugins.filter.cassandra_output import terminal_width
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import compressed_address as _ip

_STATES = [("U", "up"), ("D", "down"), ("J", "joining"), ("L", "leaving"), ("M", "moving")]
_IP = re.compile(r"^[0-9.]+$|:")
# remove_dead_node's patterns (its _ip and _host_id): an IPv4 or IPv6 address, a host ID
_DEAD_IP = re.compile(r"^[0-9.]+$|^[0-9a-fA-F:]+:[0-9a-fA-F:]*$")
_HOST_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIFFER = "WARNING  ring and inventory differ"  # the verdict's second line, when they do


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


def _header(cluster, seen_from, matches, members, total, seen_at=None, unchecked=None):
    """The lines under the verdict: "  my_cluster  seen from node1 at 2026-11-16 14:02 CET" (seen_at: when nodetool status
    answered there, the controller's clock), "  ring and inventory: the same 3 nodes". unchecked: the hosts not
    reached, taken for a ring node, when nothing else differs: not a difference seen, "not checked"."""
    where = ("seen from %s" % seen_from if seen_from else "") + (
        (" at " if seen_from else "seen at ") + out.clock(float(seen_at), date=True) if seen_at not in (None, "") else "")
    lines = ["  " + "  ".join(x for x in [cluster, where] if x)] if cluster or where else []
    if unchecked:
        return lines + ["  ring and %s: %s in the ring, %s not checked (not reached)"
                        % (members, out.plural(total, "node"), ", ".join(unchecked))]
    return lines + ["  " + (("ring and %s: the same %s" % (members, out.plural(total, "node"))) if matches
                            else "ring and %s differ (below), %s in the ring" % (members, out.plural(total, "node")))]


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


def _shown_address(n, names):
    """A ring node, {address: its name} -> how the report shows it: "node1.example (10.0.0.1)", its address alone when
    it has no name. The matching with the inventory is by address only."""
    name = (names or {}).get(_ip(n["address"]))
    return "%s (%s)" % (name, n["address"]) if name and name != n["address"] else n["address"]


def _shown_name(n, owner, guessed):
    """A ring node, {address: host}, the guesses -> the host it is ("host?" when guessed), None for none."""
    return (owner.get(_ip(n["address"])) + "?") if _ip(n["address"]) in guessed else owner.get(_ip(n["address"]))


def _sort_nodes(cluster_status, owner, elsewhere, guessed, absent, names=None):
    """The ring, {address: host} of the run's hosts and of the other hosts, the guesses, the hosts marked absent,
    {address: name} -> (in_ring: the run's hosts in the ring, stray, known: in another host of the inventory,
    leaving: marked absent, their "address (dc, UN)" texts, a stray one with its name; named: (name, datacenter,
    node) of each node, for the verdict)."""
    in_ring, stray, known, leaving, named = set(), [], [], [], []
    for dc in sorted(cluster_status):
        for n in cluster_status[dc].get("nodes", []):
            host, name = owner.get(_ip(n["address"])), (names or {}).get(_ip(n["address"]))
            if host is None and elsewhere.get(_ip(n["address"])) in (absent or []):
                leaving.append("%s = %s (%s, %s%s)" % (n["address"], elsewhere[_ip(n["address"])], dc, n["status"], n["state"]))
            elif host is None and _ip(n["address"]) in elsewhere:
                known.append("%s = %s (%s, %s%s)" % (n["address"], elsewhere[_ip(n["address"])], dc, n["status"], n["state"]))
            elif host is None:
                stray.append("%s (%s%s, %s%s)" % (n["address"], name + ", " if name else "", dc, n["status"],
                                                  n["state"]))
            else:
                in_ring.add(host)
            named.append((_shown_name(n, owner, guessed) or elsewhere.get(_ip(n["address"])) or name or n["address"],
                          dc, n))
    return in_ring, stray, known, leaving, named


# a Host ID cut to this many characters when the table is wider than the screen (its whole value: CUT_HOST_IDS)
HOST_ID_SHOWN = 8
CUT_HOST_IDS = "Host IDs shortened; full: -e cassandra_status_raw=true"


def _dc_rows(nodes, owner, guessed, names, cut):
    rows = [["Node", "--", "Address", "Load", "Tokens", "Owns", "Host ID", "Rack", ""]]
    for n in nodes:
        mark = "" if n["status"] + n["state"] == "UN" else out.ARROW + " " + (
            "down" if n["status"] == "D" else dict(_STATES).get(n["state"], n["status"] + n["state"]))
        host_id = n["host_id"][:HOST_ID_SHOWN] + "..." if cut and len(n["host_id"]) > HOST_ID_SHOWN + 3 else n["host_id"]
        rows.append([_shown_name(n, owner, guessed) or "-", n["status"] + n["state"], _shown_address(n, names),
                     n["load"] or "?", n["tokens"] or "", n["owns"] or "", host_id, n["rack"], mark])
    return rows


def _dc_block(dc, nodes, owner, guessed, names=None, width=None):
    """A datacenter, its ring nodes, {address: host}, the guesses, {address: name} -> (its lines: the nodes as
    nodetool status shows them, the ones not UN marked, each by its name and address when it has a name, then the
    summary of its racks and of the datacenter; whether its Host IDs were cut: a row wider than width has them cut to
    their first characters)."""
    table = [" " + line for line in _table(_dc_rows(nodes, owner, guessed, names, False))]
    cut = bool(width) and max(len(line) for line in table) > width
    if cut:
        table = [" " + line for line in _table(_dc_rows(nodes, owner, guessed, names, True))]
    lines = ["", "Datacenter: %s" % dc, ""]
    lines.extend(table)
    racks = sorted(set(n["rack"] for n in nodes))
    summary = [_summary(rack, [n for n in nodes if n["rack"] == rack]) for rack in racks] if len(racks) > 1 else []
    return lines + [""] + _summaries(summary + [_summary(dc, nodes)]), cut


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


def _dc_tables(cluster_status, owner, guessed, names, width):
    """The table of each datacenter, in order; when a Host ID was cut to fit width ("terminal": the terminal's),
    the line that says where they are whole."""
    if str(width) == "terminal":
        width = terminal_width()
    lines, cut = [], False
    for dc in sorted(cluster_status):
        block, cut_here = _dc_block(dc, cluster_status[dc].get("nodes", []), owner, guessed, names, width)
        lines.extend(block)
        cut = cut or cut_here
    return lines + (["", CUT_HOST_IDS] if cut else [])


def cassandra_ring_report(cluster_status, inventory, unreachable=None, limited=False, group="the inventory",
                          outside=None, absent=None, cluster="", seen_from="", places=None, names=None, seen_at=None,
                          width=None):
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
    no host has in its place (marked "host?"). names: {address: its name}
    (resolved on the node the ring was read from): a node shown by its name
    and address, its address alone without one; matched by address all the
    same. seen_at: when the ring was read (epoch, the controller's clock),
    said with its time zone after seen_from. width: a table wider than this
    ("terminal": the terminal's) has its Host IDs cut, a line saying where
    they are whole. Returns the report as a list of lines.
    e.g. (node1 to node3 of dc1/r1, node3 DN; inventory {node1: ["10.0.0.1"], ...}, cluster="my_cluster",
    seen_from="node1") -> ["1 NODE DOWN: node3 (dc1/r1)", "  my_cluster  seen from node1", "  ring and inventory: the
    same 3 nodes", "", "Datacenter: dc1", "", " Node   --  Address   Load ...", " node1  UN  10.0.0.1  1 GiB  16  h1
    r1", ..., " node3  DN  10.0.0.3  ...  \u2190 down", "", "  dc1   3 nodes   2 up, 1 down   load 3.0 GiB"]: the
    verdict first, alone on its line, then the cluster, the nodes by their inventory name first."""
    cluster_status = cluster_status or {}
    ring = [(dc, n) for dc in cluster_status for n in cluster_status[dc].get("nodes", [])]
    owner = hosts_by_address(inventory, [_ip(n["address"]) for dc, n in ring])
    elsewhere = _elsewhere(outside)
    unreachable = set(unreachable or [])
    guessed = _taken_for(owner, elsewhere, ring, inventory, unreachable, places)
    owner.update(guessed)
    names = dict((_ip(a), name) for a, name in (names or {}).items() if name)
    in_ring, stray, known, leaving, named = _sort_nodes(cluster_status, owner, elsewhere, guessed, absent, names)
    lines = _dc_tables(cluster_status, owner, guessed, names, width)
    missing = [h for h in inventory if h not in in_ring]
    run = "this run (--limit)" if limited else group
    notes = _differences(missing, known, leaving, stray, guessed, unreachable, run, outside)
    differ = bool(missing or known or leaving or stray)
    matches = not differ and not guessed  # a guess is no match
    members = "inventory" if run == group else run
    head = _head(cluster, seen_from, differ, guessed, members, len(named), seen_at) if cluster or seen_from else []
    if not head and matches:
        notes.append("The ring and %s match (%s)" % (run, out.plural(len(inventory), "node")))
    # a ring node that is another host of the inventory: expected under --limit or a sub-group, not a problem
    verdict = _verdict(named, bool(missing or stray or leaving), [h for h in inventory if h in unreachable]) if named else []
    return _laid_out(verdict, head, lines, notes)


def _head(cluster, seen_from, differ, guessed, members, count, seen_at):
    """The header lines (_header): the ring and the inventory match when they do not differ and no host was taken
    for a ring node; the only gap such a host (not reached): unchecked, not seen to differ."""
    unchecked = sorted(guessed.values()) if guessed and not differ else None
    return _header(cluster, seen_from, not differ and not guessed, members, count, seen_at, unchecked)


def _laid_out(verdict, head, tables, notes):
    """The report's lines: the verdict first, alone, the header (the cluster and where it was seen from) under it,
    then the tables, then the notes after a blank line."""
    lines = verdict[:1] + head + verdict[1:] + tables + ([""] + notes if notes else [])
    return lines[1:] if lines[:1] == [""] else lines


def cassandra_dead_address(target, ring, hostvars, inventory_hosts):
    """remove_dead_node's target (cassandra_target_nodes), the ring (cluster_status), hostvars, the inventory's
    hosts -> the dead node's ring address: the address of a host ID of the ring, an address as is, an inventory
    host's address (broadcast_address, cassandra_listen_address, ansible_host: the first that is an address, as
    text; '' when none is), else the target as given."""
    by_id = [n.get("address") for info in ring.values() for n in info["nodes"]
             if "host_id" in n and n["host_id"] == target]
    if by_id:
        return by_id[0]
    if _DEAD_IP.match(target):
        return target
    if target in inventory_hosts:
        v = hostvars[target]
        names = [(v.get("cassandra_extra_settings") or {}).get("broadcast_address", ""),
                 v.get("cassandra_listen_address", ""), v.get("ansible_host", ""), target]
        return next((str(n) for n in names if _DEAD_IP.match(str(n))), "")  # an int (YAML) as text
    return target


def cassandra_dead_node_refusal(target, address, inventory_hosts, ring_addresses):
    """remove_dead_node's target refused, its address (cassandra_dead_address), the inventory's hosts, the ring's
    addresses -> why: an inventory host with no address, neither an address nor a host ID, an inventory host
    whose address is not in the ring, else a node of the run."""
    if target in inventory_hosts and not address:
        return ("%s: the inventory gives it no address (ansible_host, cassandra_listen_address): give its address or"
                " host ID in cassandra_target_nodes." % target)
    if not _DEAD_IP.match(address) and not _HOST_ID.match(target):
        return "%s is not an address, a host ID of the ring nor a host of the inventory." % target
    if not _DEAD_IP.match(target) and target in inventory_hosts and address not in ring_addresses:
        return ("%s (%s, from the inventory) is not in the ring: give its ring address or host ID in"
                " cassandra_target_nodes (nothing to do if it was removed already)." % (target, address))
    return ("%s (%s) is a node of this run: take the dead host out of the inventory, or mark it cassandra_node_state:"
            " absent, first." % (target, address))


class FilterModule(object):
    def filters(self):
        return {"cassandra_ring_report": cassandra_ring_report, "cassandra_ip": _ip,
                "cassandra_dead_address": cassandra_dead_address, "cassandra_dead_node_refusal": cassandra_dead_node_refusal}
