# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The cluster check of start_rack (playbooks/start_rack.yml): the rack's nodes must be UN; the rest of the cluster
is only a warning, unless a node could be one of the rack.

Which inventory host a node of the ring is (_placement), computed once per read:
- a node of the run: the ring node with its Host ID, as nodetool info on it gives it (what it says of itself;
  one whose Host ID is read is placed by it alone, not by an address);
- any other host: the ring node at an address its variables or facts give (cassandra_host_addresses);
- while every node of the run is placed, a host still not placed (out of the play or not reached: no facts): what
  its names resolve to on the controller, for a ring address no host has yet.
A ring node no host is placed at is "no inventory host is known by its address"; a node of the rack no ring node
is placed at is "not placed".

Which rack a node of the ring is of (_judge), the first rule that matches:

  the node of the ring                                          judged
  a node of the rack left out by --limit                        left out
  a node of the rack (of the run)                               of the rack
  no host known by its address, every node of the run placed,  left out (it
    a node left out not placed, and the ring puts it in the       may be that
    rack or the nodes not placed outnumber the ring's nodes       one)
    of the rack no host is known by
  put in the rack's dc and rack by the ring                     of the rack
  no host known by its address, a node of the run not placed,   of the rack
    and the nodes of the rack not placed outnumber the ring's     (why said)
    nodes of the rack no host is known by
  any other                                                     outside

What the check does with what it finds (cassandra_rack_verdict); "the rack is placed": every node of the rack is:

  found                                                         outcome
  a node of the rack not reached over ssh                       STOP at once
  a node of the rack down                                       wait, then STOP
  a node of the rack left out by --limit (not started by the    WARNING, not
    run), not UN (its state from the last read)                   waited for
  a node of the rack in another state, or nodetool failing on   STOP
    it
  a node of the run whose Host ID no ring read has              STOP
  a node of the run not placed, the nodes of the rack not       STOP (nothing
    placed outnumbering the ring's nodes of the rack no host      else to stop
    is known by (one is in no ring node of the rack it may be)    on)
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

Not covered (two faults, or rare): a node of the run whose nodetool info fails is placed by its addresses; not
placed, it may be counted for a ring node of the rack no host is known by while it is in another rack, or out of the
ring while a node no host has keeps the ring's size even (no stop). A host outside the rack reached over ssh whose
ping or fact gathering fails fails the run. Two hosts giving one address: refused before the start
(cassandra_rack_shared_addresses).

cassandra_rack_members: the nodes the inventory puts in a rack (every node of the cluster, under --limit too).
cassandra_rack_shared_addresses: the stop before the start when two hosts give the same ring address.
cassandra_rack_verdict: the tables above applied to a read of the cluster.
cassandra_rack_outside_lines: the rack's nodes UN (those of the run under --limit; a node of the run not placed: the
rack's nodes UN in the ring, then that node), its ring nodes UN no host is known by, then the warnings (a node of the
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
from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import _host_var
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import compressed_address as _ip
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

# the cassandra_config defaults (roles/cassandra_config/defaults/main.yml; a unit test ties them)
DEFAULT_DC = "dc1"
DEFAULT_RACK = "rack1"

# the width of an operator message: an 80-column terminal
WIDTH = 79

# where an inventory host's ring address is given, after "give ... that address"
_WHERE = ["  (broadcast_address in cassandra_extra_settings, else", "  cassandra_listen_address or ansible_host)"]

# the other ring nodes no host is known by listed under the stop of a node of the run not placed, at most
OTHERS_SHOWN = 5


def cassandra_rack_members(hosts, hostvars, dc, rack):
    """The cluster's nodes, their hostvars, a dc and a rack -> the nodes whose cassandra_dc and cassandra_rack (the
    cassandra_config defaults when not set) are that dc and rack, in their order.
    e.g. (["n1", "n2"], {"n1": {"cassandra_rack": "r1"}, "n2": {"cassandra_rack": "r2"}}, "dc1", "r2") -> ["n2"]."""
    members = []
    for h in hosts or []:
        variables = hostvars.get(h) or {}
        if (str(variables.get("cassandra_dc", DEFAULT_DC)) == str(dc)
                and str(variables.get("cassandra_rack", DEFAULT_RACK)) == str(rack)):
            members.append(h)
    return members


def cassandra_rack_shared_addresses(hosts, hostvars, command=""):
    """The cluster's nodes, their hostvars, the run's start_rack command -> the stop's lines when two hosts give the
    same ring address, [] for none. A host's ring address: broadcast_address (cassandra_extra_settings), else
    cassandra_listen_address, else its ansible_host; two hosts sharing an ansible_host only (ccm-like hosts on
    127.0.0.1, a jump host) are not two hosts at one ring address.
    e.g. (["n2", "n3"], {n2: {ansible_host: "10.0.0.2"}, n3: {cassandra_listen_address: "10.0.0.2"}}, "x") ->
    ["10.0.0.2 is given by two hosts:", "  n2  ansible_host", "  n3  cassandra_listen_address", ...]."""
    given = {}  # address -> [[host, the variable that gives it]], and whether a ring address variable does
    for h in hosts or []:
        for variable, path in (("broadcast_address", ("cassandra_extra_settings", "broadcast_address")),
                               ("cassandra_listen_address", ("cassandra_listen_address",)),
                               ("ansible_host", ("ansible_host",))):
            address = _host_var(hostvars, h, *path)
            if address and address != "localhost":
                given.setdefault(_ip(address), []).append([h, variable])
                break
    shared = [(a, rows) for a, rows in given.items()
              if len(rows) > 1 and any(variable != "ansible_host" for h, variable in rows)]
    lines = []
    for address, rows in shared:
        lines += ["%s is given by %s hosts:" % (address, len(rows))] + out.table(rows, indent="  ")
    if not lines:
        return []
    return out.fit(lines + ["nothing was started: the check after it tells the nodes of the ring apart by",
                            "  address",
                            "WAY OUT  give each host its own address, then run start_rack again:",
                            "  " + str(command)], WIDTH)


def _ring_nodes(reads):
    """The cassandra_status results read -> each node of their rings, read after read: (address, dc, rack, state
    as nodetool status says it, Host ID)."""
    for read in reads or []:
        for dc, value in ((read.get("cluster_status") or {}) if isinstance(read, dict) else {}).items():
            for n in value.get("nodes", []):
                yield (_ip(n["address"]), str(dc), str(n.get("rack")),
                       str(n.get("status", "")) + str(n.get("state", "")), n.get("host_id"))


def _host_ids(infos, run):
    """{host: its nodetool info output}, the nodes of the run -> {Host ID: host} of those whose output gives one."""
    ids = {}
    for h in run:
        found = re.findall(r"(?m)^ID\s*:\s*(\S+)", str((infos or {}).get(h) or ""))
        if found:
            ids[found[0]] = h
    return ids


def _placement(reads, run, addresses, ids):
    """The reads, the nodes of the run, every host's addresses (cassandra_host_addresses), {Host ID: node of the
    run} -> {ring address: inventory host}: the module docstring's placement."""
    ring = _unique(n[0] for n in _ring_nodes(reads))
    at = dict((n[0], ids[n[4]]) for n in _ring_nodes(reads) if n[4] in ids)
    for address, host in (addresses or {}).items():
        if _ip(address) in ring and _ip(address) not in at and host not in ids.values():
            at[_ip(address)] = host
    rest = [a for a in ring if a not in at]
    if rest and set(run) <= set(at.values()):
        hosts = _unique(h for h in (addresses or {}).values() if h not in at.values())
        names = dict((h, [a for a, x in addresses.items() if x == h]) for h in hosts)
        at.update((a, h) for a, h in hosts_by_address(names, rest).items() if a in rest)
    return at


def _named(finding, at):
    """A finding, the placement -> a state finding by the name its address is placed at (cassandra_health_findings
    names it by the addresses alone), its text too; any other finding as it is."""
    if finding.get("kind") != "state":
        return finding
    name = at.get(_ip(finding["address"]))
    if name == finding.get("name"):
        return finding
    where = "%s, %s" % (finding["address"], finding["rack"]) if name else finding["rack"]
    return dict(finding, name=name, text="%s (%s) is %s, seen from %s" % (
        name or finding["address"], where, finding["state"], finding["seen_from"]))


def _judge(findings, at, members, left_out, unplaced, rack_unowned, dc, rack):
    """The state findings (_named), the placement, the rack's nodes, those left out by --limit, those not placed,
    the number of the ring's nodes of the rack no host is known by -> {address: "rack", "left_out", "maybe_rack" (of
    the rack only for want of a host known by its address) or "outside"}: the module docstring's first table."""
    run_placed = set(unplaced) <= set(left_out)
    may_be_one = len(unplaced) > rack_unowned  # (a ring node no host is known by may be a node not placed)
    judged = {}
    for f in findings:
        if f.get("kind") != "state":
            continue
        host = f.get("name")
        in_rack = (str(f.get("dc")), str(f.get("rack"))) == (str(dc), str(rack))
        if host in left_out or (host is None and run_placed and unplaced and (in_rack or may_be_one)):
            verdict = "left_out"
        elif host in members or in_rack:
            verdict = "rack"
        elif host is None and may_be_one:
            verdict = "maybe_rack"
        else:
            verdict = "outside"
        if judged.get(f["address"]) != "rack":  # (in the rack for one view: of the rack)
            judged[f["address"]] = verdict
    return judged


def _rack_seen_un(reads, at, members, dc, rack):
    """The reads, the placement, the rack's nodes, its dc and rack -> the nodes of the ring of the rack (placed at a
    node of the rack, or put in the rack's dc and rack by the ring), UN in every read that has them, in the ring's
    order: [address, its host or None]."""
    states, order = {}, []
    for address, ring_dc, ring_rack, state, dummy in _ring_nodes(reads):
        if at.get(address) in members or (ring_dc, ring_rack) == (str(dc), str(rack)):
            if address not in states:
                order.append(address)
            states.setdefault(address, set()).add(state)
    return [[a, at.get(a)] for a in order if states[a] == {"UN"}]


def _other_rack(reads, at, members, dc, rack):
    """The reads, the placement, the rack's nodes, its dc and rack -> [[node, its dc, its rack]] of the nodes of
    the rack the ring puts in another dc or rack."""
    rows = []
    for address, ring_dc, ring_rack, dummy, dummy in _ring_nodes(reads):
        row = [at.get(address), ring_dc, ring_rack]
        if row[0] in members and (ring_dc, ring_rack) != (str(dc), str(rack)) and row not in rows:
            rows.append(row)
    return rows


def _unowned(reads, at):
    """The reads, the placement -> the ring's nodes no host is known by, in the ring's order: [[address, dc, rack,
    its states as nodetool status says them]]."""
    nodes, by_address = [], {}
    for address, ring_dc, ring_rack, state, dummy in _ring_nodes(reads):
        if address in at:
            continue
        if address not in by_address:
            by_address[address] = [address, ring_dc, ring_rack, []]
            nodes.append(by_address[address])
        if state not in by_address[address][3]:
            by_address[address][3].append(state)
    return nodes


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
                           first_verdict=None, left_out=None, unreached=None, infos=None):
    """cassandra_health_findings' result of a read, the rack's nodes (cassandra_rack_members), its dc and rack, the
    node of the rack read from (checker), the other node read from (peer), the addresses of every host
    (cassandra_host_addresses), the cassandra_status results read, the cassandra_schema result, first_verdict: this
    filter's result before the wait (None: this read is the one before the wait), left_out: the rack's nodes left
    out by --limit (out of the play: this run does not start them), unreached: the hosts not reached over ssh,
    infos: {node of the run: its nodetool info output} -> the module docstring's tables applied:
      outside: the nodes outside the rack not UN, [{address, name, states}] (_outside_nodes).
      failed: the nodes outside the rack whose nodetool status failed (the first verdict's too), when a ring was
        read.
      warnings: the other rows of the warning, [[name, fact]]: the nodes on another schema version (a node by its
        inventory name, else its address), then the warning findings (schema versions differing while the
        versions are mixed: cassandra_allow_mixed_versions).
      other_rack: the nodes of the rack the ring puts in another dc or rack, [[name, dc, rack]].
      ring_size: [[the ring's size, the inventory's]] when they differ and that is a warning.
      maybe_rack_lines: why each node judged of the rack only for want of a host known by its address is, then
        what to do (after the wait: "then the command below", the stop's WAY OUT); [] for none.
      problems: what stops the run, as sentences (cassandra_health_sentences), a node of the run whose Host ID no
        ring read has; else, a node of the run not placed while the nodes of the rack not placed outnumber the
        ring's nodes of the rack no host is known by: its lines (_unplaced_lines).
      rack_seen_un: the nodes of the rack UN in the rings read (_rack_seen_un) a host is placed at, by name.
      rack_unowned: the nodes of the rack UN in the rings read no host is known by, [[address, "UN"]].
      run_unplaced: the nodes of the run not placed.
      read_from: the nodes read from (checker, peer; not one whose nodetool failed), this read's and the first
        verdict's.
      seen_from_run_only: read_from when they are all nodes of this run and the cluster has others (none of them
        reached in the play; a node sees itself UN): [] otherwise.
      left_out: the nodes of the rack left out by --limit not UN (_left_out_nodes). Not waited for, not in
        problems: a warning (cassandra_rack_outside_lines).
      not_readers: the hosts not to read from again: the nodes outside the rack not UN, seen down, or whose
        nodetool failed.
    e.g. ([<n4 DN from n2>, <n4 DN from n1>], ["n2", "n3"], "dc1", "r2", "n2", "n1") -> {outside: [{address:
    "10.0.0.4", name: "n4", states: ["DN"]}], failed: [], warnings: [], other_rack: [], ring_size: [],
    maybe_rack_lines: [], problems: [], rack_seen_un: ["n2", "n3"], rack_unowned: [], run_unplaced: [], read_from:
    ["n2", "n1"], seen_from_run_only: [], left_out: [], not_readers: ["n4"]}."""
    members, left_out = list(members or []), list(left_out or [])
    run = [h for h in members if h not in left_out]
    ids = _host_ids(infos, run)
    at = _placement(reads, run, addresses, ids)
    findings = [_named(f, at) for f in findings or []]
    unplaced = [h for h in members if h not in at.values()]
    unowned = _unowned(reads, at)
    rack_unowned = [n for n in unowned if (n[1], n[2]) == (str(dc), str(rack))]
    judged = _judge(findings, at, members, left_out, unplaced, len(rack_unowned), dc, rack)
    failed_on = _unique(f.get("on") for f in findings if f.get("kind") == "nodetool")
    ring_read = any(isinstance(r, dict) and r.get("cluster_status") for r in reads or [])
    rack_up = not unplaced and "rack" not in judged.values()
    other_schema = _other_versions(schema, set(a for a, h in at.items() if h in members)) if rack_up else None
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
    absent = ["%s (rack %s) is not in the ring read: no node there has its Host ID (nodetool info on it)" % (
        h, rack) for h in ids.values() if h in unplaced]
    run_unplaced = [h for h in run if h in unplaced]
    problems = cassandra_health_sentences(stops) + absent
    failed = (first_verdict or {}).get("failed", []) + [h for h in failed_on if ring_read and h not in members]
    counted_view = peer if peer and peer not in failed_on else checker
    outside = _outside_nodes(findings, judged, first_verdict, members, counted_view, unreached)
    read_from = _unique((first_verdict or {}).get("read_from", [])
                        + [h for h in (checker, peer) if h and h not in failed_on])
    seen_down = [f.get("name") for f in findings if f.get("kind") == "state" and str(f.get("state"))[:1] == "D"]
    seen_un = _rack_seen_un(reads, at, members, dc, rack)
    on_other_schema = [[at.get(a) or a, "on another schema version than the rack"] for a in other_schema or []]
    return {"outside": outside,
            "failed": _unique(failed),
            "warnings": _unique(on_other_schema + [_note_row(f) for f in notes]),
            "other_rack": _other_rack(reads, at, members, dc, rack),
            "ring_size": _unique(ring_size),
            "maybe_rack_lines": _maybe_rack_lines(findings, judged, rack, first_verdict is not None),
            "problems": problems or _unplaced_lines(run_unplaced, unplaced, unowned, dc, rack),
            "rack_seen_un": [name for a, name in seen_un if name],
            "rack_unowned": [[a, "UN"] for a, name in seen_un if not name],
            "run_unplaced": run_unplaced,
            "read_from": read_from,
            "seen_from_run_only": _seen_from_run_only(read_from, run, addresses),
            "left_out": _left_out_nodes(findings, judged, first_verdict, reads,
                                        [h for h in left_out if h in unplaced]),
            "not_readers": [h for h in _unique([n.get("name") for n in outside] + seen_down + failed_on) if h]}


def _unplaced_lines(run_unplaced, unplaced, unowned, dc, rack):
    """The nodes of the run not placed, the nodes of the rack not placed, the ring's nodes no host is known by
    (_unowned), the rack's dc and rack -> when a node of the run is not placed and the nodes not placed outnumber
    the ring's nodes of the rack no host is known by (one of them is in no ring node of the rack it may be: the ring
    puts it in another rack, or nowhere), the stop's lines: those nodes, the ring's nodes no host is known by of the
    rack, then of the others of its dc (OTHERS_SHOWN at most), what to do; else []."""
    rack_rows = [[a, "/".join(states)] for a, d, r, states in unowned if (d, r) == (str(dc), str(rack))]
    if not run_unplaced or len(unplaced) <= len(rack_rows):
        return []
    lines = ["%s (rack %s%s) is not in the ring at an address its variables give" % (
        h, rack, "" if h in run_unplaced else ", left out by --limit") for h in unplaced]
    head = "in rack %s of the ring, no inventory host known by the address:" % rack
    lines += [head] + out.table(rack_rows, indent="  ") if rack_rows else [head + " none"]
    others = [[a, r, "/".join(states)] for a, d, r, states in unowned if d == str(dc) and r != str(rack)]
    if others:
        lines += ["in the other racks of %s, no inventory host known by the address:" % dc]
        lines += out.table(others[:OTHERS_SHOWN], indent="  ")
        if len(others) > OTHERS_SHOWN:
            lines.append("  and %s more (nodetool status)" % (len(others) - OTHERS_SHOWN))
    if not rack_rows and not others:
        return out.fit(lines + ["find %s in the ring: nodetool status, its Host ID in nodetool info on it" % (
            " and ".join(run_unplaced))], WIDTH)
    return out.fit(lines + ["if %s is at one of these addresses: give it that address" % " or ".join(run_unplaced)]
                   + _WHERE, WIDTH)


def _maybe_rack_lines(findings, judged, rack, last):
    """The findings and how they were judged, the rack, whether the stop says them (the last read) -> why each
    node judged of the rack only for want of a host known by its address is, then the way out; [] for none.
    e.g. ([<10.0.0.4 r3 DN, no name>], {"10.0.0.4": "maybe_rack"}, "r2", True) -> ["10.0.0.4 (dc1, r3): no inventory
    host is known by this address,", "  so it is judged as a node of rack r2", "if it is outside rack r2: give its
    inventory host that address", "  (broadcast_address in ...", "  cassandra_listen_address or ansible_host), then
    the command below"] (the WAY OUT's, under them)."""
    lines, said = [], set()
    for f in findings:
        if f.get("kind") != "state" or f["address"] in said or judged[f["address"]] != "maybe_rack":
            continue
        said.add(f["address"])
        lines += ["%s (%s, %s): no inventory host is known by this address," % (f["address"], f.get("dc"),
                                                                                f.get("rack")),
                  "  so it is judged as a node of rack %s" % rack]
    if not lines:
        return []
    which = "it is" if len(said) == 1 else "one is"
    where = _WHERE[:-1] + [_WHERE[-1] + (", then the command below" if last else "")]
    return out.fit(lines + ["if %s outside rack %s: give its inventory host that address" % (which, rack)] + where,
                   WIDTH)


_DOING = {"J": "joining", "L": "leaving", "M": "moving"}


def _left_out_lines(verdict, dc, rack, command, viewer=()):
    """The warning on the nodes of the rack left out by --limit not UN (verdict's left_out): a row per node (by its
    name, else its address), then what to do per state, in the rows' order: down, start it (the start_rack
    command, command, with --limit that node and viewer: the node outside the rack this run read from and a node
    of the run seen UN, so that the ring is read from them too; the others left out stay as they are); the nodes
    known by their address only, together: the nodes left out they may be (not in the ring at an address of their
    own), --limit those; joining, leaving or moving, wait for it to finish; [] for none."""
    nodes = verdict.get("left_out") or []
    if not nodes:
        return []
    lines = [out.WARNING + "rack %s (%s), not UN, left out by --limit (not%sin%sthis%srun):" % (
        rack, dc, out.NBSP, out.NBSP, out.NBSP)]
    lines += out.table([[n.get("name") or n["address"], "/".join(n["states"])] for n in nodes], indent="  ")
    down = [n for n in nodes if any(s[:1] == "D" for s in n["states"])]
    by_address = [n for n in down if not n.get("name")]
    viewer = list(viewer or [])
    for n in down:
        if n.get("name"):
            lines += ["start %s: run start_rack on it%s" % (n["name"], _seen_too(viewer) if viewer else out.NBSP + "alone"),
                      "  %s --limit %s" % (command, shlex.quote(",".join(viewer + [n["name"]])))]
        elif n is by_address[0]:
            lines += _by_address_lines(by_address, command, viewer)
    for n in nodes:
        busy = [s for s in n["states"] if s[:1] == "U" and s[1:] in _DOING]
        if n not in down and busy:
            lines.append("%s is %s (%s): wait for%s" % (
                n.get("name") or n["address"], _DOING[busy[0][1:]], busy[0], out.NBSP.join(["", "it", "to", "finish"])))
        elif n not in down:  # (another state: "?N", nodetool's own)
            lines.append("%s is %s: check it with nodetool status on it" % (
                n.get("name") or n["address"], "/".join(n["states"])))
    return lines


def _seen_too(viewer):
    """The nodes added to a --limit to read the ring from -> what they are there for."""
    return ", the ring read from %s%stoo" % (" and ".join(viewer), out.NBSP)


def _by_address_lines(by_address, command, viewer):
    """The nodes left out down known by their address only -> the start of the nodes left out they may be: those
    not in the ring at an address of their own."""
    unplaced = by_address[0].get("unplaced") or []
    start = "start " if unplaced else ""
    if len(by_address) == 1:
        lines = ["%s%s: no host known by that address" % (start, by_address[0]["address"])]
    else:
        lines = ["%sthe nodes at these addresses, no host known by them:" % start] + [
            "  " + n["address"] for n in by_address]
    if not unplaced:  # (every node left out is in the ring now)
        return lines + ["  check %s with nodetool status" % ("it" if len(by_address) == 1 else "them")]
    lines += ["  %s left out by --limit, not in the ring at an address of %s:" % (
        "the node" if len(unplaced) == 1 else "the nodes", "its own" if len(unplaced) == 1 else "their own")]
    lines += ["    " + h for h in unplaced]
    if viewer:
        lines.append("  run start_rack on %s%s:" % ("it" if len(unplaced) == 1 else "them", _seen_too(viewer)))
    return lines + ["  %s --limit %s" % (command, shlex.quote(",".join(viewer + unplaced)))]


def cassandra_rack_outside_lines(verdict, unreached, dc, rack, command, run=None, left_out=None, start_command=""):
    """cassandra_rack_verdict's result after the wait, the hosts not reached over ssh, the rack's dc and rack, the
    command that checks the cluster, the rack's nodes in this run and those left out by --limit, the start_rack
    command (without --limit) -> the lines said once the rack's nodes are UN: that fact, the nodes seen UN
    (verdict's rack_seen_un; under --limit, the nodes of this run; nothing started when --limit leaves out the
    whole rack; not said when the stop was forced: verdict's problems), the rack's ring nodes UN no host is known
    by (verdict's rack_unowned, rows); a node of the run not placed (verdict's run_unplaced): what the ring shows
    instead, the rack's nodes UN in it (by name, else by address), then those nodes; a WARNING per node of the
    rack the ring puts in another rack (verdict's other_rack); "the ring as seen from" the nodes of the run only
    (verdict's seen_from_run_only), then the warning on the nodes of the rack left out by --limit not UN
    (_left_out_lines), then, when the rest of the cluster has any, a WARNING with a row
    per node of the rest of the cluster (its states as nodetool status says them, "not reached over ssh",
    "nodetool status failed", verdict's warnings), one for the ring's size, the command.
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
    unowned = verdict.get("rack_unowned") or []
    unplaced = verdict.get("run_unplaced") or []
    if verdict.get("problems"):
        lines = []
    elif unplaced:  # (what the ring shows: a node of the run is not seen UN by its name)
        lines = ["Rack %s (%s), UN in the ring:" % (rack, dc)] + out.table(
            [[h] for h in verdict.get("rack_seen_un") or []]
            + [[a, "no inventory host is known by this address"] for a, state in unowned], indent="  ")
        lines += ["not in the ring at an address %s variables give:" % ("its" if len(unplaced) == 1 else "their")]
        lines += ["  " + h for h in unplaced]
        unowned = []
    elif not left_out:
        lines = ["Rack %s (%s), every node is UN%s" % (rack, dc, ":" if up else ".")] + ["  " + h for h in up]
    elif run:
        lines = ["Rack %s (%s), the nodes of this run are UN%s" % (rack, dc, ":" if up else ".")] + [
            "  " + h for h in up]
    else:
        lines = ["Rack %s (%s): nothing started, --limit leaves out all of its nodes." % (rack, dc)]
    if lines and unowned:
        one = len(unowned) == 1
        lines += ["%s of rack %s in the ring, no inventory host known by %s:" % (
            "a node" if one else "nodes", rack, "its address" if one else "their addresses")] + out.table(
                unowned, indent="  ")
    if lines:
        lines += [out.WARNING + "%s is in rack %s (%s) of the ring, %s in the inventory" % (h, r, d, rack)
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
        return {"cassandra_rack_members": cassandra_rack_members,
                "cassandra_rack_shared_addresses": cassandra_rack_shared_addresses,
                "cassandra_rack_verdict": cassandra_rack_verdict,
                "cassandra_rack_outside_lines": cassandra_rack_outside_lines,
                "cassandra_rack_start_failed": cassandra_rack_start_failed,
                "cassandra_rack_stop_refused": cassandra_rack_stop_refused}
