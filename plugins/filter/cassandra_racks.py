# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_rack_refusals: the nodes of a datacenter the token allocator would refuse for their rack
(preflight's "Racks per datacenter")."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


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
        return {"cassandra_rack_refusals": cassandra_rack_refusals}
