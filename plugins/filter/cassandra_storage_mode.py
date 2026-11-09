# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_storage_mode_nodes: the nodes' modes from their variables; cassandra_storage_mode_behind: the 5.0
nodes not yet at storage_compatibility_mode NONE, by the inventory and by their own cassandra.yaml (what Cassandra
reads); cassandra_storage_mode_row: one of them as a line; cassandra_storage_mode_step: the next step to NONE."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

# the order of the move (5.0): CASSANDRA_4, then UPGRADING, then NONE
_ORDER = ("CASSANDRA_4", "UPGRADING", "NONE")
# a 5.0 cassandra.yaml without the line: 5.0 runs CASSANDRA_4
_UNSET = "CASSANDRA_4"


def _mode(value, default):
    value = "" if value is None else str(value).strip().strip("'\"").upper()
    return value if value in _ORDER else default


def cassandra_storage_mode_nodes(hostvars):
    """hostvars: the nodes' variables (hostvars[host] of each) -> [{node, inventory, yaml}] for
    cassandra_storage_mode_behind: yaml from the read of storage_mode_read.yml (cassandra_storage_mode_read), None
    when it was not read."""
    nodes = []
    for h in hostvars or []:
        read = h.get("cassandra_storage_mode_read") or {}
        nodes.append({"node": h.get("inventory_hostname"), "inventory": h.get("cassandra_storage_compatibility_mode"),
                      "yaml": read.get("stdout", "").strip() if read.get("rc") == 0 else None})
    return nodes


def cassandra_storage_mode_row(behind):
    """One node of cassandra_storage_mode_behind's -> its line: "  n1  inventory UPGRADING  cassandra.yaml CASSANDRA_4"."""
    yaml = ("not read" if behind["yaml"] is None
            else behind["yaml"] if behind["yaml_line"] else "no such line (5.0 runs %s)" % _UNSET)
    return "  %s  inventory %s  cassandra.yaml %s" % (behind["node"], behind["inventory"], yaml)


def cassandra_storage_mode_behind(nodes):
    """nodes: [{node, inventory (its cassandra_storage_compatibility_mode, None when not set), yaml (the value of its
    cassandra.yaml, "" when it has no such line, None when not read)}] -> [{node, inventory, yaml, mode}] of the nodes
    where either is not NONE, mode the earlier of the two."""
    behind = []
    for n in nodes or []:
        inventory = _mode(n.get("inventory"), "NONE")
        yaml = None if n.get("yaml") is None else _mode(n.get("yaml"), _UNSET)
        mode = min([inventory] + ([yaml] if yaml else []), key=_ORDER.index)
        if mode != "NONE":
            behind.append({"node": n.get("node"), "inventory": inventory, "yaml": yaml, "mode": mode,
                           "yaml_line": bool(str(n.get("yaml") or "").strip())})
    return behind


def cassandra_storage_mode_step(behind):
    """behind: cassandra_storage_mode_behind's -> what to do next (ends with a space), "" when nothing."""
    modes = [b["mode"] for b in behind or []]
    if "CASSANDRA_4" in modes:
        return ("Now set cassandra_storage_compatibility_mode: UPGRADING in the inventory and run apply_config, then NONE "
                "and apply_config again (NONE is the point of no return). ")
    if "UPGRADING" in modes:
        return ("Now set cassandra_storage_compatibility_mode: NONE in the inventory and run apply_config (NONE is the "
                "point of no return). ")
    return ""


class FilterModule(object):
    def filters(self):
        return {"cassandra_storage_mode_nodes": cassandra_storage_mode_nodes,
                "cassandra_storage_mode_row": cassandra_storage_mode_row,
                "cassandra_storage_mode_behind": cassandra_storage_mode_behind,
                "cassandra_storage_mode_step": cassandra_storage_mode_step}
