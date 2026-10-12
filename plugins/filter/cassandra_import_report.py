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
import os
import re

from ansible.module_utils.parsing.convert_bool import boolean
from ansible.utils.display import Display

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


def _who(nodes, everyone, groups=None, full=True):
    """The nodes of a row: "all" (everyone), else by the node groups when there are some ("G1 + db074"), else every
    name (full) or as ranges."""
    if everyone and sorted(nodes) == sorted(everyone):
        return "all"
    if groups:
        return out.who(nodes, everyone, groups, full=full, counted=False)
    return out.full_list(nodes) if full else _compress(nodes)


def _rows(settings, everyone, where=None, full=True, groups=None, width=None, per_node=False):
    """settings: [(name, [(value text, [nodes])])], most nodes first -> the lines of a section: the setting, its
    value and the nodes that have it ("all": every one), a line per other value marked "<- differs" on the values
    fewer nodes have, with where the inventory keeps them, aligned by cassandra_output's settings_lines.
    full: every node named (else node1..node3); groups: the node groups the nodes are named by; width: the lines
    fit in it; per_node: on a large cluster (GROUPS_FROM nodes), a setting with a value of its own on each of more
    than 5 nodes on one line (the screen)."""
    entries = []
    for name, values in settings:
        rows = []
        values = sorted(values, key=lambda v: (-len(v[1]), v[0]))
        if per_node and len(everyone) >= out.GROUPS_FROM and len(values) > 5 \
                and all(len(nodes) == 1 and text not in ("(collection's default)", "(not set)")
                        for text, nodes in values):  # (a default: said)
            kept = []
            for text, nodes in values:
                place = (where or {}).get((name, text))
                if place and place not in kept:
                    kept.append(place)
            rows.append(("(a value per node)", _who([n for t, ns in values for n in ns], everyone, groups, full),
                         "%s differs" % out.ARROW + (" (%s)" % ", ".join(kept) if kept else "")))
            entries.append((name, rows))
            continue
        for i, (text, nodes) in enumerate(values):
            note = ""
            if i > 0 and len(values) > 1:
                kept = (where or {}).get((name, text))
                note = "%s differs" % out.ARROW + (" (%s)" % kept if kept else "")
            rows.append((text, _who(nodes, everyone, groups, full), note))
        entries.append((name, rows))
    return out.settings_lines(entries, width=width)


def _by_value(pairs):
    """[(node, name, value text)] -> [(name, [(value text, [nodes])])], in the order the names come."""
    order, found = [], {}
    for node, name, text in pairs:
        if name not in found:
            order.append(name)
            found[name] = {}
        members = found[name].setdefault(text, [])
        if node not in members:  # (a node's two edits of the same setting: the node once)
            members.append(node)
    return [(name, list(found[name].items())) for name in order]


def _value_width(settings):
    """The value column: the longest value and 2 spaces, at most 30 (a longer value pushes its nodes only)."""
    return min(max([len(text) for dummy, values in settings for text, dummy2 in values] or [0]) + 2, 30)


_SETTING = re.compile(r"^\s*(-D[^=\s]+|-X\S+?(?==|$)|[\w.-]+)\s*[:=]?\s*(.*)$")


def _edits(node):
    """A node's hand edits the roles would revert -> [(file, setting, value text)], one per block of lines: a block
    that sets one setting (setting: its name), a note on a file of its own (setting None, its text), or other
    lines (setting "", its lines as the value)."""
    dummy, edits = _hand_edits_kept(node)
    found, head, plus, minus = [], None, [], []

    def flush():
        if head is None:
            return
        file_name = re.split(r"[:,]", head, maxsplit=1)[0]
        setting = _SETTING.match(plus[0]) if len(plus) == 1 else None
        if setting and setting.group(1):
            value = setting.group(2) or "present"
            found.append((file_name, setting.group(1), out.MASK if _secret(setting.group(1), value) else value))
        elif plus:
            found.append((file_name, "", " / ".join(plus)))
        elif minus:
            found.append((file_name, "", "removed: " + " / ".join(minus)))
        else:  # a line of its own (e.g. jmxremote.password's users not imported)
            found.append((file_name, None, head.split(":", 1)[1].strip() if ":" in head else head))

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
    return found


def _edit_pairs(node):
    """A node's hand edits -> [(name, value text)]: "file setting" for a setting, else the file."""
    return [("%s %s" % (f, setting) if setting else f, text) for f, setting, text in _edits(node)]


def _hand_edit_rows(read):
    """The HAND EDITS of the nodes read -> [(node, name, value text)]: a setting by its name ("cassandra.yaml
    concurrent_writes") and its value; a file's other edits counted, the same ones on the same nodes ("logback.xml":
    "10 edits"; another set of edits as many: "10 edits (variant 2)"); a note on a file as it is."""
    pairs, variants, files = [], {}, []
    for n in read:
        blocks = {}
        for f, setting, text in _edits(n):
            if setting == "":
                blocks.setdefault(f, []).append(text)
                if f not in files:
                    files.append(f)
            else:
                pairs.append((n["name"], "%s %s" % (f, setting) if setting else f, text))
        for f, texts in blocks.items():
            variants.setdefault(f, []).append((n["name"], tuple(texts)))
    for f in files:
        kinds = []
        for name, texts in variants[f]:
            if texts not in kinds:
                kinds.append(texts)
        for name, texts in variants[f]:
            alike = [k for k in kinds if len(k) == len(texts)]  # as many edits: numbered
            text = out.plural(len(texts), "edit")
            if len(alike) > 1:
                text += " (variant %d)" % (alike.index(texts) + 1)
            pairs.append((name, f, text))
    return pairs


def _os_pair(line):
    for sep in (" = ", ": "):
        if sep in line:
            name, value = line.split(sep, 1)
            return name, value
    return line, "present"


def _paths(paths):
    """Paths, host_vars/<node>/<file> counted when there are more than 3 of a file: "host_vars/<node>/main.yml of 85
    nodes"."""
    per_file, order = {}, []
    for p in paths:
        parts = p.split("/")
        key = ("host_vars/<node>/" + "/".join(parts[2:])) if len(parts) > 2 and parts[0] == "host_vars" else p
        if key not in per_file:
            per_file[key] = []
            order.append(key)
        per_file[key].append(p)
    return ", ".join(("%s of %d nodes" % (key, len(per_file[key]))) if len(per_file[key]) > 3
                     else ", ".join(per_file[key]) for key in order)


def _not_settings(read):
    """[((keys, release), [nodes])]: the cassandra.yaml keys the nodes' Cassandra rejects (not imported), the nodes
    with the same ones together."""
    groups = []
    for n in read:
        found = n.get("not_settings") or {}
        if found.get("keys"):
            what = (tuple(str(k) for k in found["keys"]), str(found.get("release") or "?"))
            entry = next((g for g in groups if g[0] == what), None)
            if entry is None:
                groups.append((what, [n["name"]]))
            else:
                entry[1].append(n["name"])
    return groups


def _what_changes(changed, removed, changes):
    """ (3 new files; changed: a, b; removed: c), '' when not known."""
    if changed is None:
        return ""
    existed = set((changes or {}).get("existed") or [])
    new = [p for p in changed if p not in existed]
    parts = ([out.plural(len(new), "new file")] if new else []) + (
        ["changed: " + _paths(p for p in changed if p in existed)] if len(new) < len(changed) else []) + (
        ["removed: " + _paths(removed)] if removed else [])
    return " (%s)" % "; ".join(parts) if parts else ""


SCREEN_WIDTH = 160  # the screen's width when no width is known (Ansible's display gives one: 79 in a log)
REPORT_WIDTH = 160  # report.txt's


def _terminal_width():
    """The width of the terminal the run shows in: Ansible's (its display's, as the ops callback; a worker has no
    terminal of its own from ansible-core 2.19 on), else the terminal's (a column spared), else COLUMNS, else
    SCREEN_WIDTH."""
    try:
        columns = int(getattr(Display(), "columns", 0) or 0)
    except Exception:  # pylint: disable=broad-except
        columns = 0
    if columns:
        return max(60, columns)
    for fd in (1, 2):
        try:
            return max(60, os.get_terminal_size(fd).columns - 1)
        except (OSError, ValueError):
            continue
    try:
        return max(60, int(os.environ.get("COLUMNS", "")) - 1)
    except ValueError:
        return SCREEN_WIDTH


def _edit_name(name):
    """cassandra.yaml concurrent_writes -> concurrent_writes (the file alone when it is all there is)."""
    return name.split(" ", 1)[1] if " " in name else name


def _settings_pairs(read, keys, from_yours):
    """The SETTINGS of the nodes read -> ([(node, setting, value text)], {setting: its variable}): a dict value one
    setting per sub-key (cassandra_extra_settings.sstable.selected_format), "(not set)" on a node whose dict has
    not that key; a secret "(in secrets.yml)", a second value of it "(in secrets.yml, value 2)"."""
    pairs, secrets, variable = [], {}, {}
    for key in keys:
        found, paths = {}, []
        for n in read:
            mine = (n.get("vars") or {})
            if key not in mine and key in from_yours.get(n["name"], {}):  # left to a file of yours
                mine = {key: from_yours[n["name"]][key]["value"]}
            if key in mine:
                found[n["name"]] = dict(out.leaves(key, mine[key]))
                for path in found[n["name"]]:
                    if path not in paths:
                        paths.append(path)
        for path in paths:
            variable[path] = key
            names = path.split(".")
            for n in read:
                if n["name"] not in found:
                    text = "(collection's default)"
                elif path not in found[n["name"]]:
                    text = "(not set)"
                else:
                    value = found[n["name"]][path]
                    # a sub-key's: its name, its parents' and the whole path; a variable's: its name
                    if _secret(key, value) if path == key else any(_secret(x, value) for x in names + [path]):
                        seen = secrets.setdefault(path, [])
                        marker = json.dumps(value, sort_keys=True, default=str)
                        if marker not in seen:
                            seen.append(marker)
                        text = "(in secrets.yml)" if seen.index(marker) == 0 else \
                            "(in secrets.yml, value %d)" % (seen.index(marker) + 1)
                    else:
                        text = _plain(value)
                pairs.append((n["name"], path, text))
    return pairs, variable


def _host_dirs(with_vars, full):
    """The host_vars written: one dir, a short list, else their count."""
    if not with_vars:
        return ""
    if len(with_vars) == 1:
        return "host_vars/%s/" % with_vars[0]
    listed = out.full_list(with_vars) if full else _compress(with_vars)
    return "host_vars/<node>/ (%s)" % listed if len(listed) <= 40 else "host_vars/<node>/ of %d nodes" % len(with_vars)


@_values_hidden
def cassandra_import_report(layout, written, report_file, self_check, self_check_ok, secrets_clear=None,
                            leftovers=None, check=False, inventory="", hosts="", allow_unread=False, screen=False,
                            self_check_error="", cwd=None, in_git=None, changes=None, todo=None, width=None):
    """layout: cassandra_inventory_layout's (its nodes); written: [the hosts file, the inventory dir];
    report_file: where report.txt goes; self_check: {node: {differences, notes}}; self_check_ok;
    secrets_clear: the secrets.yml files written in clear; leftovers: cassandra_inventory_leftovers'; check:
    --check; inventory: the -i of the next commands ('' : ansible.cfg's); hosts: their -e cassandra_hosts (''
    when the inventory holds this cluster alone); allow_unread: import_cluster_allow_unread; screen: the end of
    the run only (header, TO DO, SETTINGS); cwd: paths shown relative to it; in_git: the inventory dir is in a
    git work tree (None: looked for); changes: what the run wrote or would write, {changed: [the files written
    that change], removed: [the files removed], existed: [the files there before]}, paths in the inventory dir
    (None: not known); todo: more TO DO items (the CQL login); width: the lines fit in it (default: the
    terminal's for the screen, REPORT_WIDTH for the report). Returns the lines.
    From GROUPS_FROM nodes on (cassandra_output), the node lists are by NODE GROUPS defined at the top, the same
    in the screen and the report; DETAILS names every node."""
    cluster = layout["cluster_group"]
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    unread = [n for n in nodes if n not in read]
    names = [n["name"] for n in read]
    everyone = [n["name"] for n in nodes]
    leftovers = leftovers or {}
    hosts_file, inventory_dir = written
    full = not screen  # report.txt names every node (Q3), the screen node1..node5
    width = int(width) if width else (_terminal_width() if screen else REPORT_WIDTH)

    def shown(path):
        return out.path_from(path, cwd)

    # --- the sections' data, then their node groups (the same for the screen and the report) ---
    from_yours = layout.get("from_yours") or {}
    keys = sorted({k for n in read for k in n.get("vars") or {} if k not in _NOT_SETTINGS}, key=_sort_key)
    pairs, variable = _settings_pairs(read, keys, from_yours)
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
        groups_vars = [f for f in found if f.startswith("group_vars/")]
        if len(groups_vars) > 2:  # one per rack...: counted
            found = [f for f in found if f not in groups_vars[1:]]
            found[found.index(groups_vars[0])] = "group_vars of %d groups" % len(groups_vars)
        return ", ".join(found)

    where = dict(((path, text), placed(variable[path], members)) for path, values in settings
                 for text, members in values)
    yours = layout.get("yours") or []
    edits = _by_value(_hand_edit_rows(read))
    kept, left_groups = [], {}
    for n in nodes:
        order = list(KEEP_SHORT)
        for k, v in sorted((n.get("keep") or {}).items(),
                           key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0])):
            if k == "cassandra_config_keep_files":
                kept.append((n["name"], "config files with hand edits", ", ".join(v)))
            elif k in KEEP:
                left_groups.setdefault(KEEP_SHORT.get(k, k), []).append(n["name"])
            else:
                kept.append((n["name"], k, _plain(v)))
    tuning = _by_value([(n["name"],) + _os_pair(line) for n in read
                        for line in (n.get("os") or {}).get("lines") or []])
    others = sorted(name for name, c in (self_check or {}).items() if c.get("differences"))
    sets = [names, others] + [m for section in (settings, edits, tuning, _by_value(kept)) for dummy, values in section
                              for dummy2, m in values]
    found = {}  # the nodes of each value of yours found on them
    for y in yours:
        found.setdefault((y["key"], json.dumps(y["value"], sort_keys=True, default=str)), []).append(y["node"])
    sets += list(found.values())
    sets += list(left_groups.values())
    groups = out.node_groups(sets, everyone)

    # --- header ---
    with_vars = sorted(h for h, v in (layout.get("host_vars") or {}).items() if v)
    host_dirs = _host_dirs(with_vars, full)
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
    if groups:
        lines += out.group_lines(groups, full=full, width=width) + [""]

    # TO DO
    edit_names = []
    for n in read:
        for name, dummy in _edit_pairs(n):
            if _edit_name(name) not in edit_names:
                edit_names.append(_edit_name(name))
    alls = ("group_vars/all", "group_vars/all.yml", "group_vars/all.yaml", "group_vars/all.json")
    standard = bool(yours) and all("/".join(y["path"].split("/")[:2]) in alls for y in yours)
    differs_title = "DIFFERS FROM YOUR group_vars/all" if standard else "DIFFERS FROM YOUR OWN VARIABLES"
    more, todo = list(todo or []), []
    if unread:
        todo.append("Not read: %s: start Cassandra or fix the access, then import again%s" % (
            ", ".join("%s (%s)" % (n["name"], n.get("reason") or "unreachable") for n in unread),
            " (accepted: import_cluster_allow_unread)" if allow_unread else
            " (or -e import_cluster_allow_unread=true)"))
    for (keys, release), members in _not_settings(read):
        todo.append("%s: cassandra.yaml has %s, which Cassandra %s rejects: it would not restart; remove %s" % (
            out.full_list(members) if full else _compress(members), ", ".join(keys), release,
            "it" if len(keys) == 1 else "them"))
    if edit_names:
        todo.append("Hand edits the roles would revert: %s (see %s)" % (
            ", ".join(edit_names), "HAND EDITS in the report" if screen else "below"))
    if not self_check_ok and (self_check_error or others):
        todo.append("Self-check: %s (see DETAILS%s)" % (
            self_check_error or "the roles would change settings on " + _who(others, None, groups, False),
            " in the report" if screen else ""))
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
    todo += more
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
    if screen:  # the verdict line whole
        lines = lines[:1] + out.fit(lines[1:], width)
    lines.append("")

    # SETTINGS: the variables the nodes read have, not the collection's defaults (where a node has none: default)
    lines.append("SETTINGS %s not the collection's default" % DASH)
    lines += _rows(settings, names, where, full, groups, width, per_node=screen) or ["  none"]
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
        width_value = _value_width(rows)

        def differ_who(members):
            return _who(members, None, groups, full)

        who_width = max(len(differ_who(members)) for dummy, values in rows for dummy2, members in values)
        last = None
        block = []
        for (name, theirs), values in rows:
            block.append("  %s %s" % (("%s:" % name if name != last else "").ljust(label), theirs))
            last = name
            for text, members in sorted(values, key=lambda v: (-len(v[1]), v[0])):
                block.append("  %s %s %s   %s kept, in %s" % (
                    "".ljust(label), text.ljust(width_value) if len(text) < width_value else text + " ",
                    differ_who(members).ljust(who_width), out.ARROW, placed(name, members) or "?"))
        lines += (out.fit(block, width) if screen else block) + [""]
    if screen:
        while lines and not lines[-1]:
            lines.pop()
        return lines + ["", "full report: %s" % ("written by the run without --check" if check
                                                 else shown(report_file))]

    # HAND EDITS
    layout_only = sum(len(n.get("normalized") or []) + len(n.get("comments") or []) for n in read)
    if edits or layout_only:
        lines.append("HAND EDITS %s no variable covers them; cassandra_config would revert" % DASH)
        lines += _rows(edits, names, full=full, groups=groups, width=width)
        if layout_only:
            lines.append("  + %d layout-only edits, no effect (details at the end)" % layout_only)
        lines.append("")

    # NOT A CASSANDRA SETTING: cassandra.yaml keys the node's Cassandra rejects, not imported
    rejected = _not_settings(read)
    if rejected:
        lines.append("NOT A CASSANDRA SETTING %s not imported: Cassandra refuses to start with it, cassandra_config"
                     " leaves it out" % DASH)
        width = max(len(k) for (keys, dummy), dummy2 in rejected for k in keys)
        lines += ["  %s  %s (Cassandra %s)" % (k.ljust(width), out.full_list(members), release)
                  for (keys, release), members in rejected for k in keys]
        lines.append("")

    # LEFT AS IT IS: the parts left to the node as found, grouped by the nodes that have them
    if kept or left_groups:
        lines.append("LEFT AS IT IS (*_manage: false)")
        by_nodes = []
        for label, members in left_groups.items():
            entry = next((e for e in by_nodes if e[1] == sorted(members)), None)
            if entry is None:
                by_nodes.append([[label], sorted(members)])
            else:
                entry[0].append(label)
        label = max([len(", ".join(labels)) + 1 for labels, dummy in by_nodes] or [0])
        for labels, members in by_nodes:
            lines.append("  %s  %s" % (("%s:" % ", ".join(labels)).ljust(label), _who(members, everyone, groups, full)))
        if kept:
            lines += _rows(_by_value(kept), everyone, full=full, groups=groups, width=width)
        lines.append("")

    # OS TUNING
    if tuning:
        lines.append("OS TUNING %s live value (collection's value)" % DASH)
        lines += _rows(tuning, names, full=full, groups=groups, width=width) + [""]

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
    width_command = max(len(c) for c, dummy in commands) + 3
    lines += ["NEXT"] + ["  %s%s expect: %s" % (c.ljust(width_command), ARROW_RIGHT, e) for c, e in commands] + [""]

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
