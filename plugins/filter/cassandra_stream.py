# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_stream_progress: the progress of a streaming operation (bootstrap,
decommission, rebuild, removenode) from successive cassandra_netstats results,
for the progress wait of roles/cassandra_service/tasks/stream_wait.yml;
cassandra_stream_report: the lines that print it."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
import time

_GIB = 1024.0 ** 3
# the rate is measured over this many check intervals
_WINDOW = 3
# beyond this, the end is shown as unknown
_ETA_MAX = 30 * 86400
# the waits between the first checks, seconds (then the check interval): a short
# operation ends in seconds, a long one is checked every interval
_BACKOFF = (10, 30, 60, 120, 240)
# the longest report line: the default stdout callback prints a msg list
# indented and quoted, 100 columns in all
_WIDTH = 88
# the bar's widest and narrowest width (narrower when the header is long)
_BAR = (20, 10)
# the other ends listed one per line, the rest summed up on one more line
_PEERS = 4
# the word in the header when the wait stops (stream_wait.yml statuses), FAILED for the others
_TROUBLE = {"stalled": "STALLED", "too_long": "TOO LONG", "stopped": "STOPPED"}
# the report's item lines: "      data:      38.2 GiB / 93.1 GiB"
_ITEM = "      %-11s%s"


def _size(count, scale):
    """count bytes in the unit that suits scale bytes: "41.2 GiB"."""
    for unit, size, least in (("TiB", 1024.0 ** 4, 1024.0 ** 4), ("GiB", _GIB, _GIB / 10),
                              ("MiB", 1024.0 ** 2, 1024.0 ** 2 / 10), ("KiB", 1024.0, 1024)):
        if scale >= least:
            return "%.1f %s" % (count / size, unit)
    return "%d B" % count


def _count(number):
    """1240 as "1 240"."""
    return "{0:,}".format(int(number)).replace(",", " ")


def _duration(seconds):
    """"45s", "12m", "1h12m", "2d04h"."""
    seconds = int(seconds)
    if seconds >= 86400:
        return "%dd%02dh" % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    if seconds >= 60:
        return "%dm" % (seconds // 60)
    return "%ds" % seconds


def _rate(per_second):
    if per_second >= _GIB:
        return "%.1f GiB/s" % (per_second / _GIB)
    if per_second >= 1024.0 ** 2:
        mib = per_second / 1024.0 ** 2
        return ("%.1f MiB/s" if mib < 10 else "%d MiB/s") % mib
    if per_second >= 1024:
        return "%d KiB/s" % (per_second / 1024.0)
    return "%d B/s" % per_second


def _clock(epoch, now):
    """The controller's local time of epoch with its zone, the date too when it
    is not today: "19:03 CEST", "2026-10-07 04:26 CEST"."""
    when = time.localtime(epoch)
    zone = time.strftime("%Z", when)
    if not zone or zone[0] in "+-":
        # no abbreviation for this zone: its offset
        offset = time.strftime("%z", when)
        zone = offset[:3] + ":" + offset[3:]
    return time.strftime("%H:%M" if when[:3] == time.localtime(now)[:3] else "%Y-%m-%d %H:%M", when) + " " + zone


def cassandra_stream_progress(views, state=None, now=0, operations=None, peer=None, stall_checks=3, quiet_factor=4,
                              interval=0):
    """views: the results of cassandra_netstats looped over hosts (item: the
    host, then the module's return values); state: what
    the previous call returned (None the first time); now: epoch seconds.
    operations: the session operations to follow (e.g. ['Bootstrap']), peer:
    only the sessions with this peer (a node read from the other side).
    Progress is bytes or files streamed, or a session started or finished,
    since the previous call. interval: the check interval, seconds; the
    first checks come sooner (_BACKOFF) and a check without progress counts
    towards a stall only interval seconds or more after the previous one.
    Returns the new state, for the next call and for
    cassandra_stream_report: streams (per session: total, done, files_total,
    files_done, other: the node at the other end, way: 'from' when the data
    comes from it, 'to' when it goes to it, 'on' for a local task, gone),
    progressed (since the previous call), start, now, last_progress,
    idle_checks (calls in a row without progress), limit and stalled
    (idle_checks reached limit: stall_checks while some session has bytes
    left, stall_checks * quiet_factor otherwise), transferring, sessions
    (sessions in netstats now), answered (at least one view answered),
    bytes_done/bytes_total, first_done (bytes done at the first answer),
    samples (time and bytes done of the last checks), rate (bytes per
    second over the last 3 checks, None while unknown or when nothing moved),
    checks (calls so far) and wait (seconds before the next check)."""
    state = state or {}
    streams = dict((k, dict(v)) for k, v in (state.get("streams") or {}).items())
    start = state.get("start", now)
    progressed = False
    answered = set()
    current = set()
    for result in views:
        if result.get("failed") or result.get("skipped") or "sessions" not in result:
            continue
        answered.add(str(result.get("item", "")))
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
            # read from the other side (peer): the other end is the host read, and the
            # data goes the other way
            way = {"receiving": "from", "sending": "to"}.get(s["direction"], "on")
            if peer and way != "on":
                way = "to" if way == "from" else "from"
            streams[key] = {"total": s["bytes_total"], "done": s["bytes_done"], "mark": done, "gone": False,
                            "files_total": s["files_total"], "files_done": s["files_done"], "way": way,
                            "other": str(result.get("item", "")) if peer else s["peer"]}
    if answered:
        for key, stream in streams.items():
            if key not in current and not stream["gone"] and key.split("|", 1)[0] in answered:
                # a finished session leaves netstats: count it as fully streamed (not when its
                # host did not answer this time)
                stream.update(gone=True, done=stream["total"], files_done=stream["files_total"])
                progressed = True
    first = "last_progress" not in state
    last_progress = now if progressed or first else state["last_progress"]
    # an early check (shorter wait) without progress does not count towards a stall
    full = not interval or now - state.get("now", now) >= int(interval)
    idle_checks = 0 if progressed or first else state.get("idle_checks", 0) + (1 if full else 0)
    checks = state.get("checks", 0) + 1
    # Nothing left to transfer in netstats (no session yet, or every one at 100%): phases
    # that show no bytes (ring delay, schema, the write path of tables with views or CDC,
    # index builds, hints of a decommission, a task queued behind other compactions) get
    # quiet_factor times more checks before counting as stalled.
    transferring = any(streams[k]["done"] < streams[k]["total"] for k in current)
    limit = int(stall_checks) * (1 if transferring else int(quiet_factor))
    total = sum(s["total"] for s in streams.values())
    done = sum(s["done"] for s in streams.values())
    samples = [list(x) for x in state.get("samples") or []]
    rate = None
    if answered:
        # over the last _WINDOW check intervals; a check without answer has no new count
        oldest = samples[-_WINDOW:][0] if samples else None
        if oldest and now > oldest[0] and done > oldest[1]:
            rate = (done - oldest[1]) / float(now - oldest[0])
        samples = (samples + [[now, done]])[-_WINDOW:]
    return {
        "start": start, "now": now, "streams": streams, "progressed": progressed, "last_progress": last_progress,
        "idle_checks": idle_checks, "limit": limit, "stalled": idle_checks >= limit, "sessions": len(current),
        "transferring": transferring, "answered": bool(answered), "bytes_done": done, "bytes_total": total,
        "first_done": state["first_done"] if state.get("samples") else done, "samples": samples, "rate": rate,
        "checks": checks, "wait": min(int(interval), _BACKOFF[checks - 1] if checks <= len(_BACKOFF) else int(interval)),
    }


def _header(words, done, total, rate):
    """"node4  bootstrap  [########------------]  41%   82 MiB/s", the bar narrower
    when the line would be too long; "total unknown" without a total."""
    head = "  ".join(x for x in words if x)
    tail = ("   " + _rate(rate)) if rate else ""
    if not total:
        return head + "  total unknown" + tail
    done = min(done, total)
    pct = "%3d%%" % int(100 * done / total)
    width = max(_BAR[1], min(_BAR[0], _WIDTH - len(head) - len(pct) - len(tail) - 5))
    filled = int(width * done / total)
    return "%s  [%s%s] %s%s" % (head, "#" * filled, "-" * (width - filled), pct, tail)


def _peers(streams):
    """The other ends, the most data first, with their own progress: "node1  52% done
    (9.6 / 18.4 GiB)", the ones beyond _PEERS summed up on one line."""
    others = {}
    for s in streams:
        other = others.setdefault(s["other"], [0, 0])
        other[0] += s["total"]
        other[1] += s["done"]
    ranked = sorted(others.items(), key=lambda x: (-x[1][0], x[0]))
    if len(ranked) > _PEERS:
        rest = ranked[_PEERS - 1:]
        ranked = ranked[:_PEERS - 1] + [("%d more" % len(rest), [sum(r[1][0] for r in rest), sum(r[1][1] for r in rest)])]
    ranked = [(name, size, moved) for name, (size, moved) in ranked if size]
    width = max([len(r[0]) for r in ranked] or [0])
    return ["%s  %3d%% done  (%s / %s)" % (name.ljust(width), int(100 * min(moved, size) / size),
                                          _size(moved, size).split()[0], _size(size, size))
            for name, size, moved in ranked]


def cassandra_stream_report(state, node="", what="", status="going", names=None, files_label="files", extra=None):
    """The lines to print for a cassandra_stream_progress state (a debug msg
    list prints one per line): a header with node, what, bar, percent and
    rate, then the data, the other ends and the times, an item per line; a
    single line once done. status: as in stream_wait.yml (going, done,
    stalled, too_long, stopped, *_failed...); names: {address: inventory
    name} for the other ends; files_label: what the files are called;
    extra: more [label, text] lines after the data."""
    total, done, now = state.get("bytes_total", 0), state.get("bytes_done", 0), state.get("now", 0)
    start = state.get("start", now)
    rate = state.get("rate")
    if status == "done":
        moved, elapsed = done - state.get("first_done", 0), now - start
        if not total:
            summary = "nothing streamed" + (" in %s" % _duration(elapsed) if elapsed > 0 else "")
        elif moved <= 0 or elapsed <= 0:
            # streamed before the first check
            summary = "%s, %s" % (_size(done, total), ("after %s" % _duration(elapsed)) if elapsed > 0 else "at the first check")
        else:
            summary = "%s in %s%s, %s on average" % (
                _size(moved, total), _duration(elapsed),
                # part of it streamed before the first check (a resumed wait)
                (" (%s in all)" % _size(done, total)) if moved != done else "", _rate(moved / float(elapsed)))
        return ["  ".join(x for x in [node, what, "done", summary] if x)]
    going = status == "going"
    lines = [_header([node, what] + ([] if going else [_TROUBLE.get(status, "FAILED")]), done, total, rate if going else None)]

    def block(label, values):
        lines.append("")
        lines.extend(_ITEM % (label if i == 0 else "", v) for i, v in enumerate(values))

    data = []
    if not state.get("answered"):
        data.append("no answer at this check, the figures are from the last answer")
    streams = [dict(s, other=(names or {}).get(s["other"], s["other"])) for s in (state.get("streams") or {}).values()]
    if total:
        data.append("%s / %s" % (_size(done, total), _size(total, total)))
        files_total = sum(s["files_total"] for s in streams)
        if files_label and files_total:
            data.append("%s / %s %s" % (_count(sum(s["files_done"] for s in streams)), _count(files_total), files_label))
    else:
        data.append("nothing in progress yet")
    block("data:", data)
    for label, text in extra or []:
        lines.append(_ITEM % (label + ":", text))
    if total:
        ways = set(s["way"] for s in streams)
        block((ways.pop() if len(ways) == 1 else "with") + ":", _peers(streams))
    times = [("Now:", "current", now), ("Started:", "%s ago" % _duration(now - start), start)]
    if going and total:
        left = (total - done) / rate if rate and total > done else None
        if done >= total:
            times.append(("Finish:", "all sent, finishing", None))
        elif left is not None and left <= _ETA_MAX:
            times.append(("Finish:", "in %s" % _duration(left), now + left))
        else:
            times.append(("Finish:", "unknown, " + ("too slow to tell" if left else "no rate yet"), None))
    # the clocks in one column; a Finish without a clock does not widen it
    width = max(len(t[1]) for t in times if t[2] is not None)
    lines.append("")
    for label, relative, epoch in times:
        lines.append((_ITEM % (label, relative.ljust(width) + (" - " + _clock(epoch, now) if epoch is not None else ""))).rstrip())
    idle = state.get("idle_checks", 0)
    if idle:
        lines.append("")
        lines.append(_ITEM % ("Progress:", "none for %d check%s (%s)%s" % (
            idle, "" if idle == 1 else "s", _duration(now - state.get("last_progress", now)),
            (", stops after %d" % state.get("limit", 0)) if going else "")))
    return lines


def _host_var(hostvars, host, *path):
    """hostvars[host][path[0]][path[1]]... when it is a string, else ""."""
    try:
        value = hostvars[host]
        for key in path:
            value = value.get(key) or {}
    except Exception:  # pylint: disable=broad-except  # an undefined or broken template in that host's variables
        return ""
    return value if isinstance(value, str) else ""


def cassandra_host_addresses(hosts, hostvars):
    """{address: inventory name} of hosts, from what their variables say they
    broadcast or listen on, their ansible_host and default IPv4 fact (netstats
    names the other ends by address). A variable that can't be read is skipped."""
    names = {}
    for host in hosts:
        for path in (("cassandra_extra_settings", "broadcast_address"), ("_cassandra_preflight", "ring_address"),
                     ("_cassandra_preflight", "address"), ("cassandra_listen_address",), ("ansible_host",),
                     ("ansible_facts", "default_ipv4", "address")):
            address = _host_var(hostvars, host, *path)
            if address and address != "localhost":
                names.setdefault(address, host)
        names.setdefault(host, host)
    return names


_LOAD_UNITS = {"bytes": 1, "B": 1, "KiB": 1024, "KB": 1024, "MiB": 1024 ** 2, "MB": 1024 ** 2,
               "GiB": 1024 ** 3, "GB": 1024 ** 3, "TiB": 1024 ** 4, "TB": 1024 ** 4}


def _load_bytes(load):
    """nodetool status Load ("412.3 GiB") in bytes, None when unknown ("?")."""
    try:
        number, unit = str(load).split()
        return int(float(number) * _LOAD_UNITS[unit])
    except (ValueError, KeyError):
        return None


def _user_keyspaces(keyspaces):
    return dict((name, ks) for name, ks in (keyspaces or {}).items() if not name.startswith("system"))


def _ring_wide(keyspaces):
    """A user keyspace placed around the whole ring, whatever the datacenters
    (SimpleStrategy...): unknown replication counts as one."""
    return keyspaces is None or any(ks.get("class") != "NetworkTopologyStrategy" and ks.get("rf")
                                    for ks in _user_keyspaces(keyspaces).values())


def _rack_aware(keyspaces, dc, racks):
    """True when every user keyspace has NetworkTopologyStrategy, and those with
    replicas in dc have as many there as dc has racks: each rack holds a full copy."""
    if racks < 2 or _ring_wide(keyspaces):
        return False
    rfs = [ks["rf"][dc] for ks in _user_keyspaces(keyspaces).values() if ks["rf"].get(dc)]
    return bool(rfs) and all(rf == racks for rf in rfs)


def cassandra_add_node_plan(cluster_status, new_nodes, hosts=None, keyspaces=None):
    """What adding new_nodes ([{host, address, dc, rack, in_ring, state}];
    in_ring: already in the ring from an earlier run, state: new, joining or
    joined) to
    the ring in cluster_status (cassandra_status) means. hosts: {address:
    inventory name} of the nodes in the ring; keyspaces: cassandra_keyspaces
    (None: unknown). Returns racks ({dc: {rack: nodes after}}), warnings
    (uneven racks), estimate ([{host, bytes, basis}]: data each new node
    should receive) and cleanup ({dc: [hosts]}: the nodes that hand over
    ranges and need a cleanup afterwards, in ring order), scope ({dc: 'rack'
    when only the new nodes' racks hand over data, else 'dc'})."""
    hosts = hosts or {}
    racks, warnings, estimate, cleanup, scope = {}, [], [], {}, {}
    # the nodes that receive data in this run are not cleaned; the ones that joined in
    # an earlier run are, when others join after them
    adding = [n for n in new_nodes if n.get("state", "new" if not n.get("in_ring") else "joined") != "joined"]
    added = set(n.get("address") for n in (adding if adding else new_nodes))
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
        loads = [(n, _load_bytes(n.get("load"))) for n in ring if n["status"] + n["state"] == "UN" and n["address"] not in added]
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
        cleanup[dc] = [hosts.get(n["address"], n["address"]) for n in ring
                       if n["status"] + n["state"] == "UN" and n["address"] not in added
                       and (not aware or n["rack"] in new_racks)]
        # a node added in this run hands ranges over to the ones added after it in its
        # datacenter (its rack when each rack holds a full copy): those get a cleanup too
        mine = [n for n in (adding or new_nodes) if n["dc"] == dc]
        cleanup[dc] += [n["host"] for i, n in enumerate(mine)
                        if any(not aware or m["rack"] == n["rack"] for m in mine[i + 1:])]
    if _ring_wide(keyspaces) and keyspaces is not None:
        # replicas placed around the whole ring: every datacenter hands data over
        for dc in cluster_status:
            if dc not in cleanup:
                scope[dc] = "dc"
                cleanup[dc] = [hosts.get(n["address"], n["address"]) for n in cluster_status[dc].get("nodes", [])
                               if n["status"] + n["state"] == "UN" and n["address"] not in added]
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
    if result.get("failed") or result.get("skipped") or result.get("unreachable") or result.get("rc", 0) != 0:
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
                "cassandra_stream_report": cassandra_stream_report,
                "cassandra_host_addresses": cassandra_host_addresses,
                "cassandra_add_node_plan": cassandra_add_node_plan,
                "cassandra_compactionstats": cassandra_compactionstats,
                "cassandra_cleanup_view": cassandra_cleanup_view}
