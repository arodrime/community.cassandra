# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The cluster check of start_rack: the rack's nodes must be UN; a node outside the rack in another state, or not
reached over ssh, is a warning.

cassandra_rack_members: the nodes the inventory puts in a rack (every node of the cluster, under --limit too).
cassandra_rack_outside: the nodes a read of the cluster saw not UN that are surely outside the rack.
cassandra_rack_outside_lines: the lines that say them, once the rack is UN.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

# the cassandra_config defaults (roles/cassandra_config/defaults/main.yml)
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


def _in_rack(finding, members, dc, rack):
    """Whether a state finding may be a node of the rack: named by a node of the rack, in the rack as the ring
    says (its dc and rack), or named by no node (not known: taken as one of the rack)."""
    name = finding.get("name")
    return (not name or name in members
            or (str(finding.get("dc")) == str(dc) and str(finding.get("rack")) == str(rack)))


def cassandra_rack_outside(findings, members, dc, rack, readers):
    """cassandra_health_findings' result, the rack's nodes (cassandra_rack_members), its dc and rack, the nodes the
    ring was read from (this node and its peer) -> the nodes surely outside the rack seen not UN, in the order seen:
    [{address, name, states}]. A node is left out (judged as one of the rack) when any of its findings may be of the
    rack, or when a reader whose read succeeded saw it UN (a node just started may still see a peer DN for a few
    seconds: such a node is waited for, not said).
    e.g. ([<n4 DN from n2>, <n4 DN from n1>], ["n2", "n3"], "dc1", "r2", ["n2", "n1"]) -> [{address: "10.0.0.4",
    name: "n4", states: ["DN"]}]."""
    findings = findings or []
    failed = set(f.get("on") for f in findings if f.get("kind") == "nodetool")
    read = set(r for r in readers or [] if r not in failed)
    states = [f for f in findings if f.get("kind") == "state"]
    rack_addresses = set(f["address"] for f in states if _in_rack(f, members, dc, rack))
    order, nodes, seen_by = [], {}, {}  # (nodes, seen_by: by address)
    for f in states:
        address = f["address"]
        if address in rack_addresses:
            continue
        if address not in nodes:
            order.append(address)
            nodes[address] = {"address": address, "name": f.get("name"), "states": []}
            seen_by[address] = set()
        if f["state"] not in nodes[address]["states"]:
            nodes[address]["states"].append(f["state"])
        seen_by[address].add(f.get("seen_from"))
    return [nodes[a] for a in order if read <= seen_by[a]]


def cassandra_rack_outside_lines(outside, unreached, dc, rack, command):
    """The nodes outside the rack not UN (cassandra_rack_outside), the hosts not reached over ssh, the rack's dc and
    rack, the command that checks the cluster -> the lines said once every node of the rack is UN: the rack's fact,
    a WARNING with a row per node (its states as nodetool status says them, "not reached over ssh"), the command.
    e.g. ([{address: "10.0.0.4", name: "n4", states: ["DN"]}], [], "dc1", "r2", "ansible-playbook x") ->
    ["Rack r2 (dc1): every node is UN.", "WARNING  outside the rack, not waited for:", "  n4  DN",
    "Check the cluster:", "  ansible-playbook x"]."""
    unreached = [str(h) for h in unreached or []]
    rows = []
    for node in outside or []:
        name = node.get("name") or node["address"]
        rows.append([name, "/".join(node["states"]), "not reached over ssh" if name in unreached else ""])
    named = set(row[0] for row in rows)
    rows += [[h, "not reached over ssh", ""] for h in unreached if h not in named]
    return out.fit(["Rack %s (%s): every node is UN." % (rack, dc),
                    out.WARNING + "outside the rack, not waited for:"]
                   + out.table(rows, indent="  ")
                   + ["Check the cluster:", "  " + str(command)], WIDTH)


class FilterModule(object):
    def filters(self):
        return {"cassandra_rack_members": cassandra_rack_members,
                "cassandra_rack_outside": cassandra_rack_outside,
                "cassandra_rack_outside_lines": cassandra_rack_outside_lines}
