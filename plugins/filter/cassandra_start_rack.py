# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The cluster check of start_rack: the rack's nodes must be UN; a node outside the rack in another state, or not
reached over ssh, is a warning.

cassandra_rack_members: the nodes the inventory puts in a rack (every node of the cluster, under --limit too).
cassandra_rack_outside: the nodes a read of the cluster saw not UN that are surely outside the rack.
cassandra_rack_inside: the findings that are not only about nodes outside the rack (what stops the run).
cassandra_rack_unknown_lines: why a node of the ring no host is known by is judged as a node of the rack.
cassandra_rack_outside_lines: the lines that say the nodes outside the rack, once the rack is UN.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

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
    """The state findings -> {address: "rack" (may be a node of the rack), "unknown" (judged so only for want of
    a host known by its address), "outside" (surely outside)}.
    May be a node of the rack: named by a node of the rack, at an address a node of the rack gives in its
    variables (addresses: cassandra_host_addresses of the rack's nodes), or in the rack as the ring says (its dc
    and rack). A node no host is known by is surely outside only when the ring puts it elsewhere and every node
    of the rack is in the ring (reads: the cassandra_status results read) at an address it gives: else it may be
    a node of the rack whose address the inventory does not give."""
    member_at = {}
    for address, name in (addresses or {}).items():
        member_at.setdefault(_ip(address), name)
    in_ring = set(_ip(n["address"]) for read in reads or [] if isinstance(read, dict)
                  for value in (read.get("cluster_status") or {}).values() for n in value.get("nodes", []))
    all_in_ring = set(members or []) <= set(member_at[a] for a in in_ring if a in member_at)
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
    return judged


def cassandra_rack_outside(findings, members, dc, rack, checker="", peer="", addresses=None, reads=None, any_view=False,
                           known=None):
    """cassandra_health_findings' result of the first read, the rack's nodes (cassandra_rack_members), its dc and
    rack, the node of the rack it was read from (checker) and its second view (peer: a node outside the rack, up
    for long), the addresses of the rack's nodes and the reads (see _judge) -> the nodes surely outside the rack
    the settled view saw not UN, in the order seen: [{address, name, states}]. The settled view: the peer's,
    unless its read failed (then the checker's). A node only the checker saw not UN is left out: just started, it
    may still see a peer DN for a few seconds (waited for, not said). states: as every view saw it. any_view:
    every view counts (after the wait). known: the nodes noted before (this function's result), kept first, the
    states seen now added.
    e.g. ([<n4 DN from n2>, <n4 DN from n1>], ["n2", "n3"], "dc1", "r2", "n2", "n1") -> [{address: "10.0.0.4",
    name: "n4", states: ["DN"]}]."""
    findings = findings or []
    failed = set(f.get("on") for f in findings if f.get("kind") == "nodetool")
    settled = peer if peer and peer not in failed else checker
    judged = _judge(findings, members, dc, rack, addresses, reads)
    order, nodes, seen_by = [], {}, {}  # (nodes, seen_by: by address)
    for node in known or []:
        order.append(node["address"])
        nodes[node["address"]] = dict(node, states=list(node["states"]))
        seen_by[node["address"]] = set([settled])
    for f in findings:
        if f.get("kind") != "state" or judged[f["address"]] != "outside":
            continue
        address = f["address"]
        if address not in nodes:
            order.append(address)
            nodes[address] = {"address": address, "name": f.get("name"), "states": []}
            seen_by[address] = set()
        if f["state"] not in nodes[address]["states"]:
            nodes[address]["states"].append(f["state"])
        seen_by[address].add(f.get("seen_from"))
    return [nodes[a] for a in order if any_view or settled in seen_by[a]]


def cassandra_rack_inside(findings, members, dc, rack, checker="", peer="", addresses=None, reads=None):
    """The same arguments as cassandra_rack_outside -> the findings left once those only about nodes surely
    outside the rack are taken out (their states, in any view; a nodetool status failing on a node outside the
    rack, when another read of the ring succeeded (reads): what still stops the run after the wait."""
    findings = findings or []
    judged = _judge(findings, members, dc, rack, addresses, reads)
    ring_read = any(isinstance(r, dict) and r.get("cluster_status") for r in reads or [])
    return [f for f in findings
            if not (f.get("kind") == "state" and judged[f["address"]] == "outside")
            and not (ring_read and f.get("kind") == "nodetool" and f.get("on") not in (members or []))]


def cassandra_rack_unknown_lines(findings, members, dc, rack, checker="", peer="", addresses=None, reads=None):
    """The same arguments as cassandra_rack_outside -> the lines that say why each node seen not UN that the ring
    puts outside the rack is judged as a node of the rack (no inventory host has its address, and a node of the
    rack is not in the ring at an address it gives), then the way out; [] for none.
    e.g. ([<10.0.0.4 r3 DN, no name>], ["n2"], "dc1", "r2") -> ["10.0.0.4 (dc1, r3): no inventory host has this
    address,", "  so it is judged as a node of rack r2", "if it is outside rack r2: give its host this address",
    "  (cassandra_listen_address or ansible_host), then run start_rack again"]."""
    judged = _judge(findings, members, dc, rack, addresses, reads)
    lines, said = [], set()
    for f in findings or []:
        if f.get("kind") != "state" or f["address"] in said or judged[f["address"]] != "unknown":
            continue
        said.add(f["address"])
        lines += ["%s (%s, %s): no inventory host has this address," % (f["address"], f.get("dc"), f.get("rack")),
                  "  so it is judged as a node of rack %s" % rack]
    if not lines:
        return []
    return out.fit(lines + ["if it is outside rack %s: give its host this address" % rack,
                            "  (cassandra_listen_address or ansible_host), then run start_rack again"], WIDTH)


def cassandra_rack_outside_lines(outside, unreached, dc, rack, command, failed=None):
    """The nodes outside the rack not UN (cassandra_rack_outside), the hosts not reached over ssh, the rack's dc and
    rack, the command that checks the cluster, the nodes outside the rack whose nodetool status failed when read
    from -> the lines said once every node of the rack is UN: the rack's fact, a WARNING with a row per node (its
    states as nodetool status says them, "not reached over ssh", "nodetool status failed"), the command.
    e.g. ([{address: "10.0.0.4", name: "n4", states: ["DN"]}], [], "dc1", "r2", "ansible-playbook x") ->
    ["Rack r2 (dc1): every node is UN.", "WARNING  outside the rack, not waited for:", "  n4  DN",
    "Check the cluster:", "  ansible-playbook x"]."""
    facts, order = {}, []  # a node's facts, by its name (else its address)

    def add(name, fact):
        if name not in facts:
            order.append(name)
            facts[name] = []
        facts[name].append(fact)
    for node in outside or []:
        add(node.get("name") or node["address"], "/".join(node["states"]))
    for h in unreached or []:
        add(str(h), "not reached over ssh")
    for h in failed or []:
        add(str(h), "nodetool status failed")
    rows = [[name] + facts[name] for name in order]
    return out.fit(["Rack %s (%s): every node is UN." % (rack, dc),
                    out.WARNING + "outside the rack, not waited for:"]
                   + out.table(rows, indent="  ")
                   + ["Check the cluster:", "  " + str(command)], WIDTH)


class FilterModule(object):
    def filters(self):
        return {"cassandra_rack_members": cassandra_rack_members,
                "cassandra_rack_outside": cassandra_rack_outside,
                "cassandra_rack_inside": cassandra_rack_inside,
                "cassandra_rack_unknown_lines": cassandra_rack_unknown_lines,
                "cassandra_rack_outside_lines": cassandra_rack_outside_lines}
