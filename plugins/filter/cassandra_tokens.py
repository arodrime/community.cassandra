# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Single-token clusters (num_tokens: 1): the tokens create_cluster gives new
nodes. The math is in plugin_utils/cassandra_tokens.py (conventions there).

cassandra_token_assign: the initial_token of every node of a new cluster.

A token is unique in the whole cluster: every planned token is checked
against the tokens of all datacenters; create_cluster refuses a token twice
(its datacenters' offsets keep them apart).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from functools import wraps

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.filter.cassandra_ring import hosts_by_address
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import compressed_address as _ip
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_tokens import (
    TokenError, balanced_positions, dc_offset, ownership, parse_token, partitioner_range, plan_bisect, position,
    rack_order, token_of)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_keyspace_names import is_system_keyspace

DEFAULT_RF = 3


def _pct(frac):
    return "%.2f%%" % (float(frac) * 100)


def _has_token(node):
    """A token is set (the inventory's null comes as 'None' through | string)."""
    return str(node.get("token") if node.get("token") is not None else "").strip() not in ("", "None")


def _rf_set(keyspaces, dc, default):
    """The replication factors of the datacenter (NetworkTopologyStrategy
    keyspaces, not system_*: system_auth is often replicated to every node
    and holds next to nothing), [default] when none is known."""
    rfs = set(ks["rf"][dc] for name, ks in (keyspaces or {}).items()
              if not is_system_keyspace(name) and ks["rf"].get(dc, 0) > 0)
    return sorted(rfs) or [int(default or DEFAULT_RF)]


def _rf_by_dc(keyspaces, dcs, default):
    """The largest of them per datacenter: the factor the shares shown assume."""
    return dict((dc, _rf_set(keyspaces, dc, default)[-1]) for dc in dcs)


def _table(dc, ring, size, first, rf, label=None, marks=None):
    """Lines of one datacenter's ring: node, rack, token, primary share, effective share."""
    if not ring:
        return []
    own = ownership(ring, size, rf)
    rows = [["node", "rack", "token", "owns", "with RF %d" % rf]]
    for p, name, rack in sorted(ring):
        rows.append([name + (marks or {}).get(name, ""), rack, str(token_of(p, first, size)),
                     _pct(own[name][0]), _pct(own[name][1])])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    eff = [e for dummy, e in own.values()]
    lines = ["%s%s: %d node(s), effective ownership %s to %s" % (
        dc, (" " + label) if label else "", len(ring), _pct(min(eff)), _pct(max(eff)))]
    lines += ["  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    return lines


def _dc_index(dcs):
    return dict((dc, i) for i, dc in enumerate(sorted(set(dcs))))


def _addresses(hosts, ring):
    """{ring address: inventory name}. hosts: {address: name}; a ring address
    none of them gives is looked for in what the names (and the addresses
    that are names) resolve to on the controller, as the status playbook does."""
    known = {}
    for address, name in (hosts or {}).items():
        known.setdefault(name, []).extend([address, name])
    return hosts_by_address(known, [_ip(n["address"]) for dc in (ring or {}) for n in ring[dc]])


def _wrap(func):
    @wraps(func)
    def run(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except TokenError as exc:
            raise AnsibleFilterError("%s: %s" % (func.__name__, exc))
    return run


def _plan_new_cluster(nodes, partitioner, index, rfs, allow_partial, out):
    """create_cluster's plan for nodes none of which runs. Returns {name: position} of all of them."""
    first, size = partitioner_range(partitioner)
    planned = {}
    for dc in sorted(set(n["dc"] for n in nodes)):
        given, missing = [], []
        for n in [n for n in nodes if n["dc"] == dc]:
            if not _has_token(n):
                missing.append((n["name"], n["rack"]))
                continue
            try:
                given.append((position(parse_token(n["token"], partitioner), first, size), n["name"], n["rack"]))
            except TokenError as exc:
                out["problems"].append("%s: cassandra_initial_token %s" % (n["name"], exc))
        if given and missing and not allow_partial:
            out["problems"].append(
                "%s: cassandra_initial_token is set on %s but not on %s: set it on every node of the datacenter or on"
                " none (then the tokens are worked out), or -e cassandra_token_allow_partial=true (the others split"
                " the largest ranges)" % (dc, ", ".join(g[1] for g in given), ", ".join(m[0] for m in missing)))
            continue
        if missing and not given:
            placed = [(p, name, rack) for p, (name, rack) in
                      zip(balanced_positions(len(missing), size, dc_offset(index[dc])), rack_order(missing))]
        elif missing:
            placed = plan_bisect(given, rack_order(missing), size, rfs[dc])
        else:
            placed = []
        planned.update((name, p) for p, name, dummy in given + placed)
        out["placed"].update((name, p) for p, name, dummy in placed)
        racks = {}
        for dummy, dummy2, rack in given + placed:
            racks[rack] = racks.get(rack, 0) + 1
        if len(racks) > 1 and len(set(racks.values())) > 1:
            out["warnings"].append("%s: racks of different sizes (%s): with one token per node the ring can't"
                                   " alternate racks all the way round, some nodes hold more" % (
                                       dc, ", ".join("%s %d" % (r, c) for r, c in sorted(racks.items()))))
    return planned


@_wrap
def cassandra_token_assign(nodes, partitioner, keyspaces=None, default_rf=DEFAULT_RF, allow_partial=False,
                           ring=None, hosts=None):
    """nodes: [{'name', 'dc', 'rack', 'token'}] in inventory order, token '' when
    the inventory sets none. Each datacenter without any token gets the
    balanced ring (racks taken in turn); one with some tokens set is refused,
    unless allow_partial: then its other nodes split the largest ranges.
    ring/hosts: the running ring (parsed nodetool ring, {address: name}). The
    nodes in it keep the token they have. The others get the plan only when
    every running node has the token the plan gives it (a create that stopped
    half way); otherwise the cluster exists and they are for add_node.
    Returns {'tokens': {name: token} to give, 'lines', 'problems', 'warnings'}.
    e.g. (three nodes of dc1, no token, Murmur3Partitioner) -> {tokens: {node1: "-9223372036854775808", node2:
    "-3074457345618258603", node3: "3074457345618258602"}, lines: [the ring table], problems: [], warnings: []}."""
    first, size = partitioner_range(partitioner)
    hosts = _addresses(hosts, ring)
    dcs = sorted(set(n["dc"] for n in nodes))
    index = _dc_index(dcs + list((ring or {}).keys()))
    rfs = _rf_by_dc(keyspaces, dcs, default_rf)
    out = {"tokens": {}, "lines": [], "problems": [], "warnings": [], "placed": {}}
    names = [n["name"] for n in nodes]
    running, strangers = {}, []
    for dc in sorted(ring or {}):
        for n in ring[dc]:
            name = hosts.get(_ip(n["address"]))
            if name in names:
                running[name] = position(int(n["token"]), first, size)
            else:
                strangers.append("%s (%s)" % (n["address"], dc))
    left = [name for name in names if name not in running]
    planned = _plan_new_cluster(nodes, partitioner, index, rfs, allow_partial, out) if left else {}
    for n in nodes:
        if n["name"] in running and _has_token(n) and str(n["token"]).strip() != str(token_of(running[n["name"]], first, size)):
            out["warnings"].append(
                "%s: cassandra_initial_token %s, the ring has %d (the token a node joined with; initial_token is"
                " not read again): fix the inventory" % (n["name"], n["token"], token_of(running[n["name"]], first, size)))
    if (running or strangers) and left and (strangers or any(planned.get(k) != p for k, p in running.items())):
        out["problems"].append(
            "the cluster runs already (%s in the ring, not with the tokens worked out here): create_cluster only starts"
            " a new cluster, or one a create left half way. Add %s with add_node (cassandra_token_auto)"
            % (", ".join(sorted(running) + strangers), ", ".join(left)))
    out["placed"] = dict((name, p) for name, p in out["placed"].items() if name not in running)
    planned.update(running)
    by_pos = {}
    for name, p in planned.items():
        by_pos.setdefault(p, []).append(name)
    for p, names in sorted(by_pos.items()):
        if len(names) > 1:
            out["problems"].append("%s have the same token %d" % (" and ".join(sorted(names)), token_of(p, first, size)))
    for dc in dcs:
        dc_ring = [(planned[n["name"]], n["name"], n["rack"]) for n in nodes if n["dc"] == dc and n["name"] in planned]
        try:
            out["lines"] += _table(dc, dc_ring, size, first, rfs[dc], marks=dict((name, " *") for name in out["placed"]))
        except TokenError as exc:
            out["problems"].append("%s: %s" % (dc, exc))
    out["tokens"] = dict((name, str(token_of(p, first, size))) for name, p in out.pop("placed").items())
    if out["tokens"]:
        out["lines"].append("* worked out here (put them in the inventory, host_vars cassandra_initial_token, to keep a"
                            " record; a node that has joined keeps its token anyway)")
    return out


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_token_assign": cassandra_token_assign,
        }
