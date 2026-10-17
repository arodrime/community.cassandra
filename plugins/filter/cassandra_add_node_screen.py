# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_add_node_reset_refusals: the new nodes add_node may not reset, each with why (the input of
cassandra_add_node_refusal); cassandra_add_node_blocks: the lines of each new node on add_node's screen."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible.plugins.filter.core import to_bool  # the bool filter, as the expressions these replace used

from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import cassandra_screen_title
from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

_UNCHECKED = "the directories above could not all be checked: fix that (root, the live cassandra.yaml)"
_SEED = ("in cassandra_seeds: joins as a regular node (a seed does not bootstrap), made a seed once it is up"
         " (cassandra_seeds applied live on every node, unless a datacenter would need seeds picked: then change_seeds)")


def cassandra_add_node_reset_refusals(new, hostvars):
    """new: the hosts to add; hostvars. -> [{name, title, holds, why, way, former}] for the hosts whose reset plan
    (_cassandra_node_reset_plan) has problems: the reset check's (_cassandra_node_reset_check: why, the way out,
    what the node holds, a former member) and the plan's others."""
    refusals = []
    for name in new:
        v = hostvars[name]
        plan = v.get("_cassandra_node_reset_plan") or {}
        if not plan.get("problems"):
            continue
        check = v.get("_cassandra_node_reset_check") or {}
        facts = v.get("_cassandra_preflight") or {}
        checked = check.get("problems") or []
        other = [p for p in plan["problems"] if p not in checked]
        why = ([check.get("why", "")] if checked else []) + (["reset refused: " + "; ".join(other)] if other else [])
        where = (" (%s/%s)" % (facts["cassandra_dc"], facts.get("cassandra_rack", "?"))) if facts.get("cassandra_dc") else ""
        refusals.append({"name": name, "title": name + where, "holds": check.get("holds", ""), "why": "; ".join(why),
                         "way": check.get("way", "") if checked else _UNCHECKED,
                         "former": check.get("former", False) and not other})
    return refusals


def _medusa_fqdn(name, v):
    if "cassandra_medusa_fqdn" in v:
        fqdn = v["cassandra_medusa_fqdn"]
    elif v.get("cassandra_medusa_fqdn_domain", ""):
        fqdn = "%s.%s" % ((v.get("ansible_facts") or {}).get("hostname", name), v["cassandra_medusa_fqdn_domain"])
    else:
        fqdn = ""
    return fqdn or "(Medusa works it out)"


def cassandra_add_node_blocks(new, hostvars, estimate):
    """new: the hosts to add; hostvars; estimate: cassandra_add_node_plan's ({host, bytes, basis}). -> [{title,
    lines}]: each new node's title and what the screen says of it (the data to stream, a seed made one once up,
    its Medusa fqdn, its reset)."""
    blocks = []
    for name in new:
        v = hostvars[name]
        lines = ["about %s to stream (%s)" % (out.size(e["bytes"]), e["basis"]) for e in estimate if e["host"] == name]
        if to_bool(v.get("_cassandra_service_is_seed", False)):
            lines.append(_SEED)
        if to_bool(v.get("cassandra_medusa_enabled", False)):
            lines.append("Medusa fqdn " + _medusa_fqdn(name, v))
        reset = (v.get("_cassandra_node_reset_plan") or {}).get("line", "")
        if reset:
            lines.append(reset)
        blocks.append({"title": cassandra_screen_title(name, v["_cassandra_preflight"]), "lines": lines})
    return blocks


class FilterModule(object):
    def filters(self):
        return {"cassandra_add_node_reset_refusals": cassandra_add_node_reset_refusals,
                "cassandra_add_node_blocks": cassandra_add_node_blocks}
