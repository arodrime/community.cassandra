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
  a rack node in another state, or nodetool failing on it       STOP
  an outside node not UN for the node outside the rack read     WARNING, not
    from (its read failed, or none read: for the rack node)       waited for
  an outside node not UN for the rack node alone (just          wait, then
    started: it may still see a peer down)                        WARNING
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
cassandra_rack_outside_lines: the rack's nodes UN, then the warning (the rest of the cluster), once the rack is UN.
"""
from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import cassandra_health_sentences
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
    address) or "outside"}, whether every node of the rack is in the ring at an address it gives, the ring
    addresses of the rack's nodes found there). addresses: cassandra_host_addresses of every host (one host's
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
    return judged, all_in_ring, set(found)


def _rack_up(reads, members, dc, rack, addresses, names):
    """The cassandra_status results read, the rack's nodes, its dc and rack, every host's addresses, {ring address:
    name} -> the nodes of the ring of the rack (at an address a rack node gives, or put in the rack's dc and rack by
    the ring), UN in every read that has them, in the ring's order: by name, else by address."""
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
    return [names.get(a, a) for a in order if states[a] == {"UN"}]


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


def cassandra_rack_verdict(findings, members, dc, rack, checker, peer, addresses=None, reads=None, schema=None,
                           first_verdict=None):
    """cassandra_health_findings' result of a read, the rack's nodes (cassandra_rack_members), its dc and rack, the
    node of the rack read from (checker), the other node read from (peer), the addresses of every host and the
    cassandra_status results read (see _judge), the cassandra_schema result, first_verdict: this filter's result
    before the wait (None: this read is the one before the wait) -> the module docstring's second table applied:
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
      rack_up: the nodes of the rack UN in the rings read (_rack_up), by name, else by address.
      not_readers: the hosts not to read from again: the nodes outside the rack not UN, seen down, or whose
        nodetool failed.
    e.g. ([<n4 DN from n2>, <n4 DN from n1>], ["n2", "n3"], "dc1", "r2", "n2", "n1") -> {outside: [{address:
    "10.0.0.4", name: "n4", states: ["DN"]}], failed: [], warnings: [], ring_size: [], unknown_lines: [],
    problems: [], not_readers: ["n4"]}."""
    findings = findings or []
    judged, all_in_ring, rack_addresses = _judge(findings, members, dc, rack, addresses, reads)
    ring_read = any(isinstance(r, dict) and r.get("cluster_status") for r in reads or [])
    failed_on = _unique(f.get("on") for f in findings if f.get("kind") == "nodetool")
    counted_view = peer if peer and peer not in failed_on else checker
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
        outside = [n for n in outside if counted_view in seen_by[n["address"]]]
    failed = (first_verdict or {}).get("failed", []) + [h for h in failed_on if ring_read and h not in (members or [])]
    seen_down = [f.get("name") for f in findings if f.get("kind") == "state" and str(f.get("state"))[:1] == "D"]
    names = _names(findings, addresses)
    on_other_schema = [[names.get(a, a), "on another schema version than the rack"] for a in other_schema or []]
    return {"outside": outside,
            "failed": _unique(failed),
            "warnings": _unique(on_other_schema + [_note_row(f) for f in notes]),
            "ring_size": _unique(ring_size),
            "unknown_lines": _unknown_lines(findings, judged, rack),
            "problems": cassandra_health_sentences(stops),
            "rack_up": _rack_up(reads, members, dc, rack, addresses, names),
            "not_readers": [h for h in _unique([n.get("name") for n in outside] + seen_down + failed_on) if h]}


def _unknown_lines(findings, judged, rack):
    """The findings and how they were judged -> why each node judged of the rack only for want of a host known
    by its address is, then the way out; [] for none.
    e.g. ([<10.0.0.4 r3 DN, no name>], {"10.0.0.4": "unknown"}, "r2") -> ["10.0.0.4 (dc1, r3): no inventory host
    has this address,", "  so it is judged as a node of rack r2", "if it is outside rack r2: give its inventory
    host that address", "  (broadcast_address in ...", "  cassandra_listen_address or ..."]."""
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
                            "  cassandra_listen_address or ansible_host), then run start_rack again"], WIDTH)


def cassandra_rack_outside_lines(verdict, unreached, dc, rack, command):
    """cassandra_rack_verdict's result after the wait, the hosts not reached over ssh, the rack's dc and rack, the
    command that checks the cluster -> the lines said once the rack's nodes are UN: that fact, the nodes seen UN
    (verdict's rack_up; not when the stop was forced: verdict's problems), then, when the rest of the cluster has
    any, a WARNING with a row per node of the rest of the cluster (its states as nodetool status says them, "not
    reached over ssh", "nodetool status failed", verdict's warnings), one for the ring's size, the command.
    e.g. ({outside: [{address: "10.0.0.4", name: "n4", states: ["DN"]}], failed: [], warnings: [], ring_size:
    [[3, 4]], problems: [], rack_up: ["n2", "n3"]}, [], "dc1", "r2", "ansible-playbook x") -> ["Rack r2 (dc1): every
    node is UN (n2, n3).", "WARNING  the rest of the cluster:", "  n4  DN", "WARNING  the ring has 3 nodes, the
    inventory 4", "Check the cluster:", "  ansible-playbook x"]; nothing outside: the first line alone."""
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
    up = verdict.get("rack_up") or []
    lines = (["Rack %s (%s): every node is UN%s." % (rack, dc, " (%s)" % out.nodes(up) if up else "")]
             if not verdict.get("problems") else [])
    if rows:
        lines += [out.WARNING + "the rest of the cluster:"] + out.table(rows, indent="  ")
    lines += [out.WARNING + "the ring has %s nodes, the inventory %s" % (ring, inventory)
              for ring, inventory in verdict.get("ring_size", [])]
    if rows or verdict.get("ring_size"):
        lines += ["Check the cluster:", "  " + str(command)]
    return out.fit(lines, WIDTH)


class FilterModule(object):
    def filters(self):
        return {"cassandra_rack_members": cassandra_rack_members,
                "cassandra_rack_verdict": cassandra_rack_verdict,
                "cassandra_rack_outside_lines": cassandra_rack_outside_lines}
