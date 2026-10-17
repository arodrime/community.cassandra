# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What apply_config shows: the settings that differ, then what each node gets.

cassandra_apply_config_diff: the nodes' compare data -> the settings that
    differ, setting-centric, per file: the inventory's value with the nodes
    already on it, then the live value of each node to change, with its nodes ("<- differs").
    The JVM options and cassandra-env.sh lines are settings too, the owner,
    group and mode of a file or of a directory one setting. Secrets masked.
    Then the OS settings (cassandra_linux's) the same way.
cassandra_apply_config_outcomes: the nodes -> what each one gets, grouped
    ("node2, node4  would apply, then restart"), their notes under them but
    the warnings.
cassandra_apply_config_warnings: the nodes -> their warnings, for the plan
    (each one once, with the nodes it is for).
cassandra_apply_config_recap: the end of apply_config: a verdict line, then
    under --check the settings that differ and the outcomes, after a real run
    the outcomes (its plan showed the settings, before its question).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import base64
import functools
import hashlib
import importlib.util
import os
import re
import shlex
from collections import OrderedDict

from ansible_collections.community.cassandra.plugins.filter.cassandra_output import terminal_width
from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import stat_fields
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import DEFAULT_WHEN_ABSENT, BOOLS
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

# the order of the files in the view (the role's own); any other after them
FILES = ("cassandra.yaml", "cassandra-env.sh", "jvm-server.options", "jvm8-server.options", "jvm11-server.options",
         "jvm17-server.options", "cassandra-rackdc.properties", "logback.xml", "jmxremote.password", "jmxremote.access")
# the masking of cassandra_config's diff (its "Diff them against the live files" task): the live lines get it too,
# so that a line the diff masked compares with the live one
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_YAML_KEY = re.compile(r"^( *)(- +)?([A-Za-z0-9_][\w.\-/]*)[ \t]*:(?:[ \t]+(.*?))?[ \t]*$")
_YAML_ITEM = re.compile(r"^( *)- +(.*?)[ \t]*$")
_ASSIGN = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][\w.\-]*)[ \t]*=(.*)$")
_HEAP = re.compile(r"^(-X(?:mx|ms|mn|ss))(.+)$")
_PERMS = " (owner:group mode)"
PERM_LABEL = "owner/group/mode"
ABSENT = "absent"
# a device-mapper device's kernel name after its stable one in an OS item ("read_ahead_kb vg-lv (dm-3)"): dm-N
# differs from node to node, the item is the same setting (said with -v)
_DM_KNAME = re.compile(r" \(dm-\d+\)$")

# what follows the write, by cassandra_apply_config_then: (--check or the plan, real run)
THEN = {"restart": ("then restart", "restarted"), "start": ("then start", "started"),
         "write": ("left stopped", "left stopped"), "none": ("no restart", "no restart"),
         "os": ("no restart", "no restart")}
# said in the plan only, before the question
_WHY = {"start": "its Cassandra is not running: started once written (down over max_hint_window? repair it)",
        "write": "its Cassandra is stopped: left stopped, reads its config when it starts",
        "none": "an owner, group or mode only: Cassandra reads them when it starts",
        "os": "OS settings only: applied live"}
_WRITTEN_BACK = " written back as the running Cassandra started with "  # (apply_config's note)
# apply_config's notes: a restart pending (said before the run: not after a restart or start of this run), the
# read-ahead set live with no restart (the recap's TO DO says how to take it)
_PENDING = "restart pending ("
READ_AHEAD_LIVE = "read-ahead set live: the files Cassandra has open keep the old one until it restarts"


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
    found, stack, parents, value_of, last = {}, [], set(), None, None
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.strip() == out.MASK and last and found[last][0] == "":
            found[last][0] = out.MASK  # the value under a secret's key (a mapping, a list), masked
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
            last = name
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


# the role's node-side reader: its diffs are masked by it, the live lines here the same way to match them
CONFIG_READ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config", "files",
                           "config_read.py")


@functools.lru_cache(maxsize=None)
def _config_read():
    spec = importlib.util.spec_from_file_location("cassandra_config_read", CONFIG_READ)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _secrets_written(lines, written):
    """The new file's (masked) lines, the indexes of the ones the diff writes -> the indexes of the lines whose
    secret it writes: a line with a masked value, or the key line above a line of a secret's value (****
    alone, the lines of a YAML block, a quote going on)."""
    found = set()
    for i in written:
        if lines[i].strip() == out.MASK:
            while i > 0 and lines[i].strip() in (out.MASK, ""):
                i -= 1
            found.add(i)
        elif out.MASK in re.sub(r"(^|\s)#.*$", "", lines[i]):  # (not one in a comment)
            found.add(i)
    return found


def _masked(lines):
    """A file's lines masked as the role's diff masks them (a secret's value on several lines too)."""
    return _config_read().masked(lines)


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
            p = stat_fields(st)
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
            rows.append((target, on_it, "(inventory)" if on_it else ("(inventory, %s)", mine)))
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
            rows.append((live, names, note))
    return rows


def _default_when_absent(key, a, b):
    """One side without the key, the other with Cassandra's default for it (DEFAULT_WHEN_ABSENT): the same setting."""
    if key not in DEFAULT_WHEN_ABSENT or (a is None) == (b is None):
        return False
    value = (a if a is not None else b)[0]
    return BOOLS.get(str(value).lower()) == DEFAULT_WHEN_ABSENT[key]


def _node_settings(name, nodes):
    """One file, the nodes -> ({node: (settings before, settings after, (the secret lines written, their texts))}
    of the nodes whose live file was read, [(node, diff)] of the ones it is new on, [(node, diff)] of the ones
    whose live file was not read or no longer fits their diff)."""
    per_node, new_file, unread = {}, [], []
    for node in nodes:
        live = node["texts"].get(name)
        diff = node["diffs"].get(name)
        if diff is None:
            if live is not None:
                settings = _settings(name, _masked(live))
                per_node[node["name"]] = (settings, settings, (set(), set()))
            continue
        if live is None:  # not there (the diff writes it whole), or not read
            (new_file if re.search(r"(?m)^@@ -0,0 ", diff) else unread).append((node["name"], diff))
            continue
        patched = _patched(_masked(live), diff)
        if patched is None:
            unread.append((node["name"], diff))
            continue
        before, after = _settings(name, _masked(live)), _settings(name, patched[0])
        secrets = _secrets_written(patched[0], patched[1])
        per_node[node["name"]] = (before, after, (secrets, set(patched[0][i].strip(" \t") for i in secrets)))
    return per_node, new_file, unread


def _key_changed(key, before, after, secrets, secret_lines, yaml_file):
    """A key, a node's settings before and after, the secret lines written (and their texts), whether the file is
    YAML -> whether the key changes there."""
    a, b = before.get(key), after.get(key)
    # a masked value the diff writes (on its key's line or the lines of its value): changed, the same
    # **** on both sides
    # (a line written twice: by its text, its key gives its first line)
    secret = b is not None and a is not None and (b[1] in secrets or (b[2] is not None and b[2] in secret_lines))
    if yaml_file and _default_when_absent(key, a, b):
        return False  # (auto_bootstrap: true written or not: Cassandra's default either way)
    return (a is None) != (b is None) or (a is not None and (a[0], a[2]) != (b[0], b[2])) or secret


def _changed_keys(name, nodes, per_node, all_nodes):
    """One file, the nodes, their settings -> (the keys changed on some node, in the order of the new file;
    {node: the keys it changes}; the nodes whose diff changes no setting: comments or layout)."""
    keys, changed, layout_only = [], {}, []
    yaml_file = name.endswith((".yaml", ".yml"))
    for node_name in all_nodes:
        if node_name not in per_node:
            continue
        before, after, (secrets, secret_lines) = per_node[node_name]
        mine = []
        for key in list(after) + [k for k in before if k not in after]:
            if _key_changed(key, before, after, secrets, secret_lines, yaml_file):
                mine.append(key)
                if key not in keys:
                    keys.append(key)
        changed[node_name] = set(mine)
        if not mine and name in next(n for n in nodes if n["name"] == node_name)["diffs"]:
            layout_only.append(node_name)
    return keys, changed, layout_only


def _presence(v):
    """A setting of one form of line -> whether it is there: "present" or ABSENT."""
    return ABSENT if v is None else "present"


def _shown_value(key, v):
    """A key, its setting (value, line, text; None: absent) -> its value as the view shows it."""
    if v is None:
        return ABSENT
    value = v[0] if v[0] is not None else v[2]
    return _one_line(out.shown(key, value)) if value != "" else "(empty)"


def _setting_entry(key, per_node, changed, all_nodes, yaml):
    """A key changed on some node, the nodes' settings, {node: the keys it changes}, whether the file is YAML ->
    its entry (label, rows): a line of one form only is there or not, else by its value."""
    records = {}
    for node_name, (before, after, dummy) in per_node.items():
        records[node_name] = (before.get(key), after.get(key), key in changed.get(node_name, ()))
    forms = set(v[2] for r in records.values() for v in r[:2] if v is not None)
    if not yaml and len(forms) == 1:  # one form of the line: there or not
        label, shown_of = out.mask(list(forms)[0]), _presence
    else:
        label, shown_of = key, functools.partial(_shown_value, key)
    return label.replace("\r", "\\r"), _rows(records, shown_of, all_nodes)


def _whole_rows(name, nodes, new_file, all_nodes):
    """One file, the nodes, the ones it is new on -> the rows of the nodes it is written whole on with no diff
    shown: missing, or other content (said, not shown)."""
    wholes = [(node["name"], node["wholes"][name]) for node in nodes if name in node["wholes"]]
    absent = [n for n, d in new_file] + [n for n, before in wholes if before == ABSENT]
    rows = [(before, [n for n, b in wholes if b == before], out.ARROW + " differs (written whole)")
            for before in sorted(set(b for n, b in wholes if b != ABSENT))]
    if absent:
        rows.insert(0, (ABSENT, [n for n in all_nodes if n in absent], out.ARROW + " differs (written whole)"))
    return rows


def _file_entries(name, nodes, all_nodes):
    """The settings of one file that differ on a node -> (its settings_lines
    entries, the lines of the nodes whose live file no longer fits their
    diff). A key written twice in a file is by its lines (see _line_settings):
    one written twice on a node and once in the inventory shows as lines there
    or not."""
    per_node, new_file, unread = _node_settings(name, nodes)
    keys, changed, layout_only = _changed_keys(name, nodes, per_node, all_nodes)
    yaml = name.endswith((".yaml", ".yml"))
    entries = [_setting_entry(key, per_node, changed, all_nodes, yaml) for key in keys]
    if layout_only:
        entries.append(("comments or layout", [("differ", layout_only, "(no setting changes)")]))
    rows = _whole_rows(name, nodes, new_file, all_nodes)
    if rows:
        entries.append(("file", rows))
    # not its diff: a secret's block value (a PEM key) is in clear there
    names = out.nodes([n for n, d in unread])
    lines = ["  %s: not read, or changed since it was compared: run apply_config again" % names] if unread else []
    return entries, lines


def _os_rows(changes):
    """[(node, before, after)] of one OS setting -> its rows: each value the
    role sets ("(inventory)": only the nodes that differ are known), then the
    values of the nodes it changes ("<- differs")."""
    rows = []
    for after in OrderedDict.fromkeys(c[2] for c in changes):
        rows.append((after, [], "(inventory)"))
        befores = OrderedDict()
        for node, before, value in changes:
            if value == after:
                befores.setdefault(before, []).append(node)
        rows.extend((before, nodes, out.ARROW + " differs") for before, nodes in befores.items())
    return rows


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


def _prepare(node, verbose=False):
    """A node's compare data -> its diffs, owner/mode changes, directory
    changes and other items by name, its live files and stats."""
    diffs, perms, dirs, other, wholes = {}, {}, {}, [], {}
    for item in node.get("items") or []:
        name = str(item.get("item") or "")
        if item.get("whole"):  # a file written whole, no diff shown (the JMX files: passwords)
            wholes[name.rsplit("/", 1)[-1]] = str(item.get("before") or "")
        elif item.get("dir"):
            dirs["%s %s" % (item["dir"], item.get("path"))] = (item.get("before"), item.get("after"))
        elif "diff" in item:
            diffs[name.rsplit("/", 1)[-1]] = str(item["diff"] or "")
        elif name.endswith(_PERMS):
            perms[name[:-len(_PERMS)].rsplit("/", 1)[-1]] = (item.get("before"), item.get("after"))
        else:  # the RPM conf dir alternative, or nothing installed yet
            other.append((name, str(item.get("before") or "(none)"), str(item.get("after") or "")))
    return {"name": str(node.get("name")), "diffs": diffs, "perms": perms, "dirs": dirs, "other": other, "wholes": wholes,
            "texts": _live_texts(node), "stats": _stats(node.get("stats"), _file_key),
            "dir_stats": _stats(node.get("dir_stats"), _dir_key),
            "os": [(_os_item(i.get("item"), verbose), "(none)" if i.get("before") is None else str(i.get("before")),
                    str(i.get("after"))) for i in node.get("os") or []]}


def _os_item(item, verbose=False):
    """An OS item's name in the view: a device-mapper device by its stable name (its dm-N with verbose)."""
    item = str(item or "")
    return item if verbose else _DM_KNAME.sub("", item)


def cassandra_apply_config_diff(nodes, width=None, verbose=False):
    """nodes: [{name, items, live, stats, dir_stats}] in inventory order, the
    nodes compared: items, cassandra_config's _cassandra_config_items (diffs
    masked, owner/mode changes, directory changes, the JMX files written
    whole: said, never shown); live, the slurp results
    of the live files that differ on some node ({item: file name, content});
    stats, its cassandra_config_live_stat results; dir_stats, its
    cassandra_config_dir_stat results; os, cassandra_linux's items (the OS
    settings that differ: {item, before, after}). -> the lines: per file, each setting
    that differs on a node, the inventory's value and the nodes already on
    it ("(inventory)"), then each other live value with its nodes ("<-
    differs"); the owner, group and mode as one setting; the directories
    after the files, then the OS settings. [] when nothing differs. From GROUPS_FROM nodes on
    (cassandra_output), the node lists by NODE GROUPS defined first. width:
    the lines fit in it (None: as long as they are; "terminal": the terminal's). verbose: a device-mapper
    device's OS items with its kernel name (dm-N) too, which differs from node to node."""
    width = _width(width)
    prepared = [_prepare(n, verbose) for n in nodes or []]
    all_nodes = [n["name"] for n in prepared]
    files = set()
    for node in prepared:
        files.update(node["diffs"])
        files.update(node["perms"])
        files.update(node["wholes"])
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
    os_items = []
    for node in prepared:
        os_items.extend(i[0] for i in node["os"] if i[0] not in os_items)
    if os_items:
        entries.append("OS settings")
    for name in os_items:
        entries.append((name, _os_rows([(n["name"], i[1], i[2]) for n in prepared for i in n["os"] if i[0] == name])))
    return _named(entries, all_nodes, width)


def _named(entries, all_nodes, width=None):
    """The entries' node lists as text: on a large cluster by node groups (defined first, "NODE GROUPS"), "all"
    for every node; else as cassandra_output's nodes() names them. -> the lines."""
    rows = [row for e in entries if not isinstance(e, str) for row in e[1]]
    groups = out.node_groups([r[1] for r in rows] + [r[2][1] for r in rows if isinstance(r[2], tuple)], all_nodes)

    def named(names):
        return out.who(names, all_nodes, groups) if groups else out.nodes(names)

    def note(text):
        return text[0] % named(text[1]) if isinstance(text, tuple) else text

    entries = [e if isinstance(e, str) else (e[0], [(v, named(n), note(t)) for v, n, t in e[1]]) for e in entries]
    lines = out.settings_lines(entries, width=width)
    return (out.group_lines(groups, width=width) + [""] + lines) if groups and lines else lines


def _outcome(node, check, plan):
    """(what a node gets, its tally word)"""
    os_only = bool(node.get("os") and not node.get("todo"))  # the OS settings alone, applied live
    done_key = "os_done" if os_only else "done"
    todo_done = bool(node.get("todo") or os_only) and bool(plan or node.get(done_key))
    if todo_done:
        then = "os" if os_only else node.get("then")
        word = "would apply" if check else "will apply" if plan else "applied"  # (plan: before a real run's question)
        text = word
        said = THEN.get(then, ("", ""))[0 if check or plan else 1]
        text += (", " + said if said else "")
        if then == "restart" and node.get("why"):  # not for new settings alone: a restart for this too
            text += " (%s)" % node["why"]
        # a file written back as the running Cassandra started with it: its note says why, not "an owner... only"
        written_back = any(_WRITTEN_BACK in str(n) for n in node.get("notes") or [])
        kept_running = node.get("kept_running") if then == "none" else None  # its own reasons, when given
        if plan and kept_running:
            text += " (%s)" % "; ".join(kept_running)
        elif plan and then in _WHY and not (then == "none" and written_back):
            text += " (%s)" % _WHY[then]
        return text, word
    result = str(node.get("result") or "nothing to apply")
    if node.get("os_done") and not check and "OS settings" not in result:  # its config not done: its OS settings were
        result += ", its OS settings applied"
    if "FAILED" in result:
        return result, "failed"
    for word in ("nothing to apply", "not reached", "not in this run", "skipped"):
        if word in result:
            return result, word
    return result, "not touched"


_RANK = {"would apply": 0, "will apply": 0, "applied": 0, "failed": 1}


def _width(width):
    if str(width) == "terminal":
        return terminal_width()
    return int(width) if width else None


def _grouped(nodes, check=False, plan=False, width=None):
    """(the outcome lines, nodes with the same outcome and notes on one line;
    {tally word: count}); width: the lines fit in it."""
    groups, order, counts, rank = {}, [], {}, {}
    for node in nodes or []:
        text, word = _outcome(node, check, plan)
        counts[word] = counts.get(word, 0) + 1
        key = (text, tuple("  " + str(n) for n in node.get("notes") or [] if not out.is_warning(n)))
        if key not in groups:
            groups[key] = []
            order.append(key)
            rank[key] = _RANK.get(word, len(_RANK))
        groups[key].append(str(node["name"]))
    lines = []
    for key in sorted(order, key=lambda k: rank[k]):  # the nodes changed first, then failed, then the others
        lines += out.fit(["%s  %s" % (out.nodes(groups[key]), key[0])] + list(key[1]), _width(width))
    return lines, dict(sorted(counts.items(), key=lambda c: _RANK.get(c[0], len(_RANK))))


def cassandra_apply_config_outcomes(nodes, check=False, plan=False, width=None):
    """nodes: [{name, todo, done, os, os_done, then, result, notes, kept_running}] in inventory order ->
    what each one gets, the nodes with the same outcome on one line
    ("node2, node4  would apply, then restart"), its notes under it. plan:
    before the run (every node to do "will apply", "would apply" under check,
    with why it is not restarted: kept_running, its reasons, else the usual one). width: the lines fit in it ("terminal": the terminal's)."""
    return _grouped(nodes, check, plan, width)[0]


def cassandra_apply_config_warnings(nodes):
    """The WARNING notes of the nodes, for the plan's warnings (said there
    only, not under each node): [{label: the nodes, text}], one per text,
    the nodes with it together, in the order they come."""
    by_text, order = {}, []
    for node in nodes or []:
        for note in node.get("notes") or []:
            if out.is_warning(note):
                text = out.warning_text(note)
                if text not in by_text:
                    by_text[text] = []
                    order.append(text)
                if str(node["name"]) not in by_text[text]:
                    by_text[text].append(str(node["name"]))
    return [{"label": out.nodes(by_text[t]), "text": t} for t in order]


def _after_run(node):
    """The node as the recap tells it: no "restart pending" note once this run restarted or started it."""
    if not (node.get("done") and node.get("then") in ("restart", "start")):
        return node
    return dict(node, notes=[n for n in node.get("notes") or [] if not str(n).startswith(_PENDING)])


def _read_ahead_todo(nodes, restart_command, width=None):
    """The TO DO of the nodes whose read-ahead was set live (their OS settings
    applied, Cassandra not restarted): the rolling restart that lets Cassandra
    read its open files with it, optional. [] when none, or no command. Its
    text fits in width, the command stays on one line (to paste)."""
    live = [str(n["name"]) for n in nodes if n.get("os_done") and READ_AHEAD_LIVE in (n.get("notes") or [])]
    if not live or not restart_command:
        return []
    lines = out.todo([{
        "text": "optional, when convenient: restart %s so that Cassandra reads the files it has open with the new"
                " read-ahead (one node at a time; the new files use it already)" % out.nodes(live),
        "command": "%s --limit %s" % (restart_command, shlex.quote(",".join(live)))}])
    return [""] + out.fit(lines[:-1], width) + lines[-1:]


def _recap_verdict(counts, check, cluster, seconds):
    """The outcomes counted, --check, the cluster, the run's seconds -> the recap's verdict line: "CHECK
    apply_config  my_cluster  2 would apply, 3 nothing to apply (12s)" (DONE, FAILED when a node failed)."""
    verdict = "CHECK" if check else ("FAILED" if counts.get("failed") else "DONE")
    said = ", ".join("%d %s" % (n, word) for word, n in counts.items()) or "no node"
    if seconds is not None:
        said += " (%s)" % out.duration(seconds)
    return "  ".join(x for x in (verdict, "apply_config", str(cluster or ""), said) if x)


def _recap_warnings(nodes, check, refused, planned, width):
    """The nodes, --check, the refusal, the nodes whose warnings the plan showed, the width -> the recap's warning
    lines: how many the plan showed above (and for which nodes), then the others' (no plan: all of them, the
    refusal too)."""
    planned = [str(n) for n in planned] if planned else None  # (none: no plan; a template renders it "" or None)
    seen = [n for n in nodes or [] if planned is not None and str(n["name"]) in planned]  # their warnings in the plan
    others = [n for n in nodes or [] if n not in seen]
    count = len(cassandra_apply_config_warnings(seen)) + (1 if check and refused and planned is not None else 0)
    warned = [n["name"] for n in seen if any(out.is_warning(x) for x in n.get("notes") or [])]
    also = "the refusal" if check and refused and planned is not None else ""
    lines = [""] + out.warnings_above(count, names=warned, also=also) if count else []
    texts = ["%s: %s" % (w["label"], w["text"]) for w in cassandra_apply_config_warnings(others)]
    if check and refused and planned is None:
        texts.append(["a real run is refused, before changing anything: %s" % refused[0]] + list(refused[1:]))
    return lines + ([""] + out.fit(out.warning_lines(texts), _width(width)) if texts else [])


def cassandra_apply_config_recap(nodes, check=False, cluster="", seconds=None, diff=None, refused=None, planned=None, width=None,
                                 restart_command=""):
    """nodes: [{name, todo, done, os, os_done, then, result, notes}] in inventory order:
    todo, whether the node had config to apply; os, whether its OS settings
    differ; os_done, whether the OS play ran on it; done, whether its turn
    ended well (node_operation's cassandra_op_done); then, its
    cassandra_apply_config_then; result, its cassandra_op_result (said as is
    when not done: skipped, not reached, ...); notes, more lines (a restart
    pending, data dirs another account owns), said whatever the outcome.
    diff: the lines of cassandra_apply_config_diff. Line 1, the verdict as
    the other operations' recaps: "CHECK  apply_config  my_cluster  2 would
    apply, 3 nothing to apply (12s)" (DONE, FAILED when a node failed).
    Under check, then the settings that differ (no question was asked: the
    plan did not show them), a blank line, the outcomes; after a real run
    the outcomes only (its plan showed the settings before its question).
    refused: under check, [why a real run is refused, the way out] (a node not
    running while others are to restart). planned: the nodes whose warnings
    the plan showed (None: no plan): a line says how many are above, and for
    which nodes; the others' warnings (no plan: all of them, refused too) are
    said here, last. width: the outcome lines and the warnings fit in it
    ("terminal": the terminal's; the diff is laid out already).
    restart_command: the rolling_restart command of this cluster (as the run
    named it, no --limit): after a real run, a TO DO with it, for the nodes
    whose read-ahead was set live."""
    nodes = list(nodes or []) if check else [_after_run(n) for n in nodes or []]
    lines, counts = _grouped(nodes, check, width=width)
    shown = [_recap_verdict(counts, check, cluster, seconds)]
    if check and diff:
        shown += [""] + list(diff) + [""]
    lines = lines + _recap_warnings(nodes, check, refused, planned, width)
    return shown + lines + ([] if check else _read_ahead_todo(nodes, restart_command, _width(width)))


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_apply_config_diff": cassandra_apply_config_diff,
            "cassandra_apply_config_outcomes": cassandra_apply_config_outcomes,
            "cassandra_apply_config_recap": cassandra_apply_config_recap,
            "cassandra_apply_config_warnings": cassandra_apply_config_warnings,
        }
