# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""How far a cluster is from stock, and whether its nodes are alike (the drift playbook). Read only.

cassandra_drift_values: hostvars, nodes -> {node: {variable: value}}: the variables of the roles' defaults the
    inventory sets on each node, as Ansible resolves them (read before any other play of the run sets a fact).
cassandra_drift_dirs, cassandra_drift_shared: the inventory's dirs, the groups that reach other clusters too.
cassandra_drift_paths: the files of the inventory dir drift reads (its vars files for this cluster, its hosts files).
cassandra_drift_sections: those values, the inventory's vars files -> the lines of FROM STOCK (every setting whose
    value on a node is not the collection's default: its values and nodes, the default, where it is set, what going
    back costs and the exact edit; the files kept by hand apart) and BETWEEN NODES (the settings not the same on every
    node, as import_cluster's convergence says them). The defaults made of other variables are resolved per node as
    the import resolves them (cassandra_import's _resolved); secrets are never shown.
cassandra_drift_sync: the nodes as apply_config's compare left them (drift's IN SYNC) -> its lines: in sync, or
    what differs on each node and the command that shows it all.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import os
import re

import yaml

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import THEN
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    MISSING, _resolved, _roles_defaults, _user_data)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_convergence import (
    _KINDS, _action, _check, _kind, _setting_items, _then_text, _values_text)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_report import (
    ARROW_RIGHT, DASH, NOT_COUNTED, REPORT_WIDTH, aligned_rows, fill_text, named_groups, report_sections,
    unbreakable, who_text)
from ansible_collections.community.cassandra.plugins.filter.cassandra_output import _same_sources, terminal_width
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_apply_config_coverage import (
    CATEGORIES, IMPOSSIBLE, NOT_A_SETTING, category)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file

KEEP_FILES = "cassandra_config_keep_files"
# a vars file of the inventory, as Ansible reads it: group_vars/<group>.yml, group_vars/<group>/<any>.yml, the same
# for host_vars
VARS_FILE = re.compile(r"^(group_vars|host_vars)/([^/]+?)(?:\.(?:ya?ml|json)|/[^/]+\.(?:ya?ml|json))$")
UNKNOWN = "not known here"  # a default made of the node's facts, or of a filter the import does not render


def _plain(value):
    """A value as Ansible gives it (its own string, list and dict types, a vaulted string) -> plain Python."""
    if isinstance(value, dict):
        return dict((str(k), _plain(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


def cassandra_drift_values(hostvars, nodes):
    """hostvars; nodes: the cluster's nodes -> {node: {variable: value}}: each variable of the roles' defaults that
    the node's variables have (the inventory, -e), as Ansible resolves it; one whose template cannot be resolved
    here (a fact of the node) as its text. Read on the controller, no node contacted."""
    keys = sorted(_roles_defaults())
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
    return _resolved(key, context, _roles_defaults())


def _applies(path, name, groups):
    """A vars file (VARS_FILE) read for node name, a member of groups (and all)."""
    found = VARS_FILE.match(path)
    if not found:
        return False
    return found.group(2) == name if found.group(1) == "host_vars" else found.group(2) in ["all"] + list(groups)


def _setters(user, key, name, groups):
    """The vars files that set key for node name, the most specific last ([path])."""
    def rank(path):
        kind, owner = VARS_FILE.match(path).groups()
        return (kind == "host_vars", owner != "all", path)
    return sorted((p for p, data in user if key in data and _applies(p, name, groups)), key=rank)


def _sets(group, key, name, carried=False):
    """Whether a group of a YAML hosts file (its hosts, its children) sets key for host name: on the host, or in the
    vars of the group or of a group above it (carried)."""
    if not isinstance(group, dict):
        return False
    here = carried or key in (group.get("vars") or {})
    if any(str(host) == name and (here or key in (own or {})) for host, own in (group.get("hosts") or {}).items()):
        return True
    return any(_sets(child, key, name, here) for child in (group.get("children") or {}).values())


def _hosts_file_setters(texts, key, name):
    """The hosts files (YAML) that set key for node name, on the host or in a group's vars ([path])."""
    found = []
    for path, text in sorted(texts.items()):
        if VARS_FILE.match(path) or "/" in path or not re.search(r"\.ya?ml$", path) or text is None:
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and any(_sets(top, key, name) for top in data.values()):
            found.append(path)
    return found


def _kept(values):
    """The files kept as edited by hand (cassandra_config_keep_files) -> [(file, [nodes])]."""
    found = {}
    for name, mine in values.items():
        for f in mine.get(KEEP_FILES) or []:
            found.setdefault(str(f), []).append(name)
    return sorted(found.items())


def _rows(values, nodes, user, texts, facts):
    """Every setting a node has off its default -> ({path: {key, values: {node: value}, stock: {node: value},
    files: {node: [paths]}}}, {node: {variable: value}} the variables off their default on each node, [the
    variables set that are not node settings], [the ones per node by nature])."""
    rows, drift, other, per_node = {}, {}, set(), set()
    for name, mine in sorted(values.items()):
        drift[name] = {}
        node = nodes.get(name) or {}
        for key, value in sorted(mine.items()):
            if key == KEEP_FILES:
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
            files = _setters(user, key, name, node.get("groups") or []) + _hosts_file_setters(texts, key, name)
            for path, mine_value, stock_value in pairs:
                row = rows.setdefault(path, {"key": key, "values": {}, "stock": {}, "files": {}})
                row["values"][name] = mine_value
                row["stock"][name] = stock_value
                row["files"][name] = files
    return rows, drift, sorted(other), sorted(per_node)


def _shown(key, value):
    if value is MISSING:
        return "not set"
    text = out.shown(key, value)
    return '""' if text == "" else text


def _grouped(row, key, which):
    """{node: value} -> [(shown value, [nodes])], the most nodes first; secrets that differ told apart without their
    value: ****, **** (value 2)."""
    groups, secrets = {}, []
    for name in sorted(row[which]):
        value = row[which][name]
        text = _shown(key, value)
        if text == out.MASK:
            marker = repr(_plain(value))
            if marker not in secrets:
                secrets.append(marker)
            text += " (value %d)" % (secrets.index(marker) + 1) if secrets.index(marker) else ""
        groups.setdefault(text, []).append(name)
    return sorted(groups.items(), key=lambda g: (-len(g[1]), g[0]))


def _edit(path, row, cluster, shared):
    """The exact edit that takes the nodes of row back to the default -> text."""
    key = row["key"]
    sub = path[len(key) + 1:] if path != key else ""
    files = []
    for name in sorted(row["files"]):
        files += [f for f in row["files"][name] if f not in files]
    if not files:
        return "set outside the inventory's vars files (-e, or a vars plugin): take %s out there" % key
    places = ", ".join(files)
    stocks = set(_shown(path, v) for v in row["stock"].values())
    stock = stocks.pop() if len(stocks) == 1 else None
    if not sub:
        what = "remove %s from %s" % (key, places)
    elif all(v is MISSING for v in row["stock"].values()):
        what = "remove %s from %s in %s" % (sub, key, places)
    elif all(v is MISSING for v in row["values"].values()):
        what = "add %s: %s to %s in %s, or remove %s there (its whole default back)" % (
            sub, stock or "its default", key, places, key)
    else:
        what = "%s: %s in %s in %s" % (sub, stock or "its default", key, places)
    above = [f for f in files if VARS_FILE.match(f) and VARS_FILE.match(f).group(1) == "group_vars"
             and VARS_FILE.match(f).group(2) in shared]
    if above:
        what += " (%s %s read by other clusters too: to leave them as they are, set %s to %s in group_vars/%s/ instead)" % (
            ", ".join(above), "is" if len(above) == 1 else "are", path, stock or "its default", cluster)
    return what


def _cost(key, changing, unmanaged):
    """What going back costs on changing (convergence's words: no restart, then restart...), the coverage map's
    category when convergence has none for key."""
    kind = _kind(key)
    if kind == "unknown":
        cat = category(key)
        if cat:
            count = len(changing)
            return kind, "%s %s, apply_config: %s" % (
                out.plural(count, "node"), "changes" if count == 1 else "change", CATEGORIES[cat[0]])
    return kind, _then_text({"changing": changing, "unread": "", "kind": kind, "key": key, "unmanaged": unmanaged})


def _stock_lines(rows, other, per_node, kept, cluster, shared, who, width, commands):
    """FROM STOCK's lines."""
    nodes_off = sorted(set(n for row in rows.values() for n in row["values"]))
    title = "FROM STOCK %s the settings not at the collection's default" % DASH
    if not rows:
        lines = [title] + fill_text("every setting of every node is at the collection's default", width, "  ", "  ")
    else:
        lines = [title] + fill_text("%s off the default, on %s; each one: its values and nodes, the default, where it"
                                    " is set, what going back costs and the edit" % (
                                        out.plural(len(rows), "setting"), out.plural(len(nodes_off), "node")),
                                    width, "  ", "  ")
    for path in sorted(rows, key=lambda p: (_kind(rows[p]["key"]) not in ("none", "os"), p)):
        row = rows[path]
        key = row["key"]
        values = "; ".join("%s on %s" % (unbreakable(text), who(members)) for text, members in _grouped(row, path, "values"))
        lines += aligned_rows([(path, values)], "  ", width)
        stocks = _grouped(row, path, "stock")
        if path == key and all(v is MISSING for v in row["stock"].values()):
            stock = "default: %s (made of the node's facts, or of a filter)" % UNKNOWN
        elif len(stocks) == 1:
            stock = "default: %s" % stocks[0][0]
        else:
            stock = "default: %s" % "; ".join("%s on %s" % (text, who(members)) for text, members in stocks)
        places = []
        for name in sorted(row["files"]):
            places += [f for f in row["files"][name] if f not in places]
        lines += fill_text("%s; set in %s" % (stock, ", ".join(places) or "no vars file of the inventory (-e?)"),
                           width, "      ", "  ")
        changing = sorted(row["values"])
        unmanaged = [n for n in changing if commands["unmanaged"].get(n)]
        kind, cost = _cost(key, changing, unmanaged)
        check = unbreakable(_check(changing, kind, commands["inventory"], commands["hosts"], commands["cwd"])) \
            if _KINDS.get(kind, (0, "", False))[2] else ""
        lines += fill_text("%s %s" % (ARROW_RIGHT, _edit(path, row, cluster, shared)), width, "      ", "  ")
        lines += fill_text("%s%s" % (cost, "; check: " + check if check else ""), width, "      ", "  ")
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


def _between_lines(drift, nodes, user, cluster, everyone, who, width, commands):
    """BETWEEN NODES' lines: the settings not the same on every node, import_cluster's convergence over the values
    off the default (a node without one has the default)."""
    group_vars, host_vars = {}, {}
    for path, data in user:
        kind, owner = VARS_FILE.match(path).groups()
        (group_vars if kind == "group_vars" else host_vars).setdefault(owner, {}).update(data)
    layout = {"cluster_group": cluster, "group_vars": group_vars, "host_vars": host_vars, "from_yours": {},
              "yours": [], "nodes": [{"name": n, "read": True, "vars": drift.get(n) or {}, "hand_edits": [],
                                      "dc": (nodes.get(n) or {}).get("dc"), "rack": (nodes.get(n) or {}).get("rack"),
                                      "keep": {"cassandra_linux_manage": False} if commands["unmanaged"].get(n) else {}}
                                     for n in everyone]}
    data = report_sections(layout, None)
    items, dummy = _setting_items(layout, data, layout["nodes"])
    title = "BETWEEN NODES %s the settings not the same on every node" % DASH
    if not items:
        return [title] + fill_text("every node has the same value of every setting", width, "  ", "  "), 0
    lines = [title] + fill_text("%s not the same on every node; each one: its values and nodes, then what gives every"
                                " node the value most of them have" % out.plural(len(items), "setting"),
                                width, "  ", "  ")
    for item in items:
        lines += aligned_rows([(item["name"], _values_text(item, who))], "  ", width)
        action = _action(item, layout)
        if action.endswith(":"):  # (no file sets it apart for these nodes)
            action += " no vars file sets it apart for these nodes: a value made of each node's own variables"
        lines += fill_text(action, width, "      ", "  ")
        check = unbreakable(_check(item["changing"], item["kind"], commands["inventory"], commands["hosts"],
                                   commands["cwd"])) if _KINDS[item["kind"]][2] else "none, apply_config does not show it"
        lines += fill_text("%s; check: %s" % (_then_text(item), check), width, "      ", "  ")
    return lines, len(items)


def cassandra_drift_dirs(sources, cwd=""):
    """The inventory sources -> their dirs (a dir as it is, a file: the dir it is in), absolute, each once."""
    found = []
    for source in sources or []:
        path = os.path.expanduser(str(source))
        path = os.path.normpath(path if os.path.isabs(path) else os.path.join(str(cwd or os.getcwd()), path))
        path = path if os.path.isdir(path) else os.path.dirname(path)
        if path not in found:
            found.append(path)
    return found


def cassandra_drift_shared(groups, cluster_groups, hosts):
    """groups: the inventory's ({group: its hosts}); cluster_groups: the groups of the cluster's hosts; hosts: the
    cluster's -> the ones of cluster_groups that have other hosts too (a value set there reaches them), all too."""
    hosts = set(str(h) for h in hosts or [])
    return ["all"] + sorted(str(g) for g in cluster_groups or [] if g != "all"
                            and set(str(h) for h in (groups or {}).get(g) or []) - hosts)


def cassandra_drift_paths(paths, inventory_dir, groups=None, hosts=None):
    """paths: the files found in the inventory dir (absolute); groups, hosts: the cluster's groups and nodes -> the
    ones drift reads, relative to the dir: the vars files of all, of these groups and of these hosts (VARS_FILE),
    and the hosts files at its top (YAML)."""
    groups = ["all"] + [str(g) for g in groups or []]
    hosts = [str(h) for h in hosts or []]
    found = []
    for path in paths or []:
        rel = os.path.relpath(str(path), str(inventory_dir))
        if rel.startswith(".."):  # (another source's dir)
            continue
        match = VARS_FILE.match(rel)
        if match:
            keep = match.group(2) in (hosts if match.group(1) == "host_vars" else groups)
        else:
            keep = "/" not in rel and re.search(r"\.ya?ml$", rel) is not None
        if keep and rel not in found:
            found.append(rel)
    return sorted(found)


def cassandra_drift_sections(values, nodes, files=None, cluster="", shared=None, facts=None, inventory=None,
                             default_inventory=None, hosts="", cwd=None, width=None):
    """values: cassandra_drift_values'; nodes: [{name, groups: its groups, dc, rack}] in the inventory's order;
    files: {path: text} of the inventory dir (its vars files, its hosts files), relative to it (a vaulted one not
    read: None); cluster: the cluster's group; shared: the groups with hosts of other clusters (all too: a value set
    there reaches them); facts: {node: its facts} when the nodes were read (a default made of them resolved);
    inventory, default_inventory, hosts, cwd: the commands' (cassandra_command's); width: the lines' ("terminal": the
    terminal's; default the report's) -> {stock: [FROM STOCK's lines], between: [BETWEEN NODES' lines], counts:
    {stock, between, kept}}. Secrets shown ****.
    e.g. db1 with cassandra_concurrent_reads: 64 in group_vars/prod/main.yml -> stock: ["FROM STOCK — ...",
    "  cassandra_concurrent_reads   64 on db1 (1 node)", "      default: 32; set in group_vars/prod/main.yml",
    "      → remove cassandra_concurrent_reads from group_vars/prod/main.yml", "      1 node changes, then restart;
    check: ansible-playbook ... apply_config --check --diff --limit db1"]."""
    width = terminal_width() if width == "terminal" else int(width) if width else REPORT_WIDTH
    nodes = [dict(n) for n in nodes or []]
    by_name = dict((str(n["name"]), n) for n in nodes)
    everyone = [str(n["name"]) for n in nodes]
    texts = dict(files or {})
    user, unread = _user_data([{"path": p, "content": t} for p, t in sorted(texts.items()) if VARS_FILE.match(p)])
    values = dict((str(k), dict(v or {})) for k, v in (values or {}).items() if str(k) in by_name)
    shared = set(shared or []) | set(["all"])
    if default_inventory and _same_sources(inventory, default_inventory):
        inventory = None
    commands = {"inventory": inventory or None, "hosts": hosts or None, "cwd": cwd,
                "unmanaged": dict((n, not boolean(values.get(n, {}).get("cassandra_linux_manage", True), strict=False))
                                  for n in everyone)}
    groups = named_groups([{"name": n, "dc": by_name[n].get("dc"), "rack": by_name[n].get("rack")} for n in everyone])

    def who(members):
        return who_text(members, everyone, groups)

    rows, drift, other, per_node = _rows(values, by_name, user, texts, dict(facts or {}))
    kept = _kept(values)
    stock = _stock_lines(rows, other, per_node, kept, cluster, shared, who, width, commands)
    if unread:
        stock += fill_text("Not read (vaulted, no vault password for them): %s; where a setting is set may miss them"
                           % ", ".join(unread), width, "  ", "  ")
    between, count = _between_lines(drift, by_name, user, cluster, everyone, who, width, commands)
    return {"stock": [line.rstrip() for line in stock], "between": [line.rstrip() for line in between],
            "counts": {"stock": len(rows), "between": count, "kept": len(kept)}}


def cassandra_drift_sync(nodes, stops=None, command=""):
    """nodes: [{name, todo, then, files: [the config files that differ], os: [the OS settings], medusa: [Medusa's],
    extras: [{section}] (the repository, packages, firewall), restart: [{section}] (its Java, Cassandra's package,
    its unit)}] as apply_config's compare left them; stops: apply_config's stops before any change (a procedure of
    its own: its lines); command: apply_config --check --diff's -> IN SYNC's lines.
    e.g. ([{name: db1, todo: True, then: restart, files: [cassandra.yaml]}, {name: db2}]) -> ["IN SYNC — ...",
    "  db1  cassandra.yaml; then restart", "  db2  in sync", ...]."""
    title = "IN SYNC %s the nodes against the inventory (apply_config's compare, nothing changed)" % DASH
    lines, order, rows = [title], [], {}
    off = 0
    for node in nodes or []:
        parts = [", ".join(node.get("files") or [])] if node.get("files") else []
        if node.get("os"):
            parts.append("OS settings: %s" % ", ".join(str(i) for i in node["os"]))
        if node.get("medusa"):
            parts.append("Medusa")
        parts += sorted(set(str(i.get("section", "")) for i in (node.get("extras") or []) + (node.get("restart") or [])
                            if i.get("section")))
        todo = boolean(node.get("todo", False), strict=False) or bool(parts)
        if todo:
            off += 1
            then = THEN.get(str(node.get("then") or "none"), ("no restart",))[0]
            text = "%s; %s" % ("; ".join(parts) or "its config files", then)
        else:
            text = "in sync"
        if text not in rows:
            order.append(text)
        rows.setdefault(text, []).append(str(node.get("name")))
    for text in sorted(order, key=lambda t: t == "in sync"):
        lines.append("  %s  %s" % (out.nodes(rows[text]), text))
    if not off and not stops:
        lines.append("  every node matches the inventory: apply_config has nothing to apply")
    if stops:
        lines.append("  apply_config would stop before any change (a procedure of its own first):")
        lines += ["    %s" % line for line in stops]
    if off and command:
        lines += ["  the settings themselves, before and after (changing nothing):", "    " + command]
    return lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_drift_values": cassandra_drift_values, "cassandra_drift_dirs": cassandra_drift_dirs,
                "cassandra_drift_shared": cassandra_drift_shared, "cassandra_drift_paths": cassandra_drift_paths,
                "cassandra_drift_sections": cassandra_drift_sections,
                "cassandra_drift_sync": cassandra_drift_sync}
