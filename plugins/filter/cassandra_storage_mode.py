# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The move of a cluster upgraded from 4.x to 5.0 to storage_compatibility_mode NONE.

cassandra_storage_mode_from_4x: whether this upgrade comes from 4.x; cassandra_storage_mode_nodes: each node's
modes from its variables (the inventory's, its cassandra.yaml's, the running one: storage_mode_read.yml);
cassandra_storage_mode_behind: the nodes not at NONE on all three; cassandra_storage_mode_rows: them as a table;
cassandra_storage_mode_step: the next step of the move; cassandra_storage_mode_way_out: what to do before the
sstables are rewritten."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

# the order of the move (5.0): CASSANDRA_4, then UPGRADING, then NONE
_ORDER = ("CASSANDRA_4", "UPGRADING", "NONE")
# a 5.0 cassandra.yaml without the line: 5.0 runs CASSANDRA_4
_UNSET = "CASSANDRA_4"
_STEPS = {
    "CASSANDRA_4": ("Now set cassandra_storage_compatibility_mode: UPGRADING in the inventory and run apply_config, then"
                    " NONE and apply_config again (NONE is the point of no return)."),
    "UPGRADING": ("Now set cassandra_storage_compatibility_mode: NONE in the inventory and run apply_config (NONE is"
                  " the point of no return)."),
}


def _version(text):
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:3])


def cassandra_storage_mode_from_4x(hostvars, target):
    """hostvars: the nodes' variables; target: the version upgraded to -> whether this upgrade comes from 4.x: a
    node still runs 4.x (cassandra_upgrade_from_series), else the newest config copy prepare made
    (<conf>.pre-upgrade-<version>, cassandra_upgrade_copies) below the target is a 4.x one. A 5.0 patch upgrade
    (its copy 5.0.x) is not, whatever older copies remain."""
    copies = []
    for h in hostvars or []:
        if str(h.get("cassandra_upgrade_from_series", "")) in ("40x", "41x"):
            return True
        copies += [c.strip() for c in (h.get("cassandra_upgrade_copies") or {}).get("stdout_lines", []) if c.strip()]
    below = [c for c in copies if _version(c) and _version(c) < _version(target)]
    return bool(below) and max(below, key=_version).startswith("4.")


def cassandra_storage_mode_nodes(hostvars):
    """hostvars: the nodes' variables (hostvars[host] of each) -> [{node, inventory, yaml, running, pending,
    stopped, start_unread, future}]: inventory None when not set; yaml None when its cassandra.yaml was not read, ""
    when it has no such line; running the mode the running Cassandra runs (its cassandra.yaml's when it started after
    the file was last written, else the one it logged at its start), None when not known; pending: the file was
    written after Cassandra started; stopped: no Cassandra process found; start_unread: its start or the file's time
    not read; future: the file has a time after now (storage_mode_read.yml's lines)."""
    nodes = []
    for h in hostvars or []:
        lines = (h.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
        read = dict(line.split("=", 1) for line in lines if "=" in line)
        yaml = read.get("yaml", "") if read.get("yaml_read") == "yes" else None
        pending, future = read.get("pending") == "yes", read.get("future") == "yes"
        start_unread = read.get("start_read") == "no"
        logged = read.get("logged") or None
        running = None
        if read.get("running") == "yes":
            if logged:
                running = logged
            elif not (pending or future or start_unread) and yaml is not None:
                running = yaml or _UNSET  # no such line: what 5.0 runs then
        nodes.append({"node": h.get("inventory_hostname"), "inventory": h.get("cassandra_storage_compatibility_mode"),
                      "yaml": yaml, "running": running, "pending": pending, "stopped": read.get("running") == "no",
                      "start_unread": start_unread, "future": future})
    return nodes


def _value(value, unset):
    """-> (the mode, or None when unknown; what to show)."""
    if value is None:
        return None, "not read"
    text = str(value).strip().strip("'\"")
    if text == "" and unset:
        return unset, "no such line (5.0 runs %s)" % unset
    return (text.upper(), text) if text.upper() in _ORDER else (None, "%s (unknown)" % (text or '""'))


WRITTEN_AFTER = "its cassandra.yaml was written after Cassandra started"


def cassandra_storage_mode_behind(nodes):
    """nodes: cassandra_storage_mode_nodes' -> [{node, inventory, yaml, running (each as shown), mode (the earliest
    of the inventory's and the cassandra.yaml's; None when neither is known), running_mode, unknown (the inventory's
    value is no mode), unread ([what could not be read]), pending (a restart: its cassandra.yaml was written after
    Cassandra started, or it runs another mode), future (the file has a time after now)}] of the nodes where any of
    the three is not NONE, or not known."""
    behind = []
    for n in nodes or []:
        inventory = ("NONE", "NONE (not set)") if n.get("inventory") is None else _value(n.get("inventory"), None)
        yaml = _value(n.get("yaml"), _UNSET)
        running = _value(n.get("running"), None)
        if n.get("running") is None:
            running = (None, "no Cassandra process found" if n.get("stopped")
                       else "cassandra.yaml has a time after now" if n.get("future")
                       else "restart pending (%s)" % WRITTEN_AFTER if n.get("pending")
                       else "not read (the start of Cassandra)" if n.get("start_unread") else "not read")
        if [inventory[0], yaml[0], running[0]] == ["NONE"] * 3 and not n.get("pending"):
            continue
        written = [m for m in (inventory[0], yaml[0]) if m]
        unread = ([] if n.get("yaml") is not None else ["its cassandra.yaml"]) + (
            ["the mode it runs (no Cassandra process found)"] if n.get("stopped") else
            ["the start of its Cassandra"] if n.get("start_unread") and n.get("running") is None else
            ["the mode it runs"] if n.get("running") is None and not (n.get("pending") or n.get("future")) else [])
        behind.append({
            "node": n.get("node"), "inventory": inventory[1], "yaml": yaml[1], "running": running[1],
            "mode": min(written, key=_ORDER.index) if written else None,
            "running_mode": running[0],
            "unknown": inventory[0] is None,
            "unread": unread,
            "future": bool(n.get("future")),
            "pending": bool(n.get("pending")) or bool(yaml[0] and running[0] and yaml[0] != running[0])})
    return behind


def cassandra_storage_mode_rows(behind):
    """cassandra_storage_mode_behind's -> its table: a head, then a row per node, columns aligned."""
    rows = [("node", "inventory", "cassandra.yaml", "running")] + [
        (b["node"], b["inventory"], b["yaml"], b["running"]) for b in behind or []]
    widths = [max(len(str(r[i])) for r in rows) for i in range(3)]
    return ["  " + "  ".join([str(c).ljust(widths[i]) for i, c in enumerate(r[:3])] + [str(r[3])]) for r in rows]


def cassandra_storage_mode_step(behind):
    """behind: cassandra_storage_mode_behind's -> the next step of the move, by the inventory's and the
    cassandra.yaml's modes (ends with a space), "" when nothing."""
    modes = [b["mode"] for b in behind or []]
    for mode in ("CASSANDRA_4", "UPGRADING"):
        if mode in modes:
            return _STEPS[mode] + " "
    return ""


def _names(behind, key):
    return ", ".join(b["node"] for b in behind if b[key])


def _one(names, one, more):
    return one if "," not in names else more


# a cluster that stays before NONE (a 5.0 upgrade from 5.0, a choice): its sstables are already in the format it writes
STAYS = ("SSTables keep the 4.x format while storage_compatibility_mode is not NONE: nothing to rewrite. Skip this"
         " phase (cleanup next), or move to NONE first (point of no return) if you want the 5.0 format.")
_BEFORE = ("CASSANDRA_4", "UPGRADING")


def cassandra_storage_mode_headline(behind, from_4x=True):
    """behind: cassandra_storage_mode_behind's -> why the sstables phase is refused, one sentence per kind, then
    "Nothing was done."."""
    before = ", ".join(b["node"] for b in behind if b["mode"] in _BEFORE or b.get("running_mode") in _BEFORE)
    pending = ", ".join(b["node"] for b in behind if b["pending"] and b["mode"] not in _BEFORE)
    out = []
    if before:
        out.append("storage_compatibility_mode is not NONE on %s%s" % (before, (
            ": in a mode before NONE, 5.0 writes the sstables in the 4.x format, the sstables phase would rewrite them"
            " in it.") if from_4x else _one(before, ": its sstables are in the format it writes.",
                                            ": their sstables are in the format they write.")))
    if pending:
        out.append("A restart is pending on %s (%s)." % (pending, _one(
            pending, WRITTEN_AFTER, "their cassandra.yaml was written after Cassandra started")))
    future = _names(behind, "future")
    if future:
        out.append("On %s, cassandra.yaml has a time after now." % future)
    unknown = _names(behind, "unknown")
    if unknown:
        out.append("cassandra_storage_compatibility_mode is no mode for %s in the inventory." % unknown)
    unread = _names(behind, "unread")
    if unread:
        out.append("The mode could not be read on %s." % unread)
    return " ".join(out + ["Nothing was done."])


def cassandra_storage_mode_way_out(behind, from_4x=True, restart_command="", total=0):
    """behind: cassandra_storage_mode_behind's; from_4x: the upgrade comes from 4.x (the move to NONE is its next
    step), else a cluster that stays before NONE is told there is nothing to rewrite (and, some of its nodes at NONE
    already, how to finish the move: total, the number of nodes, more than the ones behind); restart_command: the
    command that restarts the nodes with a restart pending ->
    what to do before the sstables phase, one sentence per kind."""
    out = []
    unknown = _names(behind, "unknown")
    if unknown:
        out.append("Set cassandra_storage_compatibility_mode for %s to CASSANDRA_4, UPGRADING or NONE." % unknown)
    step = cassandra_storage_mode_step(behind).strip()
    stays = any(m in _BEFORE for b in behind for m in (b["mode"], b.get("running_mode")))
    if step and from_4x:
        out.append(step)
    elif stays and not from_4x:
        out.append(STAYS)
        if step and (_mid_move(behind) or len(behind) < total):
            out.append("To finish the move to NONE: " + step[0].lower() + step[1:])
    pending = _names(behind, "pending")
    if pending:
        out.append("Restart %s, so %s the mode %s cassandra.yaml has%s." % (
            pending, _one(pending, "it runs", "they run"), _one(pending, "its", "their"),
            (": " + restart_command) if restart_command else " (rolling_restart --limit)"))
    future = _names(behind, "future")
    if future:
        out.append("Check the clock of %s: %s cassandra.yaml has a time after now; once it is right, write the file"
                   " again (apply_config) or touch it, then restart %s (the file is then newer than %s start)." % (
                       future, _one(future, "its", "their"), _one(future, "the node", "the nodes"),
                       _one(future, "its", "their")))
    for b in behind:
        if b["unread"]:
            out.append("On %s, %s%s." % (
                b["node"], ("neither %s could be read" % " nor ".join(b["unread"])) if len(b["unread"]) > 1
                else "%s could not be read" % b["unread"][0],
                ": the cassandra.yaml of its cassandra_conf_dir must be readable" if "its cassandra.yaml" in b["unread"]
                else ""))
    return " ".join(out)


def _mid_move(behind):
    """Some nodes moved on already (a mode after another node's): the move to NONE is under way."""
    modes = set(b["mode"] for b in behind if b["mode"]) | set(b.get("running_mode") for b in behind if b.get("running_mode"))
    return len(modes) > 1


class FilterModule(object):
    def filters(self):
        return {"cassandra_storage_mode_from_4x": cassandra_storage_mode_from_4x,
                "cassandra_storage_mode_nodes": cassandra_storage_mode_nodes,
                "cassandra_storage_mode_behind": cassandra_storage_mode_behind,
                "cassandra_storage_mode_rows": cassandra_storage_mode_rows,
                "cassandra_storage_mode_step": cassandra_storage_mode_step,
                "cassandra_storage_mode_way_out": cassandra_storage_mode_way_out,
                "cassandra_storage_mode_headline": cassandra_storage_mode_headline}
