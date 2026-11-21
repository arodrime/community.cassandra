# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The cluster check of start_rack (playbooks/start_rack.yml): the rack's nodes must be UN; the rest of the cluster
is only a warning, unless a node could be one of the rack.

Which rack a node of the ring is of (_judge), the first rule that matches:

  the node                                                      judged
  its inventory name is a rack node's                           of the rack
  its address is one a rack node gives in its variables         of the rack
  the ring puts it in the rack's dc and rack                    of the rack
  no host has its address, and a rack node is not in the        of the rack
    ring at an address it gives                                   (why said)
  any other                                                     outside

What the check does with what it finds (cassandra_rack_verdict); "the rack is in the ring": every rack node is,
at an address it gives:

  found                                                         outcome
  a rack node not reached over ssh                              STOP at once
  a rack node down                                              wait, then STOP
  a rack node left out by --limit (not started by the run),     WARNING, not
    not UN (its state from the last read)                         waited for
  a rack node in another state, or nodetool failing on it       STOP
  an outside node not UN for the node outside the rack read     WARNING, not
    from (its read failed, or none read: for the rack node)       waited for
  an outside node not UN for the rack node alone, or for a      wait, then
    second node of the rack read from (just started: it may       WARNING
    still see a peer down)
  an outside node not reached over ssh                          WARNING
  nodetool failing on an outside node, a ring read from         WARNING
    another node
  a ring of another size than the inventory, the rack in        WARNING
    the ring and UN
  a schema disagreement, the rack in the ring, UN and on        WARNING
    one version held by more than half of the nodes
    describecluster lists, a node outside the rack among them
    (the nodes on another one named)
  a schema disagreement while the versions are mixed            WARNING
    (cassandra_allow_mixed_versions)
  anything else (streams on the rack node, the ring size or     STOP
    the schema otherwise, no ring read at all...)

The schema's majority, not the version of the node outside the rack read from: that node may be the one behind.
The rack just came back: its own nodes never make the majority alone (a node outside the rack, listed, so reached
by describecluster, must be on its version). A rack alone on its version, or a cluster split even: a stop.

Not covered (two faults, or rare): a rack node out of the ring while a node no host has keeps the ring's size
even (no stop); a host outside the rack reached over ssh whose ping or fact gathering fails fails the run.

cassandra_rack_members: the nodes the inventory puts in a rack (every node of the cluster, under --limit too).
cassandra_rack_verdict: the table above applied to a read of the cluster.
cassandra_rack_outside_lines: the rack's nodes UN (those of the run under --limit), its ring nodes UN no host has
the address of, then the warnings (the nodes of the rack left out by --limit not UN, the rest of the cluster), once
the rack is UN.
cassandra_rack_start_failed: a node of the rack that did not start (start_rack's rescue).
cassandra_rack_stop_refused: stop_rack's refusal (more than one replica down), with the other screens of a rack's
start and stop.
"""
from __future__ import absolute_import, division, print_function
__metaclass__ = type

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


def _judge(findings, members, dc, rack, addresses, reads):
    """The state findings -> ({address: "rack", "unknown" (of the rack only for want of a host known by its
    address) or "outside"}, whether every node of the rack is in the ring at an address it gives, {ring address:
    rack node} of the rack's nodes found there). addresses: cassandra_host_addresses of every host (one host's
    ansible_host never takes the address another one is in the ring by: 127.0.0.1 for ccm-like nodes), the rack's
    nodes' kept here; reads: the cassandra_status results read. The rules: the module docstring's first table."""
    member_at = {}
    for address, name in (addresses or {}).items():
        if name in (members or []):
            member_at.setdefault(_ip(address), name)
    in_ring = set(_ip(n["address"]) for read in reads or [] if isinstance(read, dict)
                  for value in (read.get("cluster_status") or {}).values() for n in value.get("nodes", []))
    found = dict((a, member_at[a]) for a in in_ring if a in member_at)
    all_in_ring = set(members or []) <= set(found.values())
    judged = {}
    for f in findings or []:
        if f.get("kind") != "state":
            continue
        name, address = f.get("name"), f["address"]
        if (name in (members or []) or _ip(address) in member_at
                or (str(f.get("dc")) == str(dc) and str(f.get("rack")) == str(rack))):
            judged[address] = "rack"
        elif not name and not all_in_ring:
            judged.setdefault(address, "unknown")
        else:
            judged.setdefault(address, "outside")
    return judged, all_in_ring, found


def _rack_seen_un(reads, members, dc, rack, addresses, names):
    """The cassandra_status results read, the rack's nodes, its dc and rack, every host's addresses, {ring address:
    name} -> the nodes of the ring of the rack (at an address a rack node gives, or put in the rack's dc and rack by
    the ring), UN in every read that has them, in the ring's order: [address, its name or None]."""
    member_at = set(_ip(a) for a, name in (addresses or {}).items() if name in (members or []))
    states, order = {}, []
    for read in reads or []:
        if not isinstance(read, dict):
            continue
        for ring_dc, value in (read.get("cluster_status") or {}).items():
            for n in value.get("nodes", []):
                address = _ip(n["address"])
                if address in member_at or (str(ring_dc) == str(dc) and str(n.get("rack")) == str(rack)):
                    if address not in states:
                        order.append(address)
                    states.setdefault(address, set()).add(str(n.get("status", "")) + str(n.get("state", "")))
    return [[a, names.get(a)] for a in order if states[a] == {"UN"}]


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
    nodes describecluster lists, a node outside the rack among them; else None (the disagreement may be the rack's: see
    the module docstring)."""
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


def _names(findings, addresses):
    """{ring address: inventory name}: the names the state findings give, else the addresses of every host."""
    names = {}
    for f in findings:
        if f.get("kind") == "state" and f.get("name"):
            names.setdefault(_ip(f["address"]), f["name"])
    for address, name in (addresses or {}).items():
        names.setdefault(_ip(address), name)
    return names


def _note_row(finding):
    """A warning finding -> its row: [what, the rest of its text]."""
    what, text = str(finding.get("what") or "the cluster"), str(finding.get("text", ""))
    return [what, text[len(what) + 1:] if text.startswith(what + " ") else text]


def _ring_states(reads):
    """The cassandra_status results read -> {ring address: [its states, as nodetool status says them]}."""
    states = {}
    for read in reads or []:
        if isinstance(read, dict):
            for value in (read.get("cluster_status") or {}).values():
                for n in value.get("nodes", []):
                    said = states.setdefault(_ip(n["address"]), [])
                    state = str(n.get("status", "")) + str(n.get("state", ""))
                    if state not in said:
                        said.append(state)
    return states


def _left_out_again(first_left_out, states):
    """The first verdict's nodes left out by --limit not UN, the states of the reads after the wait (_ring_states:
    cassandra_service_health_any_ok kept them out of its findings) -> those nodes with their states in these
    reads: one UN in every read now dropped, one not in them (no ring read) as it was."""
    again = []
    for n in first_left_out or []:
        now = states.get(_ip(n["address"]))
        if now is None:
            again.append(dict(n, states=list(n["states"])))
        elif [s for s in now if s != "UN"]:
            again.append(dict(n, states=[s for s in now if s != "UN"]))
    return again


def _left_out_names(left_out, addresses, reads):
    """The rack's nodes left out by --limit, every host's addresses, the reads -> {ring address: name} of those
    nodes: the addresses their variables give, else what their names (inventory name, ansible_host) resolve to on
    the controller (out of the play: no facts), only while a ring address has no host (cassandra_ring's
    hosts_by_address)."""
    if not left_out:
        return {}
    known = {}
    for address, name in (addresses or {}).items():
        if name in left_out:
            known.setdefault(name, []).append(address)
    ring = [_ip(n["address"]) for read in reads or [] if isinstance(read, dict)
            for value in (read.get("cluster_status") or {}).values() for n in value.get("nodes", [])]
    owned = dict((_ip(a), name) for a, name in (addresses or {}).items() if name not in left_out)
    found = hosts_by_address(dict((h, known.get(h, [h])) for h in left_out), [a for a in ring if a not in owned])
    return dict((a, h) for a, h in found.items() if a not in owned)


def cassandra_rack_verdict(findings, members, dc, rack, checker, peer, addresses=None, reads=None, schema=None,
                           first_verdict=None, left_out=None):
    """cassandra_health_findings' result of a read, the rack's nodes (cassandra_rack_members), its dc and rack, the
    node of the rack read from (checker), the other node read from (peer), the addresses of every host and the
    cassandra_status results read (see _judge), the cassandra_schema result, first_verdict: this filter's result
    before the wait (None: this read is the one before the wait), left_out: the rack's nodes left out by --limit
    (out of the play: this run does not start them) -> the module docstring's second table applied:
      outside: the nodes outside the rack not UN, [{address, name, states}], in the order seen. Before the wait,
        those the counted view saw so: the peer's, else (its read failed) the checker's; after it, those of any
        view, the ones of the first verdict first.
      failed: the nodes outside the rack whose nodetool status failed (the first verdict's too), when a ring was
        read.
      warnings: the other rows of the warning, [[name, fact]]: the nodes on another schema version (a node by its
        inventory name, else its address), then the warning findings (schema versions differing while the
        versions are mixed: cassandra_allow_mixed_versions).
      ring_size: [[the ring's size, the inventory's]] when they differ and that is a warning.
      unknown_lines: why each node judged of the rack only for want of a host known by its address is, then the
        way out; [] for none.
      problems: what stops the run, as sentences (cassandra_health_sentences).
      rack_seen_un: the nodes of the rack UN in the rings read (_rack_seen_un) a host has the address of, by name.
      rack_unowned: the nodes of the rack UN in the rings read no host has the address of, [[address, "UN"]].
      read_from: the nodes read from (checker, peer; not one whose nodetool failed), this read's and the first
        verdict's.
      seen_from_run_only: read_from when they are all nodes of this run and the cluster has others (none of them
        reached in the play; a node sees itself UN): [] otherwise.
      left_out: the nodes of the rack not UN that are left out by --limit, [{address, name, states}; a node by its
        address only: unplaced, the nodes left out not in the ring at an address of their own]: named as
        one of them (its address theirs: _left_out_names; a name resolved on the controller only while every
        node of the run is in the ring at an address it gives), or no host known by its address, put in the rack
        by the ring, while one of them is not in the ring at an address of its own and every node of the run is;
        the first verdict's with their states in this read. Not waited for, not in problems: a warning (cassandra_rack_outside_lines).
      not_readers: the hosts not to read from again: the nodes outside the rack not UN, seen down, or whose
        nodetool failed.
    e.g. ([<n4 DN from n2>, <n4 DN from n1>], ["n2", "n3"], "dc1", "r2", "n2", "n1") -> {outside: [{address:
    "10.0.0.4", name: "n4", states: ["DN"]}], failed: [], warnings: [], ring_size: [], unknown_lines: [],
    problems: [], rack_seen_un: ["n2", "n3"], rack_unowned: [], read_from: ["n2", "n1"], seen_from_run_only: [],
    left_out: [], not_readers: ["n4"]}."""
    findings = findings or []
    left_out = list(left_out or [])
    judged, all_in_ring, found = _judge(findings, members, dc, rack, addresses, reads)
    rack_addresses = set(found)
    run = [h for h in members or [] if h not in left_out]
    run_placed = set(run) <= set(found.values())
    # a left-out node's name resolved on the controller only while every node of the run is in the ring at an
    # address it gives (else the address may be that node's: a ring node of the rack no host has, as without it)
    known = _left_out_names(left_out, addresses, reads) if run_placed else {}
    in_ring = _ring_states(reads)
    left = _left_out_again((first_verdict or {}).get("left_out"), in_ring)
    left_at = dict((n["address"], n) for n in left)
    # a node no host is known by is taken for one left out only while one of them is not in the ring at an
    # address of its own, every node of the run is (else it may be that one), and the ring puts it in the rack
    # (else as before: a ghost entry stays the rack's)
    unplaced = set(left_out) - set(h for a, h in known.items() if a in in_ring)
    for f in findings:
        if f.get("kind") != "state" or judged[f["address"]] not in ("rack", "unknown"):
            continue
        name = f.get("name") or known.get(_ip(f["address"]))
        if name in left_out or (not name and unplaced and run_placed and judged[f["address"]] == "rack"):
            judged[f["address"]] = "left_out"
            if f["address"] not in left_at:
                left_at[f["address"]] = {"address": f["address"], "name": name, "states": []}
                left.append(left_at[f["address"]])
            if f["state"] not in left_at[f["address"]]["states"]:
                left_at[f["address"]]["states"].append(f["state"])
    for n in left:  # (a node by its address only: the nodes left out it may be)
        if not n.get("name"):
            n["unplaced"] = [h for h in left_out if h in unplaced]
    ring_read = any(isinstance(r, dict) and r.get("cluster_status") for r in reads or [])
    failed_on = _unique(f.get("on") for f in findings if f.get("kind") == "nodetool")
    counted_view = peer if peer and peer not in failed_on else checker
    read_from = _unique((first_verdict or {}).get("read_from", [])
                        + [h for h in (checker, peer) if h and h not in failed_on])
    rack_up = all_in_ring and "rack" not in judged.values()
    other_schema = _other_versions(schema, rack_addresses) if rack_up else None
    outside = [dict(n, states=list(n["states"])) for n in (first_verdict or {}).get("outside", [])]
    by_address = dict((n["address"], n) for n in outside)
    seen_by = {}  # the views that saw each node outside not UN, by its address
    stops, ring_size, notes = [], [], []
    for f in findings:
        kind = f.get("kind")
        if kind == "state" and judged[f["address"]] == "outside":
            if f["address"] not in by_address:
                by_address[f["address"]] = {"address": f["address"], "name": f.get("name"), "states": []}
                outside.append(by_address[f["address"]])
            if f["state"] not in by_address[f["address"]]["states"]:
                by_address[f["address"]]["states"].append(f["state"])
            seen_by.setdefault(f["address"], set()).add(f.get("seen_from"))
        elif kind == "state" and judged[f["address"]] == "left_out":
            continue  # (in left_out)
        elif kind == "nodetool" and ring_read and f.get("on") not in (members or []):
            continue  # (in failed)
        elif kind == "count" and rack_up:
            ring_size.append([f["ring"], f["expected"]])
        elif kind == "schema" and other_schema:
            continue  # (in warnings)
        elif kind == "warning":  # (a schema disagreement while the versions are mixed: cassandra_health_findings)
            notes.append(f)
        else:
            stops.append(f)
    if first_verdict is None:  # before the wait: the nodes the counted view saw not UN; the others are waited for
        # (all of them when that view is a second node of the rack: just started, it may still see a peer down)
        rack_peer = counted_view != checker and counted_view in (members or [])
        outside = [n for n in outside if counted_view in seen_by[n["address"]] and not rack_peer]
    failed = (first_verdict or {}).get("failed", []) + [h for h in failed_on if ring_read and h not in (members or [])]
    seen_down = [f.get("name") for f in findings if f.get("kind") == "state" and str(f.get("state"))[:1] == "D"]
    names = _names(findings, addresses)
    on_other_schema = [[names.get(a, a), "on another schema version than the rack"] for a in other_schema or []]
    seen_un = _rack_seen_un(reads, members, dc, rack, addresses, dict(names, **known))
    return {"outside": outside,
            "failed": _unique(failed),
            "warnings": _unique(on_other_schema + [_note_row(f) for f in notes]),
            "ring_size": _unique(ring_size),
            "unknown_lines": _unknown_lines(findings, judged, rack),
            "problems": cassandra_health_sentences(stops),
            "rack_seen_un": [name for a, name in seen_un if name],
            "rack_unowned": [[a, "UN"] for a, name in seen_un if not name],
            "read_from": read_from,
            "seen_from_run_only": _seen_from_run_only(read_from, run, addresses),
            "left_out": left,
            "not_readers": [h for h in _unique([n.get("name") for n in outside] + seen_down + failed_on) if h]}


def _unknown_lines(findings, judged, rack):
    """The findings and how they were judged -> why each node judged of the rack only for want of a host known
    by its address is, then the way out; [] for none.
    e.g. ([<10.0.0.4 r3 DN, no name>], {"10.0.0.4": "unknown"}, "r2") -> ["10.0.0.4 (dc1, r3): no inventory host
    has this address,", "  so it is judged as a node of rack r2", "if it is outside rack r2: give its inventory
    host that address", "  (broadcast_address in ...", "  cassandra_listen_address or ansible_host)"]."""
    lines, said = [], set()
    for f in findings:
        if f.get("kind") != "state" or f["address"] in said or judged[f["address"]] != "unknown":
            continue
        said.add(f["address"])
        lines += ["%s (%s, %s): no inventory host has this address," % (f["address"], f.get("dc"), f.get("rack")),
                  "  so it is judged as a node of rack %s" % rack]
    if not lines:
        return []
    which = "it is" if len(said) == 1 else "one is"
    return out.fit(lines + ["if %s outside rack %s: give its inventory host that address" % (which, rack),
                            "  (broadcast_address in cassandra_extra_settings, else",
                            "  cassandra_listen_address or ansible_host)"], WIDTH)


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
    whole rack; not said when the stop was forced: verdict's problems), the rack's ring nodes UN no host has the
    address of (verdict's rack_unowned, rows), "the ring as seen from" the nodes of the run only (verdict's
    seen_from_run_only), then the warning on the nodes of the rack
    left out by --limit not UN (_left_out_lines), then, when the rest of the cluster has any, a WARNING with a row
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
    if verdict.get("problems"):
        lines = []
    elif not left_out:
        lines = ["Rack %s (%s), every node is UN%s" % (rack, dc, ":" if up else ".")] + ["  " + h for h in up]
    elif run:
        lines = ["Rack %s (%s), the nodes of this run are UN%s" % (rack, dc, ":" if up else ".")] + [
            "  " + h for h in up]
    else:
        lines = ["Rack %s (%s): nothing started, --limit leaves out all of its nodes." % (rack, dc)]
    if lines and unowned:
        one = len(unowned) == 1
        lines += ["%s of rack %s in the ring, at %s no inventory host has:" % (
            "a node" if one else "nodes", rack, "an address" if one else "addresses")] + out.table(unowned, indent="  ")
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
                "cassandra_rack_verdict": cassandra_rack_verdict,
                "cassandra_rack_outside_lines": cassandra_rack_outside_lines,
                "cassandra_rack_start_failed": cassandra_rack_start_failed,
                "cassandra_rack_stop_refused": cassandra_rack_stop_refused}
