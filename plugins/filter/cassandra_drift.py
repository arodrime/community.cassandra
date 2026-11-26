# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""How far a cluster is from stock, and whether its nodes are alike (the drift playbook). Read only.

cassandra_drift_values: hostvars, nodes -> {node: {variable: value}}: the variables of the roles' defaults the
    inventory sets on each node, as Ansible resolves them (read before any other play of the run sets a fact).
cassandra_drift_sections: those values -> the lines of OFF STOCK (every setting whose value on a node is not the
    collection's default: its values and nodes, the default, what going back costs and the grep that finds where the
    inventory's files set it; the files kept by hand apart), BETWEEN NODES (the settings not the same on every node,
    as import_cluster's convergence says them, a version's newest first) and FIXES. No file is named: where a value
    is set is Ansible's precedence. The defaults made of other variables are resolved per node as the import
    resolves them (cassandra_import_layout's resolved); secrets are never shown.
cassandra_drift_sync: the nodes as apply_config's compare left them (drift's IN SYNC) -> its lines: in sync, or
    what differs on each node and the command that shows it all.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import os
import re
import shlex

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import NOT_RUNNING, THEN, not_running
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import (
    MISSING, as_bool, bool_read_settings)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_layout import (
    resolved, roles_defaults)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_convergence import (
    HARDWARE, KINDS, VERSIONS, check_command, named_value, setting_kind, setting_items, then_text)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    ARROW_RIGHT, COLLECTION_DEFAULT, DASH, NOT_COUNTED, REPORT_WIDTH, aligned_rows, fill_text, named_groups,
    node_list, report_sections, unbreakable, who_text)
from ansible_collections.community.cassandra.plugins.filter.cassandra_suspicious import how_to_keep, suspicious
from ansible_collections.community.cassandra.plugins.filter.cassandra_output import _same_sources, terminal_width
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_build
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_files import block_title
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_secrets import MASK
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_apply_config_coverage import (
    CATEGORIES, IMPOSSIBLE, LIVE, NOT_A_SETTING, RESTART, category)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file

KEEP_FILES = "cassandra_config_keep_files"
EMPTY = "(empty)"  # a value set to nothing (YAML's null, ~), on every screen and in the report
UNKNOWN = "not known here"  # a default made of the node's facts, or of a filter the import does not render
ELLIPSIS = u"…"


def _cut(text, room):
    """text cut to room characters, its end an ellipsis (room None: as it is)."""
    text = str(text)
    return text if not room or len(text) <= room else text[:max(1, room - 1)] + ELLIPSIS


def _long_name(name, width):
    """A setting's name that leaves its values less than 12 columns after it (aligned_rows: 2 before it, 3 after)."""
    return len(name) + 5 + 12 > width


def _room(room, name, width):
    """The most a value takes after a setting's name: room, less when the name leaves less; under a long name (a line
    of its own, _setting_rows), room within the width; None: as it is."""
    if not room:
        return None
    return min(room, width - 6) if _long_name(name, width) else min(room, width - len(name) - 5)


def _setting_rows(name, text, width, room):
    """A setting's name, its values after it (aligned_rows); on the screen (room), under a long name."""
    if room and _long_name(name, width):
        return ["  " + name] + fill_text(text, width, "      ", "  ")
    return aligned_rows([(name, text)], "  ", width)


def _plain(value):
    """A value as Ansible gives it (its own string, list and dict types, a vaulted string) -> plain Python."""
    if isinstance(value, dict):
        return dict((str(k), _plain(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


# read with the roles' variables though no role's: the playbooks' switch the settings checks read (Medusa's)
SWITCHES_READ = ("cassandra_medusa_enabled",)


def cassandra_drift_values(hostvars, nodes):
    """hostvars; nodes: the cluster's nodes -> {node: {variable: value}}: each variable of the roles' defaults (and
    SWITCHES_READ) that the node's variables have (the inventory, -e), as Ansible resolves it; one whose template
    cannot be resolved here (a fact of the node) as its text. Read on the controller, no node contacted."""
    keys = sorted(set(roles_defaults()) | set(SWITCHES_READ))
    found = {}
    for name in nodes or []:
        mine = {}
        try:
            v = hostvars[name]
        except Exception:  # pylint: disable=broad-except
            v = {}
        for key in keys:
            try:
                if key not in v:
                    continue
                mine[key] = _plain(v[key])
            except Exception:  # pylint: disable=broad-except  # an undefined in its template: its text
                mine[key] = "(not resolved here)"
        found[str(name)] = mine
    return found


def _as_the_roles_read(values):
    """Each node's values as the roles read them: a setting the roles read with Ansible's bool filter
    (bool_read_settings: their sources say it) as that filter reads it (as_bool: "true", "no", 1...); a value it
    would not read as a boolean ("y", " true") as it is. The others as they are (a number's text, "64", is that
    number already where drift compares: _same, the convergence's). One rule for every section and count."""
    read = bool_read_settings()
    return dict((name, dict((k, as_bool(v) if k in read else v) for k, v in mine.items()))
                for name, mine in values.items())


def _same(a, b):
    """Two values alike as the roles use them: a dict or a list item by item, "8" and 8, "true" and True."""
    if isinstance(a, dict) and isinstance(b, dict):
        return sorted(a) == sorted(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (dict, list, tuple)) or isinstance(b, (dict, list, tuple)):
        return False
    return str(a).strip().lower() == str(b).strip().lower()


@functools.lru_cache(maxsize=None)
def _role_vars():
    """The roles' vars/main.yml (what some defaults are made of: the log patterns of each series...)."""
    found = {}
    for role in sorted(os.listdir(os.path.join(os.path.dirname(__file__), "..", "..", "roles"))):
        try:
            found.update(role_file(role, "vars") or {})
        except (IOError, OSError):
            continue
    return found


def _stock(key, values, name, facts):
    """The collection's default of key on node name, resolved as the import resolves it (with the node's other
    variables, its name and its facts when known) -> its value, MISSING when not known here."""
    context = dict(_role_vars())  # (a copy: the cached one never changed)
    context.update((k, v) for k, v in values.items() if k != key)
    context["inventory_hostname"] = name
    if facts:
        context["ansible_facts"] = facts
    return resolved(key, context, roles_defaults())


def _kept(values):
    """The files kept as edited by hand (cassandra_config_keep_files) -> [(file, [nodes])]."""
    found = {}
    for name, mine in values.items():
        for f in mine.get(KEEP_FILES) or []:
            found.setdefault(str(f), []).append(name)
    return sorted(found.items())


def _rows(values, facts):
    """Every setting a node has off its default (values: Ansible's, cassandra_drift_values') -> ({path: {key, values:
    {node: value}, stock: {node: value}}}, {node: {variable: value}} the variables off their default on each node,
    [the variables set that are not node settings], [the ones per node by nature]). Where a value is set is
    Ansible's precedence: never modelled here."""
    rows, drift, other, per_node = {}, {}, set(), set()
    for name, mine in sorted(values.items()):
        drift[name] = {}
        for key, value in sorted(mine.items()):
            if key == KEEP_FILES or key in SWITCHES_READ:
                continue
            cat = category(key)
            if cat and cat[0] == NOT_A_SETTING:
                other.add(key)
                continue
            stock = _stock(key, mine, name, facts.get(name))
            if stock is not MISSING and _same(value, stock):
                continue
            if key in NOT_COUNTED or cat and cat[0] == IMPOSSIBLE:  # (its dc and rack too)
                per_node.add(key)
                continue
            drift[name][key] = value
            if stock is MISSING:
                pairs = [(key, value, MISSING)]
            else:
                ours, theirs = dict(out.leaves(key, value)), dict(out.leaves(key, stock))
                if len(ours) == 1 and key in ours or len(theirs) == 1 and key in theirs:
                    pairs = [(key, value, stock)]
                else:
                    pairs = [(p, ours.get(p, MISSING), theirs.get(p, MISSING)) for p in
                             list(ours) + [p for p in theirs if p not in ours] if not _same(ours.get(p), theirs.get(p))
                             or (p in ours) != (p in theirs)]
            for path, mine_value, stock_value in pairs:
                row = rows.setdefault(path, {"key": key, "values": {}, "stock": {}})
                row["values"][name] = mine_value
                row["stock"][name] = stock_value
    return rows, drift, sorted(other), sorted(per_node)


def _shown(key, value):
    if value is MISSING:
        return "not set"
    if value is None:
        return EMPTY
    text = out.shown(key, value)
    return '""' if text == "" else text


def _grouped(row, key, which):
    """{node: value} -> [(shown value, [nodes])], the most nodes first; secrets that differ told apart without their
    value: ****, **** (value 2)."""
    groups, secrets = {}, []
    for name in sorted(row[which]):
        value = row[which][name]
        text = _shown(key, value)
        if text == MASK:
            marker = repr(_plain(value))
            if marker not in secrets:
                secrets.append(marker)
            text += " (value %d)" % (secrets.index(marker) + 1) if secrets.index(marker) else ""
        groups.setdefault(text, []).append(name)
    return sorted(groups.items(), key=lambda g: (-len(g[1]), g[0]))


def _cost(key, changing, unmanaged):
    """What going back costs on changing (convergence's words: no restart, then restart...), the coverage map's
    category when convergence has none for key."""
    kind = setting_kind(key)
    if kind == "unknown":
        cat = category(key)
        if cat:
            count = len(changing)
            return kind, "%s %s, apply_config: %s" % (
                out.plural(count, "node"), "changes" if count == 1 else "change", CATEGORIES[cat[0]])
    return kind, then_text({"changing": changing, "unread": "", "kind": kind, "key": key, "unmanaged": unmanaged})


def _keeps_release(row):
    """A release pinned off the default "" (any release of the series): going back changes no node."""
    return row["key"] == "cassandra_package_version" and set(row["stock"].values()) <= {""}


# a cost line with no command that shows the change before it is made
NO_CHECK = "none, apply_config does not show it"


def _cost_line(key, kind, cost, changing, commands):
    """What going back costs (cost, of kind: _cost's), then the command that shows it first: apply_config --check
    --diff (change_seeds --check for the seeds) where it shows it, its coverage category too (a setting the import's
    convergence does not know: apply_config writes it), else NO_CHECK -> text."""
    shown = KINDS.get(kind, (0, "", False))[2] or (kind == "unknown" and bool(category(key)))
    # every node of the cluster: no --limit naming them all (5 names on each of 20 lines)
    everyone = commands.get("everyone") or []
    if everyone and set(changing) >= set(everyone):
        changing = []
    check = unbreakable(check_command(changing, kind if KINDS.get(kind, (0, "", False))[2] else "restart",
                                      commands["inventory"], commands["hosts"], commands["cwd"])) if shown else NO_CHECK
    return "%s; check: %s" % (cost, check)


# where the inventory's files set a setting: the grep that finds it (-R: through links, as Ansible reads them), and
# what it does not see
GREP_NOTE = "not seen: -e, vars plugins, inventory plugins or scripts, vaulted files"


def _grep(keys, commands):
    """The command that finds where the inventory's files set keys (one, or several: -E 'a|b'): in the -i sources,
    and the group_vars and host_vars next to a source file (Ansible's names for them; a source dir: all of it)."""
    if not commands["paths"]:
        return "none: the inventory is a host list, no file"
    paths = " ".join(shlex.quote(_from_cwd(p, commands["cwd"])) for p in commands["paths"])
    keys = sorted(set(keys))
    return "grep -Rnw%s %s %s" % ("E" if len(keys) > 1 else "", shlex.quote("|".join(keys)), paths)


def _from_cwd(path, cwd):
    """A path as a command run from cwd names it: relative under cwd, else as it is."""
    rel = os.path.relpath(str(path), str(cwd)) if cwd else str(path)
    return rel if cwd and not rel.startswith("..") else str(path)


def _stock_lines(rows, other, per_node, kept, who, width, commands, room=None):
    """OFF STOCK's lines; room: the most a value takes (cut, on the screen)."""
    nodes_off = sorted(set(n for row in rows.values() for n in row["values"]))
    title = "OFF STOCK %s the settings not at the collection's default" % DASH
    if not rows:
        lines = [title] + fill_text("every setting of every node is at the collection's default", width, "  ", "  ")
    else:
        lines = [title] + fill_text("%s off the default, on %s; each one: its values and nodes, the default, what"
                                    " going back costs, and the grep that finds where the inventory's files set it"
                                    " (%s)" % (
                                        out.plural(len(rows), "setting"), out.plural(len(nodes_off), "node"),
                                        GREP_NOTE), width, "  ", "  ")
    for path in sorted(rows, key=lambda p: (setting_kind(rows[p]["key"]) not in ("none", "os", "logback"), p)):
        row = rows[path]
        key = row["key"]
        values = "; ".join("%s on %s" % (unbreakable(_cut(text, _room(room, path, width))), who(members))
                           for text, members in _grouped(row, path, "values"))
        lines += _setting_rows(path, values, width, room)
        stocks = _grouped(row, path, "stock")
        if path == key and all(v is MISSING for v in row["stock"].values()):
            stock = "default: %s (made of the node's facts, or of a filter)" % UNKNOWN
        elif len(stocks) == 1:
            stock = "default: %s" % _cut(stocks[0][0], room)
        else:
            stock = "default: %s" % "; ".join("%s on %s" % (_cut(text, room), who(members)) for text, members in stocks)
        lines += fill_text(stock, width, "      ", "  ")
        changing = sorted(row["values"])
        unmanaged = [n for n in changing if commands["unmanaged"].get(n)]
        kind, cost = _cost(key, changing, unmanaged)
        cost = _cost_line(key, kind, cost, changing, commands)
        if _keeps_release(row):
            cost = "no node changes: the default keeps the release each node runs (a node added later gets the" \
                   " repository's newest of the series)"  # (nothing to check)
        lines += fill_text(cost, width, "      ", "  ")
        lines.append("      where: " + _grep([key], commands))
    if per_node:
        lines += fill_text("Not counted, per node by nature or kept by each node for life: %s" % ", ".join(per_node),
                           width, "  ", "  ")
    if other:
        lines += fill_text("Also set, not settings of the nodes (how the roles and playbooks work): %s"
                           % ", ".join(other), width, "  ", "  ")
    if kept:
        lines += ["", "  KEPT BY HAND %s %s: Ansible does not manage these files" % (DASH, KEEP_FILES)]
        lines += aligned_rows([(f, "kept on %s" % who(members)) for f, members in kept], "  ", width)
        lines += fill_text("their settings are whatever the file on the node says, not the inventory's: give one back"
                           " to the roles by taking it out of %s (apply_config then writes it from the template and"
                           " restarts the node)" % KEEP_FILES, width, "  ", "  ")
    return lines


def _secret_text(text):
    """A secret as the convergence names it, "(in secrets.yml, value 2)", as drift shows it: "**** (value 2)" (the
    inventory's secrets need not be in a secrets.yml)."""
    space = r"[\s%s]" % out.NBSP  # (kept on one line: unbreakable's)
    return re.sub(r"\(in%ssecrets\.yml(?:,%s(value%s\d+))?\)" % (space, space, space),
                  lambda m: MASK + (out.NBSP + "(%s)" % m.group(1) if m.group(1) else ""), text)


# a setting whose default is no value drift knows: "" (cassandra_package_version) keeps the release each node runs
RUNNING = "(each node's running release)"
# a value group's tag: every node of it on its own default, or some of them
DEFAULT_TAG = re.compile(r" \(default(?: on \d+ of them)?\)$")


def _shown_values(item, at_default):
    """An item's values as the screens say them (Ansible's effective values: each node's own variable or its own
    default), each tagged "(default)" when every node of it is on its own default ("default on 2 of them": some);
    a version's newest first (the convergence's order of real values), its nodes below the newest the ones changing
    (never down; RUNNING apart) -> the item."""
    shown, running = [], []
    for text, members in item["values"]:
        if item["key"] == "cassandra_package_version" and str(text) in ("", '""', "''"):
            running += members  # (the release each node runs: no value to rank, no node to change)
            continue
        on_default = [m for m in members if m in at_default]
        tag = " (default)" if len(on_default) == len(members) else \
            " (default on %d of them)" % len(on_default) if on_default else ""
        # (a node without the variable: its default not known here, made of its facts or a filter)
        said = UNKNOWN if text in (COLLECTION_DEFAULT, "(not set)") else _yaml_text(named_value(text))
        shown.append((said + tag, members, text))
    values = [(text, members) for text, members, dummy in shown] + ([(RUNNING, running)] if running else [])
    changing = item["changing"]
    if item["key"] in VERSIONS:
        changing = [n for dummy, members, dummy2 in shown[1:] for n in members]
    return dict(item, values=values, changing=changing, tie=item.get("tie") and item["key"] not in VERSIONS,
                newest=item["key"] in VERSIONS and not running)  # (a running release: which is newest not known)


def _between_lines(values, drift, nodes, cluster, everyone, who, width, commands, stock_of, room=None):
    """BETWEEN NODES' lines: the settings not the same on every node, import_cluster's convergence over each node's
    effective value (its own variable, or its own default: stock_of(node, key)) of every setting off the default
    somewhere (drift): split only where the effective values differ; room: the most a value takes (cut, on the
    screen) -> (lines, the convergence's items, their values as the screens say them)."""
    keys = sorted(set(k for mine in drift.values() for k in mine))
    effective = {}
    for name in everyone:
        mine = values.get(name) or {}
        effective[name] = dict((k, mine[k] if k in mine else stock_of(name, k)) for k in keys)
        effective[name] = dict((k, v) for k, v in effective[name].items() if v is not MISSING)
    layout = {"cluster_group": cluster, "from_yours": {}, "yours": [],
              "nodes": [{"name": n, "read": True, "vars": effective[n], "hand_edits": [],
                         "dc": (nodes.get(n) or {}).get("dc"), "rack": (nodes.get(n) or {}).get("rack"),
                         "keep": {"cassandra_linux_manage": False} if commands["unmanaged"].get(n) else {}}
                        for n in everyone]}
    data = report_sections(layout, None)
    items, dummy = setting_items(layout, data, layout["nodes"])
    items = [_shown_values(i, set(n for n in everyone if i["key"] not in (drift.get(n) or {}))) for i in items]
    title = "BETWEEN NODES %s the settings not the same on every node" % DASH
    if not items:
        return [title] + fill_text("every node has the same value of every setting", width, "  ", "  "), []
    lines = [title] + fill_text("%s not the same on every node; each one: its values and nodes, then what changing"
                                " the nodes to one value costs" % out.plural(len(items), "setting"), width, "  ", "  ")
    for item in items:
        room_of = _room(room, item["name"], width)
        values = "; ".join("%s on %s" % (unbreakable(_between_value(text, room_of)), who(members))
                           for text, members in item["values"])
        lines += _setting_rows(item["name"], _secret_text(values), width, room)
        # (the same words as OFF STOCK's: a setting the convergence does not know by its coverage category)
        if not item["changing"]:  # (the others keep their running release: nothing to change, nothing to check)
            lines += fill_text("no node changes", width, "      ", "  ")
            continue
        cost = then_text(item) if item.get("unread") else _cost(
            item["key"], item["changing"], [n for n in item["changing"] if commands["unmanaged"].get(n)])[1]
        lines += fill_text(_cost_line(item["key"], item["kind"], cost, item["changing"], commands), width, "      ", "  ")
    return lines, items


def _grep_paths(sources, cwd):
    """What the grep reads: each -i source (absolute), and the group_vars and host_vars dirs next to a source file
    when there; each once."""
    found = []
    for source in sources or []:
        if "," in str(source) and not os.path.exists(str(source)):  # (-i db1,db2,: a host list, no file)
            continue
        path = os.path.expanduser(str(source))
        path = os.path.normpath(path if os.path.isabs(path) else os.path.join(str(cwd or os.getcwd()), path))
        near = [] if os.path.isdir(path) else [os.path.join(os.path.dirname(path), d) for d in ("group_vars", "host_vars")]
        found += [p for p in [path] + [d for d in near if os.path.isdir(d)] if p not in found]
    return found


def cassandra_drift_sections(values, nodes, cluster="", facts=None, inventory=None, default_inventory=None, hosts="",
                             cwd=None, width=None):
    """values: cassandra_drift_values' (Ansible's values of each node); nodes: [{name, dc, rack}] in the inventory's
    order; cluster: the cluster's group; facts: {node: its facts} when the nodes were read (a default made of them
    resolved); inventory, default_inventory, hosts, cwd: the commands' (cassandra_command's; the inventory's dirs:
    the grep's); width: the lines' ("terminal": the screen's, SCREEN_WIDTH at most, the values cut; default the
    report's) -> {stock: [OFF STOCK's lines], between: [BETWEEN NODES' lines], look: [WORTH A LOOK's lines: the
    findings of cassandra_suspicious], fixes: [FIXES' lines: the hand edits of each setting that differs], screen:
    {nodes, look, between, fixes, stock: the screen's blocks, summed up (cassandra_drift_screen)}, counts: {stock,
    between, kept}}. Secrets shown ****. No file is named: where a value is set is Ansible's precedence, the grep
    finds the files.
    e.g. db1 with cassandra_concurrent_reads: 64 -> stock: ["OFF STOCK — ...",
    "  cassandra_concurrent_reads   64 on db1 (1 node)", "      default: 32", "      1 node changes, then restart;
    check: ansible-playbook ... apply_config --check --diff --limit db1", "      where: grep -Rnw
    cassandra_concurrent_reads inventories"]."""
    # (the screen: at most SCREEN_WIDTH, its values cut)
    room = SCREEN_VALUE if width == "terminal" else None
    width = min(SCREEN_WIDTH, terminal_width()) if width == "terminal" else int(width) if width else REPORT_WIDTH
    nodes = [dict(n) for n in nodes or []]
    by_name = dict((str(n["name"]), n) for n in nodes)
    everyone = [str(n["name"]) for n in nodes]
    values = _as_the_roles_read(dict((str(k), dict(v or {})) for k, v in (values or {}).items() if str(k) in by_name))
    facts = dict(facts or {})
    paths = _grep_paths(inventory or ([default_inventory] if default_inventory else []), cwd)
    if default_inventory and _same_sources(inventory, default_inventory):
        inventory = None
    commands = {"inventory": inventory or None, "hosts": hosts or None, "cwd": cwd, "everyone": everyone,
                "paths": paths,
                "unmanaged": dict((n, as_bool(values.get(n, {}).get("cassandra_linux_manage", True)) is False)
                                  for n in everyone)}
    groups = named_groups([{"name": n, "dc": by_name[n].get("dc"), "rack": by_name[n].get("rack")} for n in everyone])

    def who(members):
        return who_text(members, everyone, groups)

    def stock_of(node, key):
        return _stock(key, values.get(node) or {}, node, facts.get(node))

    rows, drift, other, per_node = _rows(values, facts)
    kept = _kept(values)
    stock = _stock_lines(rows, other, per_node, kept, who, width, commands, room)
    between, items = _between_lines(values, drift, by_name, cluster, everyone, who, width, commands, stock_of, room)
    look = [(row, text.format(nodes=who_text(names, everyone, groups, full=False)), fix)
            for row, names, text, fix in suspicious(values)]
    screen = {"nodes": len(everyone), "look": _look_screen(look, width),
              "between": _between_screen(items, rows, everyone, groups, width),
              "fixes": _fix_guide(items, rows, commands, width, FIXES_SHOWN, room),
              "stock": _stock_screen(rows, kept, who, width)}
    return {"stock": [line.rstrip() for line in stock], "between": [line.rstrip() for line in between],
            "look": _look_lines(look, width), "fixes": _fix_guide(items, rows, commands, width),
            "screen": screen, "counts": {"stock": len(rows), "between": len(items), "kept": len(kept)}}


def _sync_rows(nodes, stops):
    """apply_config's compare of each node (cassandra_drift_sync's nodes) -> ([(what, [nodes])] the nodes not in sync
    first, how many nodes differ from the inventory, how many were not compared)."""
    order, rows = [], {}
    off = unseen = 0
    for node in nodes or []:
        if not boolean(node.get("compared", True), strict=False):
            unseen += 1
            # (it stops before its compare: none made; else what was seen: no result of the compare for it)
            text = "not compared (apply_config stops first)" if stops else \
                "not compared: the compare gave no result for %s"
            if text not in rows:
                order.append(text)
            rows.setdefault(text, []).append(str(node.get("name")))
            continue
        # (the unit file, or the drop-in of the package's init script, that differs from the role's: by its name; one
        # apply_config writes only at the node's next restart, nothing else to do: said so, the node in sync)
        units = [u for u in node.get("unit") or [] if u]
        files = list(node.get("files") or []) + units
        parts = [", ".join(files)] if files else []
        if node.get("os"):
            parts.append("OS settings: %s" % ", ".join(str(i) for i in node["os"]))
        if node.get("medusa"):
            parts.append("Medusa")
        parts += sorted(set(str(i.get("section", "")) for i in (node.get("extras") or []) + (node.get("restart") or [])
                            if i.get("section") and not (i["section"] == "cassandra unit" and node.get("unit"))))
        todo = boolean(node.get("todo", False), strict=False) or bool(parts)
        later = units and parts == [", ".join(units)] and not boolean(node.get("todo", False), strict=False)
        if later:
            text = "in sync; %s written at its next restart (no effect now)" % ", ".join(units)
        elif todo:
            off += 1
            then = THEN.get(str(node.get("then") or "none"), ("no restart",))[0]
            text = "%s; %s" % ("; ".join(parts) or "its config files", then)
        else:  # (a node not running: its files compared, said so)
            text = "in sync" + (", " + NOT_RUNNING if not_running(node) else "")
        if text not in rows:
            order.append(text)
        rows.setdefault(text, []).append(str(node.get("name")))
    said = [(text % ("it" if len(rows[text]) == 1 else "them") if "%s" in text else text, rows[text])
            for text in sorted(order, key=lambda t: t.startswith("in sync"))]  # (for it, for them)
    return said, off, unseen


def cassandra_drift_sync(nodes, stops=None, command="", screen=False):
    """nodes: [{name, compared (false: the compare left it no facts, unreachable or failed there: "not compared",
    never in sync), todo, then, files: [the config files that differ], os: [the OS settings], medusa: [Medusa's],
    extras: [{section}] (the repository, packages, firewall), restart: [{section}] (its Java, Cassandra's package,
    its unit), unit: [the unit files that differ from the role's, by name]}] as apply_config's compare left them;
    stops: apply_config's stops before any change (a procedure of
    its own: its lines); command: apply_config --check --diff's; screen: the screen's block (IN SYNC, or NOT IN SYNC
    and how many nodes differ; the nodes in sync not listed when every node is) -> IN SYNC's lines.
    e.g. ([{name: db1, todo: True, then: restart, files: [cassandra.yaml]}, {name: db2}]) -> ["IN SYNC — ...",
    "  db1  cassandra.yaml; then restart", "  db2  in sync", ...]."""
    rows, off, unseen = _sync_rows(nodes, stops)
    alike = not off and not unseen and not stops
    state = _state(off, unseen, stops, len(nodes or [])).upper()
    if not screen:  # (the report's section: its state, then the same rows)
        lines = ["%s %s the nodes against the inventory (apply_config's compare, nothing changed)" % (state, DASH)]
    else:
        lines = ["IN SYNC" if alike else "%s  %s" % (state, cassandra_drift_verdict(nodes, stops))]
        rows = [(text, members) for text, members in rows if not alike or text != "in sync"]
    for text, members in rows:  # (on the screen: SCREEN_WIDTH, a long file list on the next lines)
        nodes = out.nodes(members)  # (a long list wraps under its start, after the nodes)
        lines += fill_text("%s  %s" % (nodes, text), SCREEN_WIDTH, "  ", " " * min(len(nodes) + 2, 24)) if screen else [
            "  %s  %s" % (out.nodes(members), text)]
    if alike:
        count = len(nodes or [])
        lines.append("  %s the inventory: apply_config has nothing to apply" % (
            "every node matches" if not screen else "the node matches" if count == 1 else "all %d nodes match" % count))
    if stops:
        lines.append("  apply_config would stop before any change (a procedure of its own first):")
        lines += ["    %s" % line for line in stops]
    if off and command:
        lines += ["  the settings themselves, before and after (changing nothing):", "    " + command]
    if unseen and not stops:
        lines.append("  WAY OUT  run drift again, then look at the run's output above for %s"
                     % ("it" if unseen == 1 else "them"))
    return lines


def _state(off, unseen, stops, count=0):
    """The verdict in a word or two: in sync, not in sync, not all compared (every node compared is in sync), none
    compared (count: the nodes)."""
    if unseen and not off and not stops:
        return "none compared" if unseen == count else "not all compared"
    return "not in sync" if off or unseen or stops else "in sync"


def cassandra_drift_verdict(nodes, stops=None, compared=True):
    """cassandra_drift_sync's nodes and stops; compared: false when no node was read (-e drift_nodes=false) -> the
    verdict on them: "in sync", "2 nodes differ from the inventory", "1 node not compared"..."""
    if not boolean(compared, strict=False):
        return "nodes not checked"
    dummy, off, unseen = _sync_rows(nodes, stops)
    said = []
    if off:
        said.append("%s from the inventory" % out.plural(off, "node differs", "nodes differ"))
    elif stops:  # (its stop said under the title, once)
        said.append("apply_config would stop before any change")
    if unseen:
        said.append("%s not compared" % out.plural(unseen, "node"))
    return ", ".join(said) or "in sync"


# --- the screen: the verdict first, what deserves a look, then each section summed up (every setting in the report;
# on the screen too with -e drift_details=true) ----------------------------------------------------------------------

SCREEN_WIDTH = 79  # the most a line of the screen takes, whatever the terminal (a command apart)
SCREEN_VALUE = 40  # the most a value takes on the screen: cut (whole in the report)
BETWEEN_SHOWN = 10  # the settings BETWEEN NODES names on the screen at most
COLUMNS_MOST = 3  # value groups BETWEEN NODES puts in columns at most (more: each setting, then its values)
VALUES_SHOWN = 3  # the values of a setting BETWEEN NODES names on the screen at most (no table)
BETWEEN_LINES = 20  # the lines BETWEEN NODES takes at most without a table (a setting whole)
LOOK_SHOWN = 3  # the findings WORTH A LOOK says on the screen at most, each one whole
AREAS_SHOWN = 8  # the areas named on the screen at most, the others counted as other
# what going back to the default does, by the convergence's kind of the setting (KINDS: then restart, no restart, a
# data move, a cluster-wide switch), the coverage map's category for one the convergence does not know; any other
# (an upgrade, the playbooks' login, not known): other
COSTS = (("restart", "restart the nodes"), ("live", "apply live"), ("move", "move data by hand"),
         ("switch", "change a cluster switch"), ("other", "cost not known"))  # (other: an upgrade, a login...)
COST_OF_KIND = {"restart": "restart", "timesync": "restart", "release": "restart", "java": "restart", "jmx": "restart",
                "keystore": "restart", "none": "live", "logback": "live", "os": "live", "seeds": "live", "tool": "live",
                "move": "move", "switch": "switch"}
NAMED_COSTS = ("move", "switch")  # their settings named after their count
# the area of a setting: the block of the import's main.yml that holds it (block_title), by a short name; any other
# block: other
AREAS = {"Cluster & topology": "topology", "Versions & packages": "install", "Directories": "directories",
         "Account & file owners": "file owners", "Network & ports": "network", "JMX": "JMX",
         "CQL login (the operations that read the replication)": "CQL login",
         "JVM & heap (cassandra-env.sh, jvm*-server.options)": "JVM", "Prometheus exporter (its Java agent)": "exporter",
         "Other cassandra.yaml settings": "cassandra.yaml", "cassandra.yaml settings no variable covers": "cassandra.yaml",
         "Logging (logback.xml)": "logging", "OS tuning (found on the nodes, for the nodes added later)": "Linux",
         "systemd unit & service": "systemd", "Medusa": "Medusa"}
IN_THE_REPORT = "each one with its values, default, cost and grep: in the report"


def _cost_of(row):
    """A row of OFF STOCK -> the COSTS of going back to its default."""
    if _keeps_release(row):  # (no node changes)
        return "other"
    kind = setting_kind(row["key"])
    if kind == "unknown":
        cat = category(row["key"])
        return {LIVE: "live", RESTART: "restart"}.get(cat[0] if cat else None, "other")
    return COST_OF_KIND.get(kind, "other")


def _short_name(path):
    return path[len("cassandra_"):] if path.startswith("cassandra_") else path


def _stock_screen(rows, kept, who, width):
    """OFF STOCK on the screen: how many settings, what going back would do and their areas, counted."""
    if not rows:
        lines = ["OFF STOCK  none: every setting of every node is at the collection's default"]
    else:
        lines = ["OFF STOCK  %s not at the collection's default" % out.plural(len(rows), "setting"),
                 "  going back to the default would:"]
        costs = {}
        for path in sorted(rows):
            costs.setdefault(_cost_of(rows[path]), []).append(path)
        room = max(len(label) for dummy, label in COSTS)
        for cost, label in COSTS:
            if cost in costs:
                line = "    %s  %3d" % (label.ljust(room), len(costs[cost]))
                if cost in NAMED_COSTS:
                    line = _cut(line + "   " + ", ".join(_short_name(p) for p in costs[cost]), width)
                lines.append(line)
        areas = {}
        for row in rows.values():
            area = AREAS.get(block_title(row["key"]), "other")
            areas[area] = areas.get(area, 0) + 1
        ranked = sorted(areas.items(), key=lambda a: (a[0] == "other", -a[1], a[0]))
        if len(ranked) > AREAS_SHOWN:
            folded = sum(count for dummy, count in ranked[AREAS_SHOWN - 1:])
            ranked = ranked[:AREAS_SHOWN - 1] + [("other", folded)]
        room = max(len(name) for name, dummy in ranked)
        cells = ["%s  %3d" % (name.ljust(room), count) for name, count in ranked]
        lines.append("  by area:")
        lines += ["    " + "    ".join(cells[i:i + 3]) for i in range(0, len(cells), 3)]
        lines += ["  " + IN_THE_REPORT, "  (on this screen too: -e drift_details=true)"]
    lines += [_cut("  kept by hand (%s): %s on %s" % (KEEP_FILES, name, who(members)), width) for name, members in kept]
    return lines


def _yaml_text(text):
    """A boolean as YAML writes it (true, false), as the screen's other values say it; None (YAML's null, ~) as
    EMPTY."""
    return {"True": "true", "False": "false", "None": EMPTY}.get(text, text)


def _between_value(text, room):
    """A value of a setting that differs as _shown_values says it, in room (cut, its default tag kept whole)."""
    tag = DEFAULT_TAG.search(text)
    tag = tag.group(0) if tag else ""
    value = _secret_text(text[:len(text) - len(tag)] if tag else text)
    return _cut(value, max(1, room - len(tag)) if room else None) + tag


def _without_count(text):
    return re.sub(r" \(\d+ nodes?\)$", "", text)


def _between_screen(items, rows, everyone, groups, width):
    """BETWEEN NODES on the screen: a table when every setting splits the nodes the same way into a few groups, not
    all of one node (a column each, its nodes said under it), else each setting then a line per value and its
    nodes; BETWEEN_SHOWN settings at most."""
    if not items:
        return ["BETWEEN NODES  none: every node has the same value of every setting"]
    lines = ["BETWEEN NODES  %s" % out.plural(len(items), "setting differs", "settings differ")]
    shown = items[:BETWEEN_SHOWN]
    splits = set(frozenset(frozenset(members) for dummy, members in item["values"]) for item in items)
    columns = [frozenset(members) for dummy, members in items[0]["values"]]
    # (one node in each group: no column a count names; each setting then its values and their node)
    if len(splits) == 1 and len(columns) <= COLUMNS_MOST and any(len(c) > 1 for c in columns):
        labels = [out.plural(len(c), "node") for c in columns]
        if len(set(labels)) < len(labels):
            labels = ["group %d" % (i + 1) for i in range(len(columns))]
        name_room = min(32, max(len(item["name"]) for item in shown))
        room = max(8, (width - 4 - name_room - 2 * (len(columns) - 1)) // len(columns))
        lines.append((" " * (4 + name_room) + "  ".join(label.ljust(room) for label in labels)).rstrip())
        for item in shown:
            cells = dict((frozenset(members), _between_value(text, room)) for text, members in item["values"])
            lines.append(("  %s  %s" % (_cut(item["name"], name_room).ljust(name_room), "  ".join(
                cells.get(c, "").ljust(room) for c in columns))).rstrip())
        if len(items) > len(shown):
            lines.append("  %d more: see BETWEEN NODES in the report" % (len(items) - len(shown)))
        lines.append(out.PARAGRAPH)
        label_room = max(len(label) for label in labels)
        for label, members in zip(labels, columns):
            hang = "  %s  " % label.rjust(label_room)
            text = _without_count(node_list(members, everyone, groups, full=False))
            lines += fill_text(hang + text, width, "", " " * len(hang))
    else:  # (each setting, then a line per value: VALUES_SHOWN at most; BETWEEN_LINES lines at most)
        told = 0
        for item in shown:
            block = [_cut("  " + item["name"], width)]
            for text, members in item["values"][:VALUES_SHOWN]:
                block.append(_cut("    %s  on %s" % (_between_value(text, SCREEN_VALUE),
                                                     node_list(members, everyone, groups, full=False)), width))
            if len(item["values"]) > VALUES_SHOWN:
                block.append("    %d more: see the report" % (len(item["values"]) - VALUES_SHOWN))
            if told and len(lines) + len(block) > BETWEEN_LINES:
                break
            lines += block
            told += 1
        if len(items) > told:
            lines.append("  %d more: see BETWEEN NODES in the report" % (len(items) - told))
    return lines


def _look_screen(look, width):
    """WORTH A LOOK on the screen: LOOK_SHOWN findings at most, each whole (wrapped); what to do in the report."""
    if not look:
        return []
    lines = ["WORTH A LOOK"]
    for dummy, text, dummy2 in look[:LOOK_SHOWN]:
        lines += fill_text(text, width, "  ", "  ")
    more = len(look) - LOOK_SHOWN
    lines.append("  %swhat to do for each: WORTH A LOOK in the report" % ("%d more, and " % more if more > 0 else ""))
    return lines


def _look_lines(look, width):
    """WORTH A LOOK in the report: each finding (health_check's and import_cluster's checks of the settings), what to
    do and how to keep it on purpose."""
    if not look:
        return []
    lines = ["WORTH A LOOK %s the settings health_check and import_cluster say could weaken the cluster" % DASH]
    for row, text, fix in look:
        lines += fill_text(text, width, "  ", "  ")
        lines += fill_text("%s %s; %s" % (ARROW_RIGHT, fix, how_to_keep(row)), width, "      ", "  ")
    return lines


# --- FIXES: the hand edits that give every node one value of a setting that differs ---------------------------------

FIXES_SHOWN = 5  # the settings FIXES names on the screen at most (all in the report)


def _fix_guide(items, rows, commands, width, shown=None, room=None):
    """FIXES' lines: each setting that differs (BETWEEN NODES' items), its values as Ansible gives them to the nodes
    (the newest first for a version), a word of care; then the command that finds where the inventory's files set
    them, and how to check the edits; shown: the settings named at most, VALUES_SHOWN values each (the others
    counted), room: the most a value takes (cut). Where a value comes from is Ansible's to say (precedence, -e,
    plugins): not modelled."""
    if not items:
        return []
    lines = ["FIXES  by hand: to give every node one value, edit where the inventory sets it"]
    for item in items[:shown]:
        lines.append(_cut("  " + item["name"], width))
        values = item["values"][:VALUES_SHOWN] if shown else item["values"]
        for i, (text, members) in enumerate(values):
            newest = "newest: " if i == 0 and item.get("newest") else ""
            lines += fill_text("%s%s on %s" % (newest, _between_value(text, room),
                                               out.nodes(members)), width, "    ", "  ")
        if len(item["values"]) > len(values):
            lines.append("    %d more values: FIXES in the report" % (len(item["values"]) - len(values)))
        lines += [line for care in _care(item) for line in fill_text("take care: " + care, width, "    ", "  ")]
    if shown and len(items) > shown:
        lines.append("  %d more: FIXES in the report" % (len(items) - shown))
    keys = sorted(set(item["key"] for item in items[:shown]))
    check = out.command("apply_config", inventory=commands["inventory"], hosts=commands["hosts"],
                        extra=["--check", "--diff"], cwd=commands["cwd"])
    return lines + ["  where: " + _grep(keys, commands)] + fill_text("(%s)" % GREP_NOTE, width, "    ", "  ") + [
        "  then: review the edits (git diff in a git repository), then", "    " + check]


def _care(item):
    """What to take care of before giving every node one value of item's setting: not an edit apply_config makes
    alone (a data move, a cluster-wide switch, an upgrade, a keystore), a value sized for the machine, a version a
    node never goes down -> [texts]. (The playbooks' logins are no node setting: never between nodes.)"""
    found = []
    if item.get("kind") in ("move", "switch", "version", "keystore"):
        found.append(KINDS[item["kind"]][1])
    elif item.get("kind") in ("release", "java", "tool"):
        found.append("a node never goes down a version")
    if item["key"] in HARDWARE:
        found.append("sized for the machine: one value only where the machines are alike")
    return found


def cassandra_drift_screen(sections, nodes=None, stops=None, command="", cluster="", compared=True, details=False):
    """sections: cassandra_drift_sections' at the screen's width; nodes, stops, command: cassandra_drift_sync's;
    compared: false when no node was read (-e drift_nodes=false); details: every setting with its values, default,
    files and edit (the report's sections), else each section summed up -> the screen's lines: DRIFT, the cluster,
    its nodes and the verdict; IN SYNC (or NOT IN SYNC); WORTH A LOOK; BETWEEN NODES; OFF STOCK (summed up, or whole)."""
    screen = sections["screen"]
    compared = boolean(compared, strict=False)
    # (the state in a word or two: the block under it says how many nodes differ, how many were not compared)
    state = _state(*_sync_rows(nodes, stops)[1:], stops=stops, count=len(nodes or [])) if compared else "not checked"
    lines = ["DRIFT  %s  %s  %s" % (cluster, out.plural(screen["nodes"], "node"), state),
             "  made by:  %s" % cassandra_build.label(), ""]
    if compared:
        lines += cassandra_drift_sync(nodes, stops, command, screen=True)
    else:
        lines += ["NOT CHECKED  the nodes", "  -e drift_nodes=false: the inventory alone, no node contacted"]
    if screen["look"]:
        lines += [""] + screen["look"]
    fixes = [""] + screen["fixes"] if screen["fixes"] else []
    if boolean(details, strict=False):
        return lines + [""] + sections["between"] + fixes + [""] + sections["stock"]
    return lines + [""] + screen["between"] + fixes + [""] + screen["stock"]


class FilterModule(object):
    def filters(self):
        return {"cassandra_drift_values": cassandra_drift_values,
                "cassandra_drift_sections": cassandra_drift_sections,
                "cassandra_drift_sync": cassandra_drift_sync, "cassandra_drift_verdict": cassandra_drift_verdict,
                "cassandra_drift_screen": cassandra_drift_screen}
