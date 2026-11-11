# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The move of a cluster upgraded from 4.x to 5.0 to storage_compatibility_mode NONE.

cassandra_storage_mode_from_4x: whether this upgrade comes from 4.x; cassandra_storage_mode_nodes: each node's
modes from its variables (the inventory's, its cassandra.yaml's, the running one: storage_mode_read.yml);
cassandra_storage_mode_behind: the nodes not at NONE on all three; cassandra_storage_mode_rows: them as a table;
cassandra_storage_mode_step: the next step of the move; cassandra_storage_mode_way_out: what to do before the
sstables are rewritten. The restart guards (storage_mode_guard.yml): cassandra_storage_mode_back (never back),
cassandra_storage_mode_jump (one step at most), cassandra_storage_mode_unsure (the mode it runs not known);
cassandra_storage_mode_ahead: a mode written since the start of Cassandra, not running yet (apply_config)."""

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
                    else " (rolling_restart --limit %s)" % b["node"],
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


def _stated(host):
    """host: a node's variables -> what -e cassandra_storage_mode_running gives for it (node:MODE,node:MODE, or a
    dict), as given ("" when nothing): the mode the operator read there by hand."""
    given = host.get("cassandra_storage_mode_running") or ""
    if isinstance(given, dict):
        pairs = list(given.items())
    else:
        pairs = [p.split(":", 1) for p in str(given).split(",") if ":" in p]
    found = [str(m).strip() for n, m in pairs if str(n).strip() == str(host.get("inventory_hostname"))]
    return found[-1] if found else ""


def _running(host):
    """The mode it runs: read over CQL or logged at its start, else as -e cassandra_storage_mode_running gives it;
    "" when none tells."""
    stated = _stated(host).upper()
    return cassandra_storage_mode_runs(host) or (stated if stated in _ORDER else "")


def cassandra_storage_mode_ahead(host):
    """host: as cassandra_storage_mode_back's -> {running, file} when Cassandra runs an earlier mode than its
    cassandra.yaml has (written since its start, not running yet: a restart), else {}. Only on evidence."""
    node = cassandra_storage_mode_nodes([host])[0]
    lines = (host.get("cassandra_storage_mode_read") or {}).get("stdout_lines") or []
    running = _running(host) if "running=yes" in lines else ""
    file_mode = _value(node["yaml"], _UNSET)[0]
    if running and file_mode and _ORDER.index(running) < _ORDER.index(file_mode):
        return {"running": running, "file": file_mode}
    return {}


def cassandra_storage_mode_unsure(host, target=None, target_sha1=None):
    """host, target: as cassandra_storage_mode_back's; target_sha1: the checksum of the cassandra.yaml written (with
    target), the one it started with when the record of its start has it (written back as it started: the mode it
    runs) -> {target, rows, cql} when a restart could take Cassandra
    back and the mode it runs could not be read: it runs, nothing tells its mode (no CQL answer, no line logged since
    its start, no -e cassandra_storage_mode_running for it) and its cassandra.yaml may have changed since that start
    (else the file tells), the mode restarted with not NONE (NONE is never back). rows: what was read and what could
    not, one fact per line; cql: where the CQL read asked. Else {}."""
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
    if mode is None or mode == "NONE":
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
    stated = _stated(host)
    if stated:
        rows.append("-e cassandra_storage_mode_running gives %s: not a mode (CASSANDRA_4, UPGRADING or NONE)" % stated)
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
                "cassandra_storage_mode_headline": cassandra_storage_mode_headline}
