# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_seed_layout: checks racks and seeds per datacenter against the
usual layout (3 racks, 3 seeds on different racks) and suggests a seed list."""

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


class FilterModule(object):
    def filters(self):
        return {"cassandra_seed_layout": cassandra_seed_layout}
