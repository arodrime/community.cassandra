# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_seed_layout: checks the seeds of each datacenter against the
seed rule (2 or 3, on different racks when there are several) and suggests
a seed list.
cassandra_start_order: an order to start the nodes of a new cluster in that
the token allocator accepts.
cassandra_rack_refusals: the nodes of a datacenter the token allocator would
refuse for their rack (preflight's "Racks per datacenter")."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def cassandra_seed_layout(nodes):
    """nodes: [{'name', 'address', 'dc', 'rack', 'seed'}] in inventory order.
    The seed rule of preflight and help (plugin_utils cassandra_output
    seed_layout): {'lines': one per datacenter, 'problems': the warnings,
    'notes', 'suggested': [addresses] (empty when there is no warning)}.
    Example: lines ["Seeds  dc1  node1 (rack1), node4 (rack2)  ok"]."""
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


def _unique(values):
    """values without repeats, in order; as the unique filter, any case (R1 is r1)."""
    out, seen = [], set()
    for value in values:
        key = value.lower() if isinstance(value, str) else value
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def cassandra_rack_refusals(inventory, ring, hint, order=None, replacing=False):
    """inventory: the preflight facts of the datacenter's hosts ({address, cassandra_rack, layout: {name}});
    ring: its nodes in the ring ({address, rack}, down ones too); hint:
    cassandra_allocate_tokens_for_local_replication_factor; order: the host names in the order they join (the
    others after them); replacing: a replacement (it allocates nothing). -> the refusals: "the inventory" when
    there is no ring and the inventory has neither 1 rack nor at least hint; else each new node joining a rack
    already in the ring while the ring (the nodes before it included) has more than 1 rack but fewer than
    hint."""
    hint = int(hint)
    racks = _unique(n["rack"] for n in ring)
    if not racks:
        count = len(_unique(h["cassandra_rack"] for h in inventory))
        return ["the inventory"] if count != 1 and count < hint else []
    if replacing:
        return []
    known = [n["address"] for n in ring]
    new = [h for h in inventory if h.get("address") not in known]
    ordered = []
    for name in order or []:
        ordered += [h for h in new if h["layout"]["name"] == name and h not in ordered]
    out = []
    for host in ordered + [h for h in new if h not in ordered]:
        if host["cassandra_rack"] in racks and len(racks) != 1 and len(racks) < hint:
            out.append("%s in %s when the ring has %s" % (host["layout"]["name"], host["cassandra_rack"], ", ".join(racks)))
        elif host["cassandra_rack"] not in racks:
            racks.append(host["cassandra_rack"])
    return out


class FilterModule(object):
    def filters(self):
        return {"cassandra_seed_layout": cassandra_seed_layout,
                "cassandra_start_order": cassandra_start_order,
                "cassandra_rack_refusals": cassandra_rack_refusals}
