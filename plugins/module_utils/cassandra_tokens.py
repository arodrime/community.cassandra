# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Single-token rings (num_tokens: 1): balanced tokens, ownership, and the
plans that add nodes or rebalance a datacenter. Exact integer math (tokens
are 64 or 127-bit integers), ownership as fractions.

Positions: a token minus the partitioner's first token, modulo the ring size,
so both partitioners are the same circle [0, size).

Conventions (see balanced_positions and dc_offset):
- N nodes of a datacenter at floor(i * size / N) + offset, i = 0..N-1
  (Murmur3: -2^63 + floor(i * 2^64 / N); Random: floor(i * 2^127 / N)).
- Datacenter offset: its index in the sorted datacenter names * 100: tokens
  stay distinct across datacenters (each datacenter has its own ring for
  NetworkTopologyStrategy, only the same token twice is refused).
- Nodes of a datacenter in rack round robin (racks sorted by name, each
  rack's nodes in inventory order): consecutive tokens on different racks.

Replicas follow NetworkTopologyStrategy (4.0+): walking the datacenter's
ring from the range's end, a node of a rack not seen yet is taken, a node of
a rack already seen only while the replication factor exceeds the number of
racks (and only that many times).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
from fractions import Fraction
from itertools import permutations

# partitioner: (first token, ring size)
PARTITIONERS = {
    "Murmur3Partitioner": (-2 ** 63, 2 ** 64),
    "RandomPartitioner": (0, 2 ** 127),
}
DC_OFFSET = 100
# per datacenter: the balanced plan tries every rotation, O(m^2 n) per spacing tried
# (~3 s for 128 nodes doubled, about a minute near the cap)
MAX_NODES = 256
# new nodes whose rack orders are all tried on the positions found (7! orders), more: slot by slot
PLACE_SEARCH = 7


class TokenError(ValueError):
    pass


def partitioner_range(partitioner):
    """(first token, ring size) of a partitioner, by class name or full name."""
    name = str(partitioner or "").strip().rsplit(".", 1)[-1]
    if name not in PARTITIONERS:
        raise TokenError("%s: tokens are only computed for Murmur3Partitioner and RandomPartitioner" % partitioner)
    return PARTITIONERS[name]


def parse_token(value, partitioner):
    """A token as an integer, checked against the partitioner's range."""
    first, size = partitioner_range(partitioner)
    text = str(value).strip()
    if not re.match(r"^-?[0-9]+$", text):
        raise TokenError("%r is not a single token (an integer)" % (value,))
    token = int(text)
    # RandomPartitioner accepts 2^127 itself (Murmur3: up to 2^63 - 1)
    # 2^127 folds onto 0 in the ring math: a ring with both is refused as one token twice
    last = first + size - (0 if first == 0 else 1)
    if not first <= token <= last:
        raise TokenError("%s is outside the %s range [%d, %d]" % (text, str(partitioner).rsplit(".", 1)[-1], first, last))
    return token


def position(token, first, size):
    return (token - first) % size


def token_of(pos, first, size):
    return first + pos % size


def dc_offset(index):
    """The offset of the index-th datacenter (datacenter names sorted)."""
    return index * DC_OFFSET


def balanced_positions(count, size, offset=0):
    """count positions spaced size / count apart (exact floor), the first one at offset."""
    if count < 1:
        return []
    if not 0 <= offset < size // count:
        raise TokenError("offset %d is not smaller than the spacing of %d nodes" % (offset, count))
    return [(i * size) // count + offset for i in range(count)]


def balanced_tokens(count, partitioner, offset=0):
    first, size = partitioner_range(partitioner)
    return [token_of(p, first, size) for p in balanced_positions(count, size, offset)]


def rack_order(nodes):
    """nodes: [(name, rack)] in inventory order. The same nodes, racks taken in
    turn (sorted by name), each rack's nodes in inventory order."""
    racks = {}
    for name, rack in nodes:
        racks.setdefault(rack, []).append(name)
    order = []
    queues = [(rack, racks[rack]) for rack in sorted(racks)]
    while any(q for dummy, q in queues):
        for rack, queue in queues:
            if queue:
                order.append((queue.pop(0), rack))
    return order


def _sorted_ring(ring, size):
    """ring: [(position, name, rack)]: sorted, no position twice."""
    pts = sorted(((p % size, name, rack) for p, name, rack in ring), key=lambda x: (x[0], x[1]))
    for a, b in zip(pts, pts[1:]):
        if a[0] == b[0]:
            raise TokenError("%s and %s have the same token" % (a[1], b[1]))
    return pts


def _replicas(pts, index, rf, rack_count):
    """Replicas of the range ending at pts[index] (NetworkTopologyStrategy)."""
    want = min(rf, len(pts))
    repeats = rf - rack_count
    seen, out = set(), []
    for k in range(len(pts)):
        if len(out) >= want:
            break
        dummy, name, rack = pts[(index + k) % len(pts)]
        if rack not in seen:
            seen.add(rack)
            out.append(name)
        elif repeats > 0:
            repeats -= 1
            out.append(name)
    return out


def ownership(ring, size, rf):
    """ring: [(position, name, rack)] of one datacenter. {name: (primary,
    effective)}: the share of the ring each node holds as primary owner, and as
    one of the rf replicas (sum: min(rf, nodes))."""
    pts = _sorted_ring(ring, size)
    racks = len(set(r for dummy, dummy2, r in pts))
    own = dict((name, [0, 0]) for dummy, name, dummy2 in pts)
    for i, (p, name, dummy) in enumerate(pts):
        length = (p - pts[i - 1][0]) % size if len(pts) > 1 else size
        own[name][0] += length
        for rep in _replicas(pts, i, rf, racks):
            own[rep][1] += length
    return dict((name, (Fraction(v[0], size), Fraction(v[1], size))) for name, v in own.items())


def _owner_index(pts, pos):
    """Index of the node whose range holds pos: the first position >= pos, cyclic."""
    for i, (p, dummy, dummy2) in enumerate(pts):
        if p >= pos:
            return i
    return 0


def transfer(before, after, size, rf):
    """{name: (gained, lost)}: the share of the ring each node replicates after
    and not before (streamed to it), before and not after (left on its disk
    until a cleanup). before, after: [(position, name, rack)] of one datacenter."""
    b, a = _sorted_ring(before, size), _sorted_ring(after, size)
    bounds = sorted(set(p for p, dummy, dummy2 in b) | set(p for p, dummy, dummy2 in a))
    racks_b = len(set(r for dummy, dummy2, r in b))
    racks_a = len(set(r for dummy, dummy2, r in a))
    out = dict((name, [0, 0]) for dummy, name, dummy2 in b + a)
    for k, end in enumerate(bounds):
        length = (end - bounds[k - 1]) % size if len(bounds) > 1 else size
        if not length:
            continue
        reps_b = set(_replicas(b, _owner_index(b, end), rf, racks_b)) if b else set()
        reps_a = set(_replicas(a, _owner_index(a, end), rf, racks_a)) if a else set()
        for name in reps_a - reps_b:
            out[name][0] += length
        for name in reps_b - reps_a:
            out[name][1] += length
    return dict((name, (Fraction(v[0], size), Fraction(v[1], size))) for name, v in out.items())


def _balance_key(ring, size, rf):
    """The effective shares, largest first, then the primary shares: a ring is
    better when its largest share is smaller, then its second largest... (a
    split that lowers no maximum still splits one of the largest ranges)."""
    own = ownership(ring, size, rf)
    return (sorted((e for dummy, e in own.values()), reverse=True),
            sorted((p for p, dummy in own.values()), reverse=True))


def _alternate(base, spots, new):
    """The new nodes on the positions in ring order, each time a node of a rack
    other than the previous node's when there is one."""
    left = rack_order(new)
    placed = []
    for t in sorted(spots):
        before = sorted(base + placed)
        prev = [r for p, dummy, r in before if p < t]
        prev_rack = prev[-1] if prev else (before[-1][2] if before else None)
        pick = next((x for x in left if x[1] != prev_rack), left[0])
        left.remove(pick)
        placed.append((t, pick[0], pick[1]))
    return placed


def _place(base, spots, new, size, rf, given=None):
    """The new nodes [(name, rack)] on the positions spots, their racks in the
    order that leaves the most even effective shares (_balance_key: with one
    token per node, which rack follows which decides the replicas): every
    order while there are at most PLACE_SEARCH nodes; beyond, the best of the
    given placement, racks alternating along the ring, and a slot by slot
    choice. Each rack's nodes in rack_order's order."""
    spots = sorted(spots)
    if len(set(r for dummy, r in new) | set(r for dummy, dummy2, r in base)) < 2:
        return given or [(p, name, rack) for p, (name, rack) in zip(spots, new)]

    def key(placed):
        return _balance_key(base + placed, size, rf)

    def build(racks):
        queues = {}
        for name, rack in rack_order(new):
            queues.setdefault(rack, []).append(name)
        return [(p, queues[rack].pop(0), rack) for p, rack in zip(spots, racks)]
    racks = [r for dummy, r in new]
    if len(new) <= PLACE_SEARCH:
        candidates = [build(order) for order in sorted(set(permutations(racks)))]
    else:
        order, left = [], list(racks)
        for dummy in spots:  # slot by slot: the rack that keeps the ring placed so far the most even
            rack = min(sorted(set(left)), key=lambda r: key(build(order + [r])))
            order.append(rack)
            left.remove(rack)
        candidates = [build(order), _alternate(base, spots, new)]
    return min(candidates + ([given] if given else []), key=key)


def _greedy_bisect(ring, new, size, rf, key):
    """Each new node in turn at the middle of the range whose split gives the
    smallest key(ring after, size, rf), then the lowest position."""
    cur = list(ring)
    placed = []
    for name, rack in new:
        if not cur:
            spot = (0, name, rack)
        else:
            pts = _sorted_ring(cur, size)
            best = None
            for i, (p, dummy, dummy2) in enumerate(pts):
                length = (p - pts[i - 1][0]) % size if len(pts) > 1 else size
                if length < 2:
                    continue
                cand = (pts[i - 1][0] + length // 2) % size
                score = (key(cur + [(cand, name, rack)], size, rf), cand)
                if best is None or score < best[0]:
                    best = (score, cand)
            if best is None:
                raise TokenError("no room left in the ring for %s" % name)
            spot = (best[1], name, rack)
        cur.append(spot)
        placed.append(spot)
    return placed


def _largest_first(ring, size, rf):
    """The effective shares largest first, then the primary ones (_balance_key)."""
    return _balance_key(ring, size, rf)


def _largest_only(ring, size, rf):
    """Only the largest effective share, then the largest primary one."""
    own = ownership(ring, size, rf)
    return (max(e for dummy, e in own.values()), max(p for p, dummy in own.values()))


def _split_largest(ring, new, size):
    """Each new node in turn at the middle of the largest range (the lowest
    one of equal ranges), whatever the racks."""
    cur = [p for p, dummy, dummy2 in _sorted_ring(ring, size)]
    placed = []
    for name, rack in new:
        if not cur:
            spot = 0
        else:
            gaps = [((p - cur[i - 1]) % size if len(cur) > 1 else size, -((cur[i - 1] + ((p - cur[i - 1]) % size or size)
                                                                             // 2) % size)) for i, p in enumerate(cur)]
            length, neg = max(gaps)
            if length < 2:
                raise TokenError("no room left in the ring for %s" % name)
            spot = -neg
        cur = sorted(cur + [spot])
        placed.append((spot, name, rack))
    return placed


def plan_bisect(ring, new, size, rf):
    """No move: each new node in turn (new: [(name, rack)]) at the middle of a
    range, picked greedily three ways (the most even effective shares, largest
    first; the smallest largest share; the largest range), the racks of the new
    nodes then ordered over each set of positions (_place); the most even of
    the three (_balance_key). A greedy plan: not always the best possible.
    Returns [(position, name, rack)] of the new nodes."""
    best, seen = None, set()
    for greedy in (_greedy_bisect(ring, new, size, rf, _largest_first), _greedy_bisect(ring, new, size, rf, _largest_only),
                   _split_largest(ring, new, size)):
        spots = tuple(sorted(p for p, dummy, dummy2 in greedy))
        if spots in seen:  # the same positions: the same racks order
            continue
        seen.add(spots)
        placed = _place(list(ring), list(spots), new, size, rf, given=greedy)
        score = _balance_key(list(ring) + placed, size, rf)
        if best is None or score < best[0]:
            best = (score, placed)
    return best[1]


def _distance(a, b, size):
    d = (b - a) % size
    return min(d, size - d)


def tolerance(count, size):
    """How far from its balanced position a node may stay: 1/10000 of a
    node's share (e.g. a ring split in its middles, a token off)."""
    return max(1, size // (count * 10000))


def _assign(existing, targets, size, tol=0):
    """Existing positions (sorted) to targets (sorted), keeping their ring
    order: fewest moves (a node within tol of its target stays), then the
    smallest total distance. Returns ((moves, distance), [target index per
    existing node])."""
    n, m = len(existing), len(targets)
    big = size * (n + 1)  # one move costs more than any total distance
    inf = big * (n + 1)
    best = None
    # the first node goes to rot[0] in one of the rotations: each rotation
    # fixes it there, the others go to rot[i..m-n+i] in order
    for s in range(m):
        rot = targets[s:] + targets[:s]
        dist = _distance(existing[0], rot[0], size)
        dp = [(0 if dist <= tol else big) + dist] + [inf] * (m - 1)
        choice = []
        for i in range(1, n):
            new, back = [inf] * m, [0] * m
            run, run_j = inf, 0
            for j in range(i, m - n + i + 1):
                if dp[j - 1] < run:
                    run, run_j = dp[j - 1], j - 1
                d = _distance(existing[i], rot[j], size)
                new[j] = run + (0 if d <= tol else big) + d
                back[j] = run_j
            dp = new
            choice.append(back)
        j = min(range(m), key=lambda x: dp[x])
        if best is None or dp[j] < best[0]:
            picks = [j]
            for back in reversed(choice):
                picks.append(back[picks[-1]])
            picks.reverse()
            best = (dp[j], [(s + x) % m for x in picks])
    return (best[0] // big, best[0] % big), best[1]


def plan_balanced(ring, new, size, rf, offset=0):
    """The datacenter's ring with its new nodes (new: [(name, rack)]) balanced:
    len(ring) + len(new) positions evenly spaced. Spacings tried: the
    convention (offset) and one starting at each node's position; existing
    nodes keep their ring order, fewest moves first (a node within
    tolerance() of its position stays where it is), then the shortest total
    distance moved, then the convention. New nodes fill the free positions,
    their racks in the order that evens the effective shares out (_place).
    Returns {'ring': [(position, name, rack)] after, 'moves': [(name, from,
    to)], 'new': [(position, name, rack)]}."""
    m = len(ring) + len(new)
    if m > MAX_NODES:
        raise TokenError("more than %d nodes in a datacenter: no balanced plan" % MAX_NODES)
    pts = _sorted_ring(ring, size)
    spacing = size // m
    tol = tolerance(m, size)
    offsets = []  # the convention first, then one per node, but not two within tol of each other
    for off in [offset % spacing] + sorted(p % spacing for p, dummy, dummy2 in pts):
        if all(min((off - o) % spacing, (o - off) % spacing) > tol for o in offsets):
            offsets.append(off)
    existing = [p for p, dummy, dummy2 in pts]

    def at_least(off):  # moves at least: the nodes with no position of this spacing within tol
        far = 0
        for p in existing:
            near = ((p - off) * m + size // 2) // size
            if min(_distance(p, (j % m) * size // m + off, size) for j in (near - 1, near, near + 1)) > tol:
                far += 1
        return far

    best = None
    for bound, rank, off in sorted((at_least(off), rank, off) for rank, off in enumerate(offsets)):
        if best is not None and bound > best[0][0][0]:
            break  # sorted by bound: no spacing left can move fewer nodes
        targets = balanced_positions(m, size, off)
        cost, picks = _assign(existing, targets, size, tol) if pts else ((0, 0), [])
        key = (cost, 0 if rank == 0 else 1)
        if best is None or key < best[0]:
            best = (key, targets, picks)
    dummy, targets, picks = best
    stays = [_distance(p, targets[j], size) <= tol for (p, dummy, dummy2), j in zip(pts, picks)]
    after = [(p if stay else targets[j], name, rack) for (p, name, rack), j, stay in zip(pts, picks, stays)]
    moves = [(name, p, targets[j]) for (p, name, dummy2), j, stay in zip(pts, picks, stays) if not stay]
    used = set(picks)
    free = [t for j, t in enumerate(targets) if j not in used]
    placed = _place(after, free, rack_order(new), size, rf)
    return {"ring": after + placed, "moves": moves, "new": placed}


def _gap(x, others):
    """Which gap between the sorted positions others x falls in (the last and
    the first gap are the same one, round the ring)."""
    below = sum(1 for o in others if o < x)
    return below % len(others)


def move_order(ring, moves, size):
    """Moves [(name, from, to)] in an order where each node moves without
    passing another node (the ring order stays the same at every step: each
    move streams only what its own range gains, and a run that stops half way
    plans the moves left again), else at least where no move lands on a token
    another node still holds. Raises when they block each other."""
    pos = dict((name, p % size) for p, name, dummy in ring)
    left = sorted(moves, key=lambda x: (x[1], x[0]))
    order = []

    def free(mv):
        return all(p != mv[2] % size for n, p in pos.items() if n != mv[0])

    def keeps_order(mv):
        others = sorted(p for n, p in pos.items() if n != mv[0])
        return not others or _gap(mv[1] % size, others) == _gap(mv[2] % size, others)
    while left:
        ready = next((mv for mv in left if free(mv) and keeps_order(mv)), None) or next(
            (mv for mv in left if free(mv)), None)
        if ready is None:
            raise TokenError("the moves of %s block each other (each one to a token another one holds)"
                             % ", ".join(mv[0] for mv in left))
        left.remove(ready)
        pos[ready[0]] = ready[2] % size
        order.append(ready)
    return order


# nodetool ring: per datacenter, a line per token
_RING_STATUS = ("Up", "Down", "?")
_RING_STATE = ("Normal", "Leaving", "Joining", "Moving", "?")


def parse_ring(stdout):
    """nodetool ring output -> {dc: [{'address', 'rack', 'status', 'state',
    'token'}]}, one entry per token line (a vnode has several)."""
    rings = {}
    dc = None
    for line in (stdout or "").splitlines():
        m = re.match(r"^Datacenter:\s*(.*?)\s*$", line)
        if m:
            dc = m.group(1)
            rings.setdefault(dc, [])
            continue
        fields = line.split()
        if dc is None or len(fields) < 6 or fields[0] == "Address":
            continue
        at = next((i for i in range(2, len(fields) - 1)
                   if fields[i] in _RING_STATUS and fields[i + 1] in _RING_STATE), None)
        if at is None or not re.match(r"^-?[0-9]+$", fields[-1]):
            continue
        rings[dc].append({"address": fields[0], "rack": " ".join(fields[1:at]), "status": fields[at],
                          "state": fields[at + 1], "token": fields[-1]})
    return rings
