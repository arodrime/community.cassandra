# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Parsing of nodetool netstats (Cassandra 4.0, 4.1, 5.0): the mode and the
stream sessions with their progress. Sizes are plain byte counts (5.0) or
"N bytes" (4.x); human-readable sizes (-H) are read too."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

_UNITS = {"": 1, "bytes": 1, "B": 1, "KB": 1024, "KiB": 1024, "MB": 1024 ** 2, "MiB": 1024 ** 2,
          "GB": 1024 ** 3, "GiB": 1024 ** 3, "TB": 1024 ** 4, "TiB": 1024 ** 4}
_SIZE = r"([0-9.]+)(?:\s*(bytes|B|KB|KiB|MB|MiB|GB|GiB|TB|TiB))?"
# "Bootstrap 1b2c...": the operation (Bootstrap, Unbootstrap, Rebuild, Restore replica count, Repair...) and the plan ID
_PLAN = re.compile(r"^(\S.*?) ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$")
_PEER = re.compile(r"^\s{4}/?(\S+?)(?::\d+)?(?: \(using .*\))?\s*$")
_TOTALS = re.compile(r"^\s+(Receiving|Sending) (\d+) files, " + _SIZE + r" total\. Already (?:received|sent) (\d+) files"
                     r" \([0-9.,]+%\), " + _SIZE + r" total")
_FILE = re.compile(r"^\s+(\S+) (\d+)/(\d+) bytes \(\d+%\) (received from|sent to) ")
# sending side: .../data/<keyspace>/<table>-<table id>/<sstable file>;
# receiving side: <keyspace>/<table>-<sequence number>
_TABLE = re.compile(r"/([^/]+)/([^/]+?)-[0-9a-f]{32}/[^/]*$")
_TABLE_RECEIVED = re.compile(r"^([^/]+)/([^/]+)-\d+$")


def _bytes(number, unit):
    return int(float(number) * _UNITS.get(unit or "", 1))


def table_of(path):
    """keyspace.table of a streamed file path, '' when not recognized."""
    m = _TABLE.search(path) or _TABLE_RECEIVED.match(path)
    return "%s.%s" % (m.group(1), m.group(2)) if m else ""


def node_mode(stdout):
    """The mode of the "Mode: X" line of nodetool netstats, None if absent."""
    for line in stdout.splitlines():
        if line.startswith("Mode:"):
            return line.split(":", 1)[1].strip()
    return None


def parse_netstats(stdout):
    """(mode, stream lines, sessions): the Mode line, the raw lines of the
    stream sessions, and one dict per session and direction: operation,
    plan_id, peer, direction (receiving/sending), files_total, files_done,
    bytes_total, bytes_done, files (in the output: path, table, done, total)."""
    mode, lines, sessions = "", [], []
    plan = peer = None
    for line in stdout.splitlines():
        if line.startswith("Mode:"):
            mode = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Read Repair Statistics") or line.startswith("Pool Name"):
            break
        if not mode or not line.strip() or line.strip() == "Not sending any streams.":
            continue
        lines.append(line)
        m = _PLAN.match(line)
        if m:
            plan, peer = (m.group(1), m.group(2)), None
            continue
        m = _TOTALS.match(line)
        if m and plan and peer:
            sessions.append({
                "operation": plan[0], "plan_id": plan[1], "peer": peer,
                "direction": "receiving" if m.group(1) == "Receiving" else "sending",
                "files_total": int(m.group(2)), "bytes_total": _bytes(m.group(3), m.group(4)),
                "files_done": int(m.group(5)), "bytes_done": _bytes(m.group(6), m.group(7)),
                "files": [],
            })
            continue
        m = _FILE.match(line)
        if m and sessions:
            sessions[-1]["files"].append({"path": m.group(1), "table": table_of(m.group(1)),
                                          "done": int(m.group(2)), "total": int(m.group(3))})
            continue
        m = _PEER.match(line)
        if m and plan and not line.startswith("     "):
            peer = m.group(1)
    return mode, lines, sessions
