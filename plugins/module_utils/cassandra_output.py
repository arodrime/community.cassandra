# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output of the playbooks, written once: every function
returns text lines (or a string) that a debug task prints, nothing else.
Pure Python, no Ansible: the filters of plugins/filter/cassandra_output.py
and the other filters call it. Conventions in
docs/docsite/rst/guide_output.rst.

- sizes, rates, durations, clocks: size, amount, parse_size, rate, duration, clock,
  count, plural;
- node lists: nodes (node1..node5, node7), full_list;
- secrets: secret, mask, shown;
- a setting and its value per node: setting_lines, by_nodes;
- before a change: plan; during it: progress_line; at the end: recap,
  perm_lines, diff_lines, changed_lines;
- what is left to do: command, extra_var, todo, inventory_steps,
  in_git_work_tree;
- the seed rule of preflight and help: seed_layout.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import os
import re
import shlex
import time

ARROW = u"←"  # the marker of a line that needs a look: "<- down", "<- differs"
GIB = 1024.0 ** 3
# each unit from this many bytes of scale on
_UNITS = (("TiB", 1024.0 ** 4, 1024.0 ** 4), ("GiB", GIB, GIB / 10), ("MiB", 1024.0 ** 2, 1024.0 ** 2 / 10),
          ("KiB", 1024.0, 1024.0))

# --- sizes, rates, durations ---------------------------------------------------------------


def size(count, scale=None):
    """count bytes, one decimal: "41.2 GiB", "260.6 MiB", "0 B". scale: the
    bytes whose unit to use (the total next to it: from 0.1 GiB on, GiB); a
    non-zero count never reads 0.0, it gets its own unit then ("52.0 KiB"
    next to "100.0 GiB")."""
    count = float(count or 0)
    alone = scale is None
    scale = count if alone else float(scale)
    for unit, factor, least in _UNITS:
        if scale >= (factor if alone else least) and (count == 0 or count / factor >= 0.1):
            return "%.1f %s" % (count / factor, unit)
    for unit, factor, least in _UNITS:
        if count >= least:
            return "%.1f %s" % (count / factor, unit)
    return "%d B" % count


def amount(done, total, sep="/"):
    """done out of total bytes, the unit once: "52.2/100.0 GiB"; done in its
    own unit when it would read 0.0 in total's: "52.0 KiB/100.0 GiB"."""
    whole, part = size(total), size(done, total)
    if part.split()[1] == whole.split()[1]:
        return "%s%s%s" % (part.split()[0], sep, whole)
    return "%s%s%s" % (part, sep, whole)


_SIZE = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(bytes|B|KiB|KB|MiB|MB|GiB|GB|TiB|TB)\s*$")
_FACTORS = {"bytes": 1, "B": 1, "KiB": 1024, "KB": 1024, "MiB": 1024 ** 2, "MB": 1024 ** 2,
            "GiB": 1024 ** 3, "GB": 1024 ** 3, "TiB": 1024 ** 4, "TB": 1024 ** 4}


def parse_size(text):
    """A size as nodetool prints it ("412.3 GiB", "1,5 GiB" in some locales,
    "100 bytes") in bytes (float), None when unknown ("?", "")."""
    match = _SIZE.match(str(text or ""))
    if not match:
        return None
    return float(match.group(1).replace(",", ".")) * _FACTORS[match.group(2)]


def rate(per_second):
    """Bytes per second: "89 MiB/s", "3.2 MiB/s", "1.1 GiB/s"."""
    per_second = float(per_second or 0)
    if per_second >= GIB:
        return "%.1f GiB/s" % (per_second / GIB)
    if per_second >= 1024.0 ** 2:
        mib = per_second / 1024.0 ** 2
        return ("%.1f MiB/s" if mib < 10 else "%d MiB/s") % mib
    if per_second >= 1024:
        return "%d KiB/s" % (per_second / 1024.0)
    return "%d B/s" % per_second


def duration(seconds, short=False):
    """"45s", "9m20s", "1h12m", "2d04h"; short: minutes at most under an
    hour ("9m"), for an ETA."""
    seconds = int(max(0, seconds or 0))
    if seconds >= 86400:
        return "%dd%02dh" % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    if seconds >= 60:
        return ("%dm" % (seconds // 60)) if short else "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%ds" % seconds


def clock(epoch, now=None):
    """The controller's local time of epoch with its zone, the date too when it
    is not today: "19:03 CEST", "2026-10-07 04:26 CEST"."""
    now = time.time() if now is None else now
    when = time.localtime(epoch)
    zone = time.strftime("%Z", when)
    if not zone or zone[0] in "+-":  # no abbreviation for this zone: its offset
        offset = time.strftime("%z", when)
        zone = offset[:3] + ":" + offset[3:]
    return time.strftime("%H:%M" if when[:3] == time.localtime(now)[:3] else "%Y-%m-%d %H:%M", when) + " " + zone


def count(number):
    """1240 as "1 240"."""
    return "{0:,}".format(int(number)).replace(",", " ")


def plural(number, word, many=None):
    """"1 node", "3 nodes"; many: the plural when it is not word + s."""
    return "%d %s" % (number, word if number == 1 else (many or word + "s"))


# --- node lists ------------------------------------------------------------------------------

def _natural(name):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(name))]


def _follows(before, name):
    """The index of the one number that goes up by 1 from before to name, the
    rest the same (node9 -> node10, node09 -> node10, rack1-n3 -> rack1-n4),
    else None."""
    a, b = re.split(r"(\d+)", before), re.split(r"(\d+)", name)
    if len(a) != len(b):
        return None
    changed = [i for i in range(len(a)) if a[i] != b[i]]
    if len(changed) != 1 or changed[0] % 2 == 0:
        return None
    x, y = a[changed[0]], b[changed[0]]
    padded = x.startswith("0") or y.startswith("0")
    if int(y) != int(x) + 1 or (padded and len(x) != len(y)):
        return None
    return changed[0]


def full_list(names, keep_order=False):
    """Every name, ", " between them (an import report, -v)."""
    names = [str(n) for n in names or []]
    return ", ".join(names if keep_order else sorted(names, key=_natural))


def nodes(names, keep_order=False, full=False):
    """Node names, short: 3 or more consecutive numbered names as a range
    (node1..node5), 2 listed (node1, node2), a gap breaks the range (never a
    node that is not there), "6 nodes: " first when there are more than 5.
    Sorted naturally unless keep_order (a run's order: ranges only where the
    order follows the numbers). full: every name (see full_list)."""
    names = [str(n) for n in names or []]
    if full:
        return full_list(names, keep_order)
    names = names if keep_order else sorted(set(names), key=_natural)
    parts, run, place = [], [], None

    def flush():
        parts.extend(["%s..%s" % (run[0], run[-1])] if len(run) >= 3 else run)
        del run[:]

    for name in names:
        step = _follows(run[-1], name) if run else None
        if step is not None and (len(run) == 1 or step == place):
            run.append(name)
            place = step
            continue
        flush()
        run.append(name)
        place = None
    flush()
    text = ", ".join(parts)
    return ("%d nodes: %s" % (len(names), text)) if len(names) > 5 else text


# --- secrets -------------------------------------------------------------------------------

# A variable or setting whose value is a secret (sse_c_key, access_key: Medusa's)
SECRET = re.compile(r"password|passwd|secret|sse_c_key|access_key|private_key|key_material", re.I)
# A secret inside a line of text (a config file line, a JVM option): its value
SECRET_VALUE = re.compile(r"(?i)([\w.-]*(?:password|passwd|secret|private_key)[\w.-]*\s*[:=]\s*)"
                          r"(\"(?:[^\"\\]|\\.)*\"?|'(?:[^']|'')*'?|\S.*?(?=\s+#|$))", re.M)
MASK = "****"
# What the output hides besides (secret and SECRET_VALUE are also what import_cluster files as secrets: kept as
# they are): more names, a quote between the name and the colon (JSON, Python), the command line forms
_HIDDEN = re.compile(r"password|passwd|secret|sse_c_key|access_key|private_key|key_material|auth_token"
                     r"|_pw$|_pass$|^pw$|^pass$|ca_key", re.I)
_HIDDEN_KEY = (r"(?i)([\w.-]*(?:password|passwd|secret|private_key|sse_c_key|access_key|key_material|auth_token|_pw|_pass)"
               r"[\w.-]*[\"']?\s*[:=]\s*)(\"(?:[^\"\\]|\\.)*\"?|'(?:[^']|'')*'?|")
# an unquoted value runs to the end of the line (a comment aside), as a YAML plain scalar or a JVM option may hold
# a comma; inside a flow mapping or JSON ({...} before it) it ends at the next comma or brace
_HIDDEN_VALUE = re.compile(_HIDDEN_KEY + r"\S.*?(?=\s+#|$))", re.M)
_HIDDEN_FLOW_VALUE = re.compile(_HIDDEN_KEY + r"\S.*?(?=\s+#|,\s|[,}]|$))", re.M)
# nodetool -pw, --password; -p only after cqlsh (elsewhere a port or mkdir -p)
_HIDDEN_OPTION = re.compile(r"((?:^|\s)(?:-pw|--password)\s+|\bcqlsh\b[^\n]*?\s-p\s+)(\S+)", re.M)
_BLOCK = re.compile(r"^\s*[|>][-+]?\d*\s*$")


def _in_flow(before):
    """True when text before a key leaves a flow mapping or JSON object open
    (a "{" not closed, Jinja's "{{" aside)."""
    before = before.replace("{{", "").replace("}}", "")
    return before.count("{") > before.count("}")


def _mask_flow(match):
    """Inside an open {...}: the value up to the next comma or brace."""
    return match.group(1) + MASK if _in_flow(match.string[:match.start()]) else match.group(0)


def _mask_line(match):
    """Elsewhere: the value up to the end of the line (a comment aside)."""
    return match.group(0) if _in_flow(match.string[:match.start()]) else match.group(1) + MASK


def mask(text):
    """text with the value of every secret setting in it as ****: key: value,
    key=value, "key": "value", -pw value, --password value, and the lines of a
    YAML block value (key: |)."""
    out = []
    block = None  # the indent of a secret's YAML block value
    for line in str(text).split("\n"):
        indent = len(line) - len(line.lstrip())
        if block is not None and (not line.strip() or indent > block):
            out.append(line[:indent] + MASK if line.strip() else line)
            continue
        block = None
        match = _HIDDEN_VALUE.search(line)
        if match and _BLOCK.match(match.group(2)):
            block = indent
        line = _HIDDEN_VALUE.sub(_mask_line, _HIDDEN_FLOW_VALUE.sub(_mask_flow, line))
        out.append(_HIDDEN_OPTION.sub(r"\1" + MASK, line))
    return "\n".join(out)


def hidden(key, value):
    """True when the output shows value of key as ****: a secret, or a name or
    a value the output hides too."""
    if isinstance(value, dict):
        return any(hidden(str(k), v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return bool(_HIDDEN.search(key)) or any(hidden(key, v) for v in value)
    if value == "" or value is None:
        return False
    if isinstance(value, str) and mask(value) != value:
        return True
    return secret(key, value) or (bool(_HIDDEN.search(key)) and not key.endswith("_file"))


def secret(key, value):
    """True when the value of key is (or holds) a secret."""
    if isinstance(value, dict):
        return any(secret(k, v) for k, v in value.items())
    if isinstance(value, list):
        return bool(SECRET.search(key)) or any(secret(key, v) for v in value if isinstance(v, (dict, str)))
    if value == "":
        return False  # e.g. a password variable set to "" to leave it out
    if isinstance(value, str) and SECRET_VALUE.search(value):
        return True  # e.g. a unit's JVM_EXTRA_OPTS=-Djavax.net.ssl.keyStorePassword=...
    return bool(SECRET.search(key)) and not key.endswith("_file")  # a path, e.g. cassandra_jmx_password_file


def shown(key, value):
    """value as the output shows it: **** for a secret, a string as is, the
    rest as compact JSON."""
    if hidden(str(key), value):
        return MASK
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, default=str)


# --- a setting and its value per node ------------------------------------------------------


def _same(value):
    return json.dumps(value, sort_keys=True, default=str)


def _groups(values, order):
    """{node: value} -> [(shown value, raw value, [nodes])], the most nodes first."""
    groups = {}
    for node in order:
        if node not in values:
            continue
        raw = values[node]
        groups.setdefault(_same(raw), [raw, []])[1].append(node)
    ranked = sorted(groups.values(), key=lambda g: (-len(g[1]), order.index(g[1][0])))
    return [tuple(group) for group in ranked]


def setting_lines(setting, values, all_nodes=None, expected=None, notes=None, where=None, full=False, indent=""):
    """One setting across nodes, setting-centric: "setting:  value  nodes".
    values: {node: value}; all_nodes: the nodes of the run (default: the
    ones of values), "all" when they all have the same value; one more line
    per other value, the minority ones marked "<- differs" (the ones not
    the expected value, when it is given). expected: the value the
    collection sets, annotated "(collection value)"; notes: {shown value:
    annotation}; where: where the value is written, a string for all or
    {shown value: place}. Secrets as ****. Returns the lines."""
    order = list(all_nodes or sorted(values, key=_natural))
    order += [n for n in sorted(values, key=_natural) if n not in order]
    groups = _groups(values, order)
    if not groups:
        return []
    notes = dict(notes or {})
    rows = []
    for index, (raw, names) in enumerate(groups):
        text = shown(setting, raw)
        who = "all" if len(groups) == 1 and len(names) == len(order) else nodes(names, full=full)
        extra = []
        if expected is not None and _same(raw) == _same(expected):
            extra.append("(collection value)")
        if text in notes:
            extra.append("(%s)" % notes[text])
        place = where.get(text) if isinstance(where, dict) else where
        if place:
            extra.append("in %s" % place)
        if expected is not None:
            differs = _same(raw) != _same(expected)
        else:  # the value most nodes have is the reference (on a tie, the first node's)
            differs = index > 0
        if differs and len(groups) > 1:
            extra.append(ARROW + " differs")
        rows.append([text, who, "  ".join(extra)])
    width, nodes_width = max(len(r[0]) for r in rows), max(len(r[1]) for r in rows)
    head = "%s%s:  " % (indent, setting)
    out = []
    for index, (text, who, extra) in enumerate(rows):
        lead = head if index == 0 else " " * len(head)
        out.append(("%s%s   %s   %s" % (lead, text.ljust(width), who.ljust(nodes_width), extra)).rstrip())
    return out


def by_nodes(pairs, full=False, every="all"):
    """pairs: [(node, [lines])] -> each line once, under the nodes that have
    it: "node1..node4" then its lines indented, the group every node shares
    first (named every)."""
    pairs = list(pairs or [])
    order, where = [], {}
    for name, lines in pairs:
        for line in lines or []:
            if line not in where:
                where[line] = []
                order.append(line)
            if name not in where[line]:
                where[line].append(name)
    groups = []
    for line in order:
        names = tuple(where[line])
        group = next((g for g in groups if g[0] == names), None)
        if group is None:
            groups.append((names, [line]))
        else:
            group[1].append(line)
    out = []
    for names, lines in sorted(groups, key=lambda g: len(g[0]) != len(pairs)):
        out.append(every if len(names) == len(pairs) and len(pairs) > 1 else nodes(names, full=full))
        out += ["  " + line for line in lines]
    return out


# --- plan ----------------------------------------------------------------------------------


def _where(step):
    if step.get("dc"):
        return "%s/%s" % (step["dc"], step.get("rack") or "?")
    return ""


def _table(rows, indent=""):
    """Rows of cells, each column as wide as its widest cell, 2 spaces between."""
    rows = [[str(c) for c in row] for row in rows]
    if not rows:
        return []
    widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(max(len(r) for r in rows))]
    return [(indent + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r) if c or i < len(r) - 1)).rstrip()
            for r in rows]


def plan(operation, cluster="", summary="", steps=None, facts=None, warnings=None, question="", version="",
         check=False, verdict="PLAN"):
    """The screen before a change, the same order everywhere:
    "PLAN  operation  cluster (Cassandra x)  summary", the numbered steps
    (each {node, dc, rack, text} or a string), a blank line, the facts
    ([label, text] or a string), the WARNING lines, then the question (none
    under check: a line says nothing will be changed). verdict: REFUSED,
    READY... for the same layout."""
    head = [verdict, operation, ("%s (Cassandra %s)" % (cluster, version)) if version and cluster else cluster,
            summary]
    lines = ["  ".join(str(x) for x in head if x)]
    rows, numbered = [], 0
    for step in steps or []:
        numbered += 1
        if isinstance(step, dict):
            rows.append(["%d." % numbered, step.get("node", ""), _where(step), step.get("text", "")])
        else:
            rows.append(["%d." % numbered, str(step)])
    lines += _table(rows, "  ")
    block = []
    labelled = [f for f in facts or [] if isinstance(f, (list, tuple))]
    width = max([len(str(f[0])) + 1 for f in labelled] or [0])
    for fact in facts or []:
        if isinstance(fact, (list, tuple)):
            block.append(("%s  %s" % ((str(fact[0]) + ":").ljust(width), fact[1])).rstrip())
        elif str(fact).strip():
            block.append(str(fact))
    tail = ["WARNING  %s" % w for w in warnings or [] if str(w).strip()]
    if check:
        tail.append("--check: nothing will be changed")
    elif question:
        tail.append(question)
    for section in (block, tail):
        if section:
            lines += [""] + section
    return lines


# --- progress ------------------------------------------------------------------------------

_BAR = 10


def _hhmm(epoch, now):
    """The controller's local time of epoch, "13:33", the date too when it is not today."""
    when = time.localtime(epoch)
    return time.strftime("%H:%M" if when[:3] == time.localtime(now)[:3] else "%Y-%m-%d %H:%M", when)


def _bar(done, total):
    filled = int(_BAR * min(done, total) / total) if total else 0
    return "[%s%s]" % ("#" * filled, "-" * (_BAR - filled))


def progress_line(index, total_steps, node, operation, mode="", done=0, total=0, speed=None, now=0, start=None,
                  idle_checks=0, limit=0, peers=None, status="going"):
    """One check of a long operation (Q2), as 2 lines:
    "[1/2] node5 bootstrap  JOINING  [#####-----]  52%  52.2/100.0 GiB  89 MiB/s  ETA 13:33 (9m)  10m"
    then always the other ends: "      from node1 18.0/34.0 GiB ok   from node3 4.2/17.0 GiB stalled".
    index/total_steps: the position in the run (no [i/n] when total_steps
    is 0); mode: netstats Mode; done/total: bytes; speed: bytes/s (None:
    not known yet); now/start: epoch seconds; idle_checks/limit: the checks
    in a row without progress and the stall limit (STALLED shown from the
    first idle check); peers: [{name, way ('from'/'to'), done, total,
    stalled}]; status: going, or stalled / failed / done (said in the line).
    Before any data flows: "waiting for streams" and the elapsed time."""
    start = now if start is None else start
    head = "  ".join(x for x in [("[%d/%d] %s %s" % (index, total_steps, node, operation)) if total_steps
                                 else "%s %s" % (node, operation), mode] if x)
    elapsed = duration(now - start, short=True)
    pct = "%d%%" % int(100 * min(done, total) / total) if total else ""
    done_total = amount(done, total) if total else ""
    if status == "done":
        line = "  ".join(x for x in [head, "done", done_total, elapsed] if x)
    elif status not in ("going",) or (idle_checks and total):  # before any stream: still waiting for them
        word = {"stalled": "STALLED", "failed": "FAILED", "too_long": "TOO LONG", "stopped": "STOPPED"}.get(
            status, "STALLED" if idle_checks else status.upper())
        count_text = "%s %d/%d checks" % (word, idle_checks, limit) if idle_checks and limit else word
        line = "  ".join(x for x in [head, count_text, pct, done_total, elapsed] if x)
    elif not total:
        checks = ["%d/%d checks" % (idle_checks, limit)] if idle_checks and limit else []
        line = "  ".join([head, "waiting for streams"] + checks + [elapsed])
    else:
        eta = ""
        if speed and total > done:
            left = (total - done) / float(speed)
            eta = "ETA %s (%s)" % (_hhmm(now + left, now), duration(left, short=True))
        elif total <= done:
            eta = "all sent, finishing"
        line = "  ".join(x for x in [head, _bar(done, total), pct, done_total, rate(speed) if speed else "", eta, elapsed]
                         if x)
    out = [line]
    if peers:
        parts = []
        for peer in sorted(peers, key=lambda p: (-p.get("total", 0), str(p.get("name")))):
            state = "done" if peer.get("total") and peer.get("done", 0) >= peer["total"] else (
                "stalled" if peer.get("stalled") else "ok")
            parts.append("%s %s %s %s" % (peer.get("way") or "with", peer.get("name"),
                                          amount(peer.get("done", 0), peer.get("total", 0)), state))
        out.append("      " + "   ".join(parts))
    return out


# --- end of run ----------------------------------------------------------------------------

_WOULD = {"restarted": "restart", "applied": "apply", "started": "start", "stopped": "stop", "removed": "remove",
          "added": "add", "changed": "change", "upgraded": "upgrade", "reset": "reset", "moved": "move",
          "cleaned up": "clean up", "decommissioned": "decommission", "replaced": "replace", "rebooted": "reboot",
          "updated": "update", "written": "write", "drained": "drain", "disabled": "disable", "enabled": "enable",
          "installed": "install", "imported": "import", "repaired": "repair", "created": "create"}


def would(outcome):
    """A done outcome as check mode says it: "applied, restarted" ->
    "would apply, restart"; a word it does not know: "would be <word>"."""
    words = [w.strip() for w in str(outcome).split(",") if w.strip()]
    if not words:
        return "would change nothing"
    known = [_WOULD.get(w) for w in words]
    if all(known):
        return "would " + ", ".join(known)
    return "would be " + ", ".join(words)


def _median(numbers):
    numbers = sorted(numbers)
    middle = len(numbers) // 2
    return numbers[middle] if len(numbers) % 2 else (numbers[middle - 1] + numbers[middle]) / 2.0


def recap(operation, cluster, outcomes, check=False, seconds=None, extra=None, full=False, slow_factor=1.5):
    """The end of a run (Q4). outcomes: [{node, outcome ('restarted',
    'applied, restarted'...), status ('ok', 'failed', 'skipped'; default
    ok), seconds (how long its part took), reason (failed or skipped),
    dc, rack}] in the order of the run. Line 1, the verdict: "DONE  op
    cluster  6 restarted (11m30s)", "FAILED  op  cluster  ..." when a node
    failed, "CHECK  op  cluster  would restart 6 nodes  nothing was changed"
    under check. Then the nodes grouped by identical outcome ("node1..node6
    restarted  (1m49s..1m58s each)"), and a line of its own for a node more
    than slow_factor times the median of its group (slow), a failed node and
    the skipped ones, with the reason. extra: lines added after (the ring
    after, versions...). full: every name (-v)."""
    outcomes = list(outcomes or [])
    for item in outcomes:
        item.setdefault("status", "ok")
    done = [o for o in outcomes if o["status"] == "ok"]
    failed = [o for o in outcomes if o["status"] == "failed"]
    skipped = [o for o in outcomes if o["status"] == "skipped"]

    def said(outcome):
        return would(outcome) if check else str(outcome)

    tally = {}
    for o in done:
        tally.setdefault(said(o.get("outcome", "")), []).append(o["node"])
    if check:
        counts = ["%s %s" % (word, plural(len(names), "node")) for word, names in tally.items()]
    else:
        counts = ["%d %s" % (len(names), word) for word, names in tally.items()]
    counts += ["%d failed" % len(failed)] if failed else []
    counts += ["%d not touched" % len(skipped)] if skipped else []
    verdict = "CHECK" if check else ("FAILED" if failed else "DONE")
    head = "  ".join(x for x in [verdict, operation, cluster, ", ".join(counts) or "nothing to do"] if x)
    if seconds is not None and not check:
        head += " (%s)" % duration(seconds)
    if check:
        head += "   nothing was changed"
    lines = [head]

    rows = []
    groups = {}
    for o in done:
        groups.setdefault(said(o.get("outcome", "")), []).append(o)
    for word, members in groups.items():
        times = [o["seconds"] for o in members if o.get("seconds") is not None]
        middle = _median(times) if times else None
        slow = [o for o in members if middle and len(times) > 1 and o.get("seconds") is not None
                and o["seconds"] > slow_factor * middle]
        usual = [o for o in members if o not in slow]
        if usual:
            spread = sorted(o["seconds"] for o in usual if o.get("seconds") is not None)
            note = ""
            if spread and not check:
                note = "(%s each)" % duration(spread[0]) if spread[0] == spread[-1] or len(usual) == 1 \
                    else "(%s..%s each)" % (duration(spread[0]), duration(spread[-1]))
                if len(usual) == 1:
                    note = duration(spread[0])
            rows.append([nodes([o["node"] for o in usual], keep_order=True, full=full), word, note])
        for o in slow:
            rows.append([o["node"], word, "%s  %s slow (median %s)" % (duration(o["seconds"]), ARROW, duration(middle))])
    for o in failed:
        rows.append([o["node"], "FAILED" + (": %s" % o["reason"] if o.get("reason") else ""), ""])
    reasons = {}
    for o in skipped:
        reasons.setdefault(o.get("reason") or "", []).append(o["node"])
    for reason, names in reasons.items():
        rows.append([nodes(names, keep_order=True, full=full), "not touched" + (" (%s)" % reason if reason else ""), ""])
    lines += _table(rows, "  ")
    if extra:
        lines += [""] + list(extra)
    return lines


def perm_lines(changes, indent="  "):
    """changes: [{file, before: {owner, group, mode}, after: {...}}] ->
    "cassandra.yaml  owner/group/mode  root/cassandra 0640 -> cassandra/cassandra 0640"."""
    def text(p):
        p = p or {}
        return "%s/%s %s" % (p.get("owner", "?"), p.get("group", "?"), p.get("mode", "?"))
    rows = [[c.get("file", ""), "owner/group/mode", "%s -> %s" % (text(c.get("before")), text(c.get("after")))]
            for c in changes or [] if text(c.get("before")) != text(c.get("after"))]
    return _table(rows, indent)


def _flat(value, path=()):
    if isinstance(value, dict) and not value and not path:
        return {}
    if isinstance(value, dict) and value:
        out = {}
        for k in value:
            out.update(_flat(value[k], path + (str(k),)))
        return out
    return {path: value}


def diff_lines(before, after, indent="  "):
    """Two dicts of settings (nested ones as in cassandra.yaml) -> the
    changed ones only, "- key: old" then "+ key: new", a nested key under its
    parents; secrets as ****."""
    old, new = _flat(before or {}), _flat(after or {})
    # a key that held a secret on one side (a dict with a password) hides its value on the other
    parents_of_secrets = set(p[:i] for p in list(old) + list(new) if hidden(p[-1], old.get(p, new.get(p)))
                             for i in range(1, len(p)))
    out, opened = [], ()
    for path in sorted(set(old) | set(new), key=lambda p: [_natural(x) for x in p]):
        a, b = old.get(path, _MISSING), new.get(path, _MISSING)
        if _same(a) == _same(b):
            continue
        parents = path[:-1]
        for depth in range(len(parents)):
            if opened[:depth + 1] != parents[:depth + 1]:
                out.append("%s%s%s:" % (indent, "  " * depth, parents[depth]))
        opened = parents
        masked = any(hidden(p, a) or hidden(p, b) for p in path) or path in parents_of_secrets
        pad = "  " * len(parents)
        for sign, value in (("-", a), ("+", b)):
            if value is not _MISSING:
                out.append("%s%s %s%s: %s" % (indent, sign, pad, path[-1], MASK if masked else shown(path[-1], value)))
    return out


_MISSING = object()


def changed_lines(diff, indent="  "):
    """A unified diff (text or lines) -> its - and + lines only (not the
    ---/+++ headers nor @@), secrets masked."""
    lines = diff.splitlines() if isinstance(diff, str) else list(diff or [])
    kept = [line for line in lines if line[:1] in "-+" and not line.startswith("---") and not line.startswith("+++")]
    # masked as one text without the signs: a secret's YAML block value spans lines
    bodies = mask("\n".join(line[1:] for line in kept)).split("\n") if kept else []
    return [indent + line[0] + body for line, body in zip(kept, bodies)]


# --- what is left to do --------------------------------------------------------------------


def path_from(path, cwd):
    """path relative to cwd when it is under it, else as given."""
    rel = os.path.relpath(path, cwd) if cwd and os.path.isabs(path) else path
    return path if rel.startswith("..") else rel


def extra_var(name, value):
    """-e name=value as the shell and Ansible's key=value parsing both take it (else as JSON)."""
    value = str(value)
    if re.match(r"^[\w.:/@,+-]+$", value):
        return "-e %s=%s" % (name, value)
    return "-e " + shlex.quote(json.dumps({name: value}))


def command(playbook, inventory=None, hosts=None, limit=None, extra=None, options=None, cwd=None):
    """The full command of a collection playbook, ready to paste:
    "ansible-playbook -i inventory.yml community.cassandra.cleanup -e
    cassandra_hosts=my_cluster --limit node1,node2 -e k=v". inventory: the
    sources (a list or a string; none: the -i is left out, ansible.cfg's
    is used); hosts: the cluster's group (none: left out, the playbooks find
    it); limit: the hosts, every one named (no range); extra: {name: value}
    or a list of ready arguments; options: other arguments as given
    (--vault-id...); cwd: paths shown relative to it."""
    sources = [inventory] if isinstance(inventory, str) else list(inventory or [])
    parts = ["ansible-playbook"] + ["-i %s" % shlex.quote(path_from(s, cwd)) for s in sources if s]
    parts += [str(o) for o in options or []]
    name = playbook if "." in playbook else "community.cassandra." + playbook
    parts.append(name)
    if hosts:
        parts.append(extra_var("cassandra_hosts", hosts))
    if limit:
        names = [limit] if isinstance(limit, str) else list(limit)
        parts.append("--limit " + shlex.quote(",".join(str(n) for n in names)))
    if isinstance(extra, dict):
        parts += [extra_var(k, extra[k]) for k in extra]
    else:
        parts += [str(e) for e in extra or []]
    return " ".join(parts)


def todo(items, title="TO DO"):
    """items: a string, or {text, command} (the command on its own line under
    it) -> "TO DO", then "  1. text" numbered; nothing when no item."""
    lines = []
    number = 0
    for item in items or []:
        if not item:
            continue
        number += 1
        text, cmd = (item.get("text", ""), item.get("command", "")) if isinstance(item, dict) else (str(item), "")
        lines.append("  %d. %s%s" % (number, text, ":" if cmd and text and not text.endswith(":") else ""))
        if cmd:
            lines.append("     " + cmd)
    return [title] + lines if lines else []


def in_git_work_tree(path):
    """True when path (a file or a dir) is inside a git work tree: a .git
    dir or file in it or above it."""
    path = os.path.abspath(str(path or "."))
    current = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def inventory_steps(inventory_file, in_git=None, message="", cwd=None):
    """The TO DO items about an inventory change: "Review the inventory:
    <file>", and only when it is inside a git work tree (in_git, or found
    out from the file) the commit command."""
    shown_path = path_from(str(inventory_file), cwd)
    if in_git is None:
        in_git = in_git_work_tree(inventory_file)
    steps = ["Review the inventory: %s" % shown_path]
    if in_git:
        steps.append({"text": "commit it", "command": "git add %s && git commit -m %s" % (
            shlex.quote(shown_path), shlex.quote(message or "Inventory: %s" % os.path.basename(shown_path)))})
    return steps


SEEDS_MIN, SEEDS_MAX = 2, 3  # per datacenter (OUTPUT Q7)


def _seed_pick(racks, target):
    """target nodes of a datacenter, one rack after the other, its seeds first
    (racks holding a seed first: well placed seeds stay)."""
    order = sorted(racks, key=lambda r: not any(n["seed"] for n in racks[r]))
    queues = dict((r, sorted(racks[r], key=lambda n: not n["seed"])) for r in order)
    picked = []
    while len(picked) < target and any(queues.values()):
        for rack in order:
            if queues[rack] and len(picked) < target:
                picked.append(queues[rack].pop(0))
    return picked


def seed_layout(nodes):
    """The seed rule (Q7), the same for preflight and help: 2 or 3 seeds per
    datacenter, on different racks when it has several; 1 seed (in a
    datacenter of more than one node) or none is a warning, more than 3 a
    note. nodes: [{name, address, dc, rack, seed}] in inventory order.
    Returns {lines: one per datacenter ("Seeds  dc1  node1 (rack_a), node3
    (rack_b)  ok", a warning's "WARNING  Seeds  dc2  ..."), dcs: [{dc, level
    ok|note|warning, text}], problems: the warnings' texts, notes, suggested: a seed list that follows the rule
    (addresses, every datacenter; empty without a warning)}."""
    dcs = {}
    for n in nodes or []:
        dcs.setdefault(n["dc"], {}).setdefault(n["rack"], []).append(n)
    width = max([len(str(dc)) for dc in dcs] or [0])
    result = {"lines": [], "dcs": [], "problems": [], "notes": [], "suggested": []}
    suggested = []
    for dc, racks in dcs.items():
        members = [n for rack in racks.values() for n in rack]
        seeds = [n for n in members if n["seed"]]
        seed_racks = []
        for n in seeds:
            if n["rack"] not in seed_racks:
                seed_racks.append(n["rack"])
        level, text = "ok", ""
        if not seeds:
            level, text = "warning", "no seed"
        elif len(seeds) == 1 and len(members) > 1:
            level, text = "warning", "1 seed: %d to %d per datacenter%s" % (
                SEEDS_MIN, SEEDS_MAX, ", on different racks" if len(racks) > 1 else "")
        elif len(seed_racks) < min(len(seeds), len(racks)):
            empty = [r for r in racks if r not in seed_racks]
            level, text = "warning", "%s on %s, none on %s: put them on different racks" % (
                plural(len(seeds), "seed"), ", ".join(seed_racks), ", ".join(empty))
        elif len(seeds) > SEEDS_MAX:
            level, text = "note", "%d seeds: %d to %d are enough" % (len(seeds), SEEDS_MIN, SEEDS_MAX)
        listed = ", ".join("%s (%s)" % (n["name"], n["rack"]) for n in seeds) or "none"
        line = "Seeds  %s  %s  %s" % (str(dc).ljust(width), listed, {"ok": "ok", "note": "note: " + text}.get(level, text))
        result["lines"].append("WARNING  " + line if level == "warning" else line)
        result["dcs"].append({"dc": dc, "level": level, "text": text})
        if level == "warning":
            result["problems"].append("%s: %s" % (dc, text))
        elif level == "note":
            result["notes"].append("%s: %s" % (dc, text))
        target = min(len(members), SEEDS_MAX, max(SEEDS_MIN, len(racks)))
        keep = seeds if level != "warning" else _seed_pick(racks, target)
        suggested += [n["address"] for n in keep]
    result["suggested"] = suggested if result["problems"] else []
    return result
