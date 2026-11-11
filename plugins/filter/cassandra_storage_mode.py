# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The move of a cluster upgraded from 4.x to 5.0 to storage_compatibility_mode NONE.

cassandra_storage_mode_from_4x: whether this upgrade comes from 4.x; cassandra_storage_mode_nodes: each node's
modes from its variables (the inventory's, its cassandra.yaml's, the running one: storage_mode_read.yml);
cassandra_storage_mode_behind: the nodes not at NONE on all three; cassandra_storage_mode_rows: them as a table;
cassandra_storage_mode_step: the next step of the move; cassandra_storage_mode_way_out: what to do before the
sstables are rewritten. The restart guards (storage_mode_guard.yml): cassandra_storage_mode_back (never back),
cassandra_storage_mode_jump (one step at most), cassandra_storage_mode_unsure (the mode it runs not known); their
stops with a way out that a run then passes (cassandra_storage_mode_back_stop, cassandra_storage_mode_jump_stop);
cassandra_storage_mode_given_wrong, cassandra_storage_mode_given_unknown: -e cassandra_storage_mode_running taken
only where nothing else tells the mode; cassandra_storage_mode_ahead: a mode written since the start of Cassandra, not
running yet (apply_config); cassandra_storage_mode_restarts_into_none: such a restart into NONE."""

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

# the inventory at the next mode already, the files not written
_APPLY = {
    "CASSANDRA_4": ("Now run apply_config (the inventory has cassandra_storage_compatibility_mode: UPGRADING), then set"
                    " NONE and run apply_config again (NONE is the point of no return)."),
    "UPGRADING": ("Now run apply_config (the inventory has cassandra_storage_compatibility_mode: NONE; NONE is the"
                  " point of no return)."),
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


def _live(cql):
    """cassandra_storage_mode_cql (storage_mode_read.yml, cassandra_cqlsh's result: its transformed_output with
    transform raw) -> the mode system_views.settings has, None when not read (null: no such line in the cassandra.yaml
    it started with, CASSANDRA_4)."""
    if not cql or cql.get("failed") or cql.get("skipped"):
        return None
    for row in str(cql.get("transformed_output") or "").splitlines():
        row = row.strip()
        if row in _ORDER:
            return row
        if row in ("null", "None"):
            return _UNSET
    return None


def _cql_host(rpc_address, listen_address=None):
    """The address storage_mode_read.yml asks over CQL (its _cassandra_storage_mode_cql_host)."""
    for address in (rpc_address, listen_address):
        if str(address or "") not in ("", "localhost", "0.0.0.0", "None"):
            return str(address)
    return "127.0.0.1"


def cassandra_storage_mode_nodes(hostvars):
    """hostvars: the nodes' variables (hostvars[host] of each) -> [{node, inventory, yaml, running, pending,
    stopped, start_unread, start_later, future, file}]: inventory None when not set; yaml None when its cassandra.yaml
    was not read, "" when it has no such line; running the mode the running Cassandra runs (system_views.settings over
    CQL, else the one it logged at its start, else its cassandra.yaml's when the file did not change since), None when
    not known; pending: the file
    changed after Cassandra started; stopped: no Cassandra process found; start_unread: its start or the file's time
    not read, or its start after now (start_later); future: the file has a time after now; file: its path
    (storage_mode_read.yml's lines)."""
    nodes = []
    for h in hostvars or []:
        lines = (h.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
        read = dict(line.split("=", 1) for line in lines if "=" in line)
        yaml = read.get("yaml", "") if read.get("yaml_read") == "yes" else None
        pending, future = read.get("pending") == "yes", read.get("future") == "yes"
        start_later = read.get("start_read") == "after-now"
        start_unread = read.get("start_read") == "no" or start_later
        logged = read.get("logged") or None
        live = _live(h.get("cassandra_storage_mode_cql"))
        running = None
        if read.get("running") == "yes":
            if live or logged:
                running = live or logged
                pending = False  # known: against the file
            elif not (pending or future or start_unread) and yaml is not None:
                running = yaml or _UNSET  # no such line: what 5.0 runs then
        nodes.append({"node": h.get("inventory_hostname"), "inventory": h.get("cassandra_storage_compatibility_mode"),
                      "yaml": yaml, "running": running, "pending": pending, "stopped": read.get("running") == "no",
                      "start_unread": start_unread, "start_later": start_later, "future": future,
                      "file": read.get("file") or "cassandra.yaml",
                      # the address storage_mode_read.yml asks over CQL (as keyspaces.yml)
                      "cql_host": _cql_host(h.get("cassandra_rpc_address"), h.get("cassandra_listen_address"))})
    return nodes


def _value(value, unset):
    """-> (the mode, or None when unknown; what to show)."""
    if value is None:
        return None, "not read"
    text = str(value).strip().strip("'\"")
    if text == "" and unset:
        return unset, "no such line (5.0 runs %s)" % unset
    return (text.upper(), text) if text.upper() in _ORDER else (None, "%s (unknown)" % (text or '""'))


WRITTEN_AFTER = "its cassandra.yaml changed after Cassandra started"


def cassandra_storage_mode_behind(nodes):
    """nodes: cassandra_storage_mode_nodes' -> [{node, inventory, yaml, running (each as shown), mode (the earliest
    of the inventory's, the cassandra.yaml's and the running one; None when none is known), running_mode, unknown
    (the inventory's value is no mode), unread ([what could not be read]), pending (a restart), ahead (it runs an
    earlier mode than its file), reverted (a later one), unsure (its file changed since its start, the mode it runs
    not read), future (the file has a time after now), stopped, start_later, file, yaml_mode, inventory_mode}] of the
    nodes where any of the three is not NONE, or not known."""
    behind = []
    for n in nodes or []:
        inventory = ("NONE", "NONE (not set)") if n.get("inventory") is None else _value(n.get("inventory"), None)
        yaml = _value(n.get("yaml"), _UNSET)
        running = _value(n.get("running"), None)
        if n.get("running") is None:
            running = (None, "no Cassandra process found" if n.get("stopped")
                       else "cassandra.yaml has a time after now" if n.get("future")
                       else "restart pending (%s)" % WRITTEN_AFTER if n.get("pending")
                       else "not read (the start of Cassandra is after now)" if n.get("start_later")
                       else "not read (the start of Cassandra)" if n.get("start_unread") else "not read")
        if [inventory[0], yaml[0], running[0]] == ["NONE"] * 3 and not n.get("pending"):
            continue
        # The mode it runs against its cassandra.yaml's. A restart takes it to the file's mode: harmful when the file
        # has an earlier one, so never advised then (nor when the mode it runs is not known); apply_config moves the
        # file one step from the file's mode: harmful when it runs an earlier mode than the file
        y, r = yaml[0], running[0]
        # runs an earlier mode than its file has (an apply_config cut after the write): a restart, nothing else
        ahead = bool(y and r and _ORDER.index(r) < _ORDER.index(y))
        # runs a later mode than its file has (edited back by hand): that mode written back, never a restart
        reverted = bool(y and r and _ORDER.index(r) > _ORDER.index(y))
        # the file changed since its start, the mode it runs not read: neither, it is read first
        unsure = bool(r is None and y and n.get("pending") and not n.get("stopped"))
        if unsure:
            running = (None, "not known (%s)" % WRITTEN_AFTER)
        # the inventory before both the file and the mode it runs (the node moved on, its inventory not): the
        # inventory set to the mode it runs, not a step from the inventory's
        lagging = bool(inventory[0] and y and r and _ORDER.index(inventory[0]) < min(_ORDER.index(y), _ORDER.index(r)))
        written = [m for m in (None if lagging else inventory[0], y, r) if m]
        unread = ([] if n.get("yaml") is not None else ["its cassandra.yaml"]) + (
            ["the mode it runs (no Cassandra process found)"] if n.get("stopped") else
            ["the start of its Cassandra"] if n.get("start_unread") and n.get("running") is None else
            ["the mode it runs"] if n.get("running") is None and not (n.get("pending") or n.get("future")) else [])
        behind.append({
            "node": n.get("node"), "inventory": inventory[1], "yaml": yaml[1], "running": running[1],
            "mode": min(written, key=_ORDER.index) if written else None,  # the earliest of the three
            "running_mode": running[0],
            "unknown": inventory[0] is None,
            "unread": unread,
            "future": bool(n.get("future")),
            "stopped": bool(n.get("stopped")),
            "start_later": bool(n.get("start_later")) and n.get("running") is None,
            "file": n.get("file") or "cassandra.yaml",
            "cql_host": n.get("cql_host") or "127.0.0.1",
            "reverted": reverted,
            "lagging": lagging,
            # its mode written back by apply_config: the inventory's, one step from the file's (it moves one at a time)
            "write_back": reverted and inventory[0] == r and _ORDER.index(r) - _ORDER.index(y) == 1,
            "ahead": ahead,
            "unsure": unsure,
            "yaml_mode": y,
            "inventory_mode": inventory[0],
            # a restart: it runs an earlier mode than its file (one step: a restart, two: by hand first), or (another
            # cause) its file changed since its start, the mode it runs being its file's
            "pending": ahead or (not (reverted or unsure) and bool(n.get("pending")))})
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
            target = _ORDER[_ORDER.index(mode) + 1]
            if all(b.get("inventory_mode") == target for b in behind if b["mode"] == mode):
                return _APPLY[mode] + " "  # the inventory has it already: apply_config writes it
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
    plain = [b for b in behind if not (b.get("ahead") or b.get("reverted") or b.get("unsure") or b.get("lagging"))]
    before = ", ".join(b["node"] for b in plain if b["mode"] in _BEFORE or b.get("running_mode") in _BEFORE)
    pending = ", ".join(b["node"] for b in plain if b["pending"] and b["mode"] not in _BEFORE)
    out = []
    if before:
        out.append("storage_compatibility_mode is not NONE on %s%s" % (before, (
            ": in a mode before NONE, 5.0 writes the sstables in the 4.x format, the sstables phase would rewrite them"
            " in it.") if from_4x else _one(before, ": its sstables are in the format it writes.",
                                            ": their sstables are in the format they write.")))
    if pending:
        out.append("A restart is pending on %s (%s)." % (pending, _one(
            pending, WRITTEN_AFTER, "their cassandra.yaml changed after Cassandra started")))
    for b in behind:
        if b.get("lagging"):
            out.append("On %s, the inventory has %s, before the %s Cassandra runs." % (
                b["node"], b["inventory_mode"], b["running_mode"]))
        if b.get("ahead"):
            out.append("On %s, Cassandra runs %s, before the %s its cassandra.yaml has." % (
                b["node"], b["running_mode"], b["yaml_mode"]))
        elif b.get("reverted"):
            out.append("On %s, cassandra.yaml has %s, before the %s Cassandra runs." % (
                b["node"], b["yaml_mode"], b["running_mode"]))
        elif b.get("unsure"):
            out.append("On %s, the mode Cassandra runs is not known: its cassandra.yaml (%s) changed after it started."
                       % (b["node"], b["yaml_mode"]))
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


QUERY = "SELECT value FROM system_views.settings WHERE name = 'storage_compatibility_mode';"


def cassandra_storage_mode_way_out(behind, from_4x=True, restart_command="", total=0, apply_command="",
                                   check_restart_command=None):
    """behind: cassandra_storage_mode_behind's; from_4x: the upgrade comes from 4.x (the move to NONE is its next
    step), else a cluster that stays before NONE is told there is nothing to rewrite (and, some of its nodes at NONE
    already, how to finish the move: total, the number of nodes, more than the ones behind); restart_command: the
    command that restarts the nodes with a restart pending; apply_command: the apply_config command of the nodes whose
    cassandra.yaml has a mode before the one it runs, the inventory's (reverted); check_restart_command: {node: the
    command that restarts that node alone} for the nodes whose running mode is not known (unsure): each sentence
    names its own node's ->
    what to do before the sstables phase, one sentence per kind."""
    out = []
    unknown = _names(behind, "unknown")
    if unknown:
        out.append("Set cassandra_storage_compatibility_mode for %s to CASSANDRA_4, UPGRADING or NONE." % unknown)
    step = cassandra_storage_mode_step(behind).strip()
    # written before NONE (a node that only runs a mode before the one written has a restart pending, then this phase)
    stays = any(b["mode"] in _BEFORE for b in behind)
    # a node to put right first (it runs another mode than its file, or that is not known): that alone, then this
    # phase again (it then says the next step)
    first = [b for b in behind if b.get("ahead") or b.get("reverted") or b.get("unsure") or b.get("lagging")]
    if first:
        pass
    elif step and from_4x:
        out.append(step)
    elif stays and not from_4x:
        out.append(STAYS)
        if step and (_mid_move(behind) or len(behind) < total or _inventory_ahead(behind)):
            out.append("To finish the move to NONE: " + step[0].lower() + step[1:])
    for b in behind:
        if b.get("lagging"):
            out.append("On %s, set cassandra_storage_compatibility_mode: %s in the inventory (the mode it runs)." % (
                b["node"], b["running_mode"]))
    for b in behind:
        if b.get("ahead") and _ORDER.index(b["yaml_mode"]) - _ORDER.index(b["running_mode"]) > 1:
            out.append("On %s, set storage_compatibility_mode: %s in %s by hand (one step after the %s it runs)." % (
                b["node"], _ORDER[_ORDER.index(b["running_mode"]) + 1], b["file"], b["running_mode"]))
    back = [b for b in behind if b.get("reverted")]
    by_apply = [b["node"] for b in back if b["write_back"]]
    for b in back:
        if b["node"] not in by_apply:
            behind_inventory = b["inventory_mode"] and _ORDER.index(b["inventory_mode"]) < _ORDER.index(b["running_mode"])
            out.append("On %s, set storage_compatibility_mode: %s back in %s by hand (the mode it runs; apply_config"
                       " writes the inventory's, one step from the file's)%s." % (
                           b["node"], b["running_mode"], b["file"],
                           (", and cassandra_storage_compatibility_mode: %s in the inventory (apply_config never moves"
                            " it back)" % b["running_mode"]) if behind_inventory else ""))
    if by_apply:
        names = ", ".join(by_apply)
        out.append("Write the inventory's mode back to the cassandra.yaml of %s (apply_config restarts %s)%s." % (
            names, _one(names, "it", "them"), (": " + apply_command) if apply_command else ""))
    for b in behind:
        if b.get("unsure"):
            y = b["yaml_mode"]
            out.append(
                "On %s, read the mode its Cassandra runs (cqlsh %s, as the cluster's CQL user): %s (null: CASSANDRA_4)."
                " %s: restart it%s.%s%s" % (
                    b["node"], b["cql_host"], QUERY,
                    ("The same as its cassandra.yaml's (%s) or the step before" % y) if y != "CASSANDRA_4"
                    else "CASSANDRA_4", (": " + check_restart_command[b["node"]])
                    if isinstance(check_restart_command, dict) and b["node"] in check_restart_command
                    else " (rolling_restart --limit %s -e cassandra_storage_mode_running=%s:<its mode>)" % (
                        b["node"], b["node"]),
                    (" Two steps before: set UPGRADING in %s by hand first." % b["file"]) if y == "NONE" else "",
                    (" Later than %s: set that mode back in %s by hand." % (y, b["file"])) if y != "NONE" else ""))
    future = [b for b in behind if b["future"]]
    for path in sorted(set(b["file"] for b in future)):
        nodes = ", ".join(b["node"] for b in future if b["file"] == path)
        out.append("Set the clock of %s right (%s cassandra.yaml has a time after now), then on %s: touch %s." % (
            nodes, _one(nodes, "its", "their"), _one(nodes, "it", "them"), path))
    restart = ", ".join(b["node"] for b in behind if b["pending"] or b["future"])
    if restart:
        out.append("Restart %s, so %s the mode %s cassandra.yaml has%s." % (
            restart, _one(restart, "it runs", "they run"), _one(restart, "its", "their"),
            (": " + restart_command) if restart_command else " (rolling_restart --limit)"))
    for b in behind:
        if b["unread"]:
            out.append("On %s, %s%s." % (
                b["node"], ("neither %s could be read" % " nor ".join(b["unread"])) if len(b["unread"]) > 1
                else "%s could not be read" % b["unread"][0],
                ": the cassandra.yaml of its cassandra_conf_dir must be readable" if "its cassandra.yaml" in b["unread"]
                else ": start Cassandra on it (systemctl start cassandra)" if b["stopped"]
                else ": its start reads as after now (the btime of /proc/stat), check its clock" if b["start_later"]
                else ": from /proc/stat (btime) and /proc/<its pid>/stat" if "the start of its Cassandra" in b["unread"]
                else ""))
    if out and (from_4x or not stays or first):
        out.append("Then run this phase again.")
    return " ".join(out)


def _inventory_ahead(behind):
    """The inventory at a later mode than a node's cassandra.yaml: the move to NONE is chosen, not applied yet."""
    return any(b.get("inventory_mode") and b["mode"] and _ORDER.index(b["inventory_mode"]) > _ORDER.index(b["mode"])
               for b in behind)


def _mid_move(behind):
    """Some nodes moved on already (a mode after another node's): the move to NONE is under way."""
    modes = set(b["mode"] for b in behind if b["mode"]) | set(b.get("running_mode") for b in behind if b.get("running_mode"))
    return len(modes) > 1


def cassandra_storage_mode_runs(host):
    """host: a node's variables with storage_mode_read.yml's reads -> the storage_compatibility_mode it runs, as read
    over CQL or logged at its start; "" when neither tells."""
    lines = (host.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
    logged = dict(line.split("=", 1) for line in lines if "=" in line).get("logged")
    running = _live(host.get("cassandra_storage_mode_cql")) or logged
    return running if running in _ORDER else ""


def _given_pairs(given):
    """-e cassandra_storage_mode_running (node:MODE,node:MODE, or a dict) -> ([(node, mode)], [the items that are no
    node:MODE]). An IPv6 address keeps its colons: the mode is after the last one."""
    if isinstance(given, dict):
        return [(str(n).strip(), str(m).strip()) for n, m in given.items()], []
    pairs, bad = [], []
    for item in str(given or "").split(","):
        item = item.strip()
        if ":" in item:
            node, mode = item.rsplit(":", 1)
            pairs.append((node.strip(), mode.strip()))
        elif item:
            bad.append(item)
    return pairs, bad


def _stated(host):
    """host: a node's variables -> what -e cassandra_storage_mode_running gives for it (node:MODE,node:MODE, or a
    dict), as given ("" when nothing): the mode the operator read there by hand."""
    found = [m for n, m in _given_pairs(host.get("cassandra_storage_mode_running"))[0]
             if n == str(host.get("inventory_hostname"))]
    return found[-1] if found else ""


GIVEN = "given with -e"
FILE_TELLS = "its cassandra.yaml has it, unchanged since Cassandra started"


def _read(host):
    """-> (the mode it runs as read: over CQL, logged at its start, else its cassandra.yaml's when the file did not
    change since that start; how it was read), ("", "") when none tells (a 4.x Cassandra: none)."""
    lines = (host.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
    read = dict(line.split("=", 1) for line in lines if "=" in line)
    if read.get("running") != "yes" or (read.get("jvm_version") and _version(read["jvm_version"])[:1] < (5,)):
        return "", ""
    live = _live(host.get("cassandra_storage_mode_cql"))
    if live:
        return live, "read over CQL"
    if read.get("logged") in _ORDER:
        return read["logged"], "logged at its start"
    node = cassandra_storage_mode_nodes([host])[0]
    if node["running"] in _ORDER:
        return node["running"], FILE_TELLS
    return "", ""


def _running_how(host):
    """-> (the mode it runs, how it is known): read over CQL or logged at its start, else as
    -e cassandra_storage_mode_running gives it when nothing else tells (its cassandra.yaml changed since its start,
    or that start not read): ("", "") when none tells, or the file tells (its own mode: no move)."""
    mode, how = _read(host)
    if mode:
        return ("", "") if how == FILE_TELLS else (mode, how)
    stated = _stated(host).upper()
    return (stated, GIVEN) if stated in _ORDER else ("", "")


def _running(host):
    """The mode it runs, as _running_how; "" when none tells."""
    return _running_how(host)[0]


def cassandra_storage_mode_given_wrong(host):
    """host: as cassandra_storage_mode_back's -> the row that says why -e cassandra_storage_mode_running cannot be
    taken for it (not a mode, or not the mode read: over CQL, logged, its cassandra.yaml unchanged since its start),
    "" when it can (or gives nothing for it)."""
    stated = _stated(host)
    if not stated:
        return ""
    name = str(host.get("inventory_hostname"))
    if stated.upper() not in _ORDER:
        return "  %s  -e gives %s: not a mode (CASSANDRA_4, UPGRADING or NONE)" % (name, stated)
    mode, how = _read(host)
    if mode and mode != stated.upper():
        return "  %s  -e gives %s, Cassandra runs %s (%s)" % (name, stated.upper(), mode, how)
    return ""


def cassandra_storage_mode_given_unknown(given, nodes):
    """given: -e cassandra_storage_mode_running; nodes: the nodes of the run -> a row per item that names none of
    them (no node:MODE, an address, a typo)."""
    pairs, bad = _given_pairs(given)
    names = set(str(n) for n in nodes or [])
    return (["  %s  not node:MODE" % b for b in bad]
            + ["  %s  -e gives %s: no such node in this run (its inventory name)" % (n, m)
               for n, m in pairs if n not in names])


def cassandra_storage_mode_ahead(host):
    """host: as cassandra_storage_mode_back's -> {running, file, given} when Cassandra runs an earlier mode than its
    cassandra.yaml has (written since its start, not running yet: a restart), else {}. Only on evidence; given: the
    mode it runs as -e cassandra_storage_mode_running gives it."""
    node = cassandra_storage_mode_nodes([host])[0]
    lines = (host.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
    running, how = _running_how(host) if "running=yes" in lines else ("", "")
    file_mode = _value(node["yaml"], _UNSET)[0]
    if running and file_mode and _ORDER.index(running) < _ORDER.index(file_mode):
        return {"running": running, "file": file_mode, "given": how == GIVEN}
    return {}


def cassandra_storage_mode_unsure(host, target=None, target_sha1=None):
    """host, target: as cassandra_storage_mode_back's; target_sha1: the checksum of the cassandra.yaml written (with
    target), the one it started with when the record of its start has it (written back as it started: the mode it
    runs) -> {target, rows, cql} when a restart could take Cassandra
    back, or more than one step on, and the mode it runs could not be read: it runs, nothing tells its mode (no CQL
    answer, no line logged since its start, no -e cassandra_storage_mode_running for it) and its cassandra.yaml may
    have changed since that start (else the file tells). NONE too: a node that runs CASSANDRA_4 would cross the point
    of no return in one step. rows: what was read and what could not, one fact per line; cql: where the CQL read
    asked. Else {}."""
    lines = (host.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
    read = dict(line.split("=", 1) for line in lines if "=" in line)
    if read.get("running") != "yes" or _running(host):
        return {}
    if read.get("jvm_version") and _version(read["jvm_version"])[:1] < (5,):
        return {}  # a 4.x Cassandra (an upgrade to 5.0): no such mode, its sstables in the 4.x format
    if target_sha1 and read.get("started_sha1") == target_sha1:
        return {}
    changed = read.get("pending") == "yes"
    future = read.get("future") == "yes"
    start = read.get("start_read")
    if not (changed or future or start in ("no", "after-now")):
        return {}
    node = cassandra_storage_mode_nodes([host])[0]
    mode = _value(node["yaml"] if target is None else target, _UNSET)[0]
    if mode is None:
        return {}
    name = str(host.get("inventory_hostname"))
    cql = "%s:%s" % (_cql_host(host.get("cassandra_rpc_address"), host.get("cassandra_listen_address")),
                     host.get("cassandra_native_transport_port") or 9042)
    rows = ["its cassandra.yaml would have %s (the inventory's)" % mode if target is not None
            else "its cassandra.yaml has %s" % mode]
    if start == "no":
        rows.append("the start of its Cassandra or the time of its cassandra.yaml could not be read")
    elif start == "after-now":
        rows.append("the start of its Cassandra reads as after now (the btime of /proc/stat)")
    elif future:
        rows.append("its cassandra.yaml has a time after now")
    else:
        rows.append("its cassandra.yaml changed since Cassandra started (%s)" % (
            "its checksum is not the one recorded at that start" if read.get("changed") == "record"
            else "its mtime or ctime is after that start"))
    result = host.get("cassandra_storage_mode_cql") or {}
    if result.get("transformed_output") is not None and not result.get("failed"):
        rows.append("CQL: no storage_compatibility_mode in system_views.settings (cqlsh on %s)" % cql)
    else:
        err = [x for x in str(result.get("err") or result.get("msg") or "").strip().splitlines() if x.strip()]
        rows.append("CQL: not read (cqlsh on %s%s)" % (cql, (": " + err[-1].strip()[:200]) if err else ""))
    rows.append("system.log: not read (the start of its Cassandra not known)" if start in ("no", "after-now")
                else "system.log: no storage_compatibility_mode logged since its start")
    return {"target": mode, "rows": ["  %s  %s" % (name, r) for r in rows], "cql": cql}


def cassandra_storage_mode_running_none(hosts, hostvars):
    """hosts, hostvars -> the hosts that run storage_compatibility_mode NONE already (read over CQL or logged)."""
    return [h for h in hosts or [] if cassandra_storage_mode_runs(hostvars[h]) == "NONE"]


def cassandra_storage_mode_jump(host, target=None):
    """host, target: as cassandra_storage_mode_back's -> the mode it runs when a restart would take it more than one
    step after it (CASSANDRA_4 to NONE: an apply_config cut after its write, the role run alone, a file edited by
    hand), else "". Only on evidence, as cassandra_storage_mode_back."""
    node = cassandra_storage_mode_nodes([host])[0]
    running = _running(host)
    mode = _value(node["yaml"] if target is None else target, _UNSET)[0]
    if running in _ORDER and mode and _ORDER.index(mode) - _ORDER.index(running) > 1:
        return running
    return ""


def cassandra_storage_mode_back(host, target=None):
    """host: a node's variables (hostvars[host], with storage_mode_read.yml's reads); target: the
    storage_compatibility_mode its cassandra.yaml will have (the inventory's, written), None: the one it has (a
    restart alone) -> the mode it runs when a restart would take it to an earlier one, else "". Only on evidence (the
    mode read over CQL or logged at its start): a mode not known never refuses (the move forward from the file is
    guarded by cassandra_config's identity check)."""
    node = cassandra_storage_mode_nodes([host])[0]
    running = _running(host)
    mode = _value(node["yaml"] if target is None else target, _UNSET)[0]
    if running in _ORDER and mode and _ORDER.index(mode) < _ORDER.index(running):
        return running
    return ""


def _file(host):
    """-> (its cassandra.yaml's mode, the line there, its path, as shown)."""
    lines = (host.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
    read = dict(line.split("=", 1) for line in lines if "=" in line)
    node = cassandra_storage_mode_nodes([host])[0]
    mode = _value(node["yaml"], _UNSET)[0]
    line = bool(node["yaml"])
    return mode, line, read.get("file") or "its cassandra.yaml", (mode if line else "%s (no such line)" % _UNSET)


def _stop(host, inventory, written, running, how):
    """The row of a refused node: the mode it runs (how, when given with -e), the one its file has or would have."""
    name = str(host.get("inventory_hostname"))
    shown = _file(host)[3]
    return "  %s  runs %s%s, its cassandra.yaml %s" % (
        name, running, " (%s)" % GIVEN if how == GIVEN else "",
        ("would have %s" % inventory) if written else ("has %s" % shown))


def _again(host, how, running):
    """The -e the next commands need when the mode it runs was given with it (nothing else tells it yet)."""
    return (" -e cassandra_storage_mode_running=%s:%s" % (host.get("inventory_hostname"), running)) if how == GIVEN else ""


def cassandra_storage_mode_back_stop(host, inventory="NONE", written=False):
    """host: as cassandra_storage_mode_back's; inventory: its cassandra_storage_compatibility_mode; written: the
    inventory's written first (cassandra_config), else a restart of its file -> {node, row, way_out} when the
    restart would take it back, else {}. The way out writes the mode it runs back the way that works: apply_config
    moves a file one step forward, from a storage_compatibility_mode line (cassandra_config's identity check);
    else by hand, the inventory set to it when behind."""
    running, how = _running_how(host)
    if not cassandra_storage_mode_back(host, inventory if written else None):
        return {}
    name = str(host.get("inventory_hostname"))
    file_mode, line, path = _file(host)[:3]
    inventory_mode = _value(inventory, None)[0]
    behind = inventory_mode is None or _ORDER.index(inventory_mode) < _ORDER.index(running)
    one_step = line and file_mode and _ORDER.index(running) - _ORDER.index(file_mode) == 1
    again = _again(host, how, running)
    if written and file_mode and _ORDER.index(file_mode) >= _ORDER.index(running):
        way_out = "On %s, set cassandra_storage_compatibility_mode: %s in the inventory (its cassandra.yaml has it)." % (
            name, file_mode)
    elif one_step and written:
        way_out = "On %s, set cassandra_storage_compatibility_mode: %s in the inventory (apply_config then writes it)." % (
            name, running)
    elif one_step:
        way_out = ("On %s, run apply_config --limit %s%s (it writes %s back)." % (name, name, again, running)
                   if inventory_mode == running else
                   "On %s, set cassandra_storage_compatibility_mode: %s in the inventory, then run apply_config --limit"
                   " %s%s (it writes it back)." % (name, running, name, again))
    else:
        way_out = ("On %s, set storage_compatibility_mode: %s in %s by hand (apply_config moves it only one step"
                   " forward, from a storage_compatibility_mode line)%s." % (
                       name, running, path, (", and set cassandra_storage_compatibility_mode: %s in the inventory" % running)
                       if behind else ""))
    return {"node": name, "row": _stop(host, inventory, written, running, how), "way_out": way_out}


def cassandra_storage_mode_jump_stop(host, inventory="NONE", written=False):
    """host, inventory, written: as cassandra_storage_mode_back_stop's -> {node, row, way_out} when the restart would
    move it more than one step, else {}. The way out takes it to UPGRADING first: a restart of a file at UPGRADING;
    apply_config writes UPGRADING from a CASSANDRA_4 line only; else by hand."""
    running, how = _running_how(host)
    if not cassandra_storage_mode_jump(host, inventory if written else None):
        return {}
    name = str(host.get("inventory_hostname"))
    file_mode, line, path = _file(host)[:3]
    again = _again(host, how, running)
    restart = "then run rolling_restart --limit %s%s (it restarts it in UPGRADING)" % (name, again)
    if file_mode == "UPGRADING":
        way_out = "On %s, run rolling_restart --limit %s%s (it restarts it in the UPGRADING its cassandra.yaml has," \
                  " one step)." % (name, name, again)
    elif file_mode == "CASSANDRA_4" and line:
        way_out = ("On %s, set cassandra_storage_compatibility_mode: UPGRADING in the inventory, then run apply_config"
                   " --limit %s%s (it writes UPGRADING and restarts it), then set NONE in the inventory again." % (
                       name, name, again))
    else:
        way_out = "On %s, set storage_compatibility_mode: UPGRADING in %s by hand (%s), %s." % (
            name, path, "apply_config never moves its NONE back" if file_mode == "NONE"
            else "apply_config moves it only from a storage_compatibility_mode line", restart)
    return {"node": name, "row": _stop(host, inventory, written, running, how), "way_out": way_out}


def cassandra_storage_mode_restarts_into_none(hosts, hostvars):
    """hosts, hostvars -> the hosts whose cassandra.yaml has NONE already while Cassandra runs an earlier mode
    (cassandra_storage_mode_ahead): their restart crosses the point of no return with no change in the file."""
    return [h for h in hosts or [] if cassandra_storage_mode_ahead(hostvars[h]).get("file") == "NONE"]


class FilterModule(object):
    def filters(self):
        return {"cassandra_storage_mode_from_4x": cassandra_storage_mode_from_4x,
                "cassandra_storage_mode_nodes": cassandra_storage_mode_nodes,
                "cassandra_storage_mode_behind": cassandra_storage_mode_behind,
                "cassandra_storage_mode_rows": cassandra_storage_mode_rows,
                "cassandra_storage_mode_step": cassandra_storage_mode_step,
                "cassandra_storage_mode_way_out": cassandra_storage_mode_way_out,
                "cassandra_storage_mode_back": cassandra_storage_mode_back,
                "cassandra_storage_mode_jump": cassandra_storage_mode_jump,
                "cassandra_storage_mode_ahead": cassandra_storage_mode_ahead,
                "cassandra_storage_mode_unsure": cassandra_storage_mode_unsure,
                "cassandra_storage_mode_runs": cassandra_storage_mode_runs,
                "cassandra_storage_mode_running_none": cassandra_storage_mode_running_none,
                "cassandra_storage_mode_headline": cassandra_storage_mode_headline,
                "cassandra_storage_mode_back_stop": cassandra_storage_mode_back_stop,
                "cassandra_storage_mode_jump_stop": cassandra_storage_mode_jump_stop,
                "cassandra_storage_mode_given_wrong": cassandra_storage_mode_given_wrong,
                "cassandra_storage_mode_given_unknown": cassandra_storage_mode_given_unknown,
                "cassandra_storage_mode_restarts_into_none": cassandra_storage_mode_restarts_into_none}
