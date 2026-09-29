# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_seed_layout: checks racks and seeds per datacenter against the
usual layout (3 racks, 3 seeds on different racks) and suggests a seed list.
cassandra_start_order: an order to start the nodes of a new cluster in that
the token allocator accepts."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

SEEDS_PER_DC = 3


def _names(nodes):
    names = [n["name"] for n in nodes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _suggest(racks, target):
    # Racks holding a seed first (keeps well-placed seeds), then one node per rack per pass
    order = sorted(racks, key=lambda r: not any(n["seed"] for n in racks[r]))
    queues = dict((r, sorted(racks[r], key=lambda n: not n["seed"])) for r in order)
    picked = []
    while len(picked) < target:
        for rack in order:
            if queues[rack] and len(picked) < target:
                picked.append(queues[rack].pop(0))
    return picked


def cassandra_seed_layout(nodes):
    """nodes: [{'name', 'address', 'dc', 'rack', 'seed'}] in inventory order.
    Returns {'problems': [...], 'suggested': [addresses]} (suggested is empty
    when there is no problem)."""
    dcs = {}
    for n in nodes:
        dcs.setdefault(n["dc"], {}).setdefault(n["rack"], []).append(n)
    problems, suggested = [], []
    for dc, racks in dcs.items():
        dc_nodes = [n for rack in racks.values() for n in rack]
        seeds = [n for n in dc_nodes if n["seed"]]
        target = min(SEEDS_PER_DC, len(dc_nodes))
        dc_problems = []
        if len(racks) < SEEDS_PER_DC and len(dc_nodes) >= SEEDS_PER_DC:
            dc_problems.append("%s has %d rack%s (%s): %d, one per replica with RF=%d, lets a whole rack go down"
                               % (dc, len(racks), "" if len(racks) == 1 else "s", ", ".join(racks),
                                  SEEDS_PER_DC, SEEDS_PER_DC))
        if len(seeds) != target:
            dc_problems.append("%s has %d seed%s%s: %d is the usual layout"
                               % (dc, len(seeds), "" if len(seeds) == 1 else "s",
                                  " (%s)" % _names(seeds) if seeds else "", target))
        empty = [r for r in racks if not any(n["seed"] for n in racks[r])]
        for rack, rack_nodes in racks.items():
            rack_seeds = [n for n in rack_nodes if n["seed"]]
            if len(rack_seeds) > 1 and empty:
                dc_problems.append("%s: seeds %s all on %s, %s ha%s none"
                                   % (dc, _names(rack_seeds), rack, " and ".join(empty), "s" if len(empty) == 1 else "ve"))
        problems += dc_problems
        suggested += [n["address"] for n in _suggest(racks, target)]
    return {"problems": problems, "suggested": suggested if problems else []}


def _true(value):
    return value is True or str(value).lower() in ("true", "yes", "1")


def _accepted(racks, rack, hint):
    """The token allocator (allocate_tokens_for_local_replication_factor: hint)
    refuses a node joining a rack its datacenter's ring (racks) already has
    while that ring has more than 1 rack but fewer than hint; a rack new to the
    ring passes."""
    return rack not in racks or len(racks) == 1 or len(racks) >= hint


def _with(racks, rack):
    return racks if rack in racks else racks + [rack]


def _feasible(racks, remaining, hint):
    """Whether the nodes of one datacenter left (their racks) can all still join a ring
    with racks, in some order."""
    if hint <= 1 or len(racks) >= hint:
        return True
    old = len([r for r in remaining if r in racks])
    new = {}
    for rack in remaining:
        if rack not in racks:
            new[rack] = new.get(rack, 0) + 1
    if len(racks) + len(new) >= hint:
        return True  # the first node of each new rack, then the others
    # the hint is never reached: only joins while the ring has 1 rack, and 1 node per new rack
    extra = sum(count - 1 for count in new.values())
    return extra == 0 and (len(racks) == 1 or old == 0)


def cassandra_start_order(nodes, hint=None, ring=None):
    """nodes: the _cassandra_preflight facts of the hosts, in inventory order
    (layout: name, dc, rack, seed; address). hint:
    cassandra_allocate_tokens_for_local_replication_factor ('' or None: no rack
    rule). ring: cassandra_status cluster_status of the nodes already running
    (their racks count, down nodes too; those nodes come first). Returns the
    host names: seeds first, then the others, each time the first node the
    allocator accepts and after which the nodes left of its datacenter can
    still all join (e.g. a node of a new rack before a seed of a rack already
    in a 2-rack ring, with a hint of 3)."""
    try:
        hint = int(hint)
    except (TypeError, ValueError):
        hint = 0
    racks = {}
    running = set()
    for dc, status in (ring or {}).items():
        for n in (status or {}).get("nodes", []):
            running.add(n["address"])
            if n["rack"] not in racks.setdefault(dc, []):
                racks[dc].append(n["rack"])
    order = [n["layout"]["name"] for n in nodes if n["address"] in running]
    left = [n for n in nodes if n["address"] not in running and _true(n["layout"]["seed"])]
    left += [n for n in nodes if n["address"] not in running and not _true(n["layout"]["seed"])]
    while left:
        accepted = [n for n in left if _accepted(racks.get(n["layout"]["dc"], []), n["layout"]["rack"], hint)]
        # the first one after which the others of its datacenter can still all join
        pick = next((n for n in accepted if _feasible(
            _with(racks.get(n["layout"]["dc"], []), n["layout"]["rack"]),
            [m["layout"]["rack"] for m in left if m is not n and m["layout"]["dc"] == n["layout"]["dc"]], hint)),
            accepted[0] if accepted else left[0])
        left.remove(pick)
        order.append(pick["layout"]["name"])
        dc_racks = racks.setdefault(pick["layout"]["dc"], [])
        if pick["layout"]["rack"] not in dc_racks:
            dc_racks.append(pick["layout"]["rack"])
    return order


class FilterModule(object):
    def filters(self):
        return {"cassandra_seed_layout": cassandra_seed_layout,
                "cassandra_start_order": cassandra_start_order}
