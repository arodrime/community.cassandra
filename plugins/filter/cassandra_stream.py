# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_stream_progress: the progress of a streaming operation (bootstrap,
decommission, rebuild, removenode) from successive cassandra_netstats results,
for the progress wait of roles/cassandra_service/tasks/stream_wait.yml."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

_GIB = 1024.0 ** 3


def _pair(done, total):
    if total >= _GIB / 10:
        return "%.1f/%.1f GiB" % (done / _GIB, total / _GIB)
    return "%.1f/%.1f MiB" % (done / 1024.0 ** 2, total / 1024.0 ** 2)


def _duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    return "%dm%02ds" % (seconds // 60, seconds % 60)


def cassandra_stream_progress(views, state=None, now=0, operations=None, peer=None, stall_checks=3, width=20):
    """views: the results of cassandra_netstats looped over hosts (item: the
    host, then the module's return values); state: what
    the previous call returned (None the first time); now: epoch seconds.
    operations: the session operations to follow (e.g. ['Bootstrap']), peer:
    only the sessions with this peer (a node read from the other side).
    Progress is bytes or files streamed, or a session started or finished,
    since the previous call. Returns the new state: streams (per session:
    total, done, gone), tables ({keyspace.table: 'done'|'streaming'}),
    progressed (since the previous call), last_progress, idle_checks (calls in
    a row without progress), stalled (stall_checks calls in a row without
    progress), sessions (sessions in netstats now),
    answered (at least one view answered), bytes_done/bytes_total, line (one
    readable line)."""
    state = state or {}
    streams = dict((k, dict(v)) for k, v in (state.get("streams") or {}).items())
    tables = dict(state.get("tables") or {})
    start = state.get("start", now)
    progressed = False
    answered = False
    current = set()
    now_files = []
    for result in views:
        if result.get("failed") or result.get("skipped") or "sessions" not in result:
            continue
        answered = True
        for s in result["sessions"]:
            if operations and s["operation"] not in operations:
                continue
            if peer and s["peer"] != peer:
                continue
            key = "|".join([str(result.get("item", "")), s["plan_id"], s["peer"], s["direction"]])
            current.add(key)
            before = streams.get(key)
            done = s["bytes_done"] + s["files_done"]
            if before is None or done > before["mark"] or s["bytes_total"] != before["total"]:
                progressed = True
            streams[key] = {"total": s["bytes_total"], "done": s["bytes_done"], "mark": done, "gone": False}
            for f in s["files"]:
                if not f["table"]:
                    continue
                if f["done"] < f["total"]:
                    tables[f["table"]] = "streaming"
                    now_files.append("%s (%s %s)" % (f["table"], {"receiving": "from", "sending": "to"}.get(
                        s["direction"], "on"), s["peer"]))
                else:
                    tables.setdefault(f["table"], "done")
    if answered:
        for key, stream in streams.items():
            if key not in current and not stream["gone"]:
                # a finished session leaves netstats: count it as fully streamed
                stream.update(gone=True, done=stream["total"])
                progressed = True
        streaming_now = set(n.split(" ", 1)[0] for n in now_files)
        for table, status in tables.items():
            if status == "streaming" and table not in streaming_now:
                tables[table] = "done"
    first = "last_progress" not in state
    last_progress = now if progressed or first else state["last_progress"]
    idle_checks = 0 if progressed or first else state.get("idle_checks", 0) + 1
    total = sum(s["total"] for s in streams.values())
    done = sum(s["done"] for s in streams.values())
    pct = int(100 * done / total) if total else 0
    filled = int(width * done / total) if total else 0
    parts = ["[%s%s] %3d%%" % ("#" * filled, "-" * (width - filled), pct), _pair(done, total)]
    if tables:
        parts.append("tables: %d done, %d streaming" % (list(tables.values()).count("done"),
                                                        list(tables.values()).count("streaming")))
    start_done = state.get("start_done", done)
    if done > start_done and total > done and now > start:
        parts.append("ETA ~%s" % _duration((total - done) * (now - start) / float(done - start_done)))
    if not answered:
        parts.append("(no answer from nodetool netstats)")
    elif not streams:
        parts.append("(no stream session yet)")
    else:
        parts.append("%d session%s" % (len(current), "" if len(current) == 1 else "s"))
    if now_files:
        parts.append("now: " + ", ".join(sorted(set(now_files))[:3]))
    if idle_checks:
        parts.append("NO PROGRESS for %s (%d check%s)" % (_duration(now - last_progress), idle_checks,
                                                          "" if idle_checks == 1 else "s"))
    return {
        "start": start, "start_done": start_done, "streams": streams, "tables": tables, "progressed": progressed,
        "last_progress": last_progress, "idle_checks": idle_checks,
        "stalled": idle_checks >= int(stall_checks), "sessions": len(current),
        "answered": answered, "bytes_done": done, "bytes_total": total, "line": "  ".join(parts),
    }


_LOAD_UNITS = {"bytes": 1, "B": 1, "KiB": 1024, "KB": 1024, "MiB": 1024 ** 2, "MB": 1024 ** 2,
               "GiB": 1024 ** 3, "GB": 1024 ** 3, "TiB": 1024 ** 4, "TB": 1024 ** 4}


def _load_bytes(load):
    """nodetool status Load ("412.3 GiB") in bytes, None when unknown ("?")."""
    try:
        number, unit = str(load).split()
        return int(float(number) * _LOAD_UNITS[unit])
    except (ValueError, KeyError):
        return None


def _rack_aware(keyspaces, dc, racks):
    """True when every user keyspace with replicas in dc (NetworkTopologyStrategy)
    has as many replicas there as dc has racks: each rack holds a full copy."""
    if keyspaces is None or racks < 2:
        return False
    rfs = [ks["rf"][dc] for name, ks in keyspaces.items()
           if not name.startswith("system") and ks.get("class") == "NetworkTopologyStrategy" and ks["rf"].get(dc)]
    return bool(rfs) and all(rf == racks for rf in rfs)


def cassandra_add_node_plan(cluster_status, new_nodes, hosts=None, keyspaces=None):
    """What adding new_nodes ([{host, address, dc, rack, in_ring}]; in_ring:
    already joining or joined, from an earlier run) to
    the ring in cluster_status (cassandra_status) means. hosts: {address:
    inventory name} of the nodes in the ring; keyspaces: cassandra_keyspaces
    (None: unknown). Returns racks ({dc: {rack: nodes after}}), warnings
    (uneven racks), estimate ([{host, bytes, basis}]: data each new node
    should receive) and cleanup ({dc: [hosts]}: the nodes that hand over
    ranges and need a cleanup afterwards, in ring order), scope ({dc: 'rack'
    when only the new nodes' racks hand over data, else 'dc'})."""
    hosts = hosts or {}
    racks, warnings, estimate, cleanup, scope = {}, [], [], {}, {}
    for dc in sorted(set(n["dc"] for n in new_nodes)):
        ring = (cluster_status.get(dc) or {}).get("nodes", [])
        after = {}
        for n in ring:
            after[n["rack"]] = after.get(n["rack"], 0) + 1
        for n in new_nodes:
            if n["dc"] == dc and not n.get("in_ring"):
                after[n["rack"]] = after.get(n["rack"], 0) + 1
        racks[dc] = after
        if len(set(after.values())) > 1:
            counts = ", ".join("%s %d" % (r, c) for r, c in sorted(after.items()))
            warnings.append(
                "%s after the add: %s nodes per rack. With NetworkTopologyStrategy and as many replicas as racks, "
                "each rack holds a full copy of the data: the nodes of a smaller rack each hold a bigger share "
                "(e.g. 1/%d against 1/%d), so they carry more data and load." % (
                    dc, counts, min(after.values()), max(after.values())))
        aware = _rack_aware(keyspaces, dc, len(after))
        loads = [(n, _load_bytes(n.get("load"))) for n in ring
                 if n["status"] + n["state"] == "UN" and n["address"] not in set(m.get("address") for m in new_nodes)]
        for n in [n for n in new_nodes if n["dc"] == dc and not n.get("in_ring")]:
            source = [b for m, b in loads if not aware or m["rack"] == n["rack"]]
            nodes_after = after[n["rack"]] if aware else sum(after.values())
            if source and None not in source:
                estimate.append({"host": n["host"], "bytes": sum(source) // nodes_after,
                                 "basis": ("the load of rack %s / %d nodes" % (n["rack"], nodes_after)) if aware
                                 else ("the load of %s / %d nodes" % (dc, nodes_after))})
        scope[dc] = "rack" if aware else "dc"
        new_racks = set(n["rack"] for n in new_nodes if n["dc"] == dc)
        # the nodes up and normal now (not the ones joining) that hand over ranges
        added = set(n.get("address") for n in new_nodes)
        cleanup[dc] = [hosts.get(n["address"], n["address"]) for n in ring
                       if n["status"] + n["state"] == "UN" and n["address"] not in added
                       and (not aware or n["rack"] in new_racks)]
    return {"racks": racks, "warnings": warnings, "estimate": estimate, "cleanup": cleanup, "scope": scope}


_COMPACTION = re.compile(r"^\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s+(.+?)\s+(\S+)\s+(\S+)"
                         r"\s+(\d+)\s+(\d+)\s+(\S+)\s+[0-9.,]+%")


def cassandra_cleanup_view(pair):
    """(nodetool compactionstats result, host): the Cleanup tasks as a view."""
    result, host = pair
    return cassandra_compactionstats(dict(result, item=host), types=["Cleanup"])


def cassandra_compactionstats(result, types=None):
    """A looped nodetool compactionstats command result (item: the host) as a
    cassandra_stream_progress view: one session per running compaction of
    the given types (e.g. ['Cleanup']), its table as the only file."""
    view = {"item": result.get("item", "")}
    if result.get("failed") or result.get("skipped") or result.get("rc", 0) != 0:
        view.update(failed=True, msg=result.get("msg", ""), stderr=result.get("stderr", ""))
        return view
    sessions = []
    for line in (result.get("stdout") or "").splitlines():
        m = _COMPACTION.match(line)
        if not m or (types and m.group(2) not in types):
            continue
        done, total = int(m.group(5)), int(m.group(6))
        sessions.append({"operation": m.group(2), "plan_id": m.group(1), "peer": view["item"], "direction": "local",
                         "files_total": 1, "files_done": 1 if done >= total else 0, "bytes_total": total,
                         "bytes_done": done, "files": [{"path": "", "table": "%s.%s" % (m.group(3), m.group(4)),
                                                        "done": done, "total": total}]})
    view["sessions"] = sessions
    return view


class FilterModule(object):
    def filters(self):
        return {"cassandra_stream_progress": cassandra_stream_progress,
                "cassandra_add_node_plan": cassandra_add_node_plan,
                "cassandra_compactionstats": cassandra_compactionstats,
                "cassandra_cleanup_view": cassandra_cleanup_view}
