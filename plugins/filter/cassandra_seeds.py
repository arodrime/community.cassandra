# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_seed_layout: checks the seeds of each datacenter against the
seed rule (2 or 3, on different racks when there are several) and suggests
a seed list.
cassandra_start_order: an order to start the nodes of a new cluster in that
the token allocator accepts."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out


def cassandra_seed_layout(nodes):
    """nodes: [{'name', 'address', 'dc', 'rack', 'seed'}] in inventory order.
    The seed rule of preflight and help (module_utils cassandra_output
    seed_layout): {'lines': one per datacenter, 'problems': the warnings,
    'notes', 'suggested': [addresses] (empty when there is no warning)}."""
    return out.seed_layout(nodes)


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
