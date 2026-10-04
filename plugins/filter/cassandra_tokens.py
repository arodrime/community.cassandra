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

A token is unique in the whole cluster: every planned token is checked
against the tokens of all datacenters. In add_node and move_node plans, a
worked out one that falls on another datacenter's token is moved on by a
token or two (no share that matters changes); create_cluster refuses a
token twice (its datacenters' offsets keep them apart).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from fractions import Fraction
from functools import wraps

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.filter.cassandra_ring import _bytes, _host_of, _ip
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_tokens import (
    TokenError, balanced_positions, dc_offset, move_order, ownership, parse_token, partitioner_range,
    plan_balanced, plan_bisect, position, rack_order, token_of, tolerance, transfer)

DEFAULT_RF = 3
GIB = 1024 ** 3
# shares that differ by less than this many tolerances per replica count as even
EVEN_TOLERANCES = 4


def _pct(frac):
    return "%.2f%%" % (float(frac) * 100)


def _gib(size):
    return "%.1f GiB" % (float(size) / GIB)


def _has_token(node):
    """A token is set (the inventory's null comes as 'None' through | string)."""
    return str(node.get("token") if node.get("token") is not None else "").strip() not in ("", "None")


def _system(name):
    return name == "system" or name.startswith("system_")


def _rf_set(keyspaces, dc, default):
    """The replication factors of the datacenter (NetworkTopologyStrategy
    keyspaces, not system_*: system_auth is often replicated to every node
    and holds next to nothing), [default] when none is known."""
    rfs = set(ks["rf"][dc] for name, ks in (keyspaces or {}).items()
              if not _system(name) and ks["rf"].get(dc, 0) > 0)
    return sorted(rfs) or [int(default or DEFAULT_RF)]


def _rf_by_dc(keyspaces, dcs, default):
    """The largest of them per datacenter: the factor the shares shown assume."""
    return dict((dc, _rf_set(keyspaces, dc, default)[-1]) for dc in dcs)


def _transfer_all(before, after, size, rfs):
    """transfer() for every replication factor of the datacenter: a node gains
    (loses) the most it gains (loses) with any of them."""
    out = {}
    for rf in rfs:
        for name, (gain, lost) in transfer(before, after, size, rf).items():
            g, lo = out.get(name, (0, 0))
            out[name] = (max(g, gain), max(lo, lost))
    return out


def _simple_warning(keyspaces):
    simple = sorted(name for name, ks in (keyspaces or {}).items()
                    if ks.get("class") == "SimpleStrategy" and not _system(name))
    if not simple:
        return []
    return ["%s use%s SimpleStrategy: its replicas follow the whole ring, across datacenters and racks, which"
            " the shares shown leave out; after tokens change, every node of the cluster needs a cleanup"
            % (", ".join(simple), "s" if len(simple) == 1 else "")]


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


def _even(ring, size, rf):
    """The effective shares differ by no more than a few tolerances (rounding,
    a node left a token off its balanced position)."""
    if len(ring) < 2:
        return True
    eff = [e for dummy, e in ownership(ring, size, rf).values()]
    return max(eff) - min(eff) <= Fraction(EVEN_TOLERANCES * rf * tolerance(len(ring), size), size)


def _better(after, before, size, rfs):
    """The effective shares are closer to each other after, for some replication factor."""
    def spread(ring, rf):
        eff = [e for dummy, e in ownership(ring, size, rf).values()]
        return max(eff) - min(eff)
    return any(spread(after, rf) < spread(before, rf) for rf in rfs)


def _dc_index(dcs):
    return dict((dc, i) for i, dc in enumerate(sorted(set(dcs))))


def _free_offset(index, count, size, taken):
    """The datacenter's offset (index * 100), or the next one whose positions
    no other datacenter holds."""
    for i in range(index, index + 1000):  # 1000 tries: far more datacenters than a cluster has
        pos = balanced_positions(count, size, dc_offset(i))
        if not set(pos) & taken:
            return dc_offset(i)
    raise TokenError("no free offset for %d nodes" % count)


def _nudge(spots, taken, size):
    """Worked out positions [(position, name, rack)] moved on by a token or two
    when another node holds one; taken grows with them."""
    out = []
    for p, name, rack in spots:
        while p in taken:
            p = (p + 1) % size
        taken.add(p)
        out.append((p, name, rack))
    return out


def _addresses(hosts, ring):
    """{ring address: inventory name}. hosts: {address: name}; a ring address
    none of them gives is looked for in what the names (and the addresses
    that are names) resolve to on the controller, as the status playbook does."""
    known = {}
    for address, name in (hosts or {}).items():
        known.setdefault(name, []).extend([address, name])
    return _host_of(known, [_ip(n["address"]) for dc in (ring or {}) for n in ring[dc]])


def _ring_entries(ring, dc, hosts, problems):
    """[(address, name, rack, token)] of one datacenter of a parsed nodetool ring;
    a node with several tokens (vnodes) is a problem."""
    entries, seen = [], {}
    for n in (ring or {}).get(dc, []):
        address = _ip(n["address"])
        seen[address] = seen.get(address, 0) + 1
        entries.append((address, hosts.get(address, address), n["rack"], n["token"]))
    multi = sorted(a for a, c in seen.items() if c > 1)
    if multi:
        problems.append("%s: %s %s several tokens (vnodes): these plans are for one token per node"
                        % (dc, ", ".join(hosts.get(a, a) for a in multi), "has" if len(multi) == 1 else "have"))
        return None
    return entries


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
    Returns {'tokens': {name: token} to give, 'lines', 'problems', 'warnings'}."""
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


def _add_dc(dc, ring, new, hosts, partitioner, index, rfs, taken, out):
    """One datacenter of cassandra_token_add_plan. taken: the positions of every
    node of the cluster, and of the new ones already planned."""
    first, size = partitioner_range(partitioner)
    entries = _ring_entries(ring, dc, hosts, out["problems"])
    if entries is None:
        return
    existing = [(position(int(t), first, size), name, rack) for dummy, name, rack, t in entries]
    fixed, free = [], []
    for n in [n for n in new if n["dc"] == dc]:
        if not _has_token(n):
            free.append((n["name"], n["rack"]))
            continue
        try:
            p = position(parse_token(n["token"], partitioner), first, size)
        except TokenError as exc:
            out["problems"].append("%s: cassandra_initial_token %s" % (n["name"], exc))
            continue
        if p in taken:
            out["problems"].append("%s: cassandra_initial_token %s is already a token of the cluster"
                                   % (n["name"], n["token"]))
            continue
        taken.add(p)
        fixed.append((p, n["name"], n["rack"]))
    rf = rfs[dc]
    new_names = set(f[0] for f in free) | set(name for dummy, name, dummy2 in fixed)
    marks = dict((name, " +") for name in new_names)
    out["lines"] += _table(dc, existing, size, first, rf, "now")
    racks = {}
    for rack in [r for dummy, dummy2, r in existing + fixed] + [r for dummy, r in free]:
        racks[rack] = racks.get(rack, 0) + 1
    if len(racks) > 1 and len(set(racks.values())) > 1:
        out["warnings"].append("%s: racks of different sizes once the nodes are added (%s): with one token per node"
                               " some nodes hold more, whatever the tokens" % (
                                   dc, ", ".join("%s %d" % (r, c) for r, c in sorted(racks.items()))))
    if not existing:  # a new datacenter: as create_cluster would do it
        if fixed:
            placed = plan_bisect(fixed, rack_order(free), size, rf)
        else:
            off = _free_offset(index[dc], len(free), size, taken)
            placed = [(p, name, rack) for p, (name, rack) in
                      zip(balanced_positions(len(free), size, off), rack_order(free))]
        bisect_ring = balanced_ring = fixed + _nudge(placed, taken, size)
        moves = []
    else:
        others = set(taken)
        bisect_ring = existing + fixed + _nudge(plan_bisect(existing + fixed, rack_order(free), size, rf), taken, size)
        balanced_ring, moves = None, []
        if fixed:
            given = sorted(name for dummy, name, dummy2 in fixed)
            out["balanced_problems"].append(
                "%s: %s %s a cassandra_initial_token: balanced only places nodes without one (use bisect, or take it"
                " out of the inventory)" % (dc, ", ".join(given), "has" if len(given) == 1 else "have"))
        else:
            try:
                plan = plan_balanced(existing, free, size, rf, dc_offset(index[dc]))
            except TokenError as exc:
                out["balanced_problems"].append("%s: %s" % (dc, exc))
                plan = None
            if plan:
                mine = set(p for p, dummy, dummy2 in existing)
                # other datacenters' tokens: a worked out position moves on by a token or two
                avoid = others - mine
                dst = dict((name, d) for name, dummy, d in plan["moves"])
                spots = _nudge([(dst.get(name, p), name, rack) for p, name, rack in plan["ring"]
                                if name in dst or name in new_names], avoid, size)
                moved = dict((name, p) for p, name, dummy in spots)
                balanced_ring = [(moved.get(name, p), name, rack) for p, name, rack in plan["ring"]]
                moves = [(name, src, moved[name]) for name, src, dummy in plan["moves"]]
                taken |= set(p for p, name, dummy in balanced_ring if name in new_names)
                blocked = [name for p, name, dummy in balanced_ring if name in new_names and p in mine]
                if blocked:
                    out["balanced_problems"].append(
                        "%s: %s would bootstrap at a token a node that moves still holds: move it first (move_node),"
                        " or use bisect" % (dc, ", ".join(blocked)))
    for kind, kring in (("bisect", bisect_ring), ("balanced", balanced_ring)):
        if kring is None:
            out["even"][kind] = False
            continue
        for p, name, dummy in kring:
            if name in new_names:
                out[kind][name] = str(token_of(p, first, size))
        out["even"][kind] = out["even"][kind] and _even(kring, size, rf)
    out["lines"] += _table(dc, bisect_ring, size, first, rf, "bisect (no move)", marks)
    if existing and balanced_ring is not None:
        for name, src, dst in moves:
            marks[name] = " >"
            out["moves"].append({"name": name, "dc": dc, "from": str(token_of(src, first, size)),
                                 "to": str(token_of(dst, first, size))})
        out["lines"] += _table(dc, balanced_ring, size, first, rf, "balanced (%d move%s)" % (
            len(moves), "" if len(moves) == 1 else "s"), marks)
    if existing and not _even(bisect_ring, size, rf) and len(existing) + len(new_names) < 2 * len(existing):
        out["warnings"].append(
            "%s: bisect leaves the ring uneven; %d new node(s) instead of %d (the datacenter doubled to %d) would"
            " split every range in two: even with no move (when the racks can alternate)"
            % (dc, len(existing), len(new_names), 2 * len(existing)))


@_wrap
def cassandra_token_add_plan(ring, new_nodes, partitioner, keyspaces=None, default_rf=DEFAULT_RF, hosts=None):
    """ring: parsed nodetool ring; new_nodes: [{'name', 'dc', 'rack', 'token',
    'address'}] in join order (a node already in the ring is left out; a token
    set in the inventory is kept, and then only bisect places the others).
    hosts: {address: inventory name}.
    Returns {'bisect': {name: token}, 'balanced': {name: token}, 'moves':
    [{'name', 'dc', 'from', 'to'}] (balanced), 'balanced_problems', 'even':
    {'bisect': bool, 'balanced': bool}, 'lines', 'problems', 'warnings'}."""
    first, size = partitioner_range(partitioner)
    hosts = _addresses(hosts, ring)
    in_ring = set(_ip(n["address"]) for dc in (ring or {}) for n in ring[dc])
    new = [n for n in new_nodes if _ip(n.get("address") or "") not in in_ring]
    dcs = sorted(set(n["dc"] for n in new))
    index = _dc_index(dcs + list((ring or {}).keys()))
    rfs = _rf_by_dc(keyspaces, dcs, default_rf)
    taken = set(position(int(n["token"]), first, size) for dc in (ring or {}) for n in ring[dc])
    out = {"bisect": {}, "balanced": {}, "moves": [], "balanced_problems": [], "even": {"bisect": True, "balanced": True},
           "lines": [], "problems": [], "warnings": _simple_warning(keyspaces) if new else []}
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
    hosts = _addresses(hosts, ring)
    names = dict((hosts.get(_ip(n["address"]), _ip(n["address"])), _ip(n["address"]))
                 for dc in (ring or {}) for n in ring[dc])
    loads = {}
    for dc in (status or {}):
        for n in status[dc].get("nodes", []):
            loads[_ip(n["address"])] = _bytes(n.get("load"))
    dcs = sorted(ring or {})
    index = _dc_index(dcs)
    rfs = _rf_by_dc(keyspaces, dcs, default_rf)
    out = {"steps": [], "cleanup": [], "lines": [], "problems": [], "warnings": [], "after": {}}
    targets = dict(targets or {})
    for name in sorted(set(targets) - set(names)):
        out["problems"].append("%s is not in the ring (nodetool ring): no token to move" % name)
    # where every node of the cluster is, after the moves planned so far
    where = dict((hosts.get(_ip(n["address"]), _ip(n["address"])), position(int(n["token"]), first, size))
                 for dc in dcs for n in ring[dc])
    lost_any = set()
    for dc in dcs:
        members = set(hosts.get(_ip(n["address"]), _ip(n["address"])) for n in ring[dc])
        wanted = dict((k, v) for k, v in targets.items() if k in members)
        problems = []
        entries = _ring_entries(ring, dc, hosts, problems)
        if entries is None:
            if wanted or not targets:
                (out["problems"] if wanted else out["warnings"]).extend(problems)
            continue
        if targets and not wanted:
            continue
        busy = sorted(hosts.get(_ip(n["address"]), n["address"]) for n in ring[dc] if n["state"] != "Normal")
        if busy:
            out["problems"].append("%s: %s not Normal (joining, leaving or moving): wait until it is" % (dc, ", ".join(busy)))
            continue
        cur = [(position(int(t), first, size), name, rack) for dummy, name, rack, t in entries]
        elsewhere = set(p for name, p in where.items() if name not in members)
        rf = rfs[dc]
        all_rfs = _rf_set(keyspaces, dc, default_rf)
        if wanted:
            moves = []
            for name in sorted(wanted):
                try:
                    dst = position(parse_token(wanted[name], partitioner), first, size)
                except TokenError as exc:
                    out["problems"].append("%s: %s" % (name, exc))
                    continue
                src = where[name]
                # a node of this datacenter that moves away first is fine (move_order); one of another
                # datacenter moves after this datacenter's moves, if at all
                holder = [n for n, p in where.items() if p == dst and n != name and (n not in members or n not in targets)]
                if holder:
                    out["problems"].append("%s: token %s is %s's, which %s" % (
                        name, wanted[name], holder[0], "does not move" if holder[0] not in targets
                        else "moves later (another datacenter): move it first, in a run of its own"))
                elif src != dst:
                    moves.append((name, src, dst))
        else:
            planned = plan_balanced(cur, [], size, rf, dc_offset(index[dc]))["moves"]
            spots = _nudge([(dst, name, None) for name, dummy, dst in planned], set(elsewhere), size)
            moves = [(name, src, p) for (name, src, dummy), (p, dummy2, dummy3) in zip(planned, spots)]
            final = dict((name, p) for name, dummy, p in moves)
            if moves and not _better([(final.get(n, p), n, r) for p, n, r in cur], cur, size, all_rfs):
                out["lines"] += _table(dc, cur, size, first, rf, "now")
                out["lines"].append("  %s: nothing to move (the moves that even out the tokens leave the shares as they"
                                    " are: racks of different sizes)" % dc)
                continue
        try:
            ordered = move_order(cur, moves, size)
        except TokenError as exc:
            out["problems"].append("%s: %s" % (dc, exc))
            continue
        out["lines"] += _table(dc, cur, size, first, rf, "now")
        if not ordered:
            out["lines"].append("  %s: nothing to move%s" % (dc, "" if wanted else " (the tokens are as even as they get with the nodes in this order)"))
            continue
        known = [loads.get(names.get(name)) for dummy, name, dummy2 in cur]
        rf_data = min(all_rfs[0], len(cur))
        unique = (sum(known) / rf_data) if known and None not in known else None
        state_ring = list(cur)
        for name, src, dst in ordered:
            after = [(dst if n == name else p, n, r) for p, n, r in state_ring]
            moved = _transfer_all(state_ring, after, size, all_rfs)
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
            where[name] = dst
        out["lines"] += _table(dc, state_ring, size, first, rf, "after the moves",
                               dict((name, " >") for name, dummy, dummy2 in ordered))
    by_pos = {}
    for name, p in where.items():
        by_pos.setdefault(p, []).append(name)
    for p, same in sorted(by_pos.items()):
        if len(same) > 1:
            out["problems"].append("%s would have the same token %d" % (" and ".join(sorted(same)), token_of(p, first, size)))
    if out["steps"]:
        simple = _simple_warning(keyspaces)
        out["warnings"] += simple
        if keyspaces is None:
            out["warnings"].append("the replication could not be read: the shares assume RF %s, and every node of the"
                                   " datacenters that move is cleaned up" % default_rf)
            for dc in set(s["dc"] for s in out["steps"]):
                lost_any |= set(hosts.get(_ip(n["address"]), _ip(n["address"])) for n in ring[dc])
        if simple:  # its replicas follow the whole ring: every node may have lost some
            lost_any |= set(where)
        if any(None in s["gain_bytes"].values() for s in out["steps"]):
            out["warnings"].append("the loads are not all known: the disk space of the nodes that receive data is not"
                                   " checked before their moves")
        out["lines"].append("Moves, one at a time, in this order:")
        for i, s in enumerate(out["steps"]):
            sizes = dict((n, "" if b is None else " ~" + _gib(b)) for n, b in s["gain_bytes"].items())
            receive = ", ".join("%s %s%s" % (n, _pct(g), sizes[n]) for n, g in sorted(s["gain"].items()))
            out["lines"].append("  %d. %s (%s): %s -> %s; streamed to %s" % (i + 1, s["name"], s["dc"], s["from"],
                                                                             s["to"], receive or "nobody"))
        out["cleanup"] = sorted(lost_any)
        out["lines"].append("Then a cleanup on the nodes that lose ranges: %s" % ", ".join(out["cleanup"]))
        out["after"] = dict((s["name"], s["to"]) for s in out["steps"])
    return out


def _disk(entry):
    """{'free', 'total'}, given as such or as {'paths', 'mounts'} (ansible_facts
    mounts): the file systems the data directories are on, each counted once."""
    if not entry or "mounts" not in entry:
        return entry
    found = {}
    for path in entry.get("paths") or ["/"]:
        on = [m for m in entry["mounts"] or [] if m.get("mount") and (
            m["mount"] == "/" or path == m["mount"] or path.startswith(m["mount"].rstrip("/") + "/"))]
        if on:
            m = max(on, key=lambda x: len(x["mount"]))
            found[m["mount"]] = m
    if not found or any(m.get("size_available") is None or not m.get("size_total") for m in found.values()):
        return None
    return {"free": sum(m["size_available"] for m in found.values()), "total": sum(m["size_total"] for m in found.values())}


def cassandra_token_disk_problems(step, disks, min_free_percent=20):
    """step: one of cassandra_token_move_plan's steps; disks: {name: {'free':
    bytes, 'total': bytes}} of the data directories' file systems, or {name:
    {'paths': data directories, 'mounts': ansible_facts mounts}}. A node that
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


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_token_assign": cassandra_token_assign,
            "cassandra_token_add_plan": cassandra_token_add_plan,
            "cassandra_token_move_plan": cassandra_token_move_plan,
            "cassandra_token_disk_problems": cassandra_token_disk_problems,
        }
