# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The report of an import (import_cluster), and the screen that ends it.

cassandra_import_report: the import's outcome -> the lines of report.txt, or
    with screen true the end of the run (its header, TO DO and SETTINGS).
    One setting per line, then the nodes that have its value ("all" when every
    node read does); a line per other value, "<- differs" on the values fewer
    nodes have, with where the inventory keeps them. Secrets never shown.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import re

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    KEEP, NOT_READ, _chains, _hand_edits_kept, _secret, _sort_key, _values_hidden)
from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

# shown in the header, or the ring's (dc and rack): not among the settings
_NOT_SETTINGS = ("cassandra_cluster_name", "cassandra_dc", "cassandra_rack")
# what LEFT AS IT IS calls each *_manage: false
KEEP_SHORT = {"cassandra_repository_manage": "repositories", "cassandra_cqlsh_python_manage": "cqlsh python",
              "cassandra_linux_manage": "OS settings", "cassandra_service_unit_manage": "unit",
              "cassandra_java_set_default": "system java", "cassandra_firewall_manage": "firewall"}
DASH = u"\u2014"
ARROW_RIGHT = u"\u2192"


def _compress(names):
    """node1, node2, node3, web -> node1..node3, web (three or more in a row)."""
    text = out.nodes(names)
    return re.sub(r"^\d+ nodes: ", "", text)


def _plain(value):
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        return ", ".join(str(v) for v in value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _rows(label_width, settings, everyone, where=None, full=True, width=28):
    """settings: [(name, [(value text, [nodes])])], most nodes first -> the lines of a section: the setting, its
    value and the nodes that have it ("all": every one), a line per other value marked "<- differs" on the values
    fewer nodes have, with where the inventory keeps them. full: every node named (else node1..node3)."""
    lines = []
    for name, values in settings:
        values = sorted(values, key=lambda v: (-len(v[1]), v[0]))
        for i, (text, nodes) in enumerate(values):
            who = "all" if everyone and sorted(nodes) == sorted(everyone) else (
                out.full_list(nodes) if full else _compress(nodes))
            head = ("%s:" % name if i == 0 else "").ljust(label_width)
            line = "  %s %s %s" % (head, text.ljust(width) if len(text) < width else text + " ", who)
            if i > 0 and len(values) > 1:
                kept = (where or {}).get((name, text))
                line += "      %s differs" % out.ARROW + (" (%s)" % kept if kept else "")
            lines.append(line.rstrip())
    return lines


def _by_value(pairs):
    """[(node, name, value text)] -> [(name, [(value text, [nodes])])], in the order the names come."""
    order, found = [], {}
    for node, name, text in pairs:
        if name not in found:
            order.append(name)
            found[name] = {}
        found[name].setdefault(text, []).append(node)
    return [(name, list(found[name].items())) for name in order]


def _width(settings):
    return max([len(name) + 1 for name, dummy in settings] or [0])


def _value_width(settings):
    """The value column: the longest value and 2 spaces, at most 30 (a longer value pushes its nodes only)."""
    return min(max([len(text) for dummy, values in settings for text, dummy2 in values] or [0]) + 2, 30)


def _edit_pairs(node):
    """A node's hand edits the roles would revert -> [(name, value text)]: one per block of lines."""
    dummy, edits = _hand_edits_kept(node)
    out, head, plus, minus = [], None, [], []

    def flush():
        if head is None:
            return
        file_name = re.split(r"[:,]", head, maxsplit=1)[0]
        setting = re.match(r"^\s*(-D[^=\s]+|-X\S+?(?==|$)|[\w.-]+)\s*[:=]?\s*(.*)$", plus[0]) if len(plus) == 1 else None
        if setting and setting.group(1):
            out.append(("%s %s" % (file_name, setting.group(1)), setting.group(2) or "present"))
        elif plus:
            out.append((file_name, " / ".join(plus)))
        elif minus:
            out.append((file_name, "removed: " + " / ".join(minus)))
        else:  # a line of its own (e.g. jmxremote.password's users not imported)
            out.append((file_name, head.split(":", 1)[1].strip() if ":" in head else head))

    for line in edits:
        if not line.startswith(" "):
            flush()
            head, plus, minus = line, [], []
        elif line.strip().startswith("+ "):
            plus.append(line.strip()[2:])
        elif line.strip().startswith("- "):
            minus.append(line.strip()[2:])
        elif head is None:
            head = line
    flush()
    return out


def _os_pair(line):
    for sep in (" = ", ": "):
        if sep in line:
            name, value = line.split(sep, 1)
            return name, value
    return line, "present"


def _what_changes(changed, removed, changes):
    """ (3 new files; changed: a, b; removed: c), '' when not known."""
    if changed is None:
        return ""
    existed = set((changes or {}).get("existed") or [])
    new = [p for p in changed if p not in existed]
    parts = ([out.plural(len(new), "new file")] if new else []) + (
        ["changed: " + ", ".join(p for p in changed if p in existed)] if len(new) < len(changed) else []) + (
        ["removed: " + ", ".join(removed)] if removed else [])
    return " (%s)" % "; ".join(parts) if parts else ""


def _edit_name(name):
    """cassandra.yaml concurrent_writes -> concurrent_writes (the file alone when it is all there is)."""
    return name.split(" ", 1)[1] if " " in name else name


@_values_hidden
def cassandra_import_report(layout, written, report_file, self_check, self_check_ok, secrets_clear=None,
                            leftovers=None, check=False, inventory="", hosts="", allow_unread=False, screen=False,
                            self_check_error="", cwd=None, in_git=None, changes=None):
    """layout: cassandra_inventory_layout's (its nodes); written: [the hosts file, the inventory dir];
    report_file: where report.txt goes; self_check: {node: {differences, notes}}; self_check_ok;
    secrets_clear: the secrets.yml files written in clear; leftovers: cassandra_inventory_leftovers'; check:
    --check; inventory: the -i of the next commands ('' : ansible.cfg's); hosts: their -e cassandra_hosts (''
    when the inventory holds this cluster alone); allow_unread: import_cluster_allow_unread; screen: the end of
    the run only (header, TO DO, SETTINGS); cwd: paths shown relative to it; in_git: the inventory dir is in a
    git work tree (None: looked for); changes: what the run wrote or would write, {changed: [the files written
    that change], removed: [the files removed], existed: [the files there before]}, paths in the inventory dir
    (None: not known). Returns the lines."""
    cluster = layout["cluster_group"]
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    unread = [n for n in nodes if n not in read]
    names = [n["name"] for n in read]
    leftovers = leftovers or {}
    hosts_file, inventory_dir = written
    full = not screen  # report.txt names every node (Q3), the screen node1..node5

    def shown(path):
        return out.path_from(path, cwd)

    with_vars = sorted(h for h, v in (layout.get("host_vars") or {}).items() if v)
    host_dirs = ("host_vars/%s/" % with_vars[0] if len(with_vars) == 1 else
                 "host_vars/<node>/ (%s)" % (out.full_list(with_vars) if full else _compress(with_vars))) \
        if with_vars else ""
    # what changes in the inventory dir (None: not known)
    changed = removed = None
    if changes is not None:
        changed, removed = list(changes.get("changed") or []), list(changes.get("removed") or [])
    same = changed == [] and removed == []
    lines = ["IMPORT %s%s %s %s read / %d %s SELF-CHECK %s" % (
        cluster, " (--check, nothing written)" if check else "", DASH, out.plural(len(read), "node"), len(nodes),
        DASH, "PASSED" if self_check_ok else "FAILED"),
        "%s %s" % ("Unchanged:" if same else "Would write:" if check else "Written:", ", ".join(
            [shown(hosts_file), "group_vars/%s*/" % cluster] + ([host_dirs] if host_dirs else [])))]
    if not screen:  # the screen ends with it
        lines.append("Report:  %s" % ("not written under --check" if check else shown(report_file)))
    lines.append("")

    # TO DO
    edit_names = []
    for n in read:
        edit_names += [_edit_name(name) for name, dummy in _edit_pairs(n) if _edit_name(name) not in edit_names]
    yours = layout.get("yours") or []
    alls = ("group_vars/all", "group_vars/all.yml", "group_vars/all.yaml", "group_vars/all.json")
    standard = bool(yours) and all("/".join(y["path"].split("/")[:2]) in alls for y in yours)
    differs_title = "DIFFERS FROM YOUR group_vars/all" if standard else "DIFFERS FROM YOUR OWN VARIABLES"
    todo = []
    if unread:
        todo.append("Not read: %s: start Cassandra or fix the access, then import again%s" % (
            ", ".join("%s (%s)" % (n["name"], n.get("reason") or "unreachable") for n in unread),
            " (accepted: import_cluster_allow_unread)" if allow_unread else
            " (or -e import_cluster_allow_unread=true)"))
    if edit_names:
        todo.append("Hand edits the roles would revert: %s (see %s)" % (
            ", ".join(edit_names), "HAND EDITS in the report" if screen else "below"))
    others = sorted(name for name, c in (self_check or {}).items() if c.get("differences"))
    if not self_check_ok and (self_check_error or others):
        todo.append("Self-check: %s (see DETAILS%s)" % (self_check_error or "the roles would change settings on "
                                                        + _compress(others), " in the report" if screen else ""))
    if yours:
        todo.append("%s, kept as found: %s (see %s%s)" % (
            "Differs from your group_vars/all" if standard else "Differs from your own variables",
            ", ".join(sorted({y["key"] for y in yours})), differs_title, " below" if screen else ""))
    if layout.get("theirs_win"):
        todo.append("Your files still win over the import there, the roles would change these nodes: %s (rename or"
                    " fix them)" % ", ".join(sorted({"%s: %s (%s)" % (w["node"], w["key"], w["path"])
                                                     for w in layout["theirs_win"]})))
    if layout.get("unread_files"):
        todo.append("Your vaulted files not read (no vault password file): %s: their values are not compared with"
                    " the nodes" % ", ".join(layout["unread_files"]))
    if layout.get("not_compared"):
        todo.append("Not compared with the nodes, check them (yours sets them with a template or a vaulted value,"
                    " or their default is not known here): %s" % ", ".join(layout["not_compared"]))
    if secrets_clear:
        todo.append("Passwords written in clear: cd %s && ansible-vault encrypt %s"
                    % (shown(inventory_dir), " ".join(secrets_clear)))
    if leftovers.get("unsure"):
        todo.append("Files of an earlier import whose cluster is not known, kept: %s (remove them if they are this"
                    " cluster's, or import again with -e import_cluster_adopt=true)" % ", ".join(leftovers["unsure"]))
    git = out.in_git_work_tree(inventory_dir) if in_git is None else in_git
    review = "git diff && git commit" if git else "the files written in %s" % shown(inventory_dir)
    if check and not same:
        todo.append("Write it: the same command without --check%s" % _what_changes(changed, removed, changes))
    elif todo and self_check_ok and not check and not same:
        todo.append("Review then commit:  %s" % review if git else "Review %s" % review)
    if todo:
        lines.append("TO DO (%d)" % len(todo))
        lines += ["  %d. %s" % (i + 1, t) for i, t in enumerate(todo)]
    elif same:
        lines.append("READY %s nothing to change" % DASH)
    else:
        lines.append("READY %s nothing to do; %s" % (DASH, "review and commit:  " + review if git else "review " + review))
    lines.append("")

    # SETTINGS: the variables the nodes read have, not the collection's defaults (where a node has none: default)
    pairs, secrets = [], {}
    from_yours = layout.get("from_yours") or {}
    keys = sorted({k for n in read for k in n.get("vars") or {} if k not in _NOT_SETTINGS}, key=_sort_key)
    for key in keys:
        for n in read:
            mine = (n.get("vars") or {})
            if key not in mine and key in from_yours.get(n["name"], {}):  # left to a file of yours
                mine = {key: from_yours[n["name"]][key]["value"]}
            value = mine.get(key, None)
            if key not in mine:
                text = "(collection's default)"
            elif _secret(key, value):
                seen = secrets.setdefault(key, [])
                marker = json.dumps(value, sort_keys=True, default=str)
                if marker not in seen:
                    seen.append(marker)
                text = "(in secrets.yml)" if seen.index(marker) == 0 else "(in secrets.yml, value %d)" % (seen.index(marker) + 1)
            else:
                text = _plain(value)
            pairs.append((n["name"], key, text))
    settings = _by_value(pairs)
    chains = _chains(layout.get("hosts") or {})

    def placed(key, members):
        """Where the inventory keeps key for these nodes (their most specific level that has it)."""
        found = []
        for m in members:
            if key in (layout.get("host_vars") or {}).get(m, {}):
                place = "host_vars"
            elif key in from_yours.get(m, {}):
                place = from_yours[m][key]["path"]
            else:
                place = next(("group_vars/%s" % g for g in reversed(chains.get(m, []))
                              if key in (layout.get("group_vars") or {}).get(g, {})), "")
            if place and place not in found:
                found.append(place)
        return ", ".join(found)

    where = dict(((key, text), placed(key, members)) for key, values in settings for text, members in values)
    lines.append("SETTINGS %s not the collection's default" % DASH)
    lines += _rows(_width(settings), settings, names, where, full, _value_width(settings)) or ["  none"]
    lines.append("")

    # DIFFERS FROM YOUR group_vars/all: the value found, the nodes, yours, where the import keeps it
    if yours:
        def yours_text(y):
            return "yours: %s (%s)" % ("(in a vars file)" if _secret(y["key"], y["yours"]) else _plain(y["yours"]),
                                       y["path"])

        # by setting, then by the value of yours (one per file of yours), each value found with its nodes
        rows = _by_value([(y["node"], (y["key"], yours_text(y)), "(in secrets.yml)" if _secret(y["key"], y["value"])
                           else _plain(y["value"])) for y in yours])
        lines.append("%s %s kept as found; delete the line to apply %s" % (
            differs_title, DASH, "your standard" if standard else "yours"))
        label = max(len(key) + 1 for (key, dummy), dummy2 in rows) + 1
        width = _value_width(rows)

        def who(members):
            return out.full_list(members) if full else _compress(members)

        who_width = max(len(who(members)) for dummy, values in rows for dummy2, members in values)
        last = None
        for (name, theirs), values in rows:
            lines.append("  %s %s" % (("%s:" % name if name != last else "").ljust(label), theirs))
            last = name
            for text, members in sorted(values, key=lambda v: (-len(v[1]), v[0])):
                lines.append("  %s %s %s   %s kept, in %s" % (
                    "".ljust(label), text.ljust(width) if len(text) < width else text + " ",
                    who(members).ljust(who_width), out.ARROW, placed(name, members) or "?"))
        lines.append("")
    if screen:
        while lines and not lines[-1]:
            lines.pop()
        return lines + ["", "full report: %s" % ("written by the run without --check" if check
                                                 else shown(report_file))]

    # HAND EDITS
    edits = _by_value([(n["name"], name, text) for n in read for name, text in _edit_pairs(n)])
    layout_only = sum(len(n.get("normalized") or []) + len(n.get("comments") or []) for n in read)
    if edits or layout_only:
        lines.append("HAND EDITS %s no variable covers them; cassandra_config would revert" % DASH)
        lines += _rows(_width(edits), edits, names, full=full, width=_value_width(edits))
        if layout_only:
            lines.append("  + %d layout-only edits, no effect (details at the end)" % layout_only)
        lines.append("")

    # LEFT AS IT IS: the parts left to the node as found, grouped by the nodes that have them
    kept, groups = [], {}
    for n in nodes:
        order = list(KEEP_SHORT)
        for k, v in sorted((n.get("keep") or {}).items(),
                           key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0])):
            if k == "cassandra_config_keep_files":
                kept.append((n["name"], "config files with hand edits", ", ".join(v)))
            elif k in KEEP:
                groups.setdefault(KEEP_SHORT.get(k, k), []).append(n["name"])
            else:
                kept.append((n["name"], k, _plain(v)))
    if kept or groups:
        everyone = [n["name"] for n in nodes]
        lines.append("LEFT AS IT IS (*_manage: false)")
        by_nodes = []
        for label, members in groups.items():
            entry = next((e for e in by_nodes if e[1] == sorted(members)), None)
            if entry is None:
                by_nodes.append([[label], sorted(members)])
            else:
                entry[0].append(label)
        label = max([len(", ".join(labels)) + 1 for labels, dummy in by_nodes] or [0])
        for labels, members in by_nodes:
            lines.append("  %s  %s" % (("%s:" % ", ".join(labels)).ljust(label), "all" if members == sorted(everyone)
                                       else out.full_list(members)))
        if kept:
            left = _by_value(kept)
            lines += _rows(_width(left), left, everyone, width=_value_width(left))
        lines.append("")

    # OS TUNING
    tuning = _by_value([(n["name"],) + _os_pair(line) for n in read for line in (n.get("os") or {}).get("lines") or []])
    if tuning:
        lines.append("OS TUNING %s live value (collection's value)" % DASH)
        lines += _rows(_width(tuning), tuning, names, width=_value_width(tuning)) + [""]

    # FILES
    for key, what in (("stale", "Removed (an earlier import's, not written again)"),
                      ("replaced", "Replaced (without the import's first line, the old one kept as <file>.<date>~)"),
                      ("kept", "Kept as they are (not the import's)")):
        if leftovers.get(key):
            lines.append("%s%s: %s" % ("Would be " if check else "", what if not check else what[0].lower() + what[1:],
                                       ", ".join(leftovers[key])))
    if any(leftovers.get(k) for k in ("stale", "replaced", "kept")):
        lines.append("")

    lines += ["NOT READ %s set in group_vars/all if they apply" % DASH, "  " + ", ".join(NOT_READ), ""]
    commands = [(out.command("apply_config", inventory=inventory or None, hosts=hosts or None,
                             extra=["--check", "--diff"], cwd=cwd), "nothing to apply"),
                (out.command("topology", inventory=inventory or None, hosts=hosts or None, extra=["--check"],
                             cwd=cwd), "nothing to do")]
    width = max(len(c) for c, dummy in commands) + 3
    lines += ["NEXT"] + ["  %s%s expect: %s" % (c.ljust(width), ARROW_RIGHT, e) for c, e in commands] + [""]

    # DETAILS: everything above came from, line by line
    lines.append("DETAILS")
    if self_check_error:
        lines.append("  self-check: " + self_check_error)
    for name, c in sorted((self_check or {}).items()):
        if c.get("differences"):
            lines.append("  SELF-CHECK %s: the roles would change" % name)
            lines += ["    " + d for d in c["differences"]]
        lines += ["  %s: %s" % (name, note) for note in c.get("notes") or []]
    lines += ["  " + line if line else "" for line in (layout.get("report") or "").split("\n")]
    lines = [line.rstrip() for line in lines]
    while lines and not lines[-1]:
        lines.pop()
    return lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_import_report": cassandra_import_report}
