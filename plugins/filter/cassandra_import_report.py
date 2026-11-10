# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The report of an import (import_cluster), and the screen that ends it.

cassandra_import_report: the import's outcome -> the lines of report.txt, with details true of
    report.details.txt, with screen true the end of the run. report.txt by urgency: a head (the verdict and its
    counts), TO DO (numbered steps, each with the command to copy and what to check after), WORTH A LOOK, HAND
    EDITS, DIFFERENT ON SOME NODES, LEFT AS THE NODES HAVE THEM (each entry: what and on which nodes, then what
    to do, step by step), WHAT TO DO NEXT. report.details.txt: what the report is, its words, where the settings
    go, FOR INFO (nothing to do), the DETAILS (the lines every node has once, then each node's own). Node lists
    in their shortest exact form, with their count: "all (12 nodes)", "dc1 - n3 + n9 (6 nodes)". A long value
    summarised ("20 lines (first: ...)"), whole in the files written. Secrets never shown.
cassandra_import_self_check_advice: the end of the run on a failed self-check: what differs, grouped by what
    makes it pass, and how (report.txt has the same under TO DO, each node's differences under it).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ast
import json
import os
import re
import textwrap
from collections import namedtuple

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IDENTITY, KEEP, NODE_LEVEL, NOT_READ, PER_NODE, group_chains, hand_edits_kept, _secret, vars_sort_key, values_hidden)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import HEAP_FILE
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_nodes import CONFIG_FILE, RESTART_DOES
from ansible_collections.community.cassandra.plugins.filter.cassandra_suspicious import (
    LINES as SUSPICIOUS_LINES, REPLACE, finding_lines, suspicious)
from ansible_collections.community.cassandra.plugins.filter.cassandra_output import terminal_width
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file

# shown in the head, or the ring's (dc and rack): not among the settings
_NOT_SETTINGS = ("cassandra_cluster_name", "cassandra_dc", "cassandra_rack")
# where a node keeps its data: another value is a move of the data by hand
DATA_DIRS = ("cassandra_data_file_directories", "cassandra_commitlog_dir", "cassandra_hints_dir",
             "cassandra_saved_caches_dir", "cassandra_data_dir", "cassandra_conf_dir")
# a value per node by nature (an address): never "differs between nodes"
PER_NODE_KEYS = tuple(PER_NODE) + tuple(NODE_LEVEL) + ("cassandra_rackdc_dc",)
# per node by nature, or kept by each node for life (a node added later gets the group's): not counted in the
# convergence
NOT_COUNTED = PER_NODE_KEYS + tuple(k for k in IDENTITY if k != "cassandra_seeds")
# what FOR INFO calls each *_manage: false
KEEP_SHORT = {"cassandra_repository_manage": "repositories", "cassandra_cqlsh_python_manage": "cqlsh python",
              "cassandra_linux_manage": "OS settings", "cassandra_service_unit_manage": "unit",
              "cassandra_java_set_default": "system java", "cassandra_firewall_manage": "firewall"}
DASH = u"—"
ARROW_RIGHT = u"→"
FEW = 3  # a group in a node list has this many nodes outside the list at most ("dc1 - n3 + n9")
LONG = 60  # a value longer than this is summarised (whole in the files written)

# What a setting or a file is for, in plain words: said the first time the report names it (the reader may be new to
# Cassandra). A sub-key (cassandra_extra_settings.x) by its variable's.
WORDS = {
    "cassandra_version": "the Cassandra release series",
    "cassandra_num_tokens": "how many slices of the data each node owns",
    "cassandra_seeds": "the nodes a starting node contacts first to join the cluster",
    "cassandra_max_heap_size": "the memory Java gives Cassandra",
    "cassandra_heap_size": "the memory Java gives Cassandra",
    "cassandra_heap_newsize": "the part of that memory for short-lived objects",
    "cassandra_concurrent_reads": "how many reads a node runs at once",
    "cassandra_concurrent_writes": "how many writes a node runs at once",
    "concurrent_writes": "how many writes a node runs at once",
    "concurrent_reads": "how many reads a node runs at once",
    "cassandra_listen_address": "the address the nodes talk to each other on",
    "cassandra_rpc_address": "the address clients connect to",
    "cassandra_broadcast_rpc_address": "the address told to clients",
    "cassandra_broadcast_address": "the address told to the other nodes",
    "cassandra_endpoint_snitch": "how a node learns its data center and rack",
    "cassandra_authenticator": "how clients prove who they are",
    "cassandra_authorizer": "what each client may do",
    "cassandra_jmx_password": "the password nodetool logs in with",
    "cassandra_jmx_username": "the user nodetool logs in as",
    "cassandra_jmx_users": "the users nodetool may log in as",
    "cassandra_config_user": "the owner of the config files",
    "cassandra_config_group": "the group of the config files",
    "cassandra_config_file_permissions": "who may read each config file",
    "cassandra_extra_settings": "cassandra.yaml settings without a variable of their own",
    "cassandra_linux_limits": "the OS limits of the Cassandra user: open files, locked memory, processes",
    "cassandra_linux_sysctl": "kernel settings",
    "cassandra_data_file_directories": "where the data files are",
    "cassandra_commitlog_dir": "where the commit log (the write journal) is",
    "cassandra_medusa_extra_settings": "settings of Medusa, the backup tool",
    "cassandra_medusa_nodetool_host": "the address Medusa reaches nodetool at",
    "cassandra_jvm_extra_options": "extra Java options",
    "cassandra_jvm8_extra_options": "extra Java options for Java 8",
    "cassandra_jvm11_extra_options": "extra Java options for Java 11",
    "cassandra_jvm17_extra_options": "extra Java options for Java 17",
    "cassandra_env_extra_lines": "lines added at the end of cassandra-env.sh",
    "cassandra_logback_extra_lines": "lines added at the end of logback.xml (logging)",
    "cassandra.yaml": "Cassandra's main settings file",
    "cassandra-env.sh": "the script that sets Java's options when Cassandra starts",
    "logback.xml": "the logging settings",
    "cassandra-rackdc.properties": "the node's data center and rack",
    "jvm-server.options": "Java's options",
    "jvm8-server.options": "Java's options, Java 8 only",
    "jvm11-server.options": "Java's options, Java 11 only",
    "jvm17-server.options": "Java's options, Java 17 and later",
}


def _compress(names):
    """node1, node2, node3, web -> node1..node3, web (three or more in a row)."""
    text = out.nodes(names)
    return re.sub(r"^\d+ nodes: ", "", text)


def _plain(value):
    """A value as the report shows it: a short list on one line, a longer one a line per item (summarised when
    shown: _short), an empty one '' and a null (empty)."""
    if value is None:
        return "(empty)"
    if value == "":
        return "''"
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        items = [str(v) for v in value]
        joined = ", ".join(items)
        return joined if len(items) <= 3 and len(joined) <= LONG else "\n".join(items)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _short(text, name=""):
    """A value as one short text: a list of lines "20 lines (first: ...)" (name: its setting, "values" for a list
    other than lines), a long text cut ("..."); the whole of it is in the files written."""
    text = str(text)
    if "\n" in text:
        items = text.split("\n")
        what = "lines" if re.search(r"(_lines|_options)$", name.split(".")[0]) else "values"
        return "%d %s (first: %s)" % (len(items), what, cut_text(items[0], 40))
    return cut_text(text, LONG)


def named_groups(nodes):
    """The groups a node list may be said by: each DC (when there are several) and each rack of several nodes that is
    not its whole DC, "dc1/r1" -> [(label, [nodes])]."""
    dcs, racks = {}, {}
    for n in nodes:
        dc = str(n.get("dc") or "?")
        dcs.setdefault(dc, []).append(n["name"])
        racks.setdefault((dc, str(n.get("rack") or "?")), []).append(n["name"])
    groups = sorted(dcs.items()) if len(dcs) > 1 else []
    groups += [("%s/%s" % key, members) for key, members in sorted(racks.items())
               if len(members) > 1 and len(members) < len(dcs[key[0]])]
    return groups


def node_list(members, everyone, groups=(), full=True):
    """The shortest exact form of a node list, its count after it: "all (12 nodes)", "all - n4 (11 nodes)",
    "dc1 + n7 (7 nodes)", "dc1 - n3 + n9 (6 nodes)", "dc2 + dc3 - n4 (..)" by the groups (named_groups'; a group
    taken when it has FEW nodes at most outside the list), else the names (full: every one, else ranges)."""
    names = _sorted(set(str(n) for n in members))
    chosen, every = set(names), set(str(n) for n in everyone or [])
    count = " (%s)" % out.plural(len(names), "node")
    if every and chosen == every:
        return "all" + count
    sets = [(label, set(m)) for label, m in groups or [] if set(m) != every]

    def form(labels, minus, plus):
        return " + ".join(labels) + "".join(" - %s" % n for n in _sorted(minus)) + "".join(
            " + %s" % n for n in _sorted(plus))

    forms = [out.full_list(names) if full else _compress(names)]
    if every and chosen <= every:
        forms.append(form(["all"], every - chosen, ()))
    base, labels = set(), []  # the groups mostly in the list, the largest first
    for label, members_of in sorted(sets, key=lambda s: -len(s[1])):
        if not members_of & base and len(members_of - chosen) <= FEW and len(members_of & chosen) > len(
                members_of - chosen):
            base |= members_of
            labels.append(label)
    if labels:
        forms.append(form(labels, base - chosen, chosen - base))
    others, gone, out_labels = every - chosen, set(), []  # all but some groups
    for label, members_of in sorted(sets, key=lambda s: -len(s[1])):
        if members_of <= others and not members_of & gone:
            gone |= members_of
            out_labels.append(label)
    if every and out_labels and chosen <= every:
        forms.append("all - " + " - ".join(out_labels) + "".join(" - %s" % n for n in _sorted(others - gone)))
    return min(forms, key=len) + count


def _sorted(names):
    return sorted(names, key=lambda n: [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", n)])


def who_text(nodes, everyone, groups=None, full=True):
    """The nodes of a row (node_list)."""
    return node_list(nodes, everyone, groups, full)


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


_SETTING = re.compile(r"^\s*(-D[^=\s]+|-X\S+?(?==|$)|[\w.-]+)\s*[:=]?\s*(.*)$")


def _edits(node):
    """A node's hand edits the roles would revert -> [(file, setting, value text)], one per block of lines: a block
    that sets one setting (setting: its name), a note on a file of its own (setting None, its text), or other
    lines (setting "", its lines as the value)."""
    dummy, edits = hand_edits_kept(node)
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


def _leaves(key, value, keys=()):
    """A variable's value as one setting per sub-key (a dict's), with the keys on the way -> [(path, [keys],
    value)]: cassandra_extra_settings.sstable.selected_format, [sstable, selected_format]."""
    if isinstance(value, dict) and value:
        found = []
        for k, v in value.items():
            found += _leaves("%s.%s" % (key, k), v, keys + (str(k),))
        return found
    return [(key, list(keys), value)]


def _is_secret(key, keys, value):
    """A setting's value is a secret: by the variable's own name, or a sub-key's name (never a file's name with a mode
    as its value: cassandra_config_file_permissions' jmxremote.password: 0400)."""
    return _secret(key, value) or any(_secret(k, value) for k in keys if not out.file_mode(k, value))


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
                found[n["name"]] = dict((path, (names, value)) for path, names, value in _leaves(key, mine[key]))
                for path in found[n["name"]]:
                    if path not in paths:
                        paths.append(path)
        for path in paths:
            variable[path] = key
            for n in read:
                if n["name"] not in found:
                    text = "(collection's default)"
                elif path not in found[n["name"]]:
                    text = "(not set)"
                else:
                    names, value = found[n["name"]][path]
                    if _is_secret(key, names, value):
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


@values_hidden
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


def cut_text(text, room):
    text = str(text)
    return text if not room or len(text) <= room else text[:room - 3].rstrip() + "..."


def _item_lines(item, who, width, room=None):
    """An item: "- <name>, on <nodes>:", then what differs under it ("node 64, inventory 32", the line the node has,
    "-XX:+UseFoo (not in the inventory)"), a line per value when the nodes differ ("node3: node 128, ..."), where a
    file of yours sets it; room: a value or a line cut to it (the screen), a sentence never."""
    def value(node_text, role_text):
        if role_text is None:
            return "%s (not in the inventory)" % cut_text(node_text, room)
        if node_text is None:
            return "not on the node, inventory %s" % cut_text(role_text, room)
        return "node %s, inventory %s" % (cut_text(node_text, room), cut_text(role_text, room))

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
        details = [cut_text(item["text"], room if item["setting"] == "" else None)]
    wins = sorted(set(w["path"] for w in item["wins"].values()))
    if wins:
        details[-1] += " (set in %s)" % ", ".join(wins)
    lines = fill_text("- %s, on %s:" % (_item_name(item), who(_item_nodes(item))), width, "    ", "    ")
    for detail in details:
        lines += fill_text(detail, width, "        ", "  ")
    return lines


def _item_name(item):
    """cassandra.yaml concurrent_reads; a JVM option's file alone when its values name it (-XX:+UseFoo; not ****)."""
    named = item["setting"] and not (item["file"].endswith(".options") and all(
        v is None or v.startswith("-") for dummy, n, r in item["pairs"] for v in (n, r)))
    return out.mask("%s %s" % (item["file"], item["setting"]) if named else item["file"] or "?")


def name_column(names, most=40):
    """The column of names (their texts right of it): the longest, at most most wide (a longer one pushes its text)."""
    return max([len(name) for name in names if len(name) <= most] or [0]) + 3


# (not cassandra_output's flow and laid_out: those lay a line out where the callback prints it, the ops callback's
# help only; these lines are laid out here, the same under any callback and in report.txt)
def aligned_rows(rows, indent, width, column=None):
    """[(name, text)] -> lines: the names in a column (name_column's by default), the texts on over lines under their
    column when longer than width."""
    column = column or name_column([name for name, dummy in rows])
    lines = []
    for name, text in rows:
        head = indent + name.ljust(column) if len(name) < column else indent + name + "   "
        hang = " " * (len(indent) + column)
        if not width or len(head) + len(text) <= width:
            lines.append((head + text).replace(out.NBSP, " ").rstrip())
            continue
        wrapped = textwrap.wrap(text, max(20, width - len(head)), break_long_words=False, break_on_hyphens=False)
        lines.append((head + (wrapped[0] if wrapped else "")).replace(out.NBSP, " ").rstrip())
        lines += fill_text(" ".join(wrapped[1:]), width, hang)
    return lines


def unbreakable(text):
    """text never cut by fill_text and aligned_rows (a command, an option and its value)."""
    return text.replace(" ", out.NBSP)


def fill_text(text, width, hang, more=""):
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
        hints.append(("hand edits", "to keep them as they are: import again with %s; these files are then not"
                      " managed by Ansible (kept as the node has them): %s. To bring them under Ansible: carry the"
                      " edit in the inventory (cassandra_jvm*_extra_options, cassandra_env_extra_lines and"
                      " cassandra_logback_extra_lines for lines at the end of the file, cassandra_extra_settings, ...)"
                      " or give the file your own template (cassandra_config_template_overrides), remove the file"
                      " from cassandra_config_keep_files, import again. To drop them instead: undo them on the node,"
                      " then import again" % (
                          unbreakable("-e import_cluster_keep_hand_edits=true"),
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
                                          unbreakable("-e import_cluster_allow_unread=true"))))
    debug = unbreakable("-e import_cluster_debug=true")
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
                      % (unbreakable("-e import_cluster_strict=false"), unbreakable(apply_check))))
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
        differs += fill_text(error[0].upper() + error[1:], width, "  ")
    for kind, title in _KINDS:
        mine = [i for i in items if i["kind"] == kind]
        if not mine:
            continue
        differs += fill_text("%s:" % title, width, "  ", "  ")
        for item in mine:
            differs += _item_lines(item, who, width, room)
            if report:  # each node's line, the same line once for its nodes
                same = {}
                for name, d in item["lines"]:
                    if name not in same.setdefault(d, []):
                        same[d].append(name)
                for d, names in same.items():
                    differs += fill_text("%s: %s" % (who(names), d), width, "          ", "  ")
    if unread:
        differs += fill_text("Not read, so not checked: %s" % ", ".join(
            "%s (%s)" % (n["name"], n.get("reason") or "unreachable") for n in unread), width, "  ")
    hints = _hints(layout, items, read, unread, who, error=error, report=report, report_file=report_file,
                   inventory=inventory, hosts=hosts, cwd=cwd)
    paragraphs = [differs, ["HOW TO MAKE IT PASS"] + aligned_rows(hints, "  ", width)]
    if not report and report_file:
        paragraphs.append(["Details: %s, section SELF-CHECK" % out.path_from(report_file, cwd)])
    return verdict, paragraphs


@values_hidden
def cassandra_import_self_check_advice(layout, self_check, report_file="", error="", allow_unread=False, check=False,
                                       inventory="", hosts="", cwd=None, width=None):
    """layout: cassandra_inventory_layout's; self_check: {node: {differences, notes}}; report_file: the report
    (report.check.txt under --check); error: why the self-check could not run ('' : it ran); allow_unread:
    import_cluster_allow_unread (the nodes not read pass); check: --check; inventory, hosts, cwd: the next
    commands' -i, -e cassandra_hosts and current dir (cassandra_import_report's); width: the lines fit in it (the
    terminal's by default, SCREEN_MOST at most) -> the lines of the run's stop on a failed self-check:
    "SELF-CHECK FAILED  <cluster group>  ...", what differs grouped by what makes it pass, HOW TO MAKE IT PASS,
    where the details are; node lists by node_list (ranges). Blank lines between them. Secrets never shown.
    e.g. -> ["SELF-CHECK FAILED  my_cluster  the inventory would change 1 setting on 2 nodes ...", "", ...]."""
    width = int(width) if width else min(terminal_width(), SCREEN_MOST)
    data = report_sections(layout, self_check)  # (the report's node lists)
    groups, everyone = data["groups"], data["everyone"]
    verdict, paragraphs = _self_check_lines(
        layout, self_check, lambda m: who_text(m, everyone, groups, False), width, report_file=report_file, error=error,
        allow_unread=boolean(allow_unread, strict=False), inventory=inventory, hosts=hosts, cwd=cwd)
    head = "SELF-CHECK FAILED  %s  %s %s" % (layout["cluster_group"], verdict, unbreakable(
        "(--check: the inventory not written)" if boolean(check, strict=False)
        else "(written for inspection only, marked NOT VALID)"))
    head = [line.replace(out.NBSP, " ") for line in out.fit([head], width)]
    blocks = [head] + paragraphs
    lines = []
    for block in blocks:
        lines += ([""] if lines else []) + block
    return lines


def report_sections(layout, self_check):
    """The data of the report's sections and the groups node lists are said by (the same for the screen, the report
    and the stop on a failed self-check): {settings, variable, edits, kept, left_groups, tuning, check_items,
    groups, everyone}."""
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    from_yours = layout.get("from_yours") or {}
    keys = sorted({k for n in read for k in n.get("vars") or {} if k not in _NOT_SETTINGS}, key=vars_sort_key)
    pairs, variable = _settings_pairs(read, keys, from_yours)
    kept, left_groups = [], {}
    for n in nodes:
        order = list(KEEP_SHORT)
        for k, v in sorted((n.get("keep") or {}).items(),
                           key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0])):
            if k == "cassandra_config_keep_files":
                kept += [(n["name"], f) for f in v]
            elif k in KEEP:
                left_groups.setdefault(KEEP_SHORT.get(k, k), []).append(n["name"])
            else:
                left_groups.setdefault("%s: %s" % (k, _plain(v)), []).append(n["name"])
    tuning = _by_value([(n["name"],) + _os_pair(line) for n in read
                        for line in (n.get("os") or {}).get("lines") or []])
    return {"settings": _by_value(pairs), "variable": variable, "edits": _by_value(_hand_edit_rows(read)),
            "kept": kept, "left_groups": left_groups, "tuning": tuning,
            "check_items": _self_check_items(read, self_check, layout.get("theirs_win")),
            # ("all" is every node of the cluster: a list of the nodes read says "all - n4" when n4 was not read)
            "groups": named_groups(nodes), "everyone": [n["name"] for n in nodes]}


# what every section of the report reads: the layout, its nodes (read, unread, their names), the groups node lists
# are said by, the group chains of the hosts, the options (full: every node named; screen; check; cwd: paths shown
# relative to it), the values of yours that differ and whether they all are in group_vars/all (standard), the
# names already said in plain words (said, filled as the report goes)
_Report = namedtuple("_Report", "layout read unread names everyone groups chains full width screen check cwd"
                                " yours standard said report_name inventory hosts")


def _who(report, members, full=None):
    """The nodes members, in their shortest exact form (node_list)."""
    return node_list(members, report.everyone, report.groups, report.full if full is None else full)


def _say(report, name):
    """A setting or a file named, what it is for after it the first time (WORDS): "cassandra_num_tokens (how many
    slices of the data each node owns)"."""
    key = name.split(".")[0] if name.startswith("cassandra_") else name
    words = WORDS.get(key)
    if not words or key in report.said:
        return name
    report.said.add(key)
    return "%s (%s)" % (name, words)


def _place_of(report, key, node):
    """Where the inventory keeps key for the node: "host_vars", the path of a file of yours, "group_vars/<group>"
    (its most specific level that has it), "" when none."""
    layout = report.layout
    from_yours = layout.get("from_yours") or {}
    if key in (layout.get("host_vars") or {}).get(node, {}):
        return "host_vars"
    if key in from_yours.get(node, {}):
        return from_yours[node][key]["path"]
    return next(("group_vars/%s" % g for g in reversed(report.chains.get(node, []))
                 if key in (layout.get("group_vars") or {}).get(g, {})), "")


def _placed(report, key, members):
    """Where the inventory keeps key for these nodes (their most specific level that has it) -> text."""
    found = []
    for m in members:
        place = _place_of(report, key, m)
        if place and place not in found:
            found.append(place)
    groups_vars = [f for f in found if f.startswith("group_vars/")]
    if len(groups_vars) > 2:  # one per rack...: counted
        found = [f for f in found if f not in groups_vars[1:]]
        found[found.index(groups_vars[0])] = "group_vars/<group>/main.yml of %d groups" % len(groups_vars)
    # (the files: host_vars/<node>/main.yml of these nodes, group_vars/<group>/main.yml)
    return ", ".join("host_vars/<node>/main.yml" if f == "host_vars" else f + "/main.yml"
                     if f.startswith("group_vars/") and "/" not in f[len("group_vars/"):] else f for f in found)


def _standard(yours):
    """yours: the values of yours that differ -> whether they all are in group_vars/all (a standard of yours)."""
    alls = ("group_vars/all", "group_vars/all.yml", "group_vars/all.yaml", "group_vars/all.json")
    return bool(yours) and all("/".join(y["path"].split("/")[:2]) in alls for y in yours)


def _inventory_changes(changes):
    """What changes in the inventory dir: changes (None: not known) -> (changed, removed, same: nothing changes),
    changed and removed None when not known."""
    changed = removed = None
    if changes is not None:
        changed, removed = list(changes.get("changed") or []), list(changes.get("removed") or [])
    return changed, removed, changed == [] and removed == []


# --- TO DO: numbered steps -------------------------------------------------------------------------------------

# a step: what to do, why (in plain words), the command to copy ('' : none), what to check after
_Step = namedtuple("_Step", "title why run then")


def _todo_steps(report, allow_unread, self_check_ok, self_check_error, check_items, secrets_clear, leftovers,
                inventory_dir, more, in_git, inventory, hosts):
    """The TO DO steps, the most urgent first: the nodes not read, the settings their Cassandra rejects, the self-check
    that failed, your files that still win, the secrets written in clear, the files of an earlier import of an
    unknown cluster, the playbook's own (more: the CQL login) -> [_Step]. Paths from the current dir; inventory,
    hosts: the import command again (as the next playbooks are run)."""
    layout = report.layout
    steps = []
    again = unbreakable(out.command("import_cluster", inventory=inventory or None, hosts=hosts or None,
                                    extra=["-e import_cluster_force=true"], cwd=report.cwd))
    if report.unread and not allow_unread:
        names = [n["name"] for n in report.unread]
        them = "it" if len(names) == 1 else "them"
        steps.append(_Step(
            "Let the import read %s, then import again" % _who(report, names),
            "the import could not read %s (%s): the inventory has no settings of %s, and a playbook run on %s would"
            " give %s the cluster's settings, not %s own. Start Cassandra there, or fix how Ansible reaches %s" % (
                them, "; ".join("%s: %s" % (n["name"], n.get("reason") or "unreachable") for n in report.unread),
                them, them, them, "its" if len(names) == 1 else "their", them),
            again,
            "the head of the new report says every node was read (to import without %s instead, add %s)" % (
                them, unbreakable("-e import_cluster_allow_unread=true"))))
    for (keys, release), members in _not_settings(report.read):
        one = len(keys) == 1
        steps.append(_Step(
            "Remove %s from cassandra.yaml on %s" % (", ".join(keys), _who(report, members)),
            "Cassandra %s does not know %s. It runs fine now, but would not start again with %s after a restart; the"
            " import left %s out of the inventory" % (release, "this setting" if one else "these settings",
                                                      "it" if one else "them", "it" if one else "them"),
            "on each of these nodes, delete the %s %s from cassandra.yaml (in the config directory DETAILS names)" % (
                "line" if one else "lines", ", ".join(keys)),
            "the next import no longer lists %s" % ("it" if one else "them")))
    if not self_check_ok and (self_check_error or check_items):
        steps.append(_Step(
            "Make the self-check pass",
            "the self-check works out the files the inventory would write for each node and compares them with the"
            " node's: %s, so the inventory does not reproduce the nodes yet" % (self_check_error or _changes(check_items)),
            "what SELF-CHECK and HOW TO MAKE IT PASS say, right below",
            "the head of the next report says self-check passed"))
    if layout.get("theirs_win"):
        steps.append(_Step(
            "Fix your own files that still win over the import",
            "Ansible would apply their value, not the node's: %s" % ", ".join(sorted(
                {"%s: %s (%s)" % (w["node"], w["key"], w["path"]) for w in layout["theirs_win"]})),
            "rename or fix them, then import again: %s" % again, "the next import no longer lists them"))
    if secrets_clear:
        paths = " ".join(out.path_from(os.path.join(inventory_dir, f), report.cwd) for f in secrets_clear)
        steps.append(_Step(
            "Encrypt the passwords file%s" % ("" if len(secrets_clear) == 1 else "s"),
            "the passwords read on the nodes are written in clear text there. ansible-vault asks for a new password:"
            " keep it safe, every playbook run on this inventory needs it from then on (--ask-vault-pass)",
            unbreakable("ansible-vault encrypt %s" % paths),
            "%s shows $ANSIBLE_VAULT;1.1;AES256" % unbreakable("head -1 %s" % paths.split(" ", maxsplit=1)[0])))
    if leftovers.get("unsure"):
        paths = " ".join(out.path_from(os.path.join(inventory_dir, f), report.cwd) for f in leftovers["unsure"])
        git = out.in_git_work_tree(inventory_dir) if in_git is None else in_git
        steps.append(_Step(
            "Is %s an old node of this cluster?" % ", ".join(leftovers["unsure"]),
            "an earlier import wrote it, for a cluster this import cannot tell. If it is an old node of this cluster,"
            " remove it; if not, leave it",
            unbreakable("%s %s" % ("git rm" if git else "rm", paths)),
            "the next import no longer lists it"))
    steps += [_Step(str(text), "", "", "") for text in more]
    return steps


def _step_lines(number, step, width):
    lines = fill_text("%d. %s" % (number, step.title), width, "  ", "   ")
    for label, text in (("", step.why), ("run:    ", step.run), ("check:  ", step.then)):
        if text:
            lines += fill_text(label + text, width, "     ", " " * len(label))
    return lines


# --- TO REVIEW: entries of two lines ---------------------------------------------------------------------------

# a kind of TO REVIEW entries: its name in the head's counts (singular, plural), its title, what it means
_Kind = namedtuple("_Kind", "one many title about")
HAND = _Kind("hand edit", "hand edits", "Hand edits",
             "lines changed by hand in a config file that no inventory variable carries; apply_config (the playbook"
             " that writes the config files) would put the file back as the collection writes it (nothing changes on"
             " the nodes until it runs)")
KEPT = _Kind("file left as is", "files left as is", "Files left as is",
             "config files Ansible leaves as each node has them (cassandra_config_keep_files); their hand edits stay,"
             " and a change of the inventory does not reach them")
MOVED = _Kind("hand edit moved", "hand edits moved", "Hand edits moved to variables",
              "lines added by hand to cassandra-env.sh or a jvm options file that now come from variables; Java gets the"
              " same options, but"
              " the next apply_config rewrites the file and restarts the node")
TO_CHECK = _Kind("thing worth a look", "things worth a look", "Worth a look",
                 "values that work but deserve a look; each says why and what to do")
DIFFERS = _Kind("setting that differs between nodes", "settings that differ between nodes",
                "Settings that differ between nodes",
                "the nodes do not all have the same value; often on purpose (machines of other sizes), sometimes a"
                " leftover")
UNREAD = _Kind("node not read", "nodes not read", "Nodes not read",
               "accepted as they are (import_cluster_allow_unread): their settings are not in the inventory")


def _entry(report, what, members, todo):
    """An entry: what and on which nodes, then what to do -> [two lines] (a longer text goes on, further in)."""
    who = _who(report, members) if members else ""
    lines = fill_text("%s   %s" % (what, unbreakable(who) if len(who) <= 40 else who) if members else what,
                      report.width, "    ", "  ")
    return lines + (fill_text(todo, report.width, "      ") if todo else []) + [""]  # (a blank line between entries)


def _hand_entries(report, edits):
    """Hand edits: a setting edited (its value), a file's other edits counted -> [entry lines]; the one way to keep
    them, the file left as the node has it."""
    entries = []
    for name, values in edits:
        f, dummy, setting = name.partition(" ")
        for text, members in values:
            if REPLACE.search(text):  # (never to keep: under Settings to review)
                todo = "a replace flag: remove it, see Settings to review"
            else:
                todo = "%sto keep %s: import again with %s (Ansible then leaves %s as each node has it)" % (
                    "" if setting else "the lines are under DETAILS, node by node; ", "it" if setting else "them",
                    unbreakable("-e import_cluster_keep_hand_edits=" + f), f)
            shown = "%s %s: %s" % (_say(report, f), setting, _short(text)) if setting else "%s: %s" % (
                _say(report, f), _short(text))
            entries += _entry(report, shown, members, todo)
    return entries


def _hand_extra(report):
    """Under the hand edits: the layout-only ones counted, why cassandra-env.sh's lines stay hand edits."""
    lines = []
    layout_only = sum(len(n.get("normalized") or []) + len(n.get("comments") or []) for n in report.read)
    if layout_only:
        lines += fill_text("also %s of the layout only (spacing, order, comments): no effect, listed under DETAILS"
                           % out.plural(layout_only, "edit"), report.width, "    ")
    not_moved = {}
    for n in report.read:
        if n.get("not_moved"):
            not_moved.setdefault(n["not_moved"], []).append(n["name"])
    for why, members in sorted(not_moved.items()):
        lines += _entry(report, "cassandra-env.sh JVM_OPTS lines and heap, not moved to variables", members, why)
    return lines


def _kept_entries(report, kept):
    """Files left as is: one entry per file, its nodes, then how to give it back to Ansible, step by step (the
    variables that carry its edits and where they go; the line to remove; the check; the run) -> [line]."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_convergence import kept_plan
    cluster = report.layout["cluster_group"]
    entries = []
    for plan in kept_plan(report.layout, report.inventory, report.hosts, report.cwd):
        nodes, name = plan["nodes"], plan["file"]
        entries += fill_text("%s   %s" % (_say(report, name), unbreakable(_who(report, nodes))), report.width, "    ",
                             "  ")
        if report.screen:  # (the steps in report.txt)
            continue
        everyone = sorted(nodes) == sorted(report.names)
        hosts = "host_vars/%s/main.yml" % nodes[0] if len(nodes) == 1 else \
            "host_vars/<node>/main.yml of %s" % out.plural(len(nodes), "node")
        if plan.get("unread"):
            # (bin/cassandra passes on the options of its Java's file only)
            entries += fill_text("not read by the Java %s runs (observed: Java %s)" % (
                nodes[0] if len(nodes) == 1 else "these nodes", plan["unread"]), report.width, "      ", "  ")
            entries += fill_text("its edits have no effect on the running JVM: no variable carries them", report.width,
                                 "      ", "  ")
        elif plan["loose"] and not plan["set"] and not plan["per_node"]:
            entries += fill_text("%s that no variable carries (each under DETAILS): Ansible leaves the file as each"
                                 " node has it; nothing to do while they are wanted" % out.plural(plan["loose"], "edit"),
                                 report.width, "      ", "  ")
            entries += [""]
            continue
        steps = []
        if plan["loose"]:
            steps.append("undo on the nodes the %s no variable carries (each under DETAILS), or keep the file as it is"
                         % out.plural(plan["loose"], "edit"))
        if plan["set"]:
            steps.append("in %s: %s" % ("group_vars/%s/main.yml" % cluster if everyone else hosts,
                                        ", ".join("%s: %s" % (k, v) for k, v in plan["set"])))
        for variable, value in plan["per_node"]:
            steps.append("in %s: %s: %s" % (hosts, variable, value))
        steps.append("in %s: remove %s from cassandra_config_keep_files" % (hosts, name))
        steps.append("check: %s %s what its diff still shows, Ansible would undo"
                     % (unbreakable(plan["check"]), ARROW_RIGHT))
        steps.append("the same without --check: it writes %s, then restarts the nodes%s" % (
            name, " (the same Java options: their Java does not read it)" if plan.get("unread") else ""))
        entries += fill_text("to give it back to Ansible%s:" % (
            " (%s: already given)" % ", ".join(plan["already"]) if plan["already"] else ""), report.width, "      ", "  ")
        for i, step in enumerate(steps):
            entries += fill_text("%d. %s" % (i + 1, step), report.width, "        ", "   ")
        entries += [""]
    return entries


def _left_alone_lines(report):
    """What Ansible leaves as each node has it (the *_manage: false the import wrote), one line per set of nodes."""
    by_nodes = {}
    for label, members in report_sections(report.layout, {})["left_groups"].items():
        if not label.startswith("cassandra_config_keep_files"):
            by_nodes.setdefault(tuple(sorted(members)), []).append(label)
    return [line for members, labels in by_nodes.items() for line in fill_text(
        "not managed by Ansible here: %s   %s" % (", ".join(labels), unbreakable(_who(report, list(members)))),
        report.width, "    ", "  ")]


def _moved_entries(report):
    moved = _by_value([(n["name"],) + tuple(line.split(" -> ", 1)) for n in report.read
                       for line in n.get("moved") or [] if " -> " in line])
    entries = []
    for where, values in moved:
        for variable, members in values:
            files = sorted(set(f for n in report.read if n["name"] in members for f in n.get("rewritten") or []))
            entries += _entry(report, "%s -> %s" % (where, variable), members,
                              "nothing to do now; the next apply_config rewrites %s in the collection's layout and"
                              " restarts these nodes (same Java options)" % (", ".join(files) or "cassandra-env.sh"))
    javas = sorted(set(v for n in report.read for line in n.get("moved") or [] if " -> " in line
                       for v in re.findall(r"\bcassandra_jvm\d+_extra_options\b", line.split(" -> ", 1)[1])))
    if javas:
        entries += fill_text("%s: read by that Java only; after update_java, move them to the new Java's"
                             % ", ".join(javas), report.width, "    ")
    return entries


def _to_check_entries(report):
    """Settings to review: the suspicious ones, the JVM options worth a look, the values of yours kept as found, what
    was not compared -> (how many, [entry lines])."""
    layout, read = report.layout, report.read
    entries, count = [], 0
    found = suspicious(dict((n["name"], dict(n.get("vars") or {}, **{SUSPICIOUS_LINES: [
        e[4:] for e in n.get("hand_edits") or [] if e.startswith("  + ")]})) for n in read), [n["name"] for n in read])
    for row, names, text, fix in found:  # (an entry as the others: what, its nodes after it, then what to do)
        count += 1
        dummy, todo = finding_lines(row, names, text, fix)
        if report.screen:  # (how to keep it on purpose: report.txt)
            todo = todo.split("; kept on purpose: ")[0]
        entries += _entry(report, text.replace(" on {nodes}", "").replace("{nodes}", "these nodes"), names,
                          todo.strip())
    for text, members, todo in _os_findings(report):
        count += 1
        entries += _entry(report, text, members, todo)
    for text, members in _jvm_warnings(read):
        count += 1
        option, sep, rest = text.partition("): ")
        entries += _entry(report, option + ")" if sep else text, members, rest)
    for (key, yours), values in _yours_rows(report):
        for value, members in values:
            count += 1
            entries += _entry(report, "%s: %s, yours: %s" % (_say(report, key), value, yours), members,
                              "kept as the nodes have it (in %s); to apply yours, delete it there, then apply_config"
                              % (_placed(report, key, members) or "?"))
    if layout.get("not_compared"):
        count += 1
        entries += _entry(report, "not compared with the nodes: %s" % ", ".join(layout["not_compared"]), None,
                          "your files set them with a template the import cannot work out, or a vaulted value, or"
                          " their default is not known here: check them on the nodes yourself")
    if layout.get("unread_files"):
        count += 1
        entries += _entry(report, "your vaulted files not read: %s" % ", ".join(layout["unread_files"]), None,
                          "no vault password was given, so their values are not compared with the nodes: import"
                          " again with --ask-vault-pass")
    return count, entries


_THP_ON = re.compile(r"^THP now: enabled (always|madvise)\b")
_THP_BY = re.compile(r"^THP set by (.+) \(not executable\)$")
_READAHEAD = re.compile(r"^(?:device|disk) .*, SSD\): read_ahead_kb (\d+)")  # (a label may hold parentheses)
_TUNED_DISK = re.compile(r"^tuned (\S+) \[disk\]: readahead=>?(\d+)")
_TUNED_PROFILE = re.compile(r"^tuned: profile (\S+)")
READAHEAD_MOST = 64  # KB: more on a data SSD is said (cassandra_linux writes 4)


def _restart_finding(n, from_yours=None):
    """A node whose unit's Restart= is not the one the inventory gives cassandra_service_restart -> (finding, what
    to do); None when they are the same (the import carries the node's), or the unit was not read. from_yours: the
    layout's (a value one of your files gives as the node has it: not in its variables)."""
    restart = str((n.get("unit") or {}).get("restart") or "")
    default = role_file("cassandra_service").get("cassandra_service_restart", "no")
    yours = ((from_yours or {}).get(n["name"]) or {}).get("cassandra_service_restart") or {}
    ours = str((n.get("vars") or {}).get("cassandra_service_restart", yours.get("value", default)))
    if not restart or restart == ours:
        return None
    does = "; ".join("Restart=%s: %s" % (v, RESTART_DOES.get(v, "see systemd.service(5)")) for v in (restart, ours))
    kept = "cassandra_service_unit_manage" in (n.get("keep") or {})
    return ("Restart=%s in Cassandra's systemd unit, cassandra_service_restart: %s in the inventory" % (restart, ours),
            "systemd, not Ansible, restarts Cassandra, by the unit's Restart=; %s; %s" % (
                does, "this node keeps its own unit (cassandra_service_unit_manage: false): cassandra_service_restart"
                      " does not change it" if kept else
                "the next run of cassandra_service writes Restart=%s in the unit" % ours))


def _os_findings(report):
    """What the import observed on the nodes that is worth a look, beyond their settings: transparent huge pages
    on, a large read-ahead on the data SSDs, a unit's Restart= other than the inventory's, Medusa installed and not
    set up -> [(finding, [nodes], what to do)], the same finding on several nodes once."""
    found = {}

    def add(text, todo, name):
        found.setdefault((text, todo), []).append(name)

    for n in report.read:
        lines = (n.get("os") or {}).get("lines") or []
        thp = next((m.group(1) for m in map(_THP_ON.match, lines) if m), "")
        if thp:
            script = next((m.group(1) for m in map(_THP_BY.match, lines) if m), "")
            add("transparent huge pages enabled %s%s" % (
                thp, ", set by %s, which is not executable" % script if script else ""),
                "Cassandra wants them off (never), as cassandra_linux sets them", n["name"])
        ahead = sorted(set(int(m.group(1)) for m in map(_READAHEAD.match, lines) if m and int(m.group(1)) > READAHEAD_MOST))
        if ahead:
            tuned = next((m for m in map(_TUNED_DISK.match, lines) if m), None)
            profile = next((m.group(1) for m in map(_TUNED_PROFILE.match, lines) if m), "")
            add("read-ahead %s KB on the data SSDs (cassandra_linux: 4)%s" % (
                "/".join(str(a) for a in ahead),
                ", and tuned (profile %s) sets readahead=>%s at each start" % (profile, tuned.group(2))
                if tuned and profile else ""),
                "a large read-ahead fills the page cache with data no read asked for" + (
                    "; tuned would set it again: a tuned profile of your own without it" if tuned else ""), n["name"])
        restart = _restart_finding(n, report.layout.get("from_yours"))
        if restart:
            add(restart[0], restart[1], n["name"])
        medusa = n.get("medusa") or {}
        if medusa.get("install") and not medusa.get("ini"):
            add("Medusa installed, no /etc/medusa/medusa.ini: it is not set up there",
                "nothing to do if the backups are made another way; cassandra_medusa sets it up", n["name"])
    return [(text, members, todo) for (text, todo), members in found.items()]


def _yours_rows(report):
    """The values of yours a node does not have -> [((key, "value (file)"), [(the node's value, [nodes])])]."""
    def yours_text(y):
        return "%s (%s)" % ("(a secret)" if _secret(y["key"], y["yours"]) else _short(_plain(y["yours"])),
                            y["path"])
    return _by_value([(y["node"], (y["key"], yours_text(y)), "(in secrets.yml)" if _secret(y["key"], y["value"])
                       else _short(_plain(y["value"]))) for y in report.yours])


def _differs(report, settings):
    """The settings whose value differs between the nodes read (not one per node by nature, nor one of yours: under
    Settings to review) -> [(setting, [(value, [nodes])])], most nodes first."""
    yours = set(y["key"] for y in report.yours)
    return [(path, sorted(values, key=lambda v: (-len(v[1]), v[0]))) for path, values in settings
            if len(values) > 1 and _variable(path) not in PER_NODE_KEYS + tuple(yours)]


def _variable(path):
    return path.split(".")[0]


def _convergence_place(report):
    """Where the convergence is: FOR INFO below, or on the screen, the report's file (only it has FOR INFO)."""
    return "%s (FOR INFO)" % report.report_name if report.screen else "FOR INFO"


def _differs_entries(report, differs):
    """Each setting that differs: a table of its values (every one, with its nodes), then where they are set; on the
    screen, a value per node (more than 5 nodes with one of their own) is one row, each in the report."""
    entries = []
    plans = _differs_plans(report) if differs and not report.screen else {}
    for path, values in differs:
        own = [(v, m) for v, m in values if len(m) == 1 and v not in ("(collection's default)", "(not set)")]
        own = own if report.screen and len(own) > 5 else []
        rows = [(_shown_value(v, path), _who(report, members), "") for v, members in values if (v, members) not in own]
        if own:
            rows.append(("%d values, one per node (each in the report)" % len(own),
                         _who(report, [m[0] for dummy, m in own]), ""))
        words = _say(report, path)
        if words != path:
            rows[0] = (rows[0][0], rows[0][1], words[len(path) + 1:])
        table = out.settings_lines([(path, rows)], indent="    ", width=report.width)
        if table[0] == "    %s:" % path or max(len(line) for line in table) > report.width:
            # a long name, or a row that does not fit after it: the name on a line of its own, its values under it
            # (a name longer than any column puts them there)
            table = ["    %s:" % path] + out.settings_lines([("-" * 99, rows)], indent="     ", width=report.width)[1:]
        entries += table
        places = []
        for dummy, members in values:
            for place in (_placed(report, _variable(path), members) or "").split(", "):
                if place and place not in places:
                    places.append(place)
        if any(v.startswith("(in secrets.yml") for v, dummy in values):  # (a secret: in secrets.yml, never main.yml)
            places = [p.replace("/main.yml", "/secrets.yml") for p in places]
        moving = " Changing it moves data: a task for an experienced operator." if _variable(path) in DATA_DIRS else ""
        if report.screen:  # (the table only: where it is set and how to give one value, in report.txt)
            entries += [""]
            continue
        entries += fill_text("set in %s.%s" % (", ".join(places) or "the collection's default", moving),
                             report.width, "      ")
        step = plans.get(path)
        if step:
            entries += fill_text("to give every node %s: %s" % (step[0], step[1]), report.width, "      ", "  ")
            entries += fill_text("then %s" % step[2], report.width, "      ", "  ")
        entries += [""]
    return entries


_ACTION = re.compile(r"^\u2192 (.*?) for all(?: \(a tie[^)]*\))?: (.*)$")


def _differs_plans(report):
    """How to give every node one value, per setting that differs (the convergence's way, said as steps) ->
    {path: (the value, what to change, what then happens and its check)}."""
    from ansible_collections.community.cassandra.plugins.filter import cassandra_import_convergence as conv
    data = report_sections(report.layout, {})
    items, dummy = conv._setting_items(report.layout, data, report.read)  # pylint: disable=protected-access
    plans = {}
    for item in items:
        m = _ACTION.match(conv._action(item, report.layout))  # pylint: disable=protected-access
        if not m or not m.group(2):
            continue
        check = "check first: %s" % unbreakable(conv._check(  # pylint: disable=protected-access
            item["changing"], item["kind"], report.inventory, report.hosts, report.cwd)) \
            if conv._KINDS[item["kind"]][2] else "no check (apply_config does not show it)"  # pylint: disable=protected-access
        value = conv._shown(m.group(1))  # pylint: disable=protected-access
        plans[item["name"]] = (value if value != "default" else "the collection's default", m.group(2),
                               "%s; %s" % (conv._then_text(item), check))  # pylint: disable=protected-access
    return plans


def _where_lines(report, settings):
    """WHERE THE SETTINGS GO: each file of the inventory and how many settings it holds (host_vars: with how many
    nodes); your own files (the values the import leaves to them) with their settings named -> [line]."""
    keys, nodes, yours = {}, {}, {}
    for path, values in settings:
        for dummy, members in values:
            for m in members:
                place = _place_of(report, _variable(path), m)
                if not place:
                    continue
                if place.startswith("group_vars/") and "/" not in place[len("group_vars/"):]:
                    place += "/main.yml"
                elif place == "host_vars":
                    place = "host_vars/<node>/main.yml"
                else:  # (a file of yours)
                    yours.setdefault(place, set()).add(_variable(path))
                    continue
                keys.setdefault(place, set()).add(_variable(path))
                nodes.setdefault(place, set()).add(m)
    rows = [("    %s" % place, out.plural(len(found), "setting") + (
        " (%s)" % out.plural(len(nodes[place]), "node") if place.startswith("host_vars") else ""))
        for place, found in sorted(keys.items(), key=lambda x: (x[0].startswith("host_vars"), x[0]))]
    rows += [("    %s (yours)" % path, "%s: %s" % (out.plural(len(found), "setting"), ", ".join(sorted(found))))
             for path, found in sorted(yours.items())]
    if not rows:
        return []
    room = max(len(a) for a, dummy in rows) + 2
    return [line for a, b in rows for line in fill_text(a.ljust(room) + b, report.width, "", " " * (room + 2))]


def _review_blocks(report, data, differs, allow_unread):
    """TO REVIEW: [(kind, how many, its entry lines)], the kinds with entries only."""
    blocks = [(HAND, sum(len(v) for dummy, v in data["edits"]), _hand_entries(report, data["edits"])
               + _hand_extra(report)),
              (KEPT, len(set(f for dummy, f in data["kept"])), _kept_entries(report, data["kept"])),
              (MOVED, len(_by_value([(n["name"], line, "") for n in report.read for line in n.get("moved") or []
                                     if " -> " in line])), _moved_entries(report))]
    blocks.append((TO_CHECK,) + _to_check_entries(report))
    blocks.append((DIFFERS, len(differs), _differs_entries(report, differs)))
    if report.unread and allow_unread:
        blocks.append((UNREAD, len(report.unread), _entry(
            report, ", ".join("%s (%s)" % (n["name"], n.get("reason") or "unreachable") for n in report.unread),
            None, "to have them in the inventory: start Cassandra there (or fix the access), then import again")))
    return [(kind, n, lines) for kind, n, lines in blocks if lines and n]


# --- FOR INFO --------------------------------------------------------------------------------------------------

SERIES_SHOWN = {"40x": "4.0.x", "41x": "4.1.x", "50x": "5.0.x"}


def _shown_value(text, path):
    """A value as the report shows it: _short, a series as its version (41x: 4.1.x)."""
    return SERIES_SHOWN.get(text, _short(text, path)) if path == "cassandra_version" else _short(text, path)


def _value_rows(report, settings):
    """FOR INFO's table: the settings every node read has the same value of, the ones per node by nature (a row
    "(a value per node)" when most have their own), each with what it is for the first time -> [line]."""
    entries = []
    for path, values in settings:
        if len(values) > 1 and _variable(path) not in PER_NODE_KEYS:
            continue  # (under TO REVIEW)
        if len(values) > 5 and all(len(m) == 1 for dummy, m in values):
            rows = [("(a value per node)", _who(report, [n for dummy, m in values for n in m]), "in host_vars")]
        else:  # (a template: each node gets its own value from it)
            rows = [(_shown_value(text, path), _who(report, members), "a template, worked out on each node"
                     if "{{" in text else "") for text, members in values]
        words = _say(report, path)
        if words != path:
            first = rows[0]
            rows[0] = (first[0], first[1], (first[2] + "  " if first[2] else "") + words[len(path) + 1:])
        entries.append((path, rows))
    return out.settings_lines(entries, indent="    ", width=report.width)


def _info_blocks(report, data, leftovers, convergence):
    """FOR INFO: [(title, lines)], the ones with lines only."""
    blocks = [("Settings that are not the collection's default (the same on every node, or one per node by nature;"
               " the ones that differ are in report.txt)", _value_rows(report, data["settings"]))]
    blocks.append(("Versions", _versions_lines(report)))
    if convergence:
        blocks.append(("Convergence: how close the nodes are to sharing every setting",
                       ["  " + line if line else "" for line in convergence[1:]]))
    by_nodes = []
    for label, members in data["left_groups"].items():
        entry = next((e for e in by_nodes if e[1] == sorted(members)), None)
        if entry is None:
            by_nodes.append([[label], sorted(members)])
        else:
            entry[0].append(label)
    blocks.append(("Parts Ansible leaves as each node has them (*_manage: false)",
                   [line for labels, members in by_nodes for line in fill_text(
                       "%s   %s" % (", ".join(labels), _who(report, members)), report.width, "    ", "    ")]))
    if data["tuning"]:
        blocks.append(("OS tuning: the live value of each setting (and the collection's)", out.settings_lines(
            [(name, [(_short(text), _who(report, members), "") for text, members in values])
             for name, values in data["tuning"]], indent="    ", width=report.width)))
    cleanup = _by_value([(n["name"], ": ".join(line.split(": ", 2)[:2]), line.split(": ", 2)[2])
                         for n in report.read for line in n.get("cleanup") or [] if line.count(": ") >= 2])
    blocks.append(("Java options the running Java does not use (left out of the lines moved, else left as they are)",
                   [line for name, values in cleanup for text, members in values
                    for line in _entry(report, name, members, text)]))
    files = []
    for key, what in (("stale", "removed (an earlier import's, not written again)"),
                      ("replaced", "replaced (without the import's first line; the old one kept as <file>.<date>~)"),
                      ("kept", "kept as they are (not the import's)")):
        if leftovers.get(key):
            files += fill_text("%s%s: %s" % ("would be " if report.check else "", what, ", ".join(leftovers[key])),
                               report.width, "    ", "  ")
    blocks.append(("Files of an earlier import", files))
    blocks.append(("Not read from the nodes, so not in the inventory (your group_vars/all may set them)",
                   fill_text(", ".join(NOT_READ), report.width, "    ")))
    return [(title, lines) for title, lines in blocks if lines]


def _versions_lines(report):
    """Cassandra, Java and the OS of the nodes read, the same ones together -> [line]."""
    found = {}
    for n in report.read:
        v = n.get("versions") or {}
        text = "Cassandra %s, Java %s, %s" % (v.get("cassandra") or "?", v.get("java") or "?", v.get("os") or "OS ?")
        found.setdefault(text, []).append(n["name"])
    if not any(n.get("versions") for n in report.read):
        return []
    return [line for text, members in found.items() for line in fill_text(
        "%s   %s" % (text, unbreakable(_who(report, members))), report.width, "    ", "    ")]


# --- the head, what to do next, the details --------------------------------------------------------------------

def _head_lines(report, cluster, hosts_file, report_file, self_check_ok, same, steps, reviews):
    """The head: the verdict with its counts, the self-check, the files written, where the report is -> [line]."""
    acted = len(steps) + sum(n for dummy, n, dummy2 in reviews)
    counts = (["%d TO DO" % len(steps)] if steps else []) + [
        "%d %s" % (n, kind.one if n == 1 else kind.many) for kind, n, dummy in reviews]
    nodes = out.plural(len(report.read) + len(report.unread), "node") + (
        " (%d read)" % len(report.read) if report.unread else "")
    verdict = "%d to %s: %s" % (acted, "act on" if steps else "review", ", ".join(counts)) if acted \
        else "nothing to act on"
    lines = fill_text("IMPORT %s%s  %s  %s" % (cluster, " (--check, nothing written)" if report.check else "", nodes,
                                               verdict), report.width, "", "  ")
    check_step = next((i + 1 for i, step in enumerate(steps) if step.title == "Make the self-check pass"), None)
    lines += fill_text("self-check %s" % (
        "passed: the inventory gives every node that was read the settings it has now" if self_check_ok
        else "not passed yet: TO DO step %d" % check_step if check_step
        else "not passed yet: the inventory does not reproduce the nodes"), report.width, "  ", "  ")
    with_vars = sorted(h for h, v in (report.layout.get("host_vars") or {}).items() if v)
    written = ", ".join([out.path_from(hosts_file, report.cwd), "group_vars/%s*/" % cluster]
                        + ([_host_dirs(with_vars, report.full)] if with_vars else []))
    lines += fill_text("%s %s" % ("unchanged:" if same else "would write:" if report.check else "written:", written),
                       report.width, "  ", "  ")
    if not report.screen:  # (the screen ends with it)
        lines.append("  report:  %s" % out.path_from(report_file, report.cwd))
    return lines


def _about_lines(report, cluster):
    """What this report is, in 3 lines, then the words it uses."""
    lines = ["WHAT THIS REPORT IS"] + [cut_text(text, report.width) for text in (
        "  import_cluster read the nodes (the Cassandra servers) of %s and wrote an Ansible inventory for them;"
        " nothing on the nodes was changed." % cluster,
        "  report.txt says what to do and what is worth a look; this file holds the rest: the settings, the convergence,"
        " the OS, what was read on each node.",
        "  The cluster keeps running as it is while you read: nothing here is urgent for it.")]
    lines += ["", "WORDS"]
    for word, meaning in WORDS_USED:
        lines += fill_text("%s  %s" % (word, meaning), report.width, "  ", "    ")
    return lines


# the words of the report, in plain words
WORDS_USED = (
    ("inventory", "the files that describe the nodes to Ansible: group_vars/<group>/ for a group of nodes,"
                  " host_vars/<node>/ for one node"),
    ("dc1, dc1/r1", "a data center (a group of nodes that holds its own copy of the data), a rack in it"),
    ("all - n4, dc1 + n7", "node lists: every node but n4; the nodes of dc1 and n7 too; the count follows"),
    ("self-check", "the import works out the files the inventory would write on each node and compares them with"
                   " the node's own"),
    ("apply_config", "the playbook that writes the config files from the inventory"),
    ("-e name=value", "an option: add it to the import command"),
)


def _next_lines(report, steps, reviews, self_check_ok, inventory_dir, in_git, changes, changed, removed, same, moves,
                inventory, hosts):
    """WHAT TO DO NEXT: the next steps, numbered, the commands to copy -> [line]."""
    items = []
    if steps:
        items.append("Do the TO DO steps above, in their order, then import again: the head should say nothing to"
                     " act on, or things to review only.")
    if reviews:
        items.append("Read the sections above: each entry says what to do; none of it stops you from using the"
                     " inventory.")
    if report.check and not same:
        items.append("Write the inventory: the same command without --check%s."
                     % _what_changes(changed, removed, changes))
    if self_check_ok and not report.check and not same:
        git = out.in_git_work_tree(inventory_dir) if in_git is None else in_git
        items.append("Review the files written, then keep them: %s." % unbreakable("git diff && git commit") if git
                     else "Review the files written in %s." % out.path_from(inventory_dir, report.cwd))
    if self_check_ok:
        apply_check = out.command("apply_config", inventory=inventory or None, hosts=hosts or None,
                                  extra=["--check", "--diff"], cwd=report.cwd)
        topology = out.command("topology", inventory=inventory or None, hosts=hosts or None, extra=["--check"],
                               cwd=report.cwd)
        items.append("Check that the inventory changes nothing on the nodes (these two commands only look, they"
                     " change nothing): %s %s expect %s; %s %s expect nothing to do." % (
                         unbreakable(apply_check), ARROW_RIGHT,
                         "%s rewritten (same Java options, see HAND EDITS), nothing else" % ", ".join(moves) if moves
                         else "nothing to apply", unbreakable(topology), ARROW_RIGHT))
    if not items:
        items.append("Nothing: the inventory matches the nodes.")
    lines = ["WHAT TO DO NEXT"]
    for i, text in enumerate(items):
        lines += fill_text("%d. %s" % (i + 1, text), report.width, "  ", "   ")
    return lines


def _details_lines(layout, self_check):
    """DETAILS: where everything above came from, line by line: the self-check's notes, the layout's report ->
    [line]."""
    lines = ["DETAILS: what the import read on each node"]
    for name, c in sorted((self_check or {}).items()):
        lines += ["  %s: %s" % (name, note) for note in c.get("notes") or []]
    addresses = dict((n["name"], n.get("address") or "") for n in layout.get("nodes") or [])
    return lines + ["  " + line if line else "" for line in _grouped(layout.get("report") or "", addresses)]


def _indent(line):
    return len(line) - len(line.lstrip())


def _grouped(text, addresses=None):
    """The layout's report with its node blocks ("== n3" and the lines under it) grouped: the lines every node has
    once, under "== n1, n2.. (N nodes): what every one has", then each node's own lines only, under the lines they
    belong to (a node's name in its lines read as <node>, its address, addresses: {name: address}, as <its
    address>) -> [line]. One node, or none: as it is."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if re.match(r"^\s*== \S+\s*$", line)]
    if len(starts) < 2:
        return lines
    ends = starts[1:] + [next((i for i in range(starts[-1] + 1, len(lines))
                               if lines[i].strip() and _indent(lines[i]) <= _indent(lines[starts[-1]])), len(lines))]
    blocks = []
    for start, end in zip(starts, ends):
        name = lines[start].strip()[3:].strip()
        body = [re.sub(r"(?<![\w.-])%s(?![\w.-])" % re.escape(name), "<node>", line) for line in lines[start + 1:end]]
        address = (addresses or {}).get(name) or ""
        if address and address != name:
            body = [re.sub(r"(?<![\w.:-])%s(?![\w:-]|\.\d)" % re.escape(address), "<its address>", line) for line in body]
        while body and not body[-1].strip():
            body.pop()
        blocks.append((name, body))
    shared = [line for line in blocks[0][1] if line.strip() and all(line in b for dummy, b in blocks[1:])]
    head = lines[starts[0]][:_indent(lines[starts[0]])]
    names = [name for name, dummy in blocks]
    out_lines = lines[:starts[0]] + ["%s== %s (%d nodes): what every one has" % (head, ", ".join(names), len(names))]
    out_lines += shared
    for name, body in blocks:
        own = [i for i, line in enumerate(body) if line.strip() and line not in shared]
        if not own:
            continue
        keep = set(own)
        for i in own:  # (the lines it belongs to: each less indented line above it)
            level = _indent(body[i])
            for j in range(i - 1, -1, -1):
                if body[j].strip() and _indent(body[j]) < level:
                    keep.add(j)
                    level = _indent(body[j])
        out_lines += ["", "%s== %s: its own" % (head, name)] + [body[i] for i in sorted(keep)]
    return out_lines + ([""] if lines[ends[-1]:ends[-1] + 1] not in ([], [""]) else []) + lines[ends[-1]:]


# a section's heading: capitals from column 0, then what it holds ("FOR INFO  in the report: ...", "WHERE ...  (...)",
# "DETAILS: ...", "CONVERGENCE — ...")
SECTION = re.compile(r"^([A-Z][A-Z-]+(?: [A-Z-]+)*)(?:$|  \((.*)\)$|  (.*)$| — (.*)$|: (.*)$)")


def _ruled(lines, width, screen):
    """Each section after the first line: its title in a rule of = (==== TO REVIEW ====...), what it holds on the
    line under it, to tell the sections apart at a glance; the rule as wide as the longest line, 100 at most (never
    wider than the text). In the report two blank lines above it and one under it. On the screen, the ops callback
    draws it as its heavy rule, and the blank lines inside a section are " ": blank there, where an empty line is a
    light rule (one is left above the screen's last line, the report's name) -> [line]."""
    room = min(width, 100, max(len(line) for line in lines))
    ruled = []
    for i, line in enumerate(lines):
        heading = SECTION.match(line) if i else None
        if heading:
            while ruled and not ruled[-1].strip():
                ruled.pop()
            title = "==== %s " % heading.group(1)
            ruled += [" ", title + "=" * max(4, room - len(title)), " "] if screen else \
                ["", "", title + "=" * max(4, room - len(title)), ""]
            about = next((g for g in heading.groups()[1:] if g), "")
            ruled += fill_text(about[:1].upper() + about[1:], width, "  ", "  ")
            continue
        if screen and not line and i + 1 < len(lines) and not lines[i + 1].startswith("full report"):
            line = " "
        ruled.append(line)
    return ruled


def cassandra_import_report(layout, written, report_file, self_check, self_check_ok, secrets_clear=None,
                            leftovers=None, check=False, inventory="", hosts="", allow_unread=False, screen=False,
                            self_check_error="", cwd=None, in_git=None, changes=None, todo=None, width=None,
                            strict=False, convergence=None, shareable=False, details=False):
    """layout: cassandra_inventory_layout's (its nodes); written: [the hosts file, the inventory dir];
    report_file: where report.txt goes (report.check.txt under --check); self_check: {node: {differences, notes}};
    self_check_ok; secrets_clear: the secrets.yml files written in clear; leftovers: cassandra_inventory_leftovers';
    check: --check; inventory: the -i of the next commands ('' : ansible.cfg's); hosts: their -e cassandra_hosts
    ('' when the inventory holds this cluster alone); allow_unread: import_cluster_allow_unread; screen: the end
    of the run (report.txt shorter: the differences and the files kept without their steps); cwd:
    paths shown relative to it; in_git: the inventory dir is in a git work tree (None: looked for); changes: what
    the run wrote or would write, {changed: [the files written that change], removed: [the files removed],
    existed: [the files there before]}, paths in the inventory dir (None: not known); todo: more TO DO steps (the
    CQL login); width: the lines fit in it (default: the terminal's for the screen, REPORT_WIDTH for the report);
    strict: import_cluster_strict (the run stops on a failed self-check, its HOW TO MAKE IT PASS below the
    screen); convergence: the lines of the CONVERGENCE section (cassandra_import_convergence's), under FOR INFO;
    shareable: import_cluster_report_shareable (the screen names the copy too); details: the lines of
    report.details.txt instead (the settings, versions, convergence, OS, what was read on each node). Returns the
    lines.
    e.g. -> ["IMPORT big_cluster  88 nodes  3 to act on: 3 hand edits", "  self-check passed: ...", ...]."""
    cluster = layout["cluster_group"]
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    leftovers = leftovers or {}
    hosts_file, inventory_dir = written
    allow_unread = boolean(allow_unread, strict=False)
    width = int(width) if width else (terminal_width() if screen else REPORT_WIDTH)
    data = report_sections(layout, self_check)
    yours = layout.get("yours") or []
    report = _Report(layout, read, [n for n in nodes if n not in read], [n["name"] for n in read], data["everyone"],
                     data["groups"], group_chains(layout.get("hosts") or {}), not screen, width, screen, check, cwd,
                     yours, _standard(yours), set(), out.path_from(report_file, cwd), inventory, hosts)
    changed, removed, same = _inventory_changes(changes)
    moves = sorted({f for n in read for f in n.get("rewritten") or []})

    if boolean(details, strict=False):
        return _finished(_details_report(report, data, layout, self_check, leftovers, convergence, cluster), width,
                         False)
    steps = _todo_steps(report, allow_unread, self_check_ok, self_check_error, data["check_items"], secrets_clear,
                        leftovers, inventory_dir, list(todo or []), in_git, inventory, hosts)
    differs = _differs(report, data["settings"])
    reviews = _review_blocks(report, data, differs, allow_unread)
    lines = _head_lines(report, cluster, hosts_file, report_file, self_check_ok, same, steps, reviews)
    if steps:
        lines += ["", "TO DO"]
        for i, step in enumerate(steps):
            lines += _step_lines(i + 1, step, width)
        if not self_check_ok and not screen:
            lines += [""] + _self_check_section(report, self_check, report_file, self_check_error, allow_unread,
                                                inventory, hosts)
    blocks = dict((kind, entries) for kind, dummy, entries in reviews)
    if TO_CHECK in blocks:
        lines += ["", "WORTH A LOOK"] + blocks[TO_CHECK]
    hand = [kind for kind in (HAND, MOVED) if kind in blocks]
    if hand:
        lines += ["", "HAND EDITS"]
        for kind in hand:
            lines += fill_text("%s: %s" % (kind.title, kind.about), width, "  ") + blocks[kind]
    if DIFFERS in blocks:
        lines += ["", "DIFFERENT ON SOME NODES"] + blocks[DIFFERS]
        if screen:
            lines += fill_text("where each value is set, and how to give every node one: %s"
                               % out.path_from(report_file, cwd), width, "  ", "  ")
    left = _left_alone_lines(report)
    if KEPT in blocks or left:
        lines += ["", "LEFT AS THE NODES HAVE THEM"] + blocks.get(KEPT, []) + left
        if screen and KEPT in blocks:
            lines += [""] + fill_text("how to give a file back to Ansible, step by step: %s"
                                      % out.path_from(report_file, cwd), width, "  ", "  ")
    if UNREAD in blocks:
        lines += ["", "NODES NOT READ"] + blocks[UNREAD]
    lines += [""] + _next_lines(report, steps, reviews, self_check_ok, inventory_dir, in_git, changes, changed,
                                removed, same, moves, inventory, hosts)
    details_file = re.sub(r"\.txt$", "", report_file) + ".details.txt"
    if screen:
        lines += ["", "full report%s: %s" % (" (--check)" if check else "", out.path_from(report_file, cwd))]
        if boolean(shareable, strict=False):
            lines.append("a copy to share (names, addresses and secrets replaced; check it before sharing): %s"
                         % out.path_from(re.sub(r"\.txt$", "", report_file) + ".shareable.txt", cwd))
    else:
        lines += ["", "everything else (the settings, the convergence, the OS, each node): %s"
                  % out.path_from(details_file, cwd)]
    return _finished(lines, width, screen)


def _finished(lines, width, screen):
    """The lines as written: no trailing space, one blank line at most, each section ruled, no blank line last."""
    lines = [line.rstrip() for line in lines]
    lines = [line for i, line in enumerate(lines) if line or not i or lines[i - 1]]  # (one blank line at most)
    lines = _ruled(lines, width, screen)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _details_report(report, data, layout, self_check, leftovers, convergence, cluster):
    """report.details.txt: what the report is and its words, where the settings go, the settings, versions,
    convergence and OS (FOR INFO), then what was read on each node (DETAILS) -> [line]."""
    lines = ["IMPORT %s  the details of report.txt" % cluster, ""] + _about_lines(report, cluster)
    where = _where_lines(report, data["settings"])
    if where:
        lines += ["", "WHERE THE SETTINGS GO  (the inventory's files; a value set in yours is not written again)"] + where
    lines += ["", "FOR INFO"]
    for title, entries in _info_blocks(report, data, leftovers, convergence):
        lines += fill_text(title, report.width, "  ") + entries
    return lines + [""] + _details_lines(layout, self_check)


def _self_check_section(report, self_check, report_file, self_check_error, allow_unread, inventory, hosts):
    """SELF-CHECK (of a failed one, under TO DO): what the roles would change, grouped by what makes it pass, each
    node's differences under it -> [line]."""
    verdict, paragraphs = _self_check_lines(
        report.layout, self_check, lambda m: _who(report, m), min(report.width, SELF_CHECK_WIDTH),
        report_file=report_file, error=self_check_error, allow_unread=allow_unread, inventory=inventory,
        hosts=hosts, cwd=report.cwd, report=True)
    lines = ["  SELF-CHECK %s %s" % (DASH, verdict)]
    for p in paragraphs:
        lines += ["  " + line if line else "" for line in p] + [""]
    return lines[:-1]


class FilterModule(object):
    def filters(self):
        return {"cassandra_import_report": cassandra_import_report,
                "cassandra_import_self_check_advice": cassandra_import_self_check_advice}
