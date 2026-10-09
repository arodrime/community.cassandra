# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_stream_progress: the progress of a streaming operation (bootstrap,
decommission, rebuild, removenode) from successive cassandra_netstats results,
for the progress wait of roles/cassandra_service/tasks/stream_wait.yml;
cassandra_stream_report: the lines that print it; cassandra_host_addresses:
the inventory names of the addresses netstats shows; for the cleanups of a
batch (cleanup_check.yml), cassandra_stream_progress_by_host and
cassandra_cleanup_report: a block per node."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import address_part, same_address
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_output import (
    clock as _clock, count as _count, rate as _rate, size as _size)

_GIB = out.GIB
# the rate is measured over the checks of this many seconds (at least the last 3)
_WINDOW = 90
# beyond this, the end is shown as unknown
_ETA_MAX = 30 * 86400
# the longest report line: the default stdout callback prints a msg list
# indented and quoted, 100 columns in all
_WIDTH = 88
# the bar's widest and narrowest width (narrower when the header is long)
_BAR = (20, 10)
# the other ends listed one per line, the rest summed up on one more line
_PEERS = 4
# the word in the header when the wait stops (stream_wait.yml statuses), FAILED for the others
_TROUBLE = {"stalled": "STALLED", "too_long": "TOO LONG", "stopped": "STOPPED", "still_running": "STILL RUNNING",
            "jmx_refused": "JMX LOGIN REFUSED"}
# the report's item lines: "      data:      38.2 GiB / 93.1 GiB"
_ITEM = "      %-11s%s"


def _duration(seconds):
    """"45s", "12m", "1h12m", "2d04h"."""
    return out.duration(seconds, short=True)


def cassandra_stream_progress(views, state=None, now=0, operations=None, peer=None, stall_time=900, quiet_factor=4,
                              interval=30, early_interval=10, early_time=300):
    """views: the results of cassandra_netstats looped over hosts (item: the
    host, then the module's return values); state: what
    the previous call returned (None the first time); now: epoch seconds.
    operations: the session operations to follow (e.g. ['Bootstrap']), peer:
    only the sessions with this peer (a node read from the other side).
    Progress is bytes or files streamed, or a session started or finished,
    since the previous call. interval: the check interval, seconds;
    early_interval: the one of the first early_time seconds (a short
    operation ends in seconds). stall_time: seconds without progress before
    it counts as stalled (quiet_factor times more while nothing is left to
    transfer). Returns the new state, for the next call and for
    cassandra_stream_report: streams (per session: total, done, files_total,
    files_done, other: the node at the other end, way: 'from' when the data
    comes from it, 'to' when it goes to it, 'on' for a local task, gone, moved:
    progress at this check, since: when it last moved),
    progressed (since the previous call), start, now, last_progress,
    idle_checks (calls in a row without progress), idle (seconds since the
    last progress), limit and stalled (idle reached limit: stall_time while
    some session has bytes left, stall_time * quiet_factor otherwise),
    transferring, sessions
    (sessions in netstats now), answered (at least one view answered),
    answered_hosts (the hosts that answered at least once in this wait),
    bytes_done/bytes_total, first_done (bytes done at the first answer),
    samples (time and bytes done of the last checks), rate (bytes per
    second over the last _WINDOW seconds, 3 checks at least; None while
    unknown or when nothing moved),
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
            moved = before is None or done > before["mark"]
            streams[key] = {"total": s["bytes_total"], "done": s["bytes_done"], "mark": done, "gone": False,
                            "moved": moved, "since": now if moved else before.get("since", now),
                            "files_total": s["files_total"], "files_done": s["files_done"], "way": way,
                            "other": str(result.get("item", "")) if peer else s["peer"]}
    if answered:
        for key, stream in streams.items():
            if key not in current and not stream["gone"] and key.split("|", 1)[0] in answered:
                # a finished session leaves netstats: count it as fully streamed (not when its
                # host did not answer this time)
                stream.update(gone=True, done=stream["total"], files_done=stream["files_total"], moved=True, since=now)
                progressed = True
    first = "last_progress" not in state
    last_progress = now if progressed or first else state["last_progress"]
    idle_checks = 0 if progressed or first else state.get("idle_checks", 0) + 1
    idle = now - last_progress
    checks = state.get("checks", 0) + 1
    # Nothing left to transfer in netstats (no session yet, or every one at 100%): phases
    # that show no bytes (ring delay, schema, the write path of tables with views or CDC,
    # index builds, hints of a decommission, a task queued behind other compactions) get
    # quiet_factor times more checks before counting as stalled.
    transferring = any(streams[k]["done"] < streams[k]["total"] for k in current)
    limit = int(stall_time) * (1 if transferring else int(quiet_factor))
    total = sum(s["total"] for s in streams.values())
    done = sum(s["done"] for s in streams.values())
    samples = [list(x) for x in state.get("samples") or []]
    rate = None
    if answered:
        # over the last _WINDOW seconds; a check without answer has no new count
        samples = _recent(samples, now)
        oldest = samples[0] if samples else None
        if oldest and now > oldest[0] and done > oldest[1]:
            rate = (done - oldest[1]) / float(now - oldest[0])
        samples = _recent(samples + [[now, done]], now)
    return {
        "start": start, "now": now, "streams": streams, "progressed": progressed, "last_progress": last_progress,
        "idle_checks": idle_checks, "idle": idle, "limit": limit, "stalled": idle >= limit, "sessions": len(current),
        "transferring": transferring, "answered": bool(answered), "bytes_done": done, "bytes_total": total,
        "answered_hosts": sorted(set(state.get("answered_hosts") or []) | answered),
        "first_done": state["first_done"] if state.get("samples") else done, "samples": samples, "rate": rate,
        "checks": checks,
        "wait": min(int(interval), int(early_interval)) if now - start < int(early_time) else int(interval),
    }


def _recent(samples, now):
    """The [time, bytes] samples of the last _WINDOW seconds, the last 3 at least."""
    return [x for i, x in enumerate(samples) if now - x[0] <= _WINDOW or i >= len(samples) - 3]


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


def _times(rows, now):
    """[label, relative, epoch or None] rows as lines after a blank one, the
    clocks in one column (a row without clock does not widen it)."""
    width = max([len(r[1]) for r in rows if r[2] is not None] or [0])
    return ([""] if rows else []) + [
        (_ITEM % (label, relative.ljust(width) + (" - " + _clock(epoch, now) if epoch is not None else ""))).rstrip()
        for label, relative, epoch in rows]


def _peers(streams):
    """The other ends, the most data first, with their own progress: "node1  52% done
    (9.6 / 18.4 GiB)", the ones beyond _PEERS summed up on one line."""
    others = {}
    for s in streams:
        other = others.setdefault(s["other"], [0, 0])
        other[0] += s["total"]
        other[1] += s["done"]
    ranked = sorted(((name, size, moved) for name, (size, moved) in others.items() if size), key=lambda x: (-x[1], x[0]))
    if len(ranked) > _PEERS:
        rest = ranked[_PEERS - 1:]
        ranked = ranked[:_PEERS - 1] + [("%d more" % len(rest), sum(r[1] for r in rest), sum(r[2] for r in rest))]
    width = max([len(r[0]) for r in ranked] or [0])
    return ["%s  %3d%% done  (%s)" % (name.ljust(width), int(100 * min(moved, size) / size), out.amount(moved, size, " / "))
            for name, size, moved in ranked]


def cassandra_stream_report(state, node="", what="", status="going", names=None, files_label="files", clocks=True,
                            stall=True):
    """The lines to print for a cassandra_stream_progress state (a debug msg
    list prints one per line): a header with node, what, bar, percent and
    rate, then the data, the other ends and the times, an item per line; a
    single line once done. status: as in stream_wait.yml (going, done,
    stalled, too_long, stopped, *_failed...); names: {address: inventory
    name} for the other ends; files_label: what the files are called;
    clocks, stall: False leaves out Now and Started, the line about the
    checks without progress (a batch shows them once)."""
    total, done, now = state.get("bytes_total", 0), state.get("bytes_done", 0), state.get("now", 0)
    start = state.get("start", now)
    rate = state.get("rate")
    if status == "done":
        moved, elapsed = done - state.get("first_done", 0), now - start
        if not total:
            summary = ("in %s, " % _duration(elapsed) if elapsed > 0 else "") + "nothing seen in progress"
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
    if total and set(s["other"] for s in streams) != set([node]):  # not a node's own tasks (cleanup)
        ways = set(s["way"] for s in streams)
        block((ways.pop() if len(ways) == 1 else "with") + ":", _peers(streams))
    times = [("Now:", "current", now), ("Started:", "%s ago" % _duration(now - start), start)] if clocks else []
    if going and total:
        left = (total - done) / rate if rate and total > done else None
        if done >= total:
            times.append(("Finish:", "all sent, finishing", None))
        elif left is not None and left <= _ETA_MAX:
            times.append(("Finish:", "in %s" % _duration(left), now + left))
        else:
            times.append(("Finish:", "unknown, " + ("too slow to tell" if left else "no rate yet"), None))
    lines.extend(_times(times, now))
    if stall:
        lines.extend(_stall(state, going))
    return lines


def _stall(state, going):
    """The line about the time without progress, after a blank one; nothing
    while the last check saw progress."""
    if not state.get("idle_checks", 0):
        return []
    idle = state.get("now", 0) - state.get("last_progress", state.get("now", 0))
    return ["", _ITEM % ("Progress:", "none for %s%s" % (
        _duration(idle), (", stops at %s" % _duration(state.get("limit", 0))) if going else ""))]


def _host_var(hostvars, host, *path):
    """hostvars[host][path[0]][path[1]]... when it is a string, else ""."""
    try:
        value = hostvars[host]
        for key in path:
            value = value.get(key) or {}
    except Exception:  # pylint: disable=broad-except  # an undefined or broken template in that host's variables
        return ""
    return value if isinstance(value, str) else ""


def cassandra_stream_progress_by_host(views, states=None, **kwargs):
    """cassandra_stream_progress for each host of views on its own: {host:
    state}; states: what the previous call returned."""
    states = states or {}
    return dict((str(v.get("item", "")), cassandra_stream_progress([v], states.get(str(v.get("item", ""))), **kwargs))
                for v in views)


def cassandra_cleanup_report(states, jobs, batch, status="going"):
    """The lines to print for the cleanups of a batch: states from
    cassandra_stream_progress_by_host, jobs: async_status results looped over
    the started jobs (item.item: the host), batch: the batch's
    cassandra_stream_progress state and status (cleanup_check.yml: the batch
    stops as a whole). One block per node, its own done line once its job
    has ended; with several nodes, a line that names them first; the clocks
    and the checks without progress once, for the batch."""
    hosts = [str(j["item"]["item"]) for j in jobs]
    lines = []
    if len(hosts) > 1:
        lines.append("%d nodes in parallel: %s" % (len(hosts), _fit(hosts, _WIDTH - 24)))
    blocks = []
    for host, job in zip(hosts, jobs):
        if job.get("finished") == 1 and not job.get("failed"):
            own = "done"
        elif job.get("failed") or ("finished" not in job and not job.get("unreachable")):
            own = "failed"  # failed, or a job that can't be followed
        elif status == "going":
            own = "going"
        else:
            own = "stalled" if status == "stalled" else "still_running"  # another node failed: the run stops
        blocks.append("")
        blocks.extend(cassandra_stream_report(states.get(host) or {}, node=host, what="cleanup", status=own,
                                              files_label="tasks", clocks=len(hosts) == 1, stall=False))
    if any(line.strip().startswith("Finish:") and not line.strip().endswith("finishing") for line in blocks):
        lines.append("Finish counts the cleanup tasks running now, not the ones queued after them.")
    lines.extend(blocks)
    now, start = batch.get("now", 0), batch.get("start", 0)
    if len(hosts) > 1:
        lines.extend(_times([("Now:", "current", now), ("Started:", "%s ago" % _duration(now - start), start)], now))
    lines.extend(_stall(batch, status == "going"))
    return lines


def _fit(names, room):
    """names joined with ", ", as many as fit in room characters, then ", ... (+N more)"."""
    for count in range(len(names), 0, -1):
        text = ", ".join(names[:count]) + ((", ... (+%d more)" % (len(names) - count)) if count < len(names) else "")
        if len(text) <= room or count == 1:
            return text
    return ""


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


def _load_bytes(load):
    """nodetool status Load ("412.3 GiB") in bytes, None when unknown ("?")."""
    size = out.parse_size(load)
    return None if size is None else int(size)


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


# nodetool's stderr lines that say nothing about the error (a JVM banner)
_BANNER = re.compile(r"^(Picked up \S+|OpenJDK .*warning|WARNING: )")


def cassandra_stream_own_error(own):
    """Why this node's own nodetool netstats gave no mode at a check
    (stream_check.yml), '' when it answered: the first line of its error,
    without JVM banners (Picked up JAVA_TOOL_OPTIONS...). own: the
    cassandra_netstats result (unreachable, failed or not)."""
    own = own or {}
    if own.get("mode"):
        return ""
    for key in ("stderr", "msg"):
        for line in str(own.get(key) or "").splitlines():
            line = line.strip()
            if line and not _BANNER.match(line):
                return line[:160]
    return "no answer" if own else ""


def _own_stopped(own):
    """Its own nodetool says Cassandra does not run there (JMX refuses the
    connection); not an unreachable host (ssh's own "Connection refused")."""
    own = own or {}
    return not own.get("unreachable") and "Connection refused" in "%s %s" % (own.get("stderr") or "", own.get("msg") or "")


def cassandra_ring_seen(results, address):
    """How the other nodes see a node in nodetool status: 'UN', 'UJ', 'DN'...
    from the first cassandra_status result that lists its address, '' when
    none does. results: the registered loop results of cassandra_status."""
    if not address:
        return ""
    for result in results or []:
        status = (result or {}).get("cluster_status") or {}
        for dc in status.values() if isinstance(status, dict) else []:
            for node in (dc.get("nodes") or []) if isinstance(dc, dict) else []:
                if same_address(address_part(str(node.get("address") or "")), address_part(str(address))):
                    return "%s%s" % (node.get("status", ""), node.get("state", ""))
    return ""


def cassandra_stream_status(state, join=False, leave=False, own=None, seen="", job=None, removal=None, max_time=0,
                            now=None, refused=None):
    """Where a streaming operation stands after a check (stream_check.yml):
    'done', 'going', or why the wait stops: job_failed, job_lost,
    join_failed, leave_failed, stopped, stalled, too_long.
    state: cassandra_stream_progress's; join: done when this node is
    NORMAL (bootstrap, replace), or, when its own nodetool can't answer
    (5.0.0 to 5.0.4 during a bootstrap, JMX login or permissions), when the
    other nodes see it UN (seen, cassandra_ring_seen); leave: done when it
    is DECOMMISSIONED (decommission); own: this node's cassandra_netstats
    result (None: not read); job: the async_status result of the job
    running it (None: no job; with leave, a job that changed nothing, the
    node LEAVING already, is not the end); removal: the nodetool removenode
    status result (None: not followed): done once no removal is left;
    max_time: cassandra_stream_max_time (0: none); refused: the JMX logins
    refused at this check (cassandra_jmx_refused): jmx_refused at once
    rather than a stall much later."""
    state = state or {}
    own = own or {}
    mode = own.get("mode") or ""
    error = cassandra_stream_own_error(own) if own else ""
    stopped = _own_stopped(own)
    job = job if job else None
    finished = job is not None and int(job.get("finished") or 0) == 1
    if ((join and (mode == "NORMAL" or (error and not stopped and seen == "UN")))
            or (leave and mode == "DECOMMISSIONED")
            or (finished and not job.get("failed") and (job.get("changed") or not leave))
            or (removal is not None and int(removal.get("rc", 1)) == 0
                and "Removing token" not in (removal.get("stdout") or ""))):
        return "done"
    if finished and job.get("failed"):
        return "job_failed"
    if job is not None and job.get("failed"):
        return "job_lost"
    if join and mode == "JOINING_FAILED":
        return "join_failed"
    if leave and mode == "DECOMMISSION_FAILED":
        return "leave_failed"
    if (join or leave) and stopped:
        return "stopped"
    if refused:
        return "jmx_refused"
    if state.get("stalled"):
        return "stalled"
    now = state.get("now", 0) if now is None else now
    if int(max_time or 0) > 0 and now - int(state.get("start", now)) >= int(max_time):
        return "too_long"
    return "going"


# A JMX login or permission refused, in nodetool's error. The JDK's file-based
# authenticator and access file say so whatever the node does; Cassandra's own
# (CassandraLoginModule, AuthorizationProxy: "Authentication error", "Access
# Denied") also refuse every login on a joining node until its auth setup is
# complete, so they count there only from the nodes already in the ring.
_JMX_REFUSED_ANYWHERE = re.compile(r"Authentication failed!|Invalid username or password|Credentials required"
                                   r"|neither username nor password can be blank|Invalid access level")
_JMX_REFUSED = re.compile(r"SecurityException|Authentication (failed|error)|Access (is )?denied", re.IGNORECASE)


def _result_host(result):
    item = result.get("item", "")
    return str(item.get("item", "") if isinstance(item, dict) else item)


def cassandra_jmx_refused(results, joining="", answered=None, hosts=None):
    """The JMX logins refused at a check (stream_check.yml, cleanup_check.yml):
    [{host, error}] from looped results of cassandra_netstats,
    cassandra_status or a nodetool command (item: the host, or {item: host}).
    joining: the node being added, whose own refusals count only when the
    file-based authenticator gives them (see above). answered: the hosts
    that answered earlier in this wait (a login that worked: a refusal now
    is a passing one, e.g. Cassandra's own authenticator timing out on
    system_auth), left out; hosts: the ones read with their own login (the
    others' refusals tell nothing about theirs), None: all."""
    refused = []
    for result in results or []:
        if not isinstance(result, dict) or result.get("skipped") or result.get("unreachable"):
            continue
        host = _result_host(result)
        if host in (answered or []) or (hosts is not None and host not in hosts):
            continue
        # failed_when: false leaves failed false: the error's words tell (stdout only from a command that failed)
        text = "\n".join(str(result.get(k) or "") for k in ("msg", "stderr", "stdout")
                         if k != "stdout" or result.get("rc", 0))
        lines = [x.strip() for x in text.splitlines() if _JMX_REFUSED_ANYWHERE.search(x)
                 or (host != joining and _JMX_REFUSED.search(x))]
        if lines and host not in [r["host"] for r in refused]:
            refused.append({"host": host, "error": lines[0][:200]})
    return refused


def cassandra_stream_waiting(own, seen="", node="", join=False):
    """The line that says why a join is not over yet when its node's own
    nodetool can't answer (stream_check.yml), [] otherwise."""
    error = cassandra_stream_own_error(own)
    if not join or not error or _own_stopped(own):
        return []
    return ["      %s: its own nodetool does not answer (%s): done once the other nodes see it UN (%s)"
            % (node or "this node", error, ("they see it " + seen) if seen else "not in their nodetool status yet")]


class FilterModule(object):
    def filters(self):
        return {"cassandra_stream_progress": cassandra_stream_progress,
                "cassandra_stream_report": cassandra_stream_report,
                "cassandra_host_addresses": cassandra_host_addresses,
                "cassandra_stream_progress_by_host": cassandra_stream_progress_by_host,
                "cassandra_cleanup_report": cassandra_cleanup_report,
                "cassandra_add_node_plan": cassandra_add_node_plan,
                "cassandra_compactionstats": cassandra_compactionstats,
                "cassandra_cleanup_view": cassandra_cleanup_view,
                "cassandra_stream_own_error": cassandra_stream_own_error,
                "cassandra_ring_seen": cassandra_ring_seen,
                "cassandra_stream_status": cassandra_stream_status,
                "cassandra_stream_waiting": cassandra_stream_waiting,
                "cassandra_jmx_refused": cassandra_jmx_refused,
                "cassandra_stream_own_stopped": _own_stopped}
