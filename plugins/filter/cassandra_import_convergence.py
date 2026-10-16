# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""How far an imported cluster is from one group, every setting in group_vars (import_cluster).

cassandra_import_convergence: the import's layout -> the lines of the CONVERGENCE section of report.txt (or, with
    screen true, of the end of the run): how many settings every node shares, each other one with its values and
    their nodes, what to do to give every node the value most of them have (the files to edit, how many nodes
    change, whether apply_config restarts them, the command to check it), and the files the nodes keep as edited
    by hand (import_cluster_keep_hand_edits): what differs from the template, which variable would carry it.
    Read only: from what the import found; secrets never shown.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import os
import re

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import FILES, _THEN
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IDENTITY, KEEP, PER_NODE, ROLE, SERIES, _chains, _hand_edits_kept, _template_vars, _undefined)
from ansible_collections.community.cassandra.plugins.filter.cassandra_output import terminal_width
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    ARROW_RIGHT, DASH, REPORT_WIDTH, SCREEN_VALUE, _aligned, _column, _cut, _fill, _sections, _who, _whole)
from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

# the files cassandra_config writes from its series' templates: a change in one is a restart (apply_config's rule);
# the JMX files' templates are not a series' (cassandra_jmx_users: _kind)
TEMPLATES = tuple(f for f in FILES if not f.startswith("jmxremote."))
# an owner, group or mode only: apply_config writes them without a restart (Cassandra reads them when it starts)
PERMS = ("cassandra_config_user", "cassandra_config_group", "cassandra_config_mode", "cassandra_config_public_mode",
         "cassandra_config_file_permissions")
# where the node keeps its data (or its config): another value is a move by hand, not a config change
MOVES = ("cassandra_conf_dir", "cassandra_data_dir", "cassandra_data_file_directories",
         "cassandra_commitlog_dir", "cassandra_hints_dir", "cassandra_saved_caches_dir")
# the logins the playbooks use (nodetool, cqlsh), not written to the nodes: the nodes' own first
LOGINS = ("cassandra_jmx_username", "cassandra_jmx_password", "cassandra_jmx_password_file", "cassandra_cql_username",
          "cassandra_cql_password")
# sized for the node's machine: one value only where the machines are alike
HARDWARE = ("cassandra_heap_size", "cassandra_heap_newsize", "cassandra_max_direct_memory_size")
# what a node runs: the newest for all, by an upgrade, never down
VERSIONS = ("cassandra_version", "cassandra_package_version", "cassandra_java_version", "cassandra_medusa_version",
            "cassandra_dsbulk_version")
# cluster-wide switches: node by node, a cluster split or clients locked out without their procedure (an *_optional
# step first)
SWITCHES = ("cassandra_internode_encryption", "cassandra_client_encryption_enabled", "cassandra_authenticator",
            "cassandra_authorizer", "cassandra_role_manager", "cassandra_network_authorizer",
            "cassandra_cidr_authorizer_class_name", "cassandra_tde_enabled")
# per node by nature, or kept by each node for life (a node added later gets the group's): not counted
NOT_COUNTED = tuple(PER_NODE) + ("cassandra_rackdc_dc",) + tuple(k for k in IDENTITY if k != "cassandra_seeds")
SCREEN_ACTIONS = 5  # the actions the end of the run shows (the report: every one)
LIMIT_MOST = 10  # a check --limit to the nodes that change, up to this many (more: the whole cluster)
# what a change does, by kind: (the order of the actions, the safest first; what the report says; what the screen
# says; apply_config --check --diff shows it)
_KINDS = {"none": (0, _THEN["none"][0], _THEN["none"][0], True),
          "os": (0, "%s (cassandra_linux, applied live)" % _THEN["os"][0], _THEN["os"][0], True),
          "restart": (1, _THEN["restart"][0], _THEN["restart"][0], True),
          "seeds": (1, "no restart (change_seeds applies it live)", "no restart (change_seeds)", True),
          "switch": (2, "then restart, node by node: a cluster-wide switch, its procedure first (its *_optional"
                        " step, then the switch)", "then restart (a cluster-wide switch)", True),
          "keystore": (2, "then restart, which fails unless the file on the node matches it: check it there first",
                       "then restart (keystore)", True),
          "unknown": (3, "restart unknown (apply_config does not write it)", "restart unknown", False),
          "login": (4, "none on the nodes: the playbooks' login, which the nodes must accept first (their"
                       " jmxremote.password, cassandra_jmx_users; their CQL role)", "their login first", False),
          "version": (5, "an upgrade (upgrade, update_java), never down", "an upgrade", False),
          "move": (5, "moved by hand first, the node stopped (apply_config would restart it on the new path)",
                   "moved by hand first", True)}


def _kind(key):
    """What apply_config does after writing key on a running node: none (an owner or mode), os (cassandra_linux's,
    live), restart (a config file), keystore (a config file naming a keystore), seeds (change_seeds' live reload),
    login (the playbooks' own), move (where its data is), unknown (not its own)."""
    if key in PERMS:
        return "none"
    if key == "cassandra_seeds":
        return "seeds"
    if key in MOVES:
        return "move"
    if key in LOGINS:
        return "login"
    if key in VERSIONS:
        return "version"
    if key in SWITCHES:
        return "switch"
    if key == "cassandra_linux_timesync":  # (synced in the node's restart window: apply_config restarts it)
        return "restart"
    if key == "cassandra_rpm_conf_alternative":  # (apply_config moves the config to it, then restarts)
        return "restart"
    if key.startswith("cassandra_linux_") or key in ("cassandra_data_readahead_kb", "cassandra_data_disk_tuning"):
        return "os"
    # (the users go to jmxremote.password and .access, whose templates are not a series')
    if key == "cassandra_jmx_users" or any(key in _template_vars(name) for name in TEMPLATES):
        return "keystore" if re.search(r"keystore|truststore", key) else "restart"
    return "unknown"


_ABSENT = object()


def _leaf(key, path, value):
    """The value of path (key, or key.sub.sub of a dict) in key's value, _ABSENT when it has none."""
    return dict(out.leaves(key, value)).get(path, _ABSENT)


def _shared(layout, path):
    """A file of yours above the cluster's groups (group_vars/all, a group of several clusters): every cluster of the
    inventory dir reads it, never edited for this one (group_vars/<cluster>/ wins over it)."""
    parts = path.split("/")
    if parts[0] != "group_vars":
        return False
    group = re.sub(r"\.(ya?ml|json)$", "", parts[1])
    return group not in set(g for chain in _chains(layout.get("hosts") or {}).values() for g in chain)


def _places(layout, key, path=None, target=_ABSENT, same=False):
    """Where the inventory keeps key below the cluster's group: [the group_vars and host_vars dirs, your files there
    (not a shared one: _shared)]; path and target: only the ones whose value of path is not target (same: is
    target)."""
    cluster = layout["cluster_group"]

    def other(variables):
        return key in (variables or {}) and (path is None or (_leaf(key, path, variables[key]) == target) == same)

    groups = sorted(g for g, v in (layout.get("group_vars") or {}).items() if g != cluster and other(v))
    hosts = [h for h, v in (layout.get("host_vars") or {}).items() if other(v)]
    yours = set(v[key]["path"] for v in (layout.get("from_yours") or {}).values()
                if key in v and not _shared(layout, v[key]["path"])
                and (path is None or (_leaf(key, path, v[key]["value"]) == target) == same))
    if not same:  # (and the ones whose value the import wrote over for these nodes: theirs would win once removed)
        yours |= set(y["path"] for y in layout.get("yours") or [] if y["key"] == key and not _shared(layout, y["path"])
                     and (path is None or _leaf(key, path, y["yours"]) != target))
    yours = sorted(yours)
    places = []
    if len(groups) <= 2:
        places += ["group_vars/%s/" % g for g in groups]
    else:
        places.append("group_vars of %d groups (%s)" % (len(groups), ", ".join(groups)))
    if len(hosts) == 1:
        places.append("host_vars/%s/" % hosts[0])
    elif hosts:
        places.append("host_vars of %s (%s)" % (out.plural(len(hosts), "node"), out.full_list(hosts)))
    return places + ["yours: %s" % p for p in yours]


def _value_of(layout, node, key):
    """A node's value of key, from the import or from your files (_ABSENT: the collection's default)."""
    if key in (node.get("vars") or {}):
        return node["vars"][key]
    mine = (layout.get("from_yours") or {}).get(node["name"]) or {}
    return mine[key]["value"] if key in mine else _ABSENT


def _version(text):
    """A version's order: its numbers, a pre-release (4.0~beta1, 4.1-rc2) before its release; a text without one
    (the collection's default) last."""
    found = re.match(r"^(?:\d+:)?(\d+(?:\.\d+)*)(.*)$", str(text))  # (a package's epoch left out)
    if not found:
        return ([], 0, [])
    rest = found.group(2)
    pieces = [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", rest)]
    return ([int(p) for p in found.group(1).split(".")], 0 if re.search(r"(?i)~|alpha|beta|rc|pre", rest) else 1,
            pieces)


def _common(values, names):
    """values: [(text, [nodes])] of a setting: one value, on every node read (names)."""
    return len(values) == 1 and sorted(values[0][1]) == sorted(names)


def _setting_items(layout, data, read):
    """The settings the read nodes do not all share -> [{name, key, values: [(text, [nodes])] most nodes first,
    target, raw (its value), changing: [nodes], without: [the changing nodes without the variable], kind, rank,
    unmanaged: [the changing nodes cassandra_linux leaves as they are]}], the safest change first, then the fewest
    nodes; and how many settings are counted."""
    items, counted = [], 0
    names = [n["name"] for n in read]
    for path, values in data["settings"]:
        key = data["variable"][path]
        if key in NOT_COUNTED or (path == key and any(p.startswith(key + ".") for p in data["variable"])):
            continue  # (a dict empty on some node: its sub-keys' rows say what differs)
        counted += 1
        values = sorted(values, key=lambda v: (-len(v[1]), str(v[0])))
        if key in VERSIONS:  # (the newest first, whatever the count)
            values = sorted(values, key=lambda v: _version(v[0]), reverse=True)
        if _common(values, names):
            continue
        target, keeping = values[0]
        tie = len(values) > 1 and len(values[1][1]) == len(keeping) and key not in VERSIONS
        first = next(n for n in read if n["name"] == keeping[0])
        value = _value_of(layout, first, key)
        changing = [n for n in read if n["name"] not in keeping]
        items.append({"name": path, "key": key, "values": values, "target": target, "kind": _kind(key), "tie": tie,
                      "raw": _ABSENT if value is _ABSENT else _leaf(key, path, value),
                      "changing": [n["name"] for n in changing],
                      "without": [n["name"] for n in changing if _value_of(layout, n, key) is _ABSENT],
                      "unmanaged": [n["name"] for n in changing
                                    if (n.get("keep") or {}).get("cassandra_linux_manage") is False]})
        kind = items[-1]["kind"]
        items[-1]["rank"] = max(_KINDS[kind][0], _KINDS["unknown"][0] if key in HARDWARE else 0)
    items.sort(key=lambda i: (i["rank"], len(i["changing"]), i["name"]))
    return items, counted


def _edit_file(line):
    """The file a hand edit's head line names ("cassandra.yaml, line 12:", "cassandra-rackdc.properties: dc") and
    the rest of it."""
    head = re.split(r"[:,]", line, maxsplit=1)
    return head[0], head[1] if len(head) > 1 else ""


def _blocks(lines):
    """A file's hand edit lines -> [(where, [removed lines], [added lines])]: a block per "<file>, line N:" or
    "<file>: <key>" head (where: "line N", the key), a note on its own line as where alone."""
    blocks = []
    for line in lines:
        if not line.startswith(" "):
            blocks.append((_edit_file(line)[1].strip().rstrip(":"), [], []))
        elif blocks and line.strip().startswith("- "):
            blocks[-1][1].append(line.lstrip()[2:])
        elif blocks and line.strip().startswith("+ "):
            blocks[-1][2].append(line.lstrip()[2:])
    # (masked a block at a time: a secret's YAML block value is on the lines under its key)
    return [(where, out.mask("\n".join(removed)).split("\n") if removed else [],
             out.mask("\n".join(added)).split("\n") if added else []) for where, removed, added in blocks]


_EXPR = re.compile(r"\{\{.*?\}\}")
_TOGGLE = re.compile(r"^\{\{[^}]*\}\}")  # a line written commented out or not: {{ '' if x else '# ' }}line
# the lines a list adds after a line (cassandra_env_extra_lines, cassandra_logback_extra_lines): not that line's value
_APPENDED = re.compile(r"\{\{[^{}]*\| join\([^{}]*\}\}$")
_LINES = {}


def _template_lines(name):
    """The lines of the cassandra_config templates of file name (every series) that a variable writes -> [(the text
    before its value, the text after it, its variable)], the longest first."""
    if name not in _LINES:
        found = set()
        for series in SERIES.values():
            path = os.path.join(ROLE, "templates", series, name + ".j2")
            if not os.path.exists(path):
                continue
            with open(path) as f:
                for line in f.read().split("\n"):
                    line = _APPENDED.sub("", _TOGGLE.sub("", line).strip())
                    first = _EXPR.search(line)
                    used = re.search(r"\b(cassandra_\w+)", first.group(0)) if first else None
                    if used and len(line[:first.start()].strip()) >= 3:
                        last = list(_EXPR.finditer(line))[-1]
                        found.add((line[:first.start()], line[last.end():], used.group(1)))
        _LINES[name] = sorted(found, key=lambda f: (-len(f[0]), f))
    return _LINES[name]


def _carry(name, line):
    """The variable that would write a hand edit's added line in the file name, and the value it would take:
    (variable, value), (None, None) when no variable does (a comment: no effect)."""
    text = line.strip()
    if not text or text.startswith(("#", "<!--")):
        return None, None
    matches = [(before, after, variable) for before, after, variable in _template_lines(name)
               if text.startswith(before.strip()) and (not after.strip() or text.endswith(after.strip()))]
    if matches:
        before, after, dummy = matches[0]
        variables = sorted(set(v for b, a, v in matches if b == before))
        value = text[len(before.strip()):len(text) - len(after.strip()) if after.strip() else None].strip()
        return " or ".join(variables), value.strip("'\"")
    options = re.match(r"^jvm(\d*)-server\.options$", name)
    if options and text.startswith("-"):
        heap = re.match(r"^-Xm([xsn])(\S+)$", text)
        if heap:
            return "cassandra_heap_newsize" if heap.group(1) == "n" else "cassandra_heap_size", heap.group(2)
        return "cassandra_jvm%s_extra_options" % options.group(1), "[%s] (if no other line of the file sets it)" % text
    if name == "cassandra-env.sh":
        option = re.match(r"""^JVM_OPTS=["']\$JVM_OPTS (-\S+?)["']$""", text)
        return ("cassandra_jvm_extra_options", "[%s]" % option.group(1)) if option else (None, None)
    if name == "cassandra-rackdc.properties":
        key = re.match(r"^(prefer_local|dc|rack)\s*=\s*(.*)$", text)
        if key:
            return {"prefer_local": "cassandra_prefer_local", "dc": "cassandra_rackdc_dc",
                    "rack": "cassandra_rackdc_rack"}[key.group(1)], key.group(2)
    if name == "cassandra.yaml":
        key = re.match(r"^([a-z0-9_]+)\s*:\s*(.*)$", text)
        if key and not re.search(r"(?m)^#?\s*%s\s*:" % key.group(1), _template_text(name)):
            return "cassandra_extra_settings", "{%s: %s}" % (key.group(1), key.group(2))
    return None, None


@functools.lru_cache(maxsize=None)
def _template_text(name):
    """The cassandra_config templates of file name, every series, as one text."""
    texts = []
    for series in SERIES.values():
        path = os.path.join(ROLE, "templates", series, name + ".j2")
        if os.path.exists(path):
            with open(path) as f:
                texts.append(f.read())
    return "\n".join(texts)


def _kept_files(read):
    """The config files the nodes keep as edited by hand (cassandra_config_keep_files) -> [(file, [(its edit lines,
    [nodes])])], the same edits on several nodes once."""
    found, order = {}, []
    for n in read:
        kept, dummy = _hand_edits_kept(n)
        per_file = {}
        current = None
        for line in kept:
            if not line.startswith(" "):
                current = _edit_file(line)[0]
            per_file.setdefault(current, []).append(line)  # (masked by _blocks, a block at a time)
        for f in (n.get("keep") or {}).get("cassandra_config_keep_files") or []:
            if f not in found:
                found[f] = []
                order.append(f)
            lines = tuple(per_file.get(f) or ())
            entry = next((e for e in found[f] if e[0] == lines), None)
            if entry is None:
                found[f].append((lines, [n["name"]]))
            else:
                entry[1].append(n["name"])
    return [(f, found[f]) for f in order]


def _screen_rows(rows, indent, width):
    """[(name, text)] -> lines: the names in a column, the texts right of it; a row too long for a line on two, the
    text under its name, a little further in."""
    column = _column([name for name, dummy in rows])
    lines = []
    for name, text in rows:
        if len(name) < column and len(indent) + column + len(text) <= width:
            lines += _aligned([(name, text)], indent, width, column)
        else:
            lines += _fill(name, width, indent, "  ") + _fill(text, width, indent + "  ", "  ")
    return lines


def _values_text(item, who):
    """2 (84 nodes)   1 (5: db101..db105)   6 (1: db113)."""
    parts = []
    for i, (text, nodes) in enumerate(item["values"]):
        shown = _whole(_shown(text))
        parts.append("%s%s(%s)" % (shown, out.NBSP, out.plural(len(nodes), "node") if i == 0
                                   else "%d:%s%s" % (len(nodes), out.NBSP, who(nodes))))
    return "   ".join(parts)


def _then_text(item, screen=False):
    """3 nodes change, then restart (the report: why to take care, too)."""
    count = len(item["changing"])
    text = "%s %s, %s" % (out.plural(count, "node"), "changes" if count == 1 else "change",
                          _KINDS[item["kind"]][2 if screen else 1])
    if screen:
        return text
    if item["key"] in HARDWARE:
        text += "; sized for the machine: one value only where the machines are alike, else keep it per group"
    if item["kind"] == "os" and item["unmanaged"]:
        text += "; not on %s (cassandra_linux_manage: false: left as it is)" % out.full_list(item["unmanaged"])
    return text


def _shown(text):
    """A value as the convergence names it: a secret by its number, a node without it as such."""
    text = re.sub(r"[\r\n]+", " ", str(text))  # (a line of its own would be read as another one)
    secret = re.match(r"^\(in secrets\.yml(?:, value (\d+))?\)$", text)
    if secret:
        return "secret %s" % (secret.group(1) or "1")  # (the first value found: 1)
    return {"(collection's default)": "default", "(not set)": "not set"}.get(text, text)


def _check(nodes, kind, inventory, hosts, cwd):
    """The command that shows what a change does before it is made: apply_config --check --diff, --limit to the nodes
    that change when they are LIMIT_MOST at most; change_seeds --check for the seeds (every node, its own rule)."""
    playbook, extra = ("change_seeds", ["--check"]) if kind == "seeds" else ("apply_config", ["--check", "--diff"])
    limit = nodes if 0 < len(nodes) <= LIMIT_MOST and kind != "seeds" else None
    return out.command(playbook, inventory=inventory or None, hosts=hosts or None, limit=limit, extra=extra, cwd=cwd)


def _action(item, layout):
    """-> 2 for all: the value most nodes have, where to write it: a variable in the cluster's group_vars, removed
    from the files that keep it below; a sub-key of a dict set in the files that keep another value of it (the
    dict whole in the cluster's group_vars for the nodes without it)."""
    cluster = layout["cluster_group"]
    target = item["target"]
    sub = item["name"][len(item["key"]) + 1:] if item["name"] != item["key"] else ""
    secret = target.startswith("(in secrets.yml")
    where = "group_vars/%s/%s" % (cluster, "secrets.yml" if secret else "main.yml")
    value = target
    if secret:
        source = _places(layout, item["key"], item["name"], item["raw"], same=True) or [where]
        value = "%s (its value in %s)" % (_shown(target), source[0])
    mine = layout.get("from_yours") or {}
    shared = sorted(set(mine[n][item["key"]]["path"] for n in item["changing"]
                        if item["key"] in mine.get(n, {}) and _shared(layout, mine[n][item["key"]]["path"])))
    if not sub:
        places = ", ".join(_places(layout, item["key"]))
        if item["raw"] is _ABSENT:
            what = "remove %s from %s" % (item["key"], places) if places else ""
            if shared:
                what += "%sset it to the collection's default in %s (yours: %s sets it, for every cluster of the" \
                        " inventory)" % ("; " if what else "", where, ", ".join(shared))
        else:
            what = "%s: %s in %s%s" % (item["key"], value, where, ", removed from %s" % places if places else "")
    else:
        places = ", ".join(_places(layout, item["key"], item["name"], item["raw"]))
        if item["raw"] is _ABSENT:
            what = "remove %s from %s in %s" % (sub, item["key"], places or "the inventory")
        else:
            what = "%s: %s in %s in %s" % (sub, value, item["key"], places) if places else ""
            # nodes on the collection's default, or on a shared file of yours: the whole dict in the cluster's group
            alone = len(item["without"]) + len([n for n in item["changing"] if item["key"] in mine.get(n, {})
                                                and _shared(layout, mine[n][item["key"]]["path"])])
            if alone:
                source = _places(layout, item["key"], item["name"], item["raw"], same=True)
                what += "%sthe whole %s in %s, its other keys too (as in %s), with %s: %s (%s%s)" % (
                    "; " if what else "", item["key"], where, source[0] if source else "the nodes that have it",
                    sub, value, out.plural(alone, "node"),
                    " on yours: %s, which every cluster of the inventory reads" % ", ".join(shared) if shared
                    else " on the collection's default")
    tie = " (a tie: or another of the values)" if item["tie"] else ""
    return "%s %s for all%s: %s" % (ARROW_RIGHT, _shown(target), tie, what)


def _kept_lines(kept, layout, read, who, width, inventory, hosts, cwd):
    """The report's lines on the files kept as edited by hand: each one's nodes, its edits by variant, the variable
    that would carry each and its value, and how to give the file back to the roles."""
    lines = []
    everyone = sorted(n["name"] for n in read)
    for name, variants in kept:
        nodes = [n for dummy, members in variants for n in members]
        several = ", %d variants" % len(variants) if len(variants) > 1 else ""
        lines += _fill("%s   kept on %s%s" % (name, who(nodes), several), width, "  ", "    ")
        carried, loose = set(), 0
        for i, (edits, members) in enumerate(variants):
            blocks = _blocks(edits)
            if len(variants) > 1:
                lines += _fill("variant %d on %s:" % (i + 1, who(members)), width, "    ", "  ")
            if not blocks:
                lines += _fill("no edit read (kept by the inventory)", width, "      ")
            for where, removed, added in blocks:
                carries = []
                for line in added:
                    variable, value = _carry(name, line)
                    if variable:
                        carried.add(variable)
                        carries.append("%s: %s" % (variable, value) if value is not None else variable)
                    elif not line.strip().startswith(("#", "<!--")):
                        loose += 1
                        carries.append("no variable")
                if removed and not added:
                    loose += 1
                    carries.append("no variable (a line taken out)")
                text = "%s:  %s" % (where or "note", "   ".join(["- " + r for r in removed] + ["+ " + a for a in added]))
                lines += _fill("%s   %s %s" % (text, ARROW_RIGHT, "; ".join(carries) or "no effect (a comment)"),
                               width, "      ", "    ")
        alone = "host_vars/%s/" % nodes[0] if len(nodes) == 1 else \
            "host_vars of %s (%s)" % (out.plural(len(nodes), "node"), out.full_list(nodes))
        if carried & set(NOT_COUNTED) or len(variants) > 1:  # (a node's own: its rack, its DC; values per variant)
            place = alone
        elif sorted(nodes) == everyone:
            place = "group_vars/%s/" % layout["cluster_group"]
        else:
            place = "%s (in group_vars/%s/ instead, the %s get them too, then restart)" % (
                alone, layout["cluster_group"], out.plural(len(everyone) - len(nodes), "other node"))
        how = ("set %s in %s%s" % (", ".join(sorted(carried)), place,
                                   "; undo the edits no variable carries on the nodes, or keep the file" if loose else "")
               if carried else "no variable carries these edits: undo them on the nodes, or keep the file")
        lines += _fill("%s to give it back to the roles: %s; then take %s out of cassandra_config_keep_files (%s):"
                       " apply_config writes it, then restarts the node; check first: %s (what its diff still shows,"
                       " the roles would revert)" % (ARROW_RIGHT, how, name,
                                                     "the same host_vars" if place.startswith(alone) else alone,
                                                     _whole(_check(nodes, "restart", inventory, hosts, cwd))),
                       width, "    ", "  ")
    return lines


def cassandra_import_convergence(layout, self_check=None, screen=False, report_file="", inventory="", hosts="",
                                 cwd=None, width=None, self_check_ok=True):
    """layout: cassandra_inventory_layout's (over your files); self_check: {node: {differences, notes}} (the node
    groups of the screen and the report are made with it); screen: the end of the run (the score, the first
    SCREEN_ACTIONS actions, where the rest is), else the report's section (every setting that differs, the files
    kept by hand); report_file: the report (named on the screen); inventory, hosts, cwd: the next commands' -i, -e
    cassandra_hosts and current dir (cassandra_import_report's); width: the lines fit in it (the terminal's for the
    screen, REPORT_WIDTH for the report); self_check_ok: false, the inventory does not reproduce the nodes yet
    (the score only on the screen, nothing said done) -> the lines of the CONVERGENCE section, [] when no node was
    read.
    A setting is one row of SETTINGS (a dict one per sub-key); the addresses and the settings a node keeps for life
    are not counted. An error of its own is a line saying so (its message hidden: values), never the import's
    failure (the section is output only)."""
    try:
        return _convergence(layout, self_check, screen, report_file, inventory, hosts, cwd, width,
                            boolean(self_check_ok, strict=False))
    except Exception as exc:  # pylint: disable=broad-except
        if _undefined(exc):
            raise
        return ["CONVERGENCE  not worked out (%s in cassandra_import_convergence; the inventory and the rest of the"
                " report are not affected)" % type(exc).__name__]


def _convergence(layout, self_check, screen, report_file, inventory, hosts, cwd, width, self_check_ok):
    screen = boolean(screen, strict=False)
    full = not screen
    width = int(width) if width else (terminal_width() if screen else REPORT_WIDTH)
    nodes = layout.get("nodes") or []
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    if not read:
        return []
    data = _sections(layout, self_check)
    groups = data["groups"]

    def who(members):
        return _who(members, None, groups, full)

    # the inventory's own groups (a DC's, a rack's) by their nodes read: a value on exactly these nodes says it
    in_group = {}
    for host, chain in _chains(layout.get("hosts") or {}).items():
        for group in chain[1:]:
            in_group.setdefault(group, set()).add(host)
    named = dict((frozenset(m & set(n["name"] for n in read)), g) for g, m in sorted(in_group.items(), reverse=True))

    def who_named(members):
        group = named.get(frozenset(members))
        return "%s (%s)" % (group, who(members)) if group and full and len(members) > 1 else who(members)

    items, counted = _setting_items(layout, data, read)
    kept = _kept_files(read)
    cluster = layout["cluster_group"]
    score = "%d/%d settings common to all nodes" % (counted - len(items), counted)
    if items:
        score += ", %d differ" % len(items)
    if kept:
        score += ", %s kept by hand" % out.plural(len(kept), "file")
    if not items and not kept and self_check_ok:
        score += " %s one group, every setting in group_vars/%s/" % (DASH, cluster)
    unread = len(nodes) - len(read)
    if unread:
        score += " (%s not read: not counted)" % out.plural(unread, "node")
    if screen:
        lines = out.fit(["CONVERGENCE  " + score], width)
        if not self_check_ok:  # (its stop below says how to make it pass)
            return lines + _fill("The self-check first: the inventory does not reproduce the nodes yet", width,
                                 "  ", "  ")
        if items:
            shown = items[:SCREEN_ACTIONS]
            lines.append("  Next (%s, the safest first, then the fewest nodes):" % (
                "%d of %d" % (len(shown), len(items)) if len(items) > len(shown) else out.plural(len(items), "setting")))
            rows = [(i["name"], "%s for all   %s" % (_whole(_cut(_shown(i["target"]), SCREEN_VALUE)), _then_text(i, True)))
                    for i in shown]
            lines += _screen_rows(rows, "    ", width)
            kinds = set("seeds" if i["kind"] == "seeds" else "restart" for i in shown if _KINDS[i["kind"]][3])
            commands = [_check([], k, inventory, hosts, cwd) for k in ("restart", "seeds") if k in kinds]
            if commands:
                lines += _fill("Check each one before it is applied: %s" % ", or ".join(_whole(c) for c in commands)
                               + (" (the seeds)" if len(commands) > 1 else ""), width, "  ", "  ")
        if kept:
            lines += _fill("Kept by hand: %s" % "; ".join(
                "%s on %s" % (name, who([n for dummy, m in variants for n in m])) for name, variants in kept),
                width, "  ", "  ")
        if items or kept:
            lines += _fill("The files to edit, the commands, the hand edits: %s, section CONVERGENCE" % (
                out.path_from(report_file, cwd) if report_file else "the report"), width, "  ", "  ")
        return lines

    lines = ["CONVERGENCE %s towards one group, every setting in group_vars/%s/" % (DASH, cluster)]
    if items:
        score += "; %s to change, on %s" % (out.plural(sum(len(i["changing"]) for i in items), "node value"),
                                            out.plural(len(set(n for i in items for n in i["changing"])), "node"))
    lines += _fill(score, width, "  ", "  ")
    if not self_check_ok:
        lines += _fill("The self-check first (SELF-CHECK above): the inventory does not reproduce the nodes yet, what"
                       " follows is from what the nodes have", width, "  ", "  ")
    names = [n["name"] for n in read]
    left = sorted(set(data["variable"][p] for p, values in data["settings"]
                      if data["variable"][p] in NOT_COUNTED and not _common(values, names)))
    if left:
        lines += _fill("Not counted, per node by nature or kept by each node for life: %s" % ", ".join(left),
                       width, "  ", "  ")
    switches = sorted(set(k for n in nodes for k in n.get("keep") or {} if k in KEEP))
    if switches:
        lines += _fill("Also in host_vars: %s (LEFT AS IT IS above)" % ", ".join("%s: false" % k for k in switches),
                       width, "  ", "  ")
    if items:
        lines += ["", "  SETTINGS THAT DIFFER %s the safest change first, then the fewest nodes" % DASH]
        lines += _fill("Each one: edit the files it names, review it with its check, run that without --check, then"
                       " import again: once every node has the value, the import keeps it in group_vars/%s/ and the"
                       " line goes away." % cluster, width, "  ", "  ")
        for item in items:
            lines += _aligned([(item["name"], _values_text(item, who_named))], "  ", width)
            lines += _fill(_action(item, layout), width, "      ", "  ")
            # (a file of yours named: it reaches other nodes too, the whole cluster checked)
            reach = [] if any(p.startswith("yours: ") for p in _places(layout, item["key"])) else item["changing"]
            check = _whole(_check(reach, item["kind"], inventory, hosts, cwd)) if _KINDS[item["kind"]][3] \
                else "none, apply_config does not show it"
            lines += _fill("%s; check: %s" % (_then_text(item), check), width, "      ", "  ")
    if kept:
        lines += ["", "  KEPT BY HAND %s cassandra_config_keep_files: left as their node has them" % DASH]
        lines += _kept_lines(kept, layout, read, who_named, width, inventory, hosts, cwd)
    return [line.rstrip() for line in lines]


class FilterModule(object):
    def filters(self):
        return {"cassandra_import_convergence": cassandra_import_convergence}
