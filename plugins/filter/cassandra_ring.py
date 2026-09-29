# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_ring_report: the cassandra_status module's cluster_status as
readable lines for the status playbook: the nodes per datacenter as nodetool
status shows them, a summary line per datacenter, and where the inventory and
the ring differ."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ipaddress
import re
import socket

# nodetool's sizes (FileUtils.stringifyFileSize), a comma as decimal mark in some locales
_SIZE = re.compile(r"^([0-9]+(?:[.,][0-9]+)?)\s*(bytes|KiB|MiB|GiB|TiB)$")
_UNITS = ["bytes", "KiB", "MiB", "GiB", "TiB"]
_STATES = [("U", "up"), ("D", "down"), ("J", "joining"), ("L", "leaving"), ("M", "moving")]
_IP = re.compile(r"^[0-9.]+$|:")


def _bytes(load):
    match = _SIZE.match((load or "").strip())
    if not match:
        return None
    return float(match.group(1).replace(",", ".")) * 1024 ** _UNITS.index(match.group(2))


def _size(value):
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024.0
        unit += 1
    return ("%d bytes" % value) if unit == 0 else "%.2f %s" % (value, _UNITS[unit])


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


def cassandra_ring_report(cluster_status, inventory, unreachable=None, limited=False, group="the inventory",
                          outside=None):
    """cluster_status: the cassandra_status module's. inventory: {host:
    [addresses and names it is known by]} of the hosts of the run. unreachable:
    the hosts Ansible could not reach. limited: the play runs on part of the
    group (--limit). group: the group of the run, as the report names it.
    outside: {host: [addresses]} of the other hosts of the inventory, matched
    by address only (no facts, no name resolution for them).
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
    for dc in sorted(cluster_status):
        nodes = cluster_status[dc].get("nodes", [])
        rows = [["--", "Address", "Load", "Tokens", "Owns", "Host ID", "Rack", "Inventory"]]
        counts = dict((word, 0) for dummy, word in _STATES)
        total = 0.0
        unknown = 0
        for n in nodes:
            host = owner.get(_ip(n["address"]))
            if host is None and _ip(n["address"]) in elsewhere:
                known.append("%s = %s (%s, %s%s)" % (n["address"], elsewhere[_ip(n["address"])], dc, n["status"], n["state"]))
            elif host is None:
                stray.append("%s (%s, %s%s)" % (n["address"], dc, n["status"], n["state"]))
            else:
                in_ring.add(host)
            rows.append([n["status"] + n["state"], n["address"], n["load"] or "?", n["tokens"] or "",
                         n["owns"] or "", n["host_id"], n["rack"], host or "-"])
            for letter, word in _STATES:
                # U/D: the status (first letter); J/L/M: the state (second)
                if letter == (n["status"] if letter in "UD" else n["state"]):
                    counts[word] += 1
            size = _bytes(n["load"])
            if size is None:
                unknown += 1
            else:
                total += size
        lines.append("Datacenter: %s" % dc)
        lines.extend("  " + line for line in _table(rows))
        summary = "  %s: %d node(s), %s; load %s" % (
            dc, len(nodes), ", ".join("%d %s" % (counts[word], word) for dummy, word in _STATES
                                      if counts[word] or word in ("up", "down")), _size(total))
        lines.append(summary + (" (%d unknown)" % unknown if unknown else ""))
    missing = [h for h in inventory if h not in in_ring]
    unreachable = set(unreachable or [])
    run = "this run (--limit)" if limited else group
    if missing:
        lines.append("In %s, not in the ring: " % run + ", ".join(
            h + (" (unreachable)" if h in unreachable else "") for h in missing))
    if known:
        lines.append("In the ring and the inventory, not in %s: " % run + ", ".join(known))
    if stray:
        lines.append("In the ring, not in %s%s: " % (run, "" if outside is None else " nor found elsewhere in the inventory")
                     + ", ".join(stray))
    if not missing and not stray and not known:
        lines.append("The ring and %s match (%d node(s))" % (run, len(inventory)))
    return lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_ring_report": cassandra_ring_report}
