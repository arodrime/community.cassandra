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
    KEEP, _chains, _hand_edits_kept, _secret, _sort_key, _values_hidden)

# shown in the header, or the ring's (dc and rack): not among the settings
_NOT_SETTINGS = ("cassandra_cluster_name", "cassandra_dc", "cassandra_rack")
# looked for by nobody's node: set in group_vars/all where they apply
NOT_READ = ("cassandra_install_url", "cassandra_install_username", "cassandra_java_tarballs",
            "cassandra_java_package", "cassandra_offline")
_NUMBERED = re.compile(r"^(.*?)(\d+)$")


def _compress(names):
    """node1, node2, node3, web -> node1..node3, web (three or more in a row)."""
    out, run = [], []

    def flush():
        if len(run) >= 3:
            out.append("%s..%s" % (run[0][2], run[-1][2]))
        else:
            out.extend(r[2] for r in run)
        del run[:]

    for name in sorted(names, key=lambda n: (_NUMBERED.match(n).group(1), int(_NUMBERED.match(n).group(2)))
                       if _NUMBERED.match(n) else (n, -1)):
        m = _NUMBERED.match(name)
        if m and run and run[-1][0] == m.group(1) and run[-1][1] + 1 == int(m.group(2)):
            run.append((m.group(1), int(m.group(2)), name))
            continue
        flush()
        if m:
            run.append((m.group(1), int(m.group(2)), name))
        else:
            out.append(name)
    flush()
    return ", ".join(out)


def _plain(value):
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        return ", ".join(str(v) for v in value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _rows(label_width, settings, everyone, where=None):
    """settings: [(name, [(value text, [nodes])])], most nodes first -> the lines of a section."""
    lines = []
    for name, values in settings:
        values = sorted(values, key=lambda v: (-len(v[1]), v[0]))
        for i, (text, nodes) in enumerate(values):
            who = "all" if sorted(nodes) == sorted(everyone) else _compress(nodes)
            head = ("%s:" % name if i == 0 else "").ljust(label_width)
            line = "  %s %s %s" % (head, text.ljust(28), who)
            if i > 0 and len(values) > 1:
                kept = (where or {}).get((name, text))
                line += "  <- differs" + (" (%s)" % kept if kept else "")
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


@_values_hidden
def cassandra_import_report(layout, written, report_file, self_check, self_check_ok, secrets_clear=None,
                            leftovers=None, check=False, inventory_args="", allow_unread=False, screen=False,
                            self_check_error=""):
    """layout: cassandra_inventory_layout's (its nodes); written: [the hosts file, the inventory dir];
    report_file: where report.txt goes; self_check: {node: {differences, notes}}; self_check_ok;
    secrets_clear: the secrets.yml files written in clear; leftovers: cassandra_inventory_leftovers'; check:
    --check; inventory_args: what the playbooks run with on this inventory; allow_unread:
    import_cluster_allow_unread; screen: the end of the run only. Returns the lines."""
    cluster = layout["cluster_group"]
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    unread = [n for n in nodes if n not in read]
    names = [n["name"] for n in read]
    leftovers = leftovers or {}
    hosts_file, inventory_dir = written
    lines = ["IMPORT %s%s - %d node(s) read / %d - SELF-CHECK %s" % (
        cluster, " (--check, nothing written)" if check else "", len(read), len(nodes),
        "PASSED" if self_check_ok else "FAILED"),
        "%s %s, group_vars/%s*/, host_vars/<node>/ (%s)" % ("Would write:" if check else "Written:", hosts_file,
                                                            cluster, _compress(sorted(layout.get("host_vars") or {})) or "none"),
        "Report:  %s" % ("not written under --check" if check else report_file), ""]

    # TO DO
    edit_names = []
    for n in read:
        edit_names += [name for name, dummy in _edit_pairs(n) if name not in edit_names]
    todo = []
    if unread:
        todo.append("Not read: %s: start Cassandra or fix the access, then import again%s" % (
            ", ".join("%s (%s)" % (n["name"], n.get("reason") or "unreachable") for n in unread),
            " (accepted: import_cluster_allow_unread)" if allow_unread else
            " (or -e import_cluster_allow_unread=true)"))
    if edit_names:
        todo.append("Hand edits the roles would revert: %s (see HAND EDITS)" % ", ".join(edit_names))
    others = sorted(name for name, c in (self_check or {}).items() if c.get("differences"))
    if not self_check_ok and (self_check_error or others):
        todo.append("Self-check: %s (see DETAILS)" % (self_check_error or "the roles would change settings on "
                                                      + _compress(others)))
    yours = layout.get("yours") or []
    if yours:
        todo.append("Kept as found against your own variables: %s (see DIFFERS FROM YOUR VARIABLES)"
                    % ", ".join(sorted({y["key"] for y in yours})))
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
                    % (inventory_dir, " ".join(secrets_clear)))
    if leftovers.get("unsure"):
        todo.append("Files of an earlier import whose cluster is not known, kept: %s (remove them if they are this"
                    " cluster's, or import again with -e import_cluster_adopt=true)" % ", ".join(leftovers["unsure"]))
    if check:
        todo.append("Write it: the same command without --check")
    elif todo and self_check_ok:
        todo.append("Review then commit:  git diff && git commit")
    if todo:
        lines.append("TO DO (%d)" % len(todo))
        lines += ["  %d. %s" % (i + 1, t) for i, t in enumerate(todo)]
    else:
        lines.append("READY - nothing to do; review and commit:  git diff && git commit")
    lines.append("")

    # SETTINGS: the variables the nodes read have, not the collection's defaults (where a node has none: default)
    pairs, secrets = [], {}
    keys = sorted({k for n in read for k in n.get("vars") or {} if k not in _NOT_SETTINGS}, key=_sort_key)
    for key in keys:
        for n in read:
            value = (n.get("vars") or {}).get(key, None)
            if key not in (n.get("vars") or {}):
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
            else:
                place = next(("group_vars/%s" % g for g in reversed(chains.get(m, []))
                              if key in (layout.get("group_vars") or {}).get(g, {})), "")
            if place and place not in found:
                found.append(place)
        return ", ".join(found)

    where = dict(((key, text), placed(key, members)) for key, values in settings for text, members in values)
    lines.append("SETTINGS - not the collection's default")
    lines += _rows(_width(settings), settings, names, where) or ["  none"]
    if yours:
        rows = _by_value([(y["node"], y["key"], "%s (yours: %s, %s)" % (
            "(in secrets.yml)" if _secret(y["key"], y["value"]) else _plain(y["value"]),
            "(in a vars file)" if _secret(y["key"], y["yours"]) else _plain(y["yours"]), y["path"])) for y in yours])
        lines += ["", "DIFFERS FROM YOUR VARIABLES - kept as found; delete the line to apply yours"]
        lines += [line + "  -> " + placed(name, members) for (name, values) in rows for text, members in values
                  for line in _rows(_width(rows), [(name, [(text, members)])], [])]
    if screen:
        return lines + ["", "Full report: %s" % ("written by the run without --check" if check else report_file)]
    lines.append("")

    # HAND EDITS
    edits = _by_value([(n["name"], name, text) for n in read for name, text in _edit_pairs(n)])
    layout_only = sum(len(n.get("normalized") or []) + len(n.get("comments") or []) for n in read)
    if edits or layout_only:
        lines.append("HAND EDITS - no variable covers them; cassandra_config would revert")
        lines += _rows(_width(edits), edits, names)
        if layout_only:
            lines.append("  + %d layout-only edits, no effect (details at the end)" % layout_only)
        lines.append("")

    # LEFT AS IT IS
    kept = []
    for n in nodes:
        for k, v in sorted((n.get("keep") or {}).items()):
            if k == "cassandra_config_keep_files":
                kept.append((n["name"], "config files with hand edits", ", ".join(v)))
            else:
                kept.append((n["name"], KEEP.get(k, k), "not managed" if k in KEEP else _plain(v)))
    if kept:
        left = _by_value(kept)
        lines.append("LEFT AS IT IS (*_manage: false)")
        lines += _rows(_width(left), left, [n["name"] for n in nodes]) + [""]

    # OS TUNING
    tuning = _by_value([(n["name"],) + _os_pair(line) for n in read for line in (n.get("os") or {}).get("lines") or []])
    if tuning:
        lines.append("OS TUNING - live value (collection's value)")
        lines += _rows(_width(tuning), tuning, names) + [""]

    # FILES
    for key, what in (("stale", "Removed (an earlier import's, not written again)"),
                      ("replaced", "Replaced (without the import's first line, the old one kept as <file>.<date>~)"),
                      ("kept", "Kept as they are (not the import's)")):
        if leftovers.get(key):
            lines.append("%s%s: %s" % ("Would be " if check else "", what if not check else what[0].lower() + what[1:],
                                       ", ".join(leftovers[key])))
    if any(leftovers.get(k) for k in ("stale", "replaced", "kept")):
        lines.append("")

    lines += ["NOT READ - set in group_vars/all if they apply", "  " + ", ".join(NOT_READ), ""]
    lines += ["NEXT",
              "  ansible-playbook %s community.cassandra.apply_config --check --diff   -> expect: nothing to apply"
              % inventory_args,
              "  ansible-playbook %s community.cassandra.topology --check              -> expect: nothing to do"
              % inventory_args, ""]

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
