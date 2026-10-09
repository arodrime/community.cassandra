# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What apply_config shows: the settings that differ, then what each node gets.

cassandra_apply_config_diff: the nodes' compare data -> the settings that
    differ, setting-centric, per file: the inventory's value with the nodes
    already on it, then the live value of each node to change, with its nodes ("<- differs").
    The JVM options and cassandra-env.sh lines are settings too, the owner,
    group and mode of a file or of a directory one setting. Secrets masked.
cassandra_apply_config_outcomes: the nodes -> what each one gets, grouped
    ("node2, node4  would apply, then restart").
cassandra_apply_config_recap: the end of apply_config: a verdict line, then
    under --check the settings that differ and the outcomes, after a real run
    the outcomes (its plan showed the settings, before its question).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import base64
import hashlib
import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import _stat
from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

# the order of the files in the view (the role's own); any other after them
FILES = ("cassandra.yaml", "cassandra-env.sh", "jvm-server.options", "jvm8-server.options", "jvm11-server.options",
         "jvm17-server.options", "cassandra-rackdc.properties", "logback.xml", "jmxremote.password", "jmxremote.access")
# the masking of cassandra_config's diff (its "Diff them against the live files" task): the live lines get it too,
# so that a line the diff masked compares with the live one
ROLE_MASK = re.compile(r"(?i)([\w.-]*(?:password|passwd|secret)[\w.-]*[ \t]*[:=][ \t]*)\S[^\r\n]*")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_YAML_KEY = re.compile(r"^( *)(- +)?([A-Za-z0-9_][\w.\-/]*)[ \t]*:(?:[ \t]+(.*?))?[ \t]*$")
_YAML_ITEM = re.compile(r"^( *)- +(.*?)[ \t]*$")
_ASSIGN = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][\w.\-]*)[ \t]*=(.*)$")
_HEAP = re.compile(r"^(-X(?:mx|ms|mn|ss))(.+)$")
_PERMS = " (owner:group mode)"
PERM_LABEL = "owner/group/mode"
ABSENT = "absent"

# what follows the write, by cassandra_apply_config_then: (--check or the plan, real run)
_THEN = {"restart": ("then restart", "restarted"), "start": ("then start", "started"),
         "write": ("left stopped", "left stopped"), "none": ("no restart", "no restart")}
# said in the plan only, before the question
_WHY = {"start": "its Cassandra is not running: started once written (down over max_hint_window? repair it)",
        "write": "its Cassandra is stopped: left stopped, reads its config when it starts",
        "none": "an owner, group or mode only: Cassandra reads them when it starts"}


def _lines(text):
    """A file's lines as the role's diff splits them, without their "\\n" (a "\\r" kept: bash reads it)."""
    return [line[:-1] if line.endswith("\n") else line for line in str(text).splitlines(True)]


def _patched(live, diff):
    """live: the live file's lines; diff: the role's unified diff of it -> (the
    new file's lines, the indexes of the ones the diff adds), or None when the
    diff does not fit the live file (changed since it was compared)."""
    new, added, at = [], set(), 0
    hunk = False
    for line in _lines(diff):
        match = _HUNK.match(line)
        if match:
            start, count = int(match.group(1)), match.group(2)
            start = start if count == "0" else start - 1  # an empty hunk inserts after its line
            if start < at or start > len(live):
                return None
            new.extend(live[at:start])
            at, hunk = start, True
            continue
        if not hunk or not line:
            continue  # the ---/+++ headers
        mark, body = line[0], line[1:]
        # (a "\r" aside: the command module strips the one ending the diff)
        if mark in " -" and (at >= len(live) or live[at].rstrip("\r") != body.rstrip("\r")):
            return None  # the live file is no longer the one compared
        if mark == " ":
            new.append(live[at])
            at += 1
        elif mark == "-":
            at += 1
        elif mark == "+":
            added.add(len(new))
            new.append(body)
    return new + live[at:], added


def _unquote(value):
    """A YAML scalar's text: its quotes undone, a trailing comment dropped."""
    value = value.strip()
    quoted = re.match(r"'((?:[^']|'')*)'|\"((?:[^\"\\]|\\.)*)\"", value)
    if quoted:
        return quoted.group(1).replace("''", "'") if quoted.group(1) is not None else quoted.group(2)
    value = re.sub(r"(^|[ \t]+)#.*$", "", value)  # "key:  # a comment": no value, its children below
    return "" if re.match(r"^[&!]\S*$", value) else value  # an anchor or a tag alone: no value either


def _yaml_settings(lines):
    """cassandra.yaml's lines -> {path: [value, line index]}: a nested key as
    parent.key (the "- " of a list of mappings as indentation), a list of
    scalars as one value "a, b", the lines more indented than a key with a
    value part of it (a block value "key: |", a PEM key, a value that goes on
    over lines); no YAML parser (a masked "****" is no valid YAML), comments
    and blank lines left out."""
    found, stack, parents, value_of = {}, [], set(), None
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if value_of and len(line) - len(line.lstrip(" ")) > value_of[0]:
            value = found[value_of[1]][0]
            found[value_of[1]][0] = (value + "\n" if value else "") + line.strip()
            continue
        value_of = None
        key = _YAML_KEY.match(line)
        if key:
            column = len(key.group(1)) + len(key.group(2) or "")
            while stack and stack[-1][0] >= column:
                stack.pop()
            path = ".".join([k for c, k in stack] + [key.group(3)])
            if stack:
                parents.add(".".join(k for c, k in stack))
            name, n = path, 1
            while name in found:  # the same key in another item of a list
                n += 1
                name = "%s[%d]" % (path, n)
            value = _unquote(key.group(4) or "")
            found[name] = ["" if re.match(r"^[|>][-+0-9]*$", value) else value, index]  # a block: its lines
            stack.append((column, name.rsplit(".", 1)[-1] if "." in name else name))
            if value:  # a block indicator too: its lines are its value
                value_of = (column, name)
            continue
        item = _YAML_ITEM.match(line)
        if item:
            while stack and stack[-1][0] > len(item.group(1)):
                stack.pop()
            if stack:
                path = ".".join(k for c, k in stack)
                if path in found:
                    value = found[path][0]
                    found[path][0] = (value + ", " if value else "") + _unquote(item.group(2))
    return dict((k, v) for k, v in found.items() if not (k in parents and v[0] == ""))


def _line_settings(lines, name):
    """The lines of a JVM options file, cassandra-env.sh, a properties file
    (any other file: its lines) -> {key: [value, line index, line]}: an option
    -Dname=value or -XX:Name=value by its name, -Xmx4G by -Xmx, NAME=value by
    NAME; a key found twice, and any other line, by the whole line (value
    None: there or not)."""
    entries = []
    for index, raw in enumerate(lines):
        line = raw.strip(" \t")
        if not line.strip() or line.startswith("#") or line.startswith("<!--"):
            continue
        key, value = line, None
        if name.endswith(".options"):
            heap = _HEAP.match(line)
            if heap:
                key, value = heap.group(1), heap.group(2)
            elif "=" in line:
                key, value = line.split("=", 1)
        elif not name.endswith(".xml"):
            assign = _ASSIGN.match(line)
            if assign:
                key, value = assign.group(1), assign.group(2)
                if name.endswith(".properties"):
                    value = value.strip()
        entries.append([key, value, index, line])
    counts = {}
    for entry in entries:
        counts[entry[0]] = counts.get(entry[0], 0) + 1
    found = {}
    for key, value, index, line in entries:
        if counts[key] > 1 or value is None:
            key, value = line, None
        found.setdefault(key, [value, index, line])
    return found


def _settings(name, lines):
    if name.endswith((".yaml", ".yml")):
        return dict((k, [v[0], v[1], None]) for k, v in _yaml_settings(lines).items())
    return _line_settings(lines, name)


def _masked(lines):
    return [ROLE_MASK.sub(r"\1" + out.MASK, line) for line in lines]


def _live_texts(node):
    """{file name: its live lines} from the node's slurp results."""
    texts = {}
    for result in node.get("live") or []:
        name = str(result.get("item") or result.get("cassandra_apply_config_file") or "").rsplit("/", 1)[-1]
        if name and result.get("content") is not None:
            try:
                texts[name] = _lines(base64.b64decode(result["content"]).decode("utf-8", "replace"))
            except (TypeError, ValueError):
                continue
    return texts


def _stats(results, key):
    """stat loop results -> {key of the item: "owner:group mode"}, existing ones only."""
    found = {}
    for result in results or []:
        st = result.get("stat") or {}
        if not st.get("exists"):
            continue
        name = key(result)
        if name:
            p = _stat(st)
            found[name] = "%s:%s %s" % (p["owner"], p["group"], p["mode"])
    return found


def _file_key(result):
    return str(result.get("cassandra_config_file") or "").rsplit("/", 1)[-1]


def _dir_key(result):
    item = result.get("item")
    return "%s %s" % (item[0], item[1]) if isinstance(item, (list, tuple)) and len(item) == 2 else ""


def _order(name):
    return (FILES.index(name) if name in FILES else len(FILES), name)


def _one_line(text):
    """A value on one line: a "\\r" said, a value over several lines (a
    certificate) as its first line, its count of lines and a digest of it."""
    text = text.replace("\r", "\\r")
    if "\n" not in text:
        return text
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    return "%s ... (%d lines, %s)" % (text.split("\n", 1)[0], text.count("\n") + 1, digest)


def _rows(records, shown_of, all_nodes):
    """records: {node: (live, target, changing)} of one setting -> its rows:
    each inventory value (its nodes already on it), then the live values of
    the nodes to change to it ("<- differs")."""
    order = [n for n in all_nodes if n in records]
    changing = [n for n in order if records[n][2]]
    targets = []
    for n in changing:
        t = shown_of(records[n][1])
        if t not in targets:
            targets.append(t)
    rows = []
    for target in targets:
        on_it = [n for n in order if not records[n][2] and shown_of(records[n][0]) == target]
        if target != ABSENT:  # with none on it: whose value it is (a node's own, e.g. listen_address)
            mine = [n for n in changing if shown_of(records[n][1]) == target]
            rows.append((target, out.nodes(on_it), "(inventory)" if on_it else "(inventory, %s)" % out.nodes(mine)))
        lives = []
        for n in changing:
            if shown_of(records[n][1]) != target:
                continue
            live = shown_of(records[n][0])
            group = next((g for g in lives if g[0] == live), None)
            if group is None:
                lives.append((live, [n]))
            else:
                group[1].append(n)
        for live, names in lives:
            why = "not in the inventory" if target == ABSENT else "masked" if live == target else ""
            note = out.ARROW + " differs" + (" (%s)" % why if why else "")
            rows.append((live, out.nodes(names), note))
    return rows


def _file_entries(name, nodes, all_nodes):
    """The settings of one file that differ on a node -> (its settings_lines
    entries, the lines of the nodes whose live file no longer fits their
    diff). A key written twice in a file is by its lines (see _line_settings):
    one written twice on a node and once in the inventory shows as lines there
    or not."""
    per_node, layout_only, new_file, unread = {}, [], [], []
    for node in nodes:
        live = node["texts"].get(name)
        diff = node["diffs"].get(name)
        if diff is None:
            if live is not None:
                settings = _settings(name, _masked(live))
                per_node[node["name"]] = (settings, settings, set())
            continue
        if live is None:  # not there (the diff writes it whole), or not read
            (new_file if re.search(r"(?m)^@@ -0,0 ", diff) else unread).append((node["name"], diff))
            continue
        patched = _patched(_masked(live), diff)
        if patched is None:
            unread.append((node["name"], diff))
            continue
        before, after = _settings(name, _masked(live)), _settings(name, patched[0])
        per_node[node["name"]] = (before, after, patched[1])
    # the keys changed on some node, in the order of the new file
    keys, changed = [], {}
    for node_name in all_nodes:
        if node_name not in per_node:
            continue
        before, after, added = per_node[node_name]
        mine = []
        for key in list(after) + [k for k in before if k not in after]:
            a, b = before.get(key), after.get(key)
            # a masked value on a line the diff writes: changed, the same **** on both sides
            secret = b is not None and b[1] in added and out.MASK in str(b[0]) and a is not None
            if (a is None) != (b is None) or (a is not None and (a[0], a[2]) != (b[0], b[2])) or secret:
                mine.append(key)
                if key not in keys:
                    keys.append(key)
        changed[node_name] = set(mine)
        if not mine and name in next(n for n in nodes if n["name"] == node_name)["diffs"]:
            layout_only.append(node_name)

    entries = []
    yaml = name.endswith((".yaml", ".yml"))
    for key in keys:
        records = {}
        for node_name, (before, after, added) in per_node.items():
            records[node_name] = (before.get(key), after.get(key), key in changed.get(node_name, ()))
        forms = set(v[2] for r in records.values() for v in r[:2] if v is not None)
        if not yaml and len(forms) == 1:  # one form of the line: there or not
            label = out.mask(list(forms)[0])

            def shown_of(v):
                return ABSENT if v is None else "present"
        else:
            label = key

            def shown_of(v, key=key):
                if v is None:
                    return ABSENT
                value = v[0] if v[0] is not None else v[2]
                return _one_line(out.shown(key, value)) if value != "" else "(empty)"
        entries.append((label.replace("\r", "\\r"), _rows(records, shown_of, all_nodes)))
    if layout_only:
        entries.append(("comments or layout", [("differ", out.nodes(layout_only), "(no setting changes)")]))
    if new_file:
        entries.append(("file", [(ABSENT, out.nodes([n for n, d in new_file]), out.ARROW + " differs (written whole)")]))
    # not its diff: a secret's block value (a PEM key) is in clear there
    names = out.nodes([n for n, d in unread])
    lines = ["  %s: not read, or changed since it was compared: run apply_config again" % names] if unread else []
    return entries, lines


def _perm_entry(key, nodes, all_nodes, field):
    """The owner, group and mode of a file or directory: what the role sets
    on the nodes it changes, the nodes already so, the ones to change."""
    records = {}
    for node in nodes:
        change = node[field].get(key)
        if change is not None:
            records[node["name"]] = (change[0], change[1], True)
        elif key in node["stats" if field == "perms" else "dir_stats"]:
            live = node["stats" if field == "perms" else "dir_stats"][key]
            records[node["name"]] = (live, live, False)
    return (PERM_LABEL, _rows(records, lambda v: ABSENT if v is None else str(v), all_nodes))


def _prepare(node):
    """A node's compare data -> its diffs, owner/mode changes, directory
    changes and other items by name, its live files and stats."""
    diffs, perms, dirs, other = {}, {}, {}, []
    for item in node.get("items") or []:
        name = str(item.get("item") or "")
        if item.get("dir"):
            dirs["%s %s" % (item["dir"], item.get("path"))] = (item.get("before"), item.get("after"))
        elif "diff" in item:
            diffs[name.rsplit("/", 1)[-1]] = str(item["diff"] or "")
        elif name.endswith(_PERMS):
            perms[name[:-len(_PERMS)].rsplit("/", 1)[-1]] = (item.get("before"), item.get("after"))
        else:  # the RPM conf dir alternative, or nothing installed yet
            other.append((name, str(item.get("before") or "(none)"), str(item.get("after") or "")))
    return {"name": str(node.get("name")), "diffs": diffs, "perms": perms, "dirs": dirs, "other": other,
            "texts": _live_texts(node), "stats": _stats(node.get("stats"), _file_key),
            "dir_stats": _stats(node.get("dir_stats"), _dir_key)}


def cassandra_apply_config_diff(nodes):
    """nodes: [{name, items, live, stats, dir_stats}] in inventory order, the
    nodes compared: items, cassandra_config's _cassandra_config_items (diffs
    masked, owner/mode changes, directory changes); live, the slurp results
    of the live files that differ on some node ({item: file name, content});
    stats, its cassandra_config_live_stat results; dir_stats, its
    cassandra_config_dir_stat results. -> the lines: per file, each setting
    that differs on a node, the inventory's value and the nodes already on
    it ("(inventory)"), then each other live value with its nodes ("<-
    differs"); the owner, group and mode as one setting; the directories
    after the files. [] when nothing differs."""
    prepared = [_prepare(n) for n in nodes or []]
    all_nodes = [n["name"] for n in prepared]
    files = set()
    for node in prepared:
        files.update(node["diffs"])
        files.update(node["perms"])
    entries = []
    for name in sorted(files, key=_order):
        settings, unread = _file_entries(name, prepared, all_nodes)
        if any(name in n["perms"] for n in prepared):
            settings.append(_perm_entry(name, prepared, all_nodes, "perms"))
        settings = [s for s in settings if s[1]]
        if settings or unread:
            entries.append(name)
            entries.extend(settings)
            entries.extend(unread)
    dirs = []
    for node in prepared:
        dirs.extend(d for d in node["dirs"] if d not in dirs)
    for name in dirs:
        entries.append(name)
        entries.append(_perm_entry(name, prepared, all_nodes, "dirs"))
    others = []
    for node in prepared:
        others.extend((o[0], o[1], o[2], node["name"]) for o in node["other"])
    for name in sorted(set(o[0] for o in others)):
        records = dict((o[3], (o[1], o[2], True)) for o in others if o[0] == name)
        entries.append(name)
        entries.append(("points to" if name.startswith("/") else "state", _rows(records, str, all_nodes)))
    return out.settings_lines(entries)


def _outcome(node, check, plan):
    """(what a node gets, its tally word)"""
    todo_done = bool(node.get("todo") and (plan or node.get("done")))
    if todo_done:
        then = node.get("then")
        text = ("would apply" if check or plan else "applied")
        said = _THEN.get(then, ("", ""))[0 if check or plan else 1]
        text += (", " + said if said else "")
        if plan and then in _WHY:
            text += " (%s)" % _WHY[then]
        return text, ("would apply" if check or plan else "applied")
    result = str(node.get("result") or "nothing to apply")
    if "FAILED" in result:
        return result, "failed"
    for word in ("nothing to apply", "not reached", "not in this run", "skipped"):
        if word in result:
            return result, word
    return result, "not touched"


_RANK = {"would apply": 0, "applied": 0, "failed": 1}


def _grouped(nodes, check=False, plan=False):
    """(the outcome lines, nodes with the same outcome and notes on one line;
    {tally word: count})"""
    groups, order, counts, rank = {}, [], {}, {}
    for node in nodes or []:
        text, word = _outcome(node, check, plan)
        counts[word] = counts.get(word, 0) + 1
        key = (text, tuple("  " + str(n) for n in node.get("notes") or []))
        if key not in groups:
            groups[key] = []
            order.append(key)
            rank[key] = _RANK.get(word, len(_RANK))
        groups[key].append(str(node["name"]))
    lines = []
    for key in sorted(order, key=lambda k: rank[k]):  # the nodes changed first, then failed, then the others
        lines.append("%s  %s" % (out.nodes(groups[key]), key[0]))
        lines.extend(key[1])
    return lines, dict(sorted(counts.items(), key=lambda c: _RANK.get(c[0], len(_RANK))))


def cassandra_apply_config_outcomes(nodes, check=False, plan=False):
    """nodes: [{name, todo, done, then, result, notes}] in inventory order ->
    what each one gets, the nodes with the same outcome on one line
    ("node2, node4  would apply, then restart"), its notes under it. plan:
    before the run (every node to do would apply, with why it is not
    restarted)."""
    return _grouped(nodes, check, plan)[0]


def cassandra_apply_config_recap(nodes, check=False, cluster="", seconds=None, diff=None):
    """nodes: [{name, todo, done, then, result, notes}] in inventory order:
    todo, whether the node had something to apply; done, whether its turn
    ended well (node_operation's cassandra_op_done); then, its
    cassandra_apply_config_then; result, its cassandra_op_result (said as is
    when not done: skipped, not reached, ...); notes, more lines (a restart
    pending, data dirs another account owns), said whatever the outcome.
    diff: the lines of cassandra_apply_config_diff. Line 1, the verdict as
    the other operations' recaps: "CHECK  apply_config  my_cluster  2 would
    apply, 3 nothing to apply (12s)" (DONE, FAILED when a node failed).
    Under check, then the settings that differ (no question was asked: the
    plan did not show them), a blank line, the outcomes; after a real run
    the outcomes only (its plan showed the settings before its question)."""
    lines, counts = _grouped(nodes, check)
    verdict = "CHECK" if check else ("FAILED" if counts.get("failed") else "DONE")
    said = ", ".join("%d %s" % (n, word) for word, n in counts.items()) or "no node"
    if seconds is not None:
        said += " (%s)" % out.duration(seconds)
    shown = ["  ".join(x for x in (verdict, "apply_config", str(cluster or ""), said) if x)]
    if check and diff:
        shown += [""] + list(diff) + [""]
    return shown + lines


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_apply_config_diff": cassandra_apply_config_diff,
            "cassandra_apply_config_outcomes": cassandra_apply_config_outcomes,
            "cassandra_apply_config_recap": cassandra_apply_config_recap,
        }
