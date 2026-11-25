# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""apply_config's view of what changes on the nodes: WILL CHANGE (a real run, before its question) or WOULD CHANGE
(--check, in its recap), the nodes with the same changes together ("the same on all 24 nodes", "on 6 nodes:
node01..node06"), each file under them with what it gets:

    jvm-server.options  rewritten
      + -Xms32730m      moved here from jvm17-server.options
      ~ -Xss1024M       kept, at another line
    cassandra.yaml  rewritten
      concurrent_reads  32 -> 48

+ a line added, - a line removed, ~ a line kept at another line (one row, never an added and a removed row for it),
"a -> b" a value changed. Lines of 79 columns at most. With --diff, each file's unified diff under it, once per group
of nodes with the same diff (DIFF_LINES lines at most, the rest in the .diff file); without, the .diff file named.
The .diff file (cassandra_apply_config_diff_text): every file's unified diff, each once per group of nodes with the
same one. Secrets are masked in both (the role's diffs are)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.filter import cassandra_apply_config as ac
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_secrets import mask

WILL, WOULD = "WILL CHANGE", "WOULD CHANGE"
WIDTH = 79
# the lines of a diff shown on screen under its file (--diff), the rest in the .diff file (no blank line in the view:
# the ops callback draws a rule at one)
DIFF_LINES = 20
LAYOUT_ONLY = "rewritten: comments and layout only"
HEADER_TITLE = 'header "# Managed by Ansible" added (a comment)'
# the files whose lines make the JVM's arguments: a line moved between two of them is said so
JVM_FILES = re.compile(r"^(cassandra-env\.sh|jvm\d*-server\.options)$")


def _text(key, setting, yaml_file):
    """A line of a setting as the view shows it: its line (masked), a YAML key as "key: value"."""
    if yaml_file or setting[2] is None:
        return "%s: %s" % (key, ac._shown_value(key, setting))
    return mask(setting[2])


def _file_rows(name, node):
    """One node's changes in one file -> (verdict, [(mark, text, note)]), None when the file does not change."""
    per_node, new_file, unread = ac._node_settings(name, [node])
    if new_file:
        return "written (it is not there)", []
    if unread:
        return "not read, or changed since it was compared: run apply_config again", []
    if node["name"] not in per_node or name not in node["diffs"]:
        return None
    before, after, (secrets, secret_lines) = per_node[node["name"]]
    yaml_file = name.endswith((".yaml", ".yml"))
    rows = []
    for key in list(after) + [k for k in before if k not in after]:
        if not ac._key_changed(key, before, after, secrets, secret_lines, yaml_file):
            continue
        a, b = before.get(key), after.get(key)
        if a is not None and b is not None:
            live, target = ac._change(ac._shown_value(key, a), ac._shown_value(key, b))
            rows.append((" ", key if yaml_file or a[2] is None else mask(key), "%s -> %s" % (live, target)))
        else:
            rows.append(("+", _text(key, b, yaml_file), "") if b is not None else ("-", _text(key, a, yaml_file), ""))
    # a line removed and added the same: kept, at another line (one row)
    added = [r[1] for r in rows if r[0] == "+"]
    moved = set(r[1] for r in rows if r[0] == "-" and r[1] in added)
    out_rows, said = [], set()
    for mark, text, note in rows:
        if text in moved:
            if text not in said:
                out_rows.append(("~", text, "kept, at another line"))
                said.add(text)
            continue
        out_rows.append((mark, text, note))
    return ("rewritten", out_rows) if out_rows else (LAYOUT_ONLY, [])


def _notes(files, node):
    """The notes of a node's added and removed lines: moved from or to another of its files, there already in
    another file the JVM reads, else added or removed."""
    where = {}
    for name, (verdict, rows) in files.items():
        for mark, text, note in rows:
            where.setdefault((mark, text), []).append(name)
    shown = {}
    for name, (verdict, rows) in files.items():
        mine = []
        for mark, text, note in rows:
            other = [f for f in where.get(("-" if mark == "+" else "+", text), []) if f != name]
            if mark == "+" and other:
                note = "moved here from %s" % other[0]
            elif mark == "-" and other:
                note = "moved to %s" % other[0]
            elif mark == "+":
                there = [f for f, lines in sorted(node["texts"].items())
                         if f != name and JVM_FILES.match(f) and JVM_FILES.match(name)
                         and text in [line.strip() for line in lines]]
                note = "added (%s has it too)" % there[0] if there else "added"
            elif mark == "-":
                note = "removed"
            mine.append((mark, text, note))
        shown[name] = (verdict, tuple(mine))
    return shown


def _changes(node):
    """A node's changes as blocks [(heading, verdict, rows)], in the order of the view; () when none."""
    files = {}
    for name in sorted(set(node["diffs"]) | set(node["wholes"]), key=ac._order):
        if name in node["wholes"]:
            files[name] = ("written whole (its content not shown: secrets)", ())
            continue
        found = _file_rows(name, node)
        if found:
            files[name] = (found[0], tuple(found[1]))
    files = _notes(files, node)
    blocks = []
    for name in sorted(set(files) | set(node["perms"]), key=ac._order):
        verdict, rows = files.get(name, ("", ()))
        if name in node["perms"]:
            rows += ((" ", "owner:group mode", "%s -> %s" % node["perms"][name]),)
        blocks.append((name, verdict, rows))
    for name, (before, after) in sorted(node["dirs"].items()):
        blocks.append((name, "", ((" ", "owner:group mode", "%s -> %s" % (before, after)),)))
    for name, before, after in node["other"]:
        blocks.append((name, "", ((" ", "", "%s -> %s" % (before, after)),)))
    if node["headers"]:
        blocks.append((HEADER_TITLE, "", tuple((" ", f, "") for f in sorted(node["headers"], key=ac._order))))
    if node["medusa"]:
        blocks.append((ac.MEDUSA_SECTION, "", tuple((" ", i, "%s -> %s" % (b, a)) for i, b, a in node["medusa"])))
    for section, title in ac.EXTRA_SECTIONS.items():
        rows = tuple((" ", i, "%s -> %s" % (b, a)) for s, i, b, a in node["extra"] if s == section)
        if rows:
            blocks.append((title, "", rows))
    if node["os"]:  # (a read-ahead in KB)
        blocks.append(("OS settings", "", tuple((" ", label, "%s -> %s" % ((unit or str)(b), (unit or str)(a)))
                                                for (label, unit), b, a in ((ac._os_setting(i), b, a)
                                                                            for i, b, a in node["os"]))))
    return tuple(blocks)


def _row_lines(rows, width):
    """A block's rows: the mark, the line, its note in a column; a note that does not fit on the next line."""
    column = max([len(r[1]) for r in rows if len(r[1]) <= 30] + [0])
    lines = []
    for mark, text, note in rows:
        lead = "    %s " % mark if mark.strip() else "    "
        if not note:
            lines += ac._item_lines(lead + text, width)
            continue
        line = lead + text.ljust(column) + "  " + note
        if len(line) <= width:
            lines.append(line)
        else:
            lines += ac._item_lines(lead + text, width) + out.fit([" " * (len(lead) + 2) + note], width)
    return lines


def _diff_body(diff):
    """A file's unified diff from its first hunk (the header lines of the tool left out)."""
    lines = ac._lines(diff)
    at = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
    return lines[at:]


def _groups(prepared):
    """[(names, shape, {node: blocks})] of the nodes with changes: the nodes whose files get the same (the same files,
    the same verdicts) together, in the nodes' order; their rows may differ (a node's own address)."""
    groups = []
    for node in prepared:
        blocks = _changes(node)
        if not blocks:
            continue
        shape = tuple((name, verdict) for name, verdict, rows in blocks)
        group = next((g for g in groups if g[1] == shape), None)
        if group is None:
            group = ([], shape, {})
            groups.append(group)
        group[0].append(node["name"])
        group[2][node["name"]] = blocks
    return groups


def _by_rows(names, rows_of):
    """[(the nodes, their rows)]: the nodes of a group with the same rows of a block together."""
    found = []
    for name in names:
        rows = rows_of[name]
        group = next((g for g in found if g[1] == rows), None)
        if group:
            group[0].append(name)
        else:
            found.append(([name], rows))
    return found


def _named(names, every, same=True):
    """The nodes of a group as its heading says them."""
    if len(every) > 2 and sorted(names) == sorted(every):
        return ("the same on all %d nodes" if same else "on all %d nodes") % len(every)
    return "on " + out.nodes(names)


def cassandra_apply_config_changes(nodes, check=False, width="terminal", show_diff=False, diff_file="", margin=0,
                                   cwd=None):
    """nodes: as cassandra_apply_config_diff takes them -> the view's lines: its title (WILL CHANGE, WOULD CHANGE
    under check; with the nodes when they are all the same: "WILL CHANGE  the same on all 24 nodes"), then each group
    of nodes whose files get the same, its blocks (a file, what it gets, its rows; the rows of its nodes apart when
    they differ, "on node3") 2 columns in; [] when nothing changes.
    show_diff (--diff): under each file its unified diff, once per group of nodes with the same one, DIFF_LINES at
    most (the rest: the .diff file, diff_file); without, a last line names diff_file. width: the lines fit in it
    ("terminal": the terminal's, WIDTH at most), margin columns less (the caller's indent); a diff line is never cut.
    cwd: diff_file said from it when it is under it."""
    diff_file = out.path_from(diff_file, cwd) if diff_file and cwd else diff_file
    width = min(ac._width(width, margin), WIDTH - margin) if str(width) == "terminal" else ac._width(width, margin)
    width = width or WIDTH
    prepared = [ac._prepare(n) for n in nodes or []]
    by_name = dict((n["name"], n) for n in prepared)
    every = [n["name"] for n in prepared]
    groups = _groups(prepared)
    if not groups:
        return []
    title = WOULD if check else WILL
    lines = []
    for names, shape, blocks_of in groups:
        split = [_by_rows(names, dict((n, blocks_of[n][i][2]) for n in names)) for i in range(len(shape))]
        heading = _named(names, every, same=all(len(s) == 1 for s in split))
        if len(groups) == 1 and heading.startswith(("the same", "on all")):
            title += "  " + heading
        else:
            lines += out.fit([heading], width)
        for (name, verdict), by_rows in zip(shape, split):
            lines += out.fit(["  %s  %s" % (name, verdict) if verdict else "  " + name], width)
            for members, rows in by_rows:
                if len(by_rows) > 1:
                    lines += out.fit(["   on %s:" % out.nodes(members)], width)
                lines += _row_lines(rows, width)
            if show_diff and name in by_name[names[0]]["diffs"]:
                lines += _diff_lines(name, names, every, by_name, diff_file, width)
    if not show_diff and diff_file:
        lines += out.fit(["real diffs: %s" % diff_file], width)
    return [title] + lines


def _diff_lines(name, names, every, by_name, diff_file, width):
    """A file's unified diff under it: once per group of its nodes with the same one, DIFF_LINES at most (its lines
    never cut, the others fit in width)."""
    by_diff = []
    for n in names:
        diff = _diff_body(by_name[n]["diffs"].get(name, ""))
        group = next((g for g in by_diff if g[1] == diff), None)
        if group:
            group[0].append(n)
        else:
            by_diff.append(([n], diff))
    lines = []
    for members, body in by_diff:
        said = ("all %d nodes" % len(every)) if len(every) > 2 and sorted(members) == sorted(every) \
            else out.nodes(members)
        lines += out.fit(["    %s diff on %s:" % ("same" if len(members) > 1 else "its", said)], width)
        lines += ["      " + line for line in body[:DIFF_LINES]]
        if len(body) > DIFF_LINES:
            lines += out.fit(["      (%d more lines: see %s)" % (len(body) - DIFF_LINES, diff_file or "the .diff file")],
                             width)
    return lines


def cassandra_apply_config_diff_text(nodes, cluster="", check=False):
    """nodes: as cassandra_apply_config_diff takes them -> the text of the .diff file: every file's unified diff (masked
    as the role's), once per group of nodes with the same one; "" when no file has one."""
    prepared = [ac._prepare(n) for n in nodes or []]
    every = [n["name"] for n in prepared]
    files = sorted(set(f for n in prepared for f in n["diffs"]), key=ac._order)
    text = []
    for name in files:
        by_diff = []
        for node in prepared:
            if name not in node["diffs"]:
                continue
            diff = _diff_body(node["diffs"][name])
            group = next((g for g in by_diff if g[1] == diff), None)
            if group:
                group[0].append(node["name"])
            else:
                by_diff.append(([node["name"]], diff))
        for members, diff in by_diff:
            said = ("all %d nodes" % len(every)) if len(every) > 2 and sorted(members) == sorted(every) \
                else out.full_list(members)
            text += ["== %s  on %s" % (name, said)] + diff + [""]
    if not text:
        return ""
    head = "apply_config%s %s: the files' diffs (%s), secrets masked" % (
        " --check" if check else "", cluster, "what a real run would write" if check else "what the run writes")
    return "\n".join([head, ""] + text)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_apply_config_changes": cassandra_apply_config_changes,
            "cassandra_apply_config_diff_text": cassandra_apply_config_diff_text,
        }
