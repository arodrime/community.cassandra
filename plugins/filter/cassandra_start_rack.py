# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The cluster check of start_rack (playbooks/start_rack.yml): the rack's nodes must be UN; the rest of the cluster
is only a warning, unless the ring puts a node in the rack.

Which inventory host a node of the ring is (_placement), computed once per read:
- a node of the run: the ring node with its Host ID, as nodetool info on it gives it (what it says of itself),
  never by an address;
- any other host (left out by --limit, outside the rack): the ring node at an address its variables or facts give
  (cassandra_host_addresses); an address several hosts give (a copy-pasted listen address, a stale ansible_host):
  the one of them the inventory puts in the ring's dc and rack for that node, what a host broadcasts or listens on
  first, else none; then what its names resolve to on the controller, for a ring address no host has yet that the
  ring puts in the host's own dc and rack (a stale name never names a node of another rack).
A ring node no host is placed at: "no host known by its address"; a node of the rack no ring node is placed at:
"not placed". A node of the run whose Host ID is not read is placed nowhere, the ring node at an address of its own
only named after it (in the stop on it).

Which rack a node of the ring is of (_judge), the first rule that matches:

  the node of the ring                                          judged
  a node of the rack left out by --limit                        left out
  no host known by its address, every node of the run placed,  left out (it
    a node left out not placed, and the ring puts it in the       may be that
    rack or the nodes left out not placed outnumber the ring's    one)
    nodes of the rack no host is known by
  a node of the rack (of the run), or put in the rack's dc and  of the rack
    rack by the ring
  any other                                                     outside

While a node of the run is not placed (its Host ID not read), a ring node of the rack no host is known by is of the
rack, so waited for while down, even when it may be a node left out: it may be that node of the run too (the run
stops on it anyway, after the wait).

What the check does with what it finds (cassandra_rack_verdict); "the rack is placed": every node of the rack is:

  found                                                         outcome
  a node of the rack not reached over ssh                       STOP at once
  a node of the rack down                                       wait, then STOP
  a node of the rack left out by --limit (not started by the    WARNING, not
    run), not UN (its state from the last read)                   waited for
  a node of the rack in another state, or nodetool failing on   STOP
    it
  a node of the run whose Host ID is not read (nodetool info    STOP (after the
    failing on it, or no ID line)                                 wait, read
                                                                  again)
  a node of the run whose Host ID no ring read has              STOP
  a node of the rack placed in another rack of the ring         WARNING
  an outside node not UN for the node outside the rack read     WARNING, not
    from                                                          waited for
  an outside node not UN for a node of the rack read from: a    wait, then
    second one, or the checker (no node outside the rack          WARNING
    read, or its read failed); just started, it may still see
    a peer down
  an outside node not reached over ssh                          WARNING, not
                                                                  waited for
  nodetool failing on an outside node, a ring read from         WARNING
    another node
  a ring of another size than the inventory, the rack placed    WARNING
    and UN
  a schema disagreement, the rack placed, UN and on one         WARNING
    version held by more than half of the nodes describecluster
    lists, a node outside the rack among them (the nodes on
    another one named)
  a schema disagreement while the versions are mixed            WARNING
    (cassandra_allow_mixed_versions)
  anything else (streams on the rack node, the ring size or     STOP
    the schema otherwise, no ring read at all...)

The schema's majority, not the version of the node outside the rack read from: that node may be the one behind.
The rack just came back: its own nodes never make the majority alone (a node outside the rack, listed, so reached
by describecluster, must be on its version). A rack alone on its version, or a cluster split even: a stop.

Not covered: a host outside the rack reached over ssh whose ping or fact gathering fails fails the run.

cassandra_host_racks: each host's dc and rack in the inventory.
cassandra_rack_members: the nodes the inventory puts in a rack (every node of the cluster, under --limit too).
cassandra_rack_verdict: the tables above applied to a read of the cluster.
cassandra_rack_outside_lines: the rack's nodes UN (those of the run under --limit), then the warnings (a node of the
rack in another rack of the ring, the nodes of the rack left out by --limit not UN, the rest of the cluster), once
the rack is UN.
cassandra_rack_start_failed: a node of the rack that did not start (start_rack's rescue).
cassandra_rack_stop_refused: stop_rack's refusal (more than one replica down), with the other screens of a rack's
start and stop.
"""
from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
import shlex

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import cassandra_health_sentences
from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_refused
from ansible_collections.community.cassandra.plugins.filter.cassandra_ring import hosts_by_address
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import compressed_address as _ip
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

# the cassandra_config defaults (roles/cassandra_config/defaults/main.yml; a unit test ties them)
DEFAULT_DC = "dc1"
DEFAULT_RACK = "rack1"

# the width of an operator message: an 80-column terminal
WIDTH = 79


def cassandra_host_racks(hosts, hostvars):
    """The cluster's nodes, their hostvars -> {node: [its cassandra_dc, its cassandra_rack]} (the cassandra_config
    defaults when not set).
    e.g. (["n1", "n2"], {"n1": {}, "n2": {"cassandra_rack": "r2"}}) -> {"n1": ["dc1", "rack1"], "n2": ["dc1", "r2"]}."""
    racks = {}
    for h in hosts or []:
        variables = hostvars.get(h) or {}
        racks[h] = [str(variables.get("cassandra_dc", DEFAULT_DC)), str(variables.get("cassandra_rack", DEFAULT_RACK))]
    return racks


def cassandra_rack_members(hosts, hostvars, dc, rack):
    """The cluster's nodes, their hostvars, a dc and a rack -> the nodes whose cassandra_dc and cassandra_rack (the
    cassandra_config defaults when not set) are that dc and rack, in their order.
    e.g. (["n1", "n2"], {"n1": {"cassandra_rack": "r1"}, "n2": {"cassandra_rack": "r2"}}, "dc1", "r2") -> ["n2"]."""
    racks = cassandra_host_racks(hosts, hostvars)
    return [h for h in hosts or [] if racks[h] == [str(dc), str(rack)]]


def _ring_nodes(reads):
    """The cassandra_status results read -> each node of their rings, read after read: (address, dc, rack, state
    as nodetool status says it, Host ID)."""
    for read in reads or []:
        for dc, value in ((read.get("cluster_status") or {}) if isinstance(read, dict) else {}).items():
            for n in value.get("nodes", []):
                yield (_ip(n["address"]), str(dc), str(n.get("rack")),
                       str(n.get("status", "")) + str(n.get("state", "")), n.get("host_id"))


def _first_line(*texts):
    """Texts -> the first line of the first one that has one, stripped; "" for none."""
    for text in texts:
        lines = [line.strip() for line in str(text or "").split("\n") if line.strip()]
        if lines:
            return lines[0]
    return ""


def _host_ids(infos, run, rack):
    """{node of the run: its nodetool info result (rc, stdout, stderr, msg)}, the nodes of the run, the rack ->
    ({Host ID, lower-case: node} of those whose output gives one of its own, the others in run order (no Host ID
    read, or one another node of the run gives too), the lines of the stop on them)."""
    by_id, unread, lines = {}, [], []
    for h in run:
        info = (infos or {}).get(h) or {}
        found = re.findall(r"(?m)^ID\s*:\s*(\S+)", str(info.get("stdout") or ""))
        error = _first_line(info.get("stderr"), info.get("msg"))
        if found:
            by_id.setdefault(found[0].lower(), []).append(h)
            continue
        unread.append(h)
        if info.get("rc") == 0 or (info.get("stdout") and not error):
            lines.append("%s (rack %s): nodetool info on it gives no ID line" % (h, rack))
        elif error:
            lines += ["%s (rack %s): nodetool info failed on it:" % (h, rack), "  " + error]
        else:
            lines.append("%s (rack %s): its Host ID is not read (nodetool info on it)" % (h, rack))
    for host_id, hosts in by_id.items():
        if len(hosts) > 1:  # (one JVM answering for both: a nodetool wrapper, a JMX port)
            unread += hosts
            lines += ["%s (rack %s): nodetool info on each gives one Host ID:" % (" and ".join(hosts), rack),
                      "  " + host_id]
    return (dict((i, hosts[0]) for i, hosts in by_id.items() if len(hosts) == 1),
            [h for h in run if h in unread], lines)


def _givers(addresses, givers):
    """Every host's addresses (cassandra_host_addresses), the hosts giving each (cassandra_host_address_givers, else
    the one addresses has) -> {address as the ring has it: [[the hosts that broadcast or listen on it], [the
    others]]}."""
    by_ip = {}
    for address, levels in (givers or dict((a, [[], [h]]) for a, h in (addresses or {}).items())).items():
        merged = by_ip.setdefault(_ip(address), [[], []])
        for level, hosts in zip(merged, levels):
            level.extend(h for h in hosts if h not in level)
    return by_ip


def _giver(address, levels, where, racks):
    """A ring address, the hosts giving it (_givers), the ring's (address, dc, rack), every host's [dc, rack] -> the
    host at that address by its addresses: the one giving it; of several, the one the inventory puts in the ring's
    dc and rack there (what a host broadcasts or listens on first); else None."""
    hosts = _unique(levels[0] + levels[1])
    if len(hosts) == 1:
        return hosts[0]
    for level in levels:
        here = [h for h in level if (address,) + tuple((racks or {}).get(h) or ()) in where]
        if here:
            return here[0] if len(here) == 1 else None
    return None


def _placement(reads, run, addresses, ids, racks, givers):
    """The reads, the nodes of the run, every host's addresses (cassandra_host_addresses), {Host ID: node of the
    run}, every host's [dc, rack] (cassandra_host_racks), the hosts giving each address (_givers) -> {ring address:
    inventory host}: the module docstring's placement."""
    ring = _unique(n[0] for n in _ring_nodes(reads))
    where = set(n[:3] for n in _ring_nodes(reads))  # (address, dc, rack)
    host_at = dict((n[0], ids[str(n[4]).lower()]) for n in _ring_nodes(reads) if str(n[4]).lower() in ids)
    for address in ring:
        host = _giver(address, givers.get(address) or [[], []], where, racks)
        if host and address not in host_at and host not in run:
            host_at[address] = host
    rest = [a for a in ring if a not in host_at]
    hosts = _unique(h for h in (addresses or {}).values() if h not in run and h not in host_at.values())
    if rest and hosts:
        names = dict((h, [a for a, x in addresses.items() if x == h]) for h in hosts)
        host_at.update((a, h) for a, h in hosts_by_address(names, rest).items()
                       if a in rest and (a,) + tuple((racks or {}).get(h) or ()) in where)
    return host_at


def _unread_named(host_at, reads, givers, racks, unread):
    """The placement, the reads, the hosts giving each address (_givers), every host's [dc, rack], the nodes of the
    run whose Host ID is not read -> the placement with the ring nodes at an address of one of those nodes (_giver)
    named after it: names only (that node stays not placed)."""
    where = set(n[:3] for n in _ring_nodes(reads))
    names = dict(host_at)
    for address in _unique(n[0] for n in _ring_nodes(reads)):
        host = _giver(address, givers.get(address) or [[], []], where, racks)
        if host in unread and address not in names:
            names[address] = host
    return names


def _named(finding, host_at):
    """A finding, the placement -> a state finding by the name its address is placed at (cassandra_health_findings
    names it by the addresses alone), its text too; any other finding as it is."""
    if finding.get("kind") != "state":
        return finding
    name = host_at.get(_ip(finding["address"]))
    if name == finding.get("name"):
        return finding
    where = "%s, %s" % (finding["address"], finding["rack"]) if name else finding["rack"]
    return dict(finding, name=name, text="%s (%s) is %s, seen from %s" % (
        name or finding["address"], where, finding["state"], finding["seen_from"]))


def _judge(findings, members, left_out, left_unplaced, rack_unowned, run_placed, dc, rack):
    """The state findings (_named), the rack's nodes, those left out by --limit, those left out not placed, the
    number of the ring's nodes of the rack no host is known by, whether every node of the run is placed -> {address:
    "rack", "left_out" or "outside"}: the module docstring's first table."""
    may_be_one = len(left_unplaced) > rack_unowned  # (a ring node no host is known by may be a node left out)
    judged = {}
    for f in findings:
        if f.get("kind") != "state":
            continue
        host = f.get("name")
        in_rack = (str(f.get("dc")), str(f.get("rack"))) == (str(dc), str(rack))
        if host in left_out or (host is None and run_placed and left_unplaced and (in_rack or may_be_one)):
            verdict = "left_out"
        elif host in members or in_rack:
            verdict = "rack"
        else:
            verdict = "outside"
        if judged.get(f["address"]) != "rack":  # (in the rack for one view: of the rack)
            judged[f["address"]] = verdict
    return judged


def _rack_seen_un(reads, host_at, members, dc, rack):
    """The reads, the placement, the rack's nodes, its dc and rack -> the hosts placed at the nodes of the ring of
    the rack (placed at a node of the rack, or put in the rack's dc and rack by the ring), UN in every read that has
    them, in the ring's order."""
    states, order = {}, []
    for address, ring_dc, ring_rack, state, dummy in _ring_nodes(reads):
        if host_at.get(address) in members or (ring_dc, ring_rack) == (str(dc), str(rack)):
            if address not in states:
                order.append(address)
            states.setdefault(address, set()).add(state)
    return [host_at[a] for a in order if states[a] == {"UN"} and host_at.get(a)]


def _other_rack(reads, host_at, members, dc, rack):
    """The reads, the placement, the rack's nodes, its dc and rack -> [[node, its dc, its rack]] of the nodes of
    the rack the ring puts in another dc or rack."""
    rows = []
    for address, ring_dc, ring_rack, dummy, dummy in _ring_nodes(reads):
        row = [host_at.get(address), ring_dc, ring_rack]
        if row[0] in members and (ring_dc, ring_rack) != (str(dc), str(rack)) and row not in rows:
            rows.append(row)
    return rows


def _ring_states(reads):
    """The cassandra_status results read -> {ring address: [its states, as nodetool status says them]}."""
    states = {}
    for address, dummy, dummy, state, dummy in _ring_nodes(reads):
        if state not in states.setdefault(address, []):
            states[address].append(state)
    return states


def _seen_from_run_only(read_from, run, addresses):
    """The nodes read from (both reads), the nodes of the run, every host's addresses -> the nodes read from when
    they are all of the run and the cluster has other nodes, else []."""
    others = set((addresses or {}).values()) - set(run)
    return read_from if read_from and set(read_from) <= set(run) and others else []


def _without_port(address):
    """An address as describecluster prints it (10.0.0.1:7000, [2001:db8::1]:7000 on 4.0+) -> the address."""
    address = str(address).strip()
    if address.startswith("[") and "]" in address:
        return address[1:address.index("]")]
    return address.split(":", maxsplit=1)[0] if address.count(":") == 1 else address


def _other_versions(schema, rack_addresses):
    """cassandra_schema's result (its last read succeeded) and the rack's ring addresses -> the addresses on
    another schema version than the rack's, when every rack node is on one version held by more than half of the
    nodes describecluster lists, a node outside the rack among them; else None (the disagreement may be the rack's:
    see the module docstring)."""
    versions = dict((v, [_ip(_without_port(a)) for a in hosts])
                    for v, hosts in ((schema or {}).get("schema_status") or {}).items())
    ours = [v for v, hosts in versions.items() if rack_addresses & set(hosts)]
    if (schema or {}).get("rc") or len(versions) < 2 or not ours or not rack_addresses <= set(versions[ours[0]]):
        return None  # (a rack node not listed, or on two versions)
    if 2 * len(versions[ours[0]]) <= sum(len(hosts) for hosts in versions.values()):
        return None  # the rack's version is not the majority's
    if not set(versions[ours[0]]) - rack_addresses:
        return None  # the rack alone on its version
    return [a for v in sorted(versions) if v != ours[0] for a in versions[v]]


def _unique(items):
    kept = []
    for item in items:
        if item not in kept:
            kept.append(item)
    return kept


def _note_row(finding):
    """A warning finding -> its row: [what, the rest of its text]."""
    what, text = str(finding.get("what") or "the cluster"), str(finding.get("text", ""))
    return [what, text[len(what) + 1:] if text.startswith(what + " ") else text]


def _left_out_nodes(findings, judged, first_verdict, reads, left_unplaced):
    """The findings (_named) and how they were judged, the first verdict (None before the wait), the reads, the
    nodes left out by --limit not placed -> the nodes of the rack left out not UN: [{address, name, states}], the
    first verdict's with their states in these reads (cassandra_service_health_any_ok kept them out of the
    findings: one UN in every read dropped, one not in them as it was), then this read's; a node by its address
    only gets "unplaced": the nodes left out not placed (the ones it may be)."""
    states = _ring_states(reads)
    left = []
    for n in (first_verdict or {}).get("left_out") or []:
        now = states.get(_ip(n["address"]))
        if now is None or [s for s in now if s != "UN"]:
            left.append(dict(n, states=list(n["states"]) if now is None else [s for s in now if s != "UN"]))
    by_address = dict((n["address"], n) for n in left)
    for f in findings:
        if f.get("kind") == "state" and judged[f["address"]] == "left_out":
            if f["address"] not in by_address:
                by_address[f["address"]] = {"address": f["address"], "name": f.get("name"), "states": []}
                left.append(by_address[f["address"]])
            if f["state"] not in by_address[f["address"]]["states"]:
                by_address[f["address"]]["states"].append(f["state"])
    for n in left:
        if not n.get("name"):
            n["unplaced"] = list(left_unplaced)
    return left


def _outside_nodes(findings, judged, first_verdict, members, counted_view, unreached):
    """The findings (_named) and how they were judged, the first verdict (None before the wait), the rack's nodes,
    the view counted at once (the peer, else the checker), the hosts not reached over ssh -> the nodes outside the
    rack not UN, [{address, name, states}], in the order seen, the first verdict's first; before the wait only the
    ones not reached over ssh and those the counted view saw so when it is outside the rack (the others are waited
    for: a node of the rack read from just started, it may still see a peer down)."""
    outside = [dict(n, states=list(n["states"])) for n in (first_verdict or {}).get("outside", [])]
    by_address = dict((n["address"], n) for n in outside)
    seen_by = {}  # the views that saw each node outside not UN, by its address
    for f in findings:
        if f.get("kind") == "state" and judged[f["address"]] == "outside":
            if f["address"] not in by_address:
                by_address[f["address"]] = {"address": f["address"], "name": f.get("name"), "states": []}
                outside.append(by_address[f["address"]])
            if f["state"] not in by_address[f["address"]]["states"]:
                by_address[f["address"]]["states"].append(f["state"])
            seen_by.setdefault(f["address"], set()).add(f.get("seen_from"))
    if first_verdict is None:
        settled = counted_view not in members
        outside = [n for n in outside if n.get("name") in (unreached or [])
                   or (settled and counted_view in seen_by[n["address"]])]
    return outside


def cassandra_rack_verdict(findings, members, dc, rack, checker, peer, addresses=None, reads=None, schema=None,
                           first_verdict=None, left_out=None, unreached=None, infos=None, racks=None, givers=None):
    """cassandra_health_findings' result of a read, the rack's nodes (cassandra_rack_members), its dc and rack, the
    node of the rack read from (checker), the other node read from (peer), the addresses of every host
    (cassandra_host_addresses), the cassandra_status results read, the cassandra_schema result, first_verdict: this
    filter's result before the wait (None: this read is the one before the wait), left_out: the rack's nodes left
    out by --limit (out of the play: this run does not start them), unreached: the hosts not reached over ssh,
    infos: {node of the run: its nodetool info result (rc, stdout, stderr, msg)}, racks: every host's [dc, rack]
    (cassandra_host_racks), givers: the hosts giving each address (cassandra_host_address_givers; None: the one
    addresses has) -> the module docstring's tables applied:
      outside: the nodes outside the rack not UN, [{address, name, states}] (_outside_nodes).
      failed: the nodes outside the rack whose nodetool status failed (the first verdict's too), when a ring was
        read.
      warnings: the other rows of the warning, [[name, fact]]: the nodes on another schema version (a node by its
        inventory name, else its address), then the warning findings (schema versions differing while the
        versions are mixed: cassandra_allow_mixed_versions).
      other_rack: the nodes of the rack the ring puts in another dc or rack, [[name, dc, rack]].
      ring_size: [[the ring's size, the inventory's]] when they differ and that is a warning.
      problems: what stops the run, as sentences (cassandra_health_sentences), then the lines of each node of the
        run whose Host ID is not read (_host_ids), then a line per node of the run whose Host ID no ring read has.
      host_id_unread: the nodes of the run whose Host ID is not read (read again after the wait).
      rack_seen_un: the nodes of the rack UN in the rings read (_rack_seen_un), by name.
      read_from: the nodes read from (checker, peer; not one whose nodetool failed), this read's and the first
        verdict's.
      seen_from_run_only: read_from when they are all nodes of this run and the cluster has others (none of them
        reached in the play; a node sees itself UN): [] otherwise.
      left_out: the nodes of the rack left out by --limit not UN (_left_out_nodes). Not waited for, not in
        problems: a warning (cassandra_rack_outside_lines).
      not_readers: the hosts not to read from again: the nodes outside the rack not UN, seen down, or whose
        nodetool failed.
    e.g. ([<n4 DN from n2>, <n4 DN from n1>], ["n2", "n3"], "dc1", "r2", "n2", "n1", infos={n2: {stdout: "ID : a"},
    n3: {stdout: "ID : b"}}) -> {outside: [{address: "10.0.0.4", name: "n4", states: ["DN"]}], failed: [],
    warnings: [], other_rack: [], ring_size: [], problems: [], host_id_unread: [], rack_seen_un: ["n2", "n3"],
    read_from: ["n2", "n1"], seen_from_run_only: [], left_out: [], not_readers: ["n4"]}."""
    members, left_out = list(members or []), list(left_out or [])
    run = [h for h in members if h not in left_out]
    ids, unread, unread_lines = _host_ids(infos, run, rack)
    givers = _givers(addresses, givers)
    host_at = _placement(reads, run, addresses, ids, racks, givers)
    names = _unread_named(host_at, reads, givers, racks, unread)
    findings = [_named(f, names) for f in findings or []]
    unplaced = [h for h in members if h not in host_at.values()]
    left_unplaced = [h for h in left_out if h in unplaced]
    rack_unowned = set(n[0] for n in _ring_nodes(reads)
                       if n[0] not in host_at and (n[1], n[2]) == (str(dc), str(rack)))
    run_placed = set(run) <= set(host_at.values())
    judged = _judge(findings, members, left_out, left_unplaced, len(rack_unowned), run_placed, dc, rack)
    failed_on = _unique(f.get("on") for f in findings if f.get("kind") == "nodetool")
    ring_read = any(isinstance(r, dict) and r.get("cluster_status") for r in reads or [])
    ring_nodes_read = any(True for dummy in _ring_nodes(reads))  # (no node read: "not in the ring read" not said)
    rack_up = not unplaced and "rack" not in judged.values()
    other_schema = _other_versions(schema, set(a for a, h in host_at.items() if h in members)) if rack_up else None
    stops, ring_size, notes = [], [], []
    for f in findings:
        kind = f.get("kind")
        if kind == "state" and judged[f["address"]] in ("outside", "left_out"):
            continue  # (in outside, left_out)
        elif kind == "nodetool" and ring_read and f.get("on") not in members:
            continue  # (in failed)
        elif kind == "count" and rack_up:
            ring_size.append([f["ring"], f["expected"]])
        elif kind == "schema" and other_schema:
            continue  # (in warnings)
        elif kind == "warning":  # (a schema disagreement while the versions are mixed: cassandra_health_findings)
            notes.append(f)
        else:
            stops.append(f)
    problems = cassandra_health_sentences(stops) + unread_lines
    if unread:
        problems.append("the check places each node of the run in the ring by its Host ID")
    problems += ["%s (rack %s) is not in the ring read: no node there has its Host ID (nodetool info on it)" % (
        h, rack) for h in ids.values() if h in unplaced and ring_nodes_read]
    failed = (first_verdict or {}).get("failed", []) + [h for h in failed_on if ring_read and h not in members]
    counted_view = peer if peer and peer not in failed_on else checker
    outside = _outside_nodes(findings, judged, first_verdict, members, counted_view, unreached)
    read_from = _unique((first_verdict or {}).get("read_from", [])
                        + [h for h in (checker, peer) if h and h not in failed_on])
    seen_down = [f.get("name") for f in findings if f.get("kind") == "state" and str(f.get("state"))[:1] == "D"]
    on_other_schema = [[host_at.get(a) or a, "on another schema version than the rack"] for a in other_schema or []]
    return {"outside": outside,
            "failed": _unique(failed),
            "warnings": _unique(on_other_schema + [_note_row(f) for f in notes]),
            "other_rack": _other_rack(reads, host_at, members, dc, rack),
            "ring_size": _unique(ring_size),
            "problems": problems,
            "host_id_unread": unread,
            "rack_seen_un": _rack_seen_un(reads, host_at, members, dc, rack),
            "read_from": read_from,
            "seen_from_run_only": _seen_from_run_only(read_from, run, addresses),
            "left_out": _left_out_nodes(findings, judged, first_verdict, reads, left_unplaced),
            "not_readers": [h for h in _unique([n.get("name") for n in outside] + seen_down + failed_on) if h]}


_DOING = {"J": "joining", "L": "leaving", "M": "moving"}


def _left_out_lines(verdict, dc, rack, command, viewer=()):
    """The warning on the nodes of the rack left out by --limit not UN (verdict's left_out): a row per node (by its
    name, else its address and "no host known by it"), then a WAY OUT per state, in the rows' order: down, start it
    (the start_rack command, command, with --limit that node and viewer: the node outside the rack this run read
    from and a node of the run seen UN, so that the ring is read from them too; the others left out stay as they
    are); the nodes known by their address only, together: a WARNING with the nodes left out they may be (not in
    the ring at an address of their own) as rows, then the WAY OUT that starts those; joining, leaving or moving,
    wait for it to finish; [] for none."""
    nodes = verdict.get("left_out") or []
    if not nodes:
        return []
    lines = [out.WARNING + "rack %s (%s), not UN, left out by --limit (not%sin%sthis%srun):" % (
        rack, dc, out.NBSP, out.NBSP, out.NBSP)]
    lines += out.table([[n.get("name") or n["address"], "/".join(n["states"])] + ([] if n.get("name") else [
        "no host known by it"]) for n in nodes], indent="  ")
    down = [n for n in nodes if any(s[:1] == "D" for s in n["states"])]
    by_address = [n for n in down if not n.get("name")]
    viewer = list(viewer or [])
    for n in down:
        if n.get("name"):
            lines += [_WAY_OUT + "start %s: run start_rack on it%s:" % (
                n["name"], _seen_too(viewer) if viewer else out.NBSP + "alone"),
                "  %s --limit %s" % (command, shlex.quote(",".join(viewer + [n["name"]])))]
        elif n is by_address[0]:
            lines += _by_address_lines(by_address, command, viewer)
    for n in nodes:
        busy = [s for s in n["states"] if s[:1] == "U" and s[1:] in _DOING]
        if n not in down and busy:
            lines.append(_WAY_OUT + "%s is %s (%s): wait for%s" % (
                n.get("name") or n["address"], _DOING[busy[0][1:]], busy[0], out.NBSP.join(["", "it", "to", "finish"])))
        elif n not in down:  # (another state: "?N", nodetool's own)
            lines.append(_WAY_OUT + "%s is %s: check it with nodetool status on it" % (
                n.get("name") or n["address"], "/".join(n["states"])))
    return lines


_WAY_OUT = "WAY OUT  "


def _seen_too(viewer):
    """The nodes added to a --limit to read the ring from -> what they are there for."""
    return ", the ring read from %s%stoo" % (" and ".join(viewer), out.NBSP)


def _by_address_lines(by_address, command, viewer):
    """The nodes left out down known by their address only (their rows above say "no host known by it") -> the
    nodes left out they may be: those not in the ring at an address of their own, a row each, then the WAY OUT
    that starts them; none: the WAY OUT that checks the addresses."""
    unplaced = by_address[0].get("unplaced") or []
    if not unplaced:  # (every node left out is in the ring now)
        return [_WAY_OUT + ("check %s with nodetool status" % by_address[0]["address"] if len(by_address) == 1 else
                            "check the addresses no host is known by with nodetool status")]
    one = len(unplaced) == 1
    lines = [out.WARNING + "left out by --limit, not in the ring at an address of %s:" % (
        "its own" if one else "their own")]
    lines += ["  " + h for h in unplaced]
    lines.append(_WAY_OUT + "start %s: run start_rack on %s%s:" % (
        "it" if one else "them", "it" if one else "them",
        _seen_too(viewer) if viewer else (out.NBSP + "alone" if one else "")))
    return lines + ["  %s --limit %s" % (command, shlex.quote(",".join(viewer + unplaced)))]


def cassandra_rack_outside_lines(verdict, unreached, dc, rack, command, run=None, left_out=None, start_command=""):
    """cassandra_rack_verdict's result after the wait, the hosts not reached over ssh, the rack's dc and rack, the
    command that checks the cluster, the rack's nodes in this run and those left out by --limit, the start_rack
    command (without --limit) -> the lines said once the rack's nodes are UN: that fact, the nodes seen UN
    (verdict's rack_seen_un; under --limit, the nodes of this run; nothing started when --limit leaves out the
    whole rack; not said when the stop was forced: verdict's problems); a WARNING per node of the rack the ring
    puts in another rack (verdict's other_rack); "the ring as seen from" the nodes of the run only (verdict's
    seen_from_run_only), then the warning on the nodes of the rack left out by --limit not UN (_left_out_lines),
    then, when the rest of the cluster has any, a WARNING with a row per node of the rest of the cluster (its
    states as nodetool status says them, "not reached over ssh", "nodetool status failed", verdict's warnings),
    one for the ring's size, the command.
    e.g. ({outside: [{address: "10.0.0.4", name: "n4", states: ["DN"]}], failed: [], warnings: [], ring_size:
    [[3, 4]], problems: [], rack_seen_un: ["n2", "n3"]}, [], "dc1", "r2", "ansible-playbook x") -> ["Rack r2
    (dc1), every node is UN:", "  n2", "  n3", "WARNING  the rest of the cluster:", "  n4  DN", "WARNING  the ring
    has 3 nodes, the inventory 4", "Check the cluster:", "  ansible-playbook x"]; nothing outside: the first
    lines alone."""
    facts, order = {}, []  # a node's facts, by its name (else its address)

    def add(name, fact):
        if name not in facts:
            order.append(name)
            facts[name] = []
        facts[name].append(fact)
    for node in verdict.get("outside", []):
        add(node.get("name") or node["address"], "/".join(node["states"]))
    for h in unreached or []:
        add(str(h), "not reached over ssh")
    for h in verdict.get("failed", []):
        add(str(h), "nodetool status failed")
    for name, fact in verdict.get("warnings", []):
        add(str(name), fact)
    rows = [[name, "  ".join(facts[name])] for name in order]  # (the facts in one cell: no column padded)
    up = [h for h in verdict.get("rack_seen_un") or [] if h in (run or []) or not left_out]
    if verdict.get("problems"):
        lines = []
    elif not left_out:
        lines = ["Rack %s (%s), every node is UN%s" % (rack, dc, ":" if up else ".")] + ["  " + h for h in up]
    elif run:
        lines = ["Rack %s (%s), the nodes of this run are UN%s" % (rack, dc, ":" if up else ".")] + [
            "  " + h for h in up]
    else:
        lines = ["Rack %s (%s): nothing started, --limit leaves out all of its nodes." % (rack, dc)]
    if lines:
        lines += [out.WARNING + "%s is in rack %s (%s) of the ring, %s (%s) in the inventory" % (h, r, d, rack, dc)
                  for h, d, r in verdict.get("other_rack") or []]
    views = verdict.get("seen_from_run_only") or []
    if lines and views:
        lines.append(out.WARNING + ("the ring as seen from %s only (no node outside this run read)" % views[0]
                                    if len(views) == 1 else
                                    "the ring as seen from the nodes of this run only (no other node read)"))
    outside_reader = [h for h in verdict.get("read_from") or [] if h not in (run or []) + (left_out or [])]
    lines += _left_out_lines(verdict, dc, rack, start_command, outside_reader[:1] + up[:1])
    if rows:
        lines += [out.WARNING + "the rest of the cluster:"] + out.table(rows, indent="  ")
    lines += [out.WARNING + "the ring has %s nodes, the inventory %s" % (ring, inventory)
              for ring, inventory in verdict.get("ring_size", [])]
    if rows or verdict.get("ring_size"):
        lines += ["Check the cluster:", "  " + str(command)]
    return out.fit(lines, WIDTH)


# the tasks of the cassandra_service role whose failure is the node's start failing: systemd's start fails; the
# node stopped while it was waited for (its journal: wait_normal_check.yml); still not NORMAL at the end of the
# wait (wait_normal.yml: it runs)
_START_FAILED = "Start or restart cassandra"
_STOPPED = "Cassandra is not running"
_NOT_NORMAL = "Give up after cassandra_service_wait_timeout"


def cassandra_rack_start_failed(result, task, node, rack, command=""):
    """The failed result of the cassandra_service role on a node of the rack (ansible_failed_result), the name of
    its task that failed, the node, its rack, the run's start_rack command -> the stop's lines, within 79 columns:
    the role's message in full, indented (a list: its first line, then the next ones further in than its own
    wrapped lines: the journal),
    then where to look (a command per line) and the way out with the command; not NORMAL at the end of the wait:
    what was seen ("not NORMAL after 600s ...: mode JOINING"), not "did not start" (it runs). Any other failure of
    the role (a refusal before the start: an account missing, a new seed): its message as it is, then, unless it
    has a WAY OUT of its own, "fix what the message above says" and the command.
    e.g. ({msg: ["cassandra on n2 is failed (failed, exit-code): ...", "<journal line>"]}, "Cassandra is not
    running", "n2", "r2", "ansible-playbook x") -> ["n2 (rack r2) did not start:", "  cassandra on n2 is failed
    ...", "      <journal line>", ..., "WAY OUT  fix it, then run start_rack again:", "  ansible-playbook x"]."""
    msg = (result or {}).get("msg") or "failed"
    again = ["  " + str(command)] if command else []
    if task not in (_START_FAILED, _STOPPED, _NOT_NORMAL):  # its words
        said = [str(line) for line in msg] if isinstance(msg, (list, tuple)) else str(msg).split("\n")
        if not any(line.strip().startswith("WAY OUT") for line in said):
            said += ["WAY OUT  fix what the message above says, then run start_rack again:"] + again
        return out.fit(said, WIDTH)
    if isinstance(msg, (list, tuple)):
        said = ["  " + str(msg[0])] + ["      " + str(line) for line in msg[1:]]
    else:
        said = ["  " + line for line in str(msg).split("\n")]
    said = [line.rstrip() for line in said if line.strip()]
    if task == _NOT_NORMAL:
        head = said[0].strip()
        head = head[len(node) + 1:] if head.startswith(str(node) + " ") else head
        return out.fit(["%s (rack %s) %s" % (node, rack, head)] + said[1:] + [
            "see on it:", "  nodetool netstats", "  journalctl -u cassandra",
            "WAY OUT  once it is NORMAL (or fixed), run start_rack again:"] + again, WIDTH)
    return out.fit(["%s (rack %s) did not start:" % (node, rack)] + said + [
        "see why on it:", "  systemctl status cassandra", "  journalctl -u cassandra",
        "WAY OUT  fix it, then run start_rack again:"] + again, WIDTH)


def cassandra_rack_stop_refused(problems, warnings, nodes, dc, rack, cluster, command):
    """stop_rack's refusal when the rack's stop would take more than one replica of some data down: the problems,
    the warnings, the rack's nodes, its dc and rack, the cluster's name, the stop_rack command forced (-e
    cassandra_rack_force=true) -> its REFUSED block (cassandra_refused): the fact, the problems, the nodes as rows,
    the warnings, what a forced stop does, then the way out with the command.
    e.g. (["ks has 3 replicas in dc1, which has 2 rack(s): ..."], [], ["n2"], "dc1", "r2", "prod", "ansible-playbook
    x") -> ["REFUSED  stop_rack  prod  nothing was changed", "  Stopping dc1/r2 would take down more than one replica
    of some data:", "    ks has 3 ...", "  its nodes:", "    n2", "  Forced, ...", "  WAY OUT  fix that first, or
    force the stop:", "    ansible-playbook x"]."""
    lines = ["Stopping %s/%s would take down more than one replica of some data:" % (dc, rack)]
    lines += ["  " + str(p) for p in problems or []] + ["its nodes:"] + ["  " + str(n) for n in nodes or []]
    lines += [out.WARNING + str(w) for w in warnings or []]
    lines += ["Forced, reads and writes needing those replicas fail, and data only on them is unavailable until"
              " the rack is back.", "WAY OUT  fix that first, or force the stop:", "  " + str(command)]
    return cassandra_refused("stop_rack  %s" % cluster, lines)


class FilterModule(object):
    def filters(self):
        return {"cassandra_host_racks": cassandra_host_racks,
                "cassandra_rack_members": cassandra_rack_members,
                "cassandra_rack_verdict": cassandra_rack_verdict,
                "cassandra_rack_outside_lines": cassandra_rack_outside_lines,
                "cassandra_rack_start_failed": cassandra_rack_start_failed,
                "cassandra_rack_stop_refused": cassandra_rack_stop_refused}
