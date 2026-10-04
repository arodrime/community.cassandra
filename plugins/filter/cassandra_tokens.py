# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Single-token clusters (num_tokens: 1): the tokens create_cluster gives new
nodes, the plans add_node offers, the moves move_node runs. The math is in
module_utils/cassandra_tokens.py (conventions there).

cassandra_token_assign: the initial_token of every node of a new cluster.
cassandra_token_add_plan: where new nodes go in a running cluster, two ways:
    bisect (split the largest ranges, nothing moves) and balanced (an even
    ring, existing nodes move).
cassandra_token_move_plan: the moves that even out each datacenter (or the
    ones given), in a safe order, with what each one streams and leaves behind.
cassandra_token_disk_problems: a move's data against the free space of the
    nodes that receive it.
cassandra_ring_tokens: nodetool ring output as {dc: [{address, rack, status,
    state, token}]}.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from fractions import Fraction

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_tokens import (
    TokenError, balanced_positions, dc_offset, move_order, ownership, parse_ring, parse_token, partitioner_range,
    plan_balanced, plan_bisect, position, rack_order, token_of, tolerance, transfer)

DEFAULT_RF = 3
GIB = 1024 ** 3
_UNITS = {"bytes": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": GIB, "TiB": 1024 ** 4}


def _pct(frac):
    return "%.2f%%" % (float(frac) * 100)


def _gib(size):
    return "%.1f GiB" % (float(size) / GIB)


def _bytes(load):
    """nodetool's Load ('1.5 GiB', '1,5 GiB' in some locales) -> bytes, None when unknown."""
    parts = str(load or "").split()
    if len(parts) != 2 or parts[1] not in _UNITS:
        return None
    try:
        return float(parts[0].replace(",", ".")) * _UNITS[parts[1]]
    except ValueError:
        return None


def _rf_by_dc(keyspaces, dcs, default):
    """The largest replication factor of each datacenter (NetworkTopologyStrategy
    keyspaces), default when no keyspace is replicated there or keyspaces is unknown."""
    out = {}
    for dc in dcs:
        rfs = [ks["rf"][dc] for ks in (keyspaces or {}).values() if dc in ks.get("rf", {})]
        out[dc] = max(rfs) if rfs and max(rfs) > 0 else int(default or DEFAULT_RF)
    return out


def _table(dc, ring, size, first, rf, label=None, marks=None):
    """Lines of one datacenter's ring: node, rack, token, primary share, effective share."""
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


def _spread(ring, size, rf):
    eff = [e for dummy, e in ownership(ring, size, rf).values()]
    return max(eff) - min(eff)


def _even(ring, size, rf):
    """Balanced: the effective shares differ by no more than a few tolerances."""
    return _spread(ring, size, rf) <= Fraction(4 * rf * tolerance(len(ring), size), size)


def _dc_index(dcs):
    return dict((dc, i) for i, dc in enumerate(sorted(set(dcs))))


def _free_offset(index, count, size, taken):
    """The datacenter's offset (index * 100), or the next one whose positions
    no other datacenter holds."""
    for i in range(index, index + 1000):
        pos = balanced_positions(count, size, dc_offset(i))
        if not set(pos) & taken:
            return dc_offset(i)
    raise TokenError("no free offset for %d nodes" % count)


def _ring_entries(ring, dc, hosts, problems):
    """[(address, name, rack, token)] of one datacenter of a parsed nodetool ring;
    a node with several tokens (vnodes) is a problem."""
    entries, seen = [], {}
    for n in (ring or {}).get(dc, []):
        seen[n["address"]] = seen.get(n["address"], 0) + 1
        entries.append((n["address"], (hosts or {}).get(n["address"], n["address"]), n["rack"], n["token"]))
    multi = sorted(a for a, c in seen.items() if c > 1)
    if multi:
        problems.append("%s: %s %s several tokens (vnodes): these plans are for one token per node"
                        % (dc, ", ".join((hosts or {}).get(a, a) for a in multi), "has" if len(multi) == 1 else "have"))
        return None
    return entries


def _wrap(func):
    def run(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except TokenError as exc:
            raise AnsibleFilterError("%s: %s" % (func.__name__, exc))
    run.__name__ = func.__name__
    run.__doc__ = func.__doc__
    return run


@_wrap
def cassandra_token_assign(nodes, partitioner, keyspaces=None, default_rf=DEFAULT_RF, allow_partial=False,
                           ring=None, hosts=None):
    """nodes: [{'name', 'dc', 'rack', 'token'}] in inventory order, token '' when
    the inventory sets none. Each datacenter without any token gets the
    balanced ring (racks taken in turn); one with some tokens set is refused,
    unless allow_partial: then its other nodes split the largest ranges.
    ring/hosts: a running ring (parsed nodetool ring, {address: name}): every
    node in it must already have the token planned for it (a create that
    stopped half way), else there is nothing to work out here (add_node).
    Returns {'tokens': {name: token} for the nodes with none set, 'lines',
    'problems', 'warnings'}."""
    first, size = partitioner_range(partitioner)
    dcs = sorted(set(n["dc"] for n in nodes))
    index = _dc_index(dcs + list((ring or {}).keys()))
    rfs = _rf_by_dc(keyspaces, dcs, default_rf)
    tokens, lines, problems, warnings, all_pos = {}, [], [], [], {}
    for dc in dcs:
        members = [n for n in nodes if n["dc"] == dc]
        given, missing = [], []
        for n in members:
            if str(n.get("token", "") if n.get("token") is not None else "").strip() == "":
                missing.append((n["name"], n["rack"]))
            else:
                try:
                    given.append((position(parse_token(n["token"], partitioner), first, size), n["name"], n["rack"]))
                except TokenError as exc:
                    problems.append("%s: cassandra_initial_token %s" % (n["name"], exc))
        if given and missing and not allow_partial:
            problems.append(
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
        for p, name, dummy in placed:
            tokens[name] = str(token_of(p, first, size))
        dc_ring = given + placed
        for p, name, dummy in dc_ring:
            all_pos.setdefault(p, []).append(name)
        racks = {}
        for dummy, dummy2, rack in dc_ring:
            racks[rack] = racks.get(rack, 0) + 1
        if len(racks) > 1 and len(set(racks.values())) > 1:
            warnings.append("%s: racks of different sizes (%s): with one token per node the ring can't alternate"
                            " racks all the way round, some nodes hold more" % (
                                dc, ", ".join("%s %d" % (r, c) for r, c in sorted(racks.items()))))
        try:
            lines += _table(dc, dc_ring, size, first, rfs[dc],
                            marks=dict((name, " *") for dummy, name, dummy2 in placed))
        except TokenError as exc:
            problems.append("%s: %s" % (dc, exc))
    for p, names in sorted(all_pos.items()):
        if len(names) > 1:
            problems.append("%s have the same token %d" % (" and ".join(names), token_of(p, first, size)))
    if tokens:
        lines.append("* worked out here (put them in the inventory, host_vars cassandra_initial_token, to keep a"
                     " record; a node that has joined keeps its token anyway)")
    planned = dict((n["name"], tokens.get(n["name"], n.get("token"))) for n in nodes)
    for dc in sorted(ring or {}):
        for n in ring[dc]:
            name = (hosts or {}).get(n["address"])
            want = planned.get(name)
            if name is None or want in (None, "") or parse_token(want, partitioner) != int(n["token"]):
                problems.append(
                    "%s (%s) runs in the ring with token %s, not %s: the cluster exists, create_cluster only works out"
                    " the tokens of a new one. Set cassandra_initial_token of the running nodes from nodetool ring and"
                    " add the others with add_node (cassandra_token_auto)"
                    % (name or "a node outside the inventory", n["address"], n["token"], want or "a planned one"))
    return {"tokens": tokens, "lines": lines, "problems": problems, "warnings": warnings}


def _add_dc(dc, ring, new, hosts, partitioner, index, rfs, taken, out):
    """One datacenter of cassandra_token_add_plan."""
    first, size = partitioner_range(partitioner)
    entries = _ring_entries(ring, dc, hosts, out["problems"])
    if entries is None:
        return
    existing = [(position(int(t), first, size), name, rack) for dummy, name, rack, t in entries]
    fixed, free = [], []
    for n in [n for n in new if n["dc"] == dc]:
        if str(n.get("token") if n.get("token") is not None else "").strip():
            try:
                fixed.append((position(parse_token(n["token"], partitioner), first, size), n["name"], n["rack"]))
            except TokenError as exc:
                out["problems"].append("%s: cassandra_initial_token %s" % (n["name"], exc))
        else:
            free.append((n["name"], n["rack"]))
    base = existing + fixed
    rf = rfs[dc]
    out["lines"] += _table(dc, existing, size, first, rf, "now") if existing else []
    if not existing:  # a new datacenter: as create_cluster would do it
        if fixed:
            bisect_ring = fixed + plan_bisect(fixed, rack_order(free), size, rf)
        else:
            off = _free_offset(index[dc], len(free), size, taken)
            bisect_ring = [(p, name, rack) for p, (name, rack) in
                           zip(balanced_positions(len(free), size, off), rack_order(free))]
        balanced_ring = bisect_ring
        moves = []
    else:
        bisect_ring = base + plan_bisect(base, free, size, rf)
        plan = plan_balanced(base, free, size, rf, dc_offset(index[dc]))
        balanced_ring = plan["ring"]
        moves = plan["moves"]
    fixed_names = set(name for dummy, name, dummy2 in fixed)
    for kind, kring in (("bisect", bisect_ring), ("balanced", balanced_ring)):
        for p, name, dummy in kring:
            if name in set(f[0] for f in free) | fixed_names:
                out[kind][name] = str(token_of(p, first, size))
        out["even"][kind] = out["even"][kind] and _even(kring, size, rf)
    new_names = set(f[0] for f in free) | fixed_names
    marks = dict((name, " +") for name in new_names)
    out["lines"] += _table(dc, bisect_ring, size, first, rf, "bisect (no move)", marks)
    if existing:
        for name, src, dst in moves:
            marks[name] = " >"
            out["moves"].append({"name": name, "dc": dc, "from": str(token_of(src, first, size)),
                                 "to": str(token_of(dst, first, size))})
        out["lines"] += _table(dc, balanced_ring, size, first, rf, "balanced (%d move%s)" % (
            len(moves), "" if len(moves) == 1 else "s"), marks)
        held = set(p for p, dummy, dummy2 in existing)
        blocked = [name for p, name, dummy in balanced_ring if name in new_names and p in held]
        if blocked:
            out["balanced_problems"].append(
                "%s: %s would bootstrap at a token a node that moves still holds: move it first (move_node), or"
                " use bisect" % (dc, ", ".join(blocked)))
    taken |= set(p for p, dummy, dummy2 in balanced_ring)
    n_after = len(base) + len(free)
    if existing and not out["even"]["bisect"] and n_after < 2 * len(existing):
        out["warnings"].append(
            "%s: bisect leaves the ring uneven; %d new node(s) instead of %d (the datacenter doubled to %d) would"
            " split every range in two: even, with no move" % (dc, len(existing), len(free) + len(fixed),
                                                               2 * len(existing)))


@_wrap
def cassandra_token_add_plan(ring, new_nodes, partitioner, keyspaces=None, default_rf=DEFAULT_RF, hosts=None):
    """ring: parsed nodetool ring; new_nodes: [{'name', 'dc', 'rack', 'token',
    'address'}] in join order (a node already in the ring is left out; a token
    set in the inventory is kept). hosts: {address: inventory name}.
    Returns {'bisect': {name: token}, 'balanced': {name: token}, 'moves':
    [{'name', 'dc', 'from', 'to'}] (balanced), 'balanced_problems', 'even':
    {'bisect': bool, 'balanced': bool}, 'lines', 'problems', 'warnings'}."""
    first, size = partitioner_range(partitioner)
    hosts = dict(hosts or {})
    in_ring = set(n["address"] for dc in (ring or {}) for n in ring[dc])
    new = [n for n in new_nodes if n.get("address") not in in_ring]
    dcs = sorted(set(n["dc"] for n in new))
    index = _dc_index(dcs + list((ring or {}).keys()))
    rfs = _rf_by_dc(keyspaces, dcs, default_rf)
    taken = set(position(int(n["token"]), first, size) for dc in (ring or {}) for n in ring[dc])
    out = {"bisect": {}, "balanced": {}, "moves": [], "balanced_problems": [], "even": {"bisect": True, "balanced": True},
           "lines": [], "problems": [], "warnings": []}
    for dc in dcs:
        try:
            _add_dc(dc, ring, new, hosts, partitioner, index, rfs, taken, out)
        except TokenError as exc:
            out["problems"].append("%s: %s" % (dc, exc))
    out["lines"] += ["+ new node, > node that moves (move_node, after the add)"] if out["lines"] else []
    return out


@_wrap
def cassandra_token_move_plan(ring, partitioner, keyspaces=None, default_rf=DEFAULT_RF, hosts=None, targets=None,
                              status=None):
    """The moves that even out each single-token datacenter of the ring (fewest
    moves), or targets: {name: token} given by the operator. hosts: {address:
    inventory name}; status: cassandra_status cluster_status (the loads, for
    the data each move streams). In an order where no node moves to a token
    another one still holds.
    Returns {'steps': [{'name', 'address', 'dc', 'from', 'to', 'gain': {name:
    share}, 'loss': {name: share}, 'gain_bytes': {name: bytes or None}}],
    'cleanup': [names that lose ranges], 'lines', 'problems', 'warnings'}."""
    first, size = partitioner_range(partitioner)
    hosts = dict(hosts or {})
    names = dict((hosts.get(n["address"], n["address"]), n["address"]) for dc in (ring or {}) for n in ring[dc])
    loads = {}
    for dc in (status or {}):
        for n in status[dc].get("nodes", []):
            loads[n["address"]] = _bytes(n.get("load"))
    dcs = sorted(ring or {})
    index = _dc_index(dcs)
    rfs = _rf_by_dc(keyspaces, dcs, default_rf)
    out = {"steps": [], "cleanup": [], "lines": [], "problems": [], "warnings": []}
    targets = dict(targets or {})
    for name in sorted(set(targets) - set(names)):
        out["problems"].append("%s is not in the ring (nodetool ring): no token to move" % name)
    all_tokens = {}
    lost_any = set()
    for dc in dcs:
        wanted = dict((k, v) for k, v in targets.items() if k in names and any(
            n["address"] == names[k] for n in ring[dc]))
        problems = []
        entries = _ring_entries(ring, dc, hosts, problems)
        if entries is None:
            if wanted or not targets:
                (out["problems"] if wanted else out["warnings"]).extend(problems)
            continue
        if targets and not wanted:
            continue
        state = dict((n["address"], n["state"]) for n in ring[dc])
        busy = sorted(hosts.get(a, a) for a, s in state.items() if s != "Normal")
        if busy:
            out["problems"].append("%s: %s not Normal (joining, leaving or moving): wait until it is" % (dc, ", ".join(busy)))
            continue
        cur = [(position(int(t), first, size), name, rack) for dummy, name, rack, t in entries]
        rf = rfs[dc]
        if wanted:
            moves = []
            for name in sorted(wanted):
                try:
                    dst = position(parse_token(wanted[name], partitioner), first, size)
                except TokenError as exc:
                    out["problems"].append("%s: %s" % (name, exc))
                    continue
                src = next(p for p, n, dummy in cur if n == name)
                if src != dst:
                    moves.append((name, src, dst))
        else:
            moves = plan_balanced(cur, [], size, rf, dc_offset(index[dc]))["moves"]
        try:
            ordered = move_order(cur, moves, size)
        except TokenError as exc:
            out["problems"].append("%s: %s" % (dc, exc))
            continue
        out["lines"] += _table(dc, cur, size, first, rf, "now")
        if not ordered:
            out["lines"].append("  %s: nothing to move%s" % (dc, "" if wanted else " (the ring is as even as it gets)"))
            continue
        known = [loads.get(names.get(name)) for dummy, name, dummy2 in cur]
        unique = (sum(known) / min(rf, len(cur))) if known and None not in known else None
        state_ring = list(cur)
        for name, src, dst in ordered:
            after = [(dst if n == name else p, n, r) for p, n, r in state_ring]
            moved = transfer(state_ring, after, size, rf)
            gain = dict((n, g) for n, (g, dummy) in moved.items() if g)
            loss = dict((n, lost) for n, (dummy, lost) in moved.items() if lost)
            lost_any |= set(loss)
            out["steps"].append({
                "name": name, "address": names[name], "dc": dc,
                "from": str(token_of(src, first, size)), "to": str(token_of(dst, first, size)),
                "gain": dict((n, float(g)) for n, g in gain.items()),
                "loss": dict((n, float(lost)) for n, lost in loss.items()),
                "gain_bytes": dict((n, int(g * unique) if unique is not None else None) for n, g in gain.items())})
            state_ring = after
        out["lines"] += _table(dc, state_ring, size, first, rf, "after the moves",
                               dict((name, " >") for name, dummy, dummy2 in ordered))
        for p, name, dummy in state_ring:
            all_tokens.setdefault(p, []).append(name)
    for p, same in sorted(all_tokens.items()):
        if len(same) > 1:
            out["problems"].append("%s would have the same token %d" % (" and ".join(same), token_of(p, first, size)))
    if out["steps"]:
        out["lines"].append("Moves, one at a time, in this order:")
        for i, s in enumerate(out["steps"]):
            sizes = dict((n, "" if b is None else " ~" + _gib(b)) for n, b in s["gain_bytes"].items())
            receive = ", ".join("%s %s%s" % (n, _pct(g), sizes[n]) for n, g in sorted(s["gain"].items()))
            out["lines"].append("  %d. %s (%s): %s -> %s; streamed to %s" % (i + 1, s["name"], s["dc"], s["from"],
                                                                             s["to"], receive or "nobody"))
        out["cleanup"] = sorted(lost_any)
        out["lines"].append("Then a cleanup on the nodes that lose ranges: %s" % ", ".join(out["cleanup"]))
    return out


def _disk(entry):
    """{'free', 'total'}, given as such or as {'path', 'mounts'} (ansible_facts
    mounts: the file system the path is on)."""
    if not entry or "mounts" not in entry:
        return entry
    path = entry.get("path") or "/"
    on = [m for m in entry["mounts"] or [] if m.get("mount") and (
        m["mount"] == "/" or path == m["mount"] or path.startswith(m["mount"].rstrip("/") + "/"))]
    if not on:
        return None
    m = max(on, key=lambda x: len(x["mount"]))
    return {"free": m.get("size_available"), "total": m.get("size_total")}


def cassandra_token_disk_problems(step, disks, min_free_percent=20):
    """step: one of cassandra_token_move_plan's steps; disks: {name: {'free':
    bytes, 'total': bytes}} of the data directories' file systems, or {name:
    {'path': data directory, 'mounts': ansible_facts mounts}}. A node that
    receives data must keep min_free_percent of its disk free afterwards (it
    keeps the ranges it gives away until a cleanup)."""
    problems = []
    for name, size in sorted((step.get("gain_bytes") or {}).items()):
        disk = _disk((disks or {}).get(name))
        if size is None or not disk or not disk.get("total") or disk.get("free") is None:
            continue
        left = disk["free"] - size
        if left < disk["total"] * float(min_free_percent) / 100:
            problems.append("%s gets about %s from moving %s, it has %s free of %s: less than %s%% would be left free"
                            " (cassandra_move_min_free_percent)" % (name, _gib(size), step["name"], _gib(disk["free"]),
                                                                    _gib(disk["total"]), min_free_percent))
    return problems


def cassandra_ring_tokens(stdout):
    return parse_ring(stdout)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_token_assign": cassandra_token_assign,
            "cassandra_token_add_plan": cassandra_token_add_plan,
            "cassandra_token_move_plan": cassandra_token_move_plan,
            "cassandra_token_disk_problems": cassandra_token_disk_problems,
            "cassandra_ring_tokens": cassandra_ring_tokens,
        }
