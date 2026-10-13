# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The report of an import (import_cluster), and the screen that ends it.

cassandra_import_report: the import's outcome -> the lines of report.txt, or
    with screen true the end of the run (its header, TO DO and SETTINGS).
    One setting per line, then the nodes that have its value ("all" when every
    node read does); a line per other value, "<- differs" on the values fewer
    nodes have, with where the inventory keeps them. Secrets never shown.
cassandra_import_self_check_advice: the end of the run on a failed
    self-check: what differs, grouped by what makes it pass, and how (report.txt
    has the same in its SELF-CHECK section, each node's differences under it).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ast
import json
import re
import textwrap

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    KEEP, NOT_READ, _chains, _hand_edits_kept, _secret, _sort_key, _values_hidden)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import HEAP_FILE
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_nodes import CONFIG_FILE
from ansible_collections.community.cassandra.plugins.filter.cassandra_suspicious import (
    LINES as SUSPICIOUS_LINES, how_to_keep, suspicious)
from ansible_collections.community.cassandra.plugins.filter.cassandra_output import terminal_width
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


def _nodes(nodes, everyone, groups=None, full=True):
    """_who, "all nodes" for every one."""
    who = _who(nodes, everyone, groups, full)
    return "all nodes" if who == "all" else who


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


def _jvm_warnings(read):
    """[(warning, [nodes])]: the JVM options worth a look (cassandra_config_import's jvm_warnings), the nodes with
    the same one together."""
    groups = []
    for n in read:
        for text in n.get("jvm_warnings") or []:
            entry = next((g for g in groups if g[0] == text), None)
            if entry is None:
                groups.append((text, [n["name"]]))
            elif n["name"] not in entry[1]:
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


REPORT_WIDTH = 160  # report.txt's


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


# a self-check difference (cassandra_import_check's, values masked there): "<file>: <setting>: node has <value>,
# import would write <value>", "<file>: node has <line>, import would write <line>" for a file compared line by line,
# "<file>: not on the node, the roles would create it", else a line of its own (an error)
_LINES = re.compile(r"^(cassandra-env\.sh|[^:\s]+\.xml): node has (.*), import would write (.*)$", re.S)
_SETTING_DIFFERENCE = re.compile(r"^(%s|[^:\s]+): (.+?): node has (.*), import would write (.*)$" % re.escape(HEAP_FILE),
                                 re.S)
_CREATED = re.compile(r"^([^:\s]+): not on the node, the roles would create it$")
# the kinds of differences, by what makes the self-check pass, in the order shown
_HAND, _YOURS, _OTHER = "hand", "yours", "other"
_KINDS = ((_HAND, "Hand edits no variable covers (cassandra_config would revert them)"),
          (_YOURS, "Your inventory files override the import (Ansible will apply your value)"),
          (_OTHER, "Other differences"))
SCREEN_VALUE = 50  # a value longer than this is cut on the screen (whole in the report)
SCREEN_MOST = 100  # the stop's lines at most (the ops callback's rules)
SELF_CHECK_WIDTH = 110  # the report's SELF-CHECK section


def _hand_files(read):
    """{node: the config files with hand edits import_cluster_keep_hand_edits would keep as the node has them}."""
    files_of = {}
    for n in read:
        kept = (n.get("keep") or {}).get("cassandra_config_keep_files") or []
        heads = [re.split(r"[:,]", line, maxsplit=1)[0] for line in n.get("hand_edits") or []
                 if not line.startswith(" ")]
        files_of[n["name"]] = set(f for f in heads if CONFIG_FILE.match(f) and f not in kept)
    return files_of


def _letters(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _winner(name, setting, theirs_win):
    """The file of yours that still wins for a node's setting ({node, key, path} of the layout's theirs_win): the
    variable named as the setting, or its last part (cassandra_concurrent_reads: concurrent_reads,
    cassandra_service_limit_nofile: [Service] LimitNOFILE), else None."""
    names = set([_letters(setting), _letters(re.split(r"[.\]]", setting)[-1])]) - set([""])
    return next((w for w in theirs_win or []
                 if w["node"] == name and _letters(re.sub(r"^cassandra_", "", w["key"])) in names), None)


def _difference(text):
    """A self-check difference -> (file, setting, node's value, the roles' value): setting "" for a line of a file
    compared line by line (cassandra-env.sh, logback.xml); None for a file the roles would create (no values), or
    another line (an error: what comes after its file, or its first ": ", as the node's value)."""
    found = _LINES.match(text)
    if found:
        return found.group(1), "", found.group(2), found.group(3)
    found = _SETTING_DIFFERENCE.match(text)
    if found:
        f, setting, node_text, role_text = found.groups()
        return ("heap" if f == HEAP_FILE else f), setting, node_text, role_text
    found = _CREATED.match(text)
    if found:
        return found.group(1), None, None, None
    head, sep, rest = text.partition(": ")  # "cassandra.yaml: cannot be read...", "owner, group and mode: not read..."
    return (head, None, rest, None) if sep else ("", None, text, None)


@_values_hidden
def _self_check_items(read, self_check, theirs_win=None):
    """The self-check differences -> [{kind, file, setting, text, pairs: [(node, node's value, the roles')], lines:
    [(node, its difference)], wins: {node: its theirs_win entry}}], one per setting (or line, or file) of the same
    kind, with the nodes that have it. Kind _HAND in a config file with hand edits import_cluster_keep_hand_edits
    would keep (its owner, group and mode aside; not a file the node has not), _YOURS where a file of yours still
    wins over the import (theirs_win), else _OTHER. Values as the self-check gives them (secrets masked), masked
    again."""
    files_of = _hand_files(read)
    items, found = [], {}
    for name, c in sorted((self_check or {}).items()):
        for d in c.get("differences") or []:
            f, setting, node_text, role_text = _difference(d)
            real = "cassandra-env.sh" if f == "heap" else f
            winner = _winner(name, setting, theirs_win) if setting else None
            created = setting is None and node_text is None  # (not one import_cluster_keep_hand_edits keeps)
            if real in files_of.get(name, ()) and not d.startswith(f + ": owner") and not created:
                kind = _HAND
            elif winner:
                kind = _YOURS
            else:
                kind = _OTHER
            if created:
                text = "not on the node, the roles would create it"
            elif setting is None:
                text = node_text
            elif setting:
                text = ""
            elif node_text != "nothing":
                text = node_text
            else:
                text = "not on the node: " + role_text
            key = (kind, f, setting, text)
            if key not in found:
                found[key] = {"kind": kind, "file": f, "setting": setting, "text": out.mask(text), "pairs": [],
                              "lines": [], "wins": {}}
                items.append(found[key])
            item = found[key]
            if setting:
                node_text, role_text = _unquoted(node_text), _unquoted(role_text)
                secret = _secret(setting, node_text) or _secret(setting, role_text)
                item["pairs"].append(tuple([name] + [None if v is None else out.MASK if secret else out.mask(v)
                                                     for v in (node_text, role_text)]))
            item["lines"].append((name, out.mask(d)))
            if winner:
                item["wins"][name] = winner
    order = [k for k, dummy in _KINDS]
    return sorted(items, key=lambda i: order.index(i["kind"]))


def _unquoted(text):
    """A value as the self-check quotes a string ('64') -> 64, '' as is; "nothing" (none on that side) -> None."""
    if text == "nothing":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        return (value or "''") if isinstance(value, str) else text
    return text


def _item_nodes(item):
    names = []
    for name, dummy in item["lines"]:
        if name not in names:
            names.append(name)
    return names


def _changes(items):
    """the inventory would change 3 settings on 2 nodes."""
    on = set(n for i in items for n in _item_nodes(i))
    return "the inventory would change %s on %s" % (out.plural(len(items), "setting"), out.plural(len(on), "node"))


def _cut(text, room):
    text = str(text)
    return text if not room or len(text) <= room else text[:room - 3].rstrip() + "..."


def _item_lines(item, who, width, room=None):
    """An item: "- <name>, on <nodes>:", then what differs under it ("node 64, inventory 32", the line the node has,
    "-XX:+UseFoo (not in the inventory)"), a line per value when the nodes differ ("node3: node 128, ..."), where a
    file of yours sets it; room: a value or a line cut to it (the screen), a sentence never."""
    def value(node_text, role_text):
        if role_text is None:
            return "%s (not in the inventory)" % _cut(node_text, room)
        if node_text is None:
            return "not on the node, inventory %s" % _cut(role_text, room)
        return "node %s, inventory %s" % (_cut(node_text, room), _cut(role_text, room))

    if item["setting"]:
        values = []
        for name, node_text, role_text in item["pairs"]:
            entry = next((v for v in values if v[0] == (node_text, role_text)), None)
            if entry is None:
                values.append(((node_text, role_text), [name]))
            elif name not in entry[1]:
                entry[1].append(name)
        details = [value(*pair) for pair, names in values] if len(values) == 1 else \
            ["%s: %s" % (who(names), value(*pair)) for pair, names in values]
    else:
        details = [_cut(item["text"], room if item["setting"] == "" else None)]
    wins = sorted(set(w["path"] for w in item["wins"].values()))
    if wins:
        details[-1] += " (set in %s)" % ", ".join(wins)
    lines = _fill("- %s, on %s:" % (_item_name(item), who(_item_nodes(item))), width, "    ", "    ")
    for detail in details:
        lines += _fill(detail, width, "        ", "  ")
    return lines


def _item_name(item):
    """cassandra.yaml concurrent_reads; a JVM option's file alone when its values name it (-XX:+UseFoo; not ****)."""
    named = item["setting"] and not (item["file"].endswith(".options") and all(
        v is None or v.startswith("-") for dummy, n, r in item["pairs"] for v in (n, r)))
    return out.mask("%s %s" % (item["file"], item["setting"]) if named else item["file"] or "?")


def _column(names, most=40):
    """The column of names (their texts right of it): the longest, at most most wide (a longer one pushes its text)."""
    return max([len(name) for name in names if len(name) <= most] or [0]) + 3


# (not cassandra_output's flow and laid_out: those lay a line out where the callback prints it, the ops callback's
# help only; these lines are laid out here, the same under any callback and in report.txt)
def _aligned(rows, indent, width, column=None):
    """[(name, text)] -> lines: the names in a column (_column's by default), the texts on over lines under their
    column when longer than width."""
    column = column or _column([name for name, dummy in rows])
    lines = []
    for name, text in rows:
        head = indent + name.ljust(column) if len(name) < column else indent + name + "   "
        hang = " " * (len(indent) + column)
        if not width or len(head) + len(text) <= width:
            lines.append((head + text).replace(out.NBSP, " ").rstrip())
            continue
        wrapped = textwrap.wrap(text, max(20, width - len(head)), break_long_words=False, break_on_hyphens=False)
        lines.append((head + (wrapped[0] if wrapped else "")).replace(out.NBSP, " ").rstrip())
        lines += _fill(" ".join(wrapped[1:]), width, hang)
    return lines


def _whole(text):
    """text never cut by _fill and _aligned (a command, an option and its value)."""
    return text.replace(" ", out.NBSP)


def _fill(text, width, hang, more=""):
    """text on lines of width, from column hang, its next lines more further in (a word longer than the room stays
    whole)."""
    if not text:
        return []
    return [line.replace(out.NBSP, " ") for line in textwrap.wrap(
        text, max(20, width), initial_indent=hang, subsequent_indent=hang + more, break_long_words=False,
        break_on_hyphens=False) or [hang + text]]


def _hints(layout, items, read, unread, who, error="", report=False, report_file="", inventory="", hosts="",
           cwd=None):
    """HOW TO MAKE IT PASS: [(kind of difference, what to do)]."""
    hints, hand = [], {}
    for item in items:
        if item["kind"] == _HAND:
            f = "cassandra-env.sh" if item["file"] == "heap" else item["file"]
            hand.setdefault(f, [])
            hand[f] += [n for n in _item_nodes(item) if n not in hand[f]]
    if hand:
        hints.append(("hand edits", "to keep them as they are: import again with %s; these files are then NOT"
                      " managed by Ansible (kept as the node has them): %s. To bring them under Ansible: carry the"
                      " edit in the inventory (cassandra_jvm*_extra_options, cassandra_env_extra_lines and"
                      " cassandra_logback_extra_lines for lines at the end of the file, cassandra_extra_settings, ...)"
                      " or give the file your own template (cassandra_config_template_overrides), remove the file"
                      " from cassandra_config_keep_files, import again. To drop them instead: undo them on the node,"
                      " then import again" % (
                          _whole("-e import_cluster_keep_hand_edits=true"),
                          "; ".join("%s on %s" % (f, who(m)) for f, m in sorted(hand.items())))))
    yours = {}  # (the file of yours, its variable): the nodes' values
    for item in items:
        for w in item["wins"].values():
            value = next((y["value"] for y in layout.get("yours") or []
                          if y["node"] == w["node"] and y["key"] == w["key"]), None)
            shown = None if value is None else out.MASK if _secret(w["key"], value) else _plain(value)
            entry = yours.setdefault((w["path"], w["key"]), [])
            if shown not in entry:
                entry.append(shown)
    for (path, key), values in sorted(yours.items()):
        hints.append(("your files", "in %s, set %s to the node's value (%s) or remove the line, then import again" % (
            path, key, values[0] if len(values) == 1 and values[0] is not None
            else "see the DIFFERS FROM YOUR... section")))
    if unread and not read:  # (no node checked: never valid, import_cluster_allow_unread or not)
        hints.append(("not read", "no node could be read: start Cassandra or fix the access on one of them at least,"
                      " then import again"))
    elif unread:
        hints.append(("not read", "start Cassandra or fix the access on %s, then import again; or accept %s"
                      " unchecked: %s" % (who([n["name"] for n in unread]), "it" if len(unread) == 1 else "them",
                                          _whole("-e import_cluster_allow_unread=true"))))
    debug = _whole("-e import_cluster_debug=true")
    if error:
        hints.append(("could not run", "import again with %s to see the error (it may show passwords), fix it, then"
                      " import again" % debug))
    if any(i["kind"] == _OTHER for i in items):
        if report:
            where = "each node's lines above"
        elif report_file:
            where = "the details (%s, section SELF-CHECK)" % out.path_from(report_file, cwd)
        else:
            where = "the report's SELF-CHECK section"
        hints.append(("other", "not sorted out here: see %s; change the node, or take it as a planned change"
                      " (below)" % where))
    if items:
        apply_check = out.command("apply_config", inventory=inventory or None, hosts=hosts or None,
                                  extra=["--check", "--diff"], cwd=cwd)
        hints.append(("a change you want", "it is a planned change: import again with %s (it only writes the"
                      " inventory), then review with %s before applying"
                      % (_whole("-e import_cluster_strict=false"), _whole(apply_check))))
    return hints or [("unknown", "import again with %s to see what failed (it may show passwords)" % debug)]


def _self_check_lines(layout, self_check, who, width, report_file="", error="", allow_unread=False,
                      inventory="", hosts="", cwd=None, report=False):
    """A failed self-check -> (its verdict, [paragraphs of lines]): what differs, grouped by what makes it pass
    (report: each node's differences under each item, the values whole), then HOW TO MAKE IT PASS."""
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    unread = [n for n in nodes if n not in read] if not allow_unread or not read else []
    items = _self_check_items(read, self_check, layout.get("theirs_win"))
    parts = [_changes(items)] if items else []
    if error:
        parts.append("it could not run")
    if unread:
        parts.append("no node read" if not read else "%s not read" % out.plural(len(unread), "node"))
    verdict = "; ".join(parts) or "nothing says the inventory reproduces the nodes"
    room = None if report else SCREEN_VALUE
    differs = []
    if error:
        differs += _fill(error[0].upper() + error[1:], width, "  ")
    for kind, title in _KINDS:
        mine = [i for i in items if i["kind"] == kind]
        if not mine:
            continue
        differs += _fill("%s:" % title, width, "  ", "  ")
        for item in mine:
            differs += _item_lines(item, who, width, room)
            if report:  # each node's line, the same line once for its nodes
                same = {}
                for name, d in item["lines"]:
                    if name not in same.setdefault(d, []):
                        same[d].append(name)
                for d, names in same.items():
                    differs += _fill("%s: %s" % (who(names), d), width, "          ", "  ")
    if unread:
        differs += _fill("Not read, so not checked: %s" % ", ".join(
            "%s (%s)" % (n["name"], n.get("reason") or "unreachable") for n in unread), width, "  ")
    hints = _hints(layout, items, read, unread, who, error=error, report=report, report_file=report_file,
                   inventory=inventory, hosts=hosts, cwd=cwd)
    paragraphs = [differs, ["HOW TO MAKE IT PASS"] + _aligned(hints, "  ", width)]
    if not report and report_file:
        paragraphs.append(["Details: %s, section SELF-CHECK" % out.path_from(report_file, cwd)])
    return verdict, paragraphs


@_values_hidden
def cassandra_import_self_check_advice(layout, self_check, report_file="", error="", allow_unread=False, check=False,
                                       inventory="", hosts="", cwd=None, width=None):
    """layout: cassandra_inventory_layout's; self_check: {node: {differences, notes}}; report_file: the report
    (report.check.txt under --check); error: why the self-check could not run ('' : it ran); allow_unread:
    import_cluster_allow_unread (the nodes not read pass); check: --check; inventory, hosts, cwd: the next
    commands' -i, -e cassandra_hosts and current dir (cassandra_import_report's); width: the lines fit in it (the
    terminal's by default, SCREEN_MOST at most) -> the lines of the run's stop on a failed self-check:
    "SELF-CHECK FAILED  <cluster group>  ...", what differs grouped by what makes it pass, HOW TO MAKE IT PASS,
    where the details are; from GROUPS_FROM nodes on (cassandra_output), the node lists by the NODE GROUPS of the
    summary above it, defined again under the verdict. Blank lines between them. Secrets never shown."""
    width = int(width) if width else min(terminal_width(), SCREEN_MOST)
    groups = _sections(layout, self_check)["groups"]  # (the summary's, just above)
    verdict, paragraphs = _self_check_lines(
        layout, self_check, lambda m: _who(m, None, groups, False), width, report_file=report_file, error=error,
        allow_unread=boolean(allow_unread, strict=False), inventory=inventory, hosts=hosts, cwd=cwd)
    head = "SELF-CHECK FAILED  %s  %s %s" % (layout["cluster_group"], verdict, _whole(
        "(--check: the inventory not written)" if boolean(check, strict=False)
        else "(written for inspection only, marked NOT VALID)"))
    head = [line.replace(out.NBSP, " ") for line in out.fit([head], width)]
    blocks = [head] + ([out.group_lines(groups, width=width)] if groups else []) + paragraphs
    lines = []
    for block in blocks:
        lines += ([""] if lines else []) + block
    return lines


def _sections(layout, self_check):
    """The data of the report's sections and their node groups (the same for the screen, the report and the stop on a
    failed self-check): {settings, variable, edits, kept, left_groups, tuning, check_items, groups}."""
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    names = [n["name"] for n in read]
    everyone = [n["name"] for n in nodes]
    from_yours = layout.get("from_yours") or {}
    keys = sorted({k for n in read for k in n.get("vars") or {} if k not in _NOT_SETTINGS}, key=_sort_key)
    pairs, variable = _settings_pairs(read, keys, from_yours)
    settings = _by_value(pairs)
    yours = layout.get("yours") or []
    edits = _by_value(_hand_edit_rows(read))
    kept, left_groups = [], {}
    for n in nodes:
        order = list(KEEP_SHORT)
        for k, v in sorted((n.get("keep") or {}).items(),
                           key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0])):
            if k == "cassandra_config_keep_files":
                kept.append((n["name"], "kept as is (hand edits the collection's variables cannot express yet)", ", ".join(v)))
            elif k in KEEP:
                left_groups.setdefault(KEEP_SHORT.get(k, k), []).append(n["name"])
            else:
                kept.append((n["name"], k, _plain(v)))
    tuning = _by_value([(n["name"],) + _os_pair(line) for n in read
                        for line in (n.get("os") or {}).get("lines") or []])
    others = sorted(name for name, c in (self_check or {}).items() if c.get("differences"))
    check_items = _self_check_items(read, self_check, layout.get("theirs_win"))
    sets = [names, others] + [m for section in (settings, edits, tuning, _by_value(kept)) for dummy, values in section
                              for dummy2, m in values]
    sets += [_item_nodes(i) for i in check_items]
    found = {}  # the nodes of each value of yours found on them
    for y in yours:
        found.setdefault((y["key"], json.dumps(y["value"], sort_keys=True, default=str)), []).append(y["node"])
    sets += list(found.values())
    sets += list(left_groups.values())
    return {"settings": settings, "variable": variable, "edits": edits, "kept": kept, "left_groups": left_groups,
            "tuning": tuning, "check_items": check_items, "groups": out.node_groups(sets, everyone)}


def cassandra_import_report(layout, written, report_file, self_check, self_check_ok, secrets_clear=None,
                            leftovers=None, check=False, inventory="", hosts="", allow_unread=False, screen=False,
                            self_check_error="", cwd=None, in_git=None, changes=None, todo=None, width=None,
                            strict=False, convergence=None):
    """layout: cassandra_inventory_layout's (its nodes); written: [the hosts file, the inventory dir];
    report_file: where report.txt goes (report.check.txt under --check); self_check: {node: {differences, notes}}; self_check_ok;
    secrets_clear: the secrets.yml files written in clear; leftovers: cassandra_inventory_leftovers'; check:
    --check; inventory: the -i of the next commands ('' : ansible.cfg's); hosts: their -e cassandra_hosts (''
    when the inventory holds this cluster alone); allow_unread: import_cluster_allow_unread; screen: the end of
    the run only (header, TO DO, SETTINGS); cwd: paths shown relative to it; in_git: the inventory dir is in a
    git work tree (None: looked for); changes: what the run wrote or would write, {changed: [the files written
    that change], removed: [the files removed], existed: [the files there before]}, paths in the inventory dir
    (None: not known); todo: more TO DO items (the CQL login); width: the lines fit in it (default: the
    terminal's for the screen, REPORT_WIDTH for the report); strict: import_cluster_strict (the run stops on a failed
    self-check, its HOW TO MAKE IT PASS below the screen); convergence: the lines of the CONVERGENCE section
    (cassandra_import_convergence's, the screen's or the report's), before where the full report is on the screen,
    before NOT READ in the report. Returns the lines.
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
    width = int(width) if width else (terminal_width() if screen else REPORT_WIDTH)

    def shown(path):
        return out.path_from(path, cwd)

    # --- the sections' data, then their node groups (the same for the screen, the report and the stop) ---
    from_yours = layout.get("from_yours") or {}
    data = _sections(layout, self_check)
    settings, variable, edits, kept = data["settings"], data["variable"], data["edits"], data["kept"]
    left_groups, tuning, check_items, groups = data["left_groups"], data["tuning"], data["check_items"], data["groups"]
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
    others = sorted(name for name, c in (self_check or {}).items() if c.get("differences"))

    # --- header ---
    with_vars = sorted(h for h, v in (layout.get("host_vars") or {}).items() if v)
    host_dirs = _host_dirs(with_vars, full)
    # what changes in the inventory dir (None: not known)
    changed = removed = None
    if changes is not None:
        changed, removed = list(changes.get("changed") or []), list(changes.get("removed") or [])
    same = changed == [] and removed == []
    lines = ["IMPORT %s%s %s %s read / %d %s SELF-CHECK %s" % (
        cluster, " (--check, inventory not written)" if check else "", DASH, out.plural(len(read), "node"), len(nodes),
        DASH, "PASSED" if self_check_ok else "FAILED"),
        "%s %s" % ("Unchanged:" if same else "Would write:" if check else "Written:", ", ".join(
            [shown(hosts_file), "group_vars/%s*/" % cluster] + ([host_dirs] if host_dirs else [])))]
    if not screen:  # the screen ends with it
        lines.append("Report:  %s" % shown(report_file))
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
            _who(members, None, groups, full), ", ".join(keys), release,
            "it" if len(keys) == 1 else "them"))
    # sensitive or suspicious settings found on the nodes (kept as they are)
    # (every node read is a member of the ring; its hand edits are checked too: a replace flag in cassandra-env.sh)
    found = suspicious(dict((n["name"], dict(n.get("vars") or {}, **{SUSPICIOUS_LINES: [
        e[4:] for e in n.get("hand_edits") or [] if e.startswith("  + ")]})) for n in read), [n["name"] for n in read])
    for row, names, setting, why in found:
        todo.append("WARNING %s: %s, kept as found %s %s (%s)"
                    % (_who(names, None, groups, full), setting, DASH, why, how_to_keep(row)))
    for text, members in _jvm_warnings(read):
        todo.append("WARNING  %s: %s" % (_who(members, None, groups, full), text))
    moves = sorted({f for n in read for f in n.get("rewritten") or []})
    if moves:
        todo.append("cassandra-env.sh hand edits moved to variables (see MOVED%s): apply_config rewrites %s and"
                    " restarts %s (same JVM arguments)" % (" in the report" if screen else "", ", ".join(moves),
                                                           _nodes([n["name"] for n in read if n.get("rewritten")],
                                                                  [n["name"] for n in read], groups, full)))
    if edit_names:
        todo.append("Hand edits the roles would revert: %s (see %s)" % (
            ", ".join(edit_names), "HAND EDITS in the report" if screen else "below"))
    if not self_check_ok and (self_check_error or others):
        todo.append("Self-check: %s (how to make it pass: %s)" % (
            self_check_error or _changes(check_items),
            "SELF-CHECK below" if not screen else "SELF-CHECK FAILED below" if strict else "SELF-CHECK in the report"))
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
        if convergence:
            lines += [""] + list(convergence)
        return lines + ["", "full report%s: %s" % (" (--check)" if check else "", shown(report_file))]

    # SELF-CHECK: what the roles would change, grouped by what makes it pass, each node's differences under it
    if not self_check_ok:
        verdict, paragraphs = _self_check_lines(
            layout, self_check, lambda m: _who(m, None, groups, full), min(width, SELF_CHECK_WIDTH),
            report_file=report_file, error=self_check_error, allow_unread=allow_unread, inventory=inventory,
            hosts=hosts, cwd=cwd, report=True)
        lines.append("SELF-CHECK %s FAILED: %s" % (DASH, verdict))
        lines += [line for p in paragraphs for line in p + [""]]

    # HAND EDITS
    layout_only = sum(len(n.get("normalized") or []) + len(n.get("comments") or []) for n in read)
    if edits or layout_only:
        lines.append("HAND EDITS %s no variable covers them; cassandra_config would revert" % DASH)
        lines += _rows(edits, names, full=full, groups=groups, width=width)
        if layout_only:
            lines.append("  + %d layout-only edits, no effect (details at the end)" % layout_only)
        not_moved = {}  # why cassandra-env.sh's JVM_OPTS lines or heap stay hand edits: its nodes
        for n in read:
            if n.get("not_moved"):
                not_moved.setdefault(n["not_moved"], []).append(n["name"])
        for why, who in sorted(not_moved.items()):
            lines += out.fit(["  cassandra-env.sh JVM_OPTS lines, heap: not moved to variables, %s (%s)" % (
                why, _who(who, names, groups, full))], width)
        lines.append("")

    # NOT A CASSANDRA SETTING: cassandra.yaml keys the node's Cassandra rejects, not imported
    rejected = _not_settings(read)
    if rejected:
        lines.append("NOT A CASSANDRA SETTING %s not imported: Cassandra refuses to start with it, cassandra_config"
                     " leaves it out" % DASH)
        key_width = max(len(k) for (keys, dummy), dummy2 in rejected for k in keys)
        lines += ["  %s  %s (Cassandra %s)" % (k.ljust(key_width), _who(members, None, groups, True), release)
                  for (keys, release), members in rejected for k in keys]
        lines.append("")

    # MOVED: cassandra-env.sh's hand edits the variables now set, the JVM getting the same arguments
    moved = _by_value([(n["name"],) + tuple(line.split(" -> ", 1)) for n in read for line in n.get("moved") or []
                       if " -> " in line])
    if moved:
        lines.append("MOVED %s cassandra-env.sh hand edits now set by variables, the JVM gets the same arguments"
                     % DASH)
        lines += _rows(moved, names, full=full, groups=groups, width=width)
        rewrites = {}
        for n in read:
            if n.get("rewritten"):
                rewrites.setdefault(", ".join(n["rewritten"]), []).append(n["name"])
        javas = sorted(set(v for n in read for line in n.get("moved") or [] if " -> " in line
                           for v in re.findall(r"\bcassandra_jvm\d+_extra_options\b", line.split(" -> ", 1)[1])))
        if javas:
            lines += out.fit(["  %s: read by that Java only; after update_java, move them to the new Java's"
                              % ", ".join(javas)], width)
        for files, members in sorted(rewrites.items()):
            lines.append("  apply_config rewrites %s on %s in the collection's layout (same JVM arguments; it"
                         " restarts the nodes it writes)" % (files, _nodes(members, names, groups, full)))
        lines.append("")

    # CLEANUP: options the running JVM does not use (where: option: why)
    cleanup = _by_value([(n["name"], ": ".join(line.split(": ", 2)[:2]), line.split(": ", 2)[2])
                         for n in read for line in n.get("cleanup") or [] if line.count(": ") >= 2])
    if cleanup:
        lines.append("CLEANUP %s options the running JVM does not use: left out of the lines moved, else left as they"
                     " are" % DASH)
        lines += _rows(cleanup, names, full=full, groups=groups, width=width)
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

    if convergence:
        lines += list(convergence) + [""]
    lines += ["NOT READ %s set in group_vars/all if they apply" % DASH, "  " + ", ".join(NOT_READ), ""]
    commands = [(out.command("apply_config", inventory=inventory or None, hosts=hosts or None,
                             extra=["--check", "--diff"], cwd=cwd),
                 "%s rewritten (same JVM arguments, see MOVED), nothing else" % ", ".join(moves) if moves
                 else "nothing to apply"),
                (out.command("topology", inventory=inventory or None, hosts=hosts or None, extra=["--check"],
                             cwd=cwd), "nothing to do")]
    width_command = max(len(c) for c, dummy in commands) + 3
    lines += ["NEXT"] + ["  %s%s expect: %s" % (c.ljust(width_command), ARROW_RIGHT, e) for c, e in commands] + [""]

    # DETAILS: everything above came from, line by line
    lines.append("DETAILS")
    for name, c in sorted((self_check or {}).items()):
        lines += ["  %s: %s" % (name, note) for note in c.get("notes") or []]
    lines += ["  " + line if line else "" for line in (layout.get("report") or "").split("\n")]
    lines = [line.rstrip() for line in lines]
    while lines and not lines[-1]:
        lines.pop()
    return lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_import_report": cassandra_import_report,
                "cassandra_import_self_check_advice": cassandra_import_self_check_advice}
