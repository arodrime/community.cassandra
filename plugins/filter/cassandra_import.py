# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters behind the import_cluster playbook.

cassandra_ring_nodes: cassandra_status module's cluster_status -> list of nodes.
cassandra_config_import: a node's config files -> cassandra_config variables,
    plus what the role would still change (hand edits / normalized lines).
cassandra_inventory_layout: every node's variables -> inventory groups,
    group_vars, host_vars, drift between nodes and the report.
cassandra_inventory_files: that layout -> the files to write, passwords apart.
cassandra_config_ignored_vars: variable names, series -> the cassandra_config
    variables among them that series' templates don't use (e.g. 4.0 names after
    an upgrade to 4.1).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ast
import difflib
import json
import os
import re

import jinja2
import yaml

from ansible.errors import AnsibleFilterError
from ansible.module_utils.parsing.convert_bool import boolean

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config")
EXPR = re.compile(r"(\{\{.*?\}\})")
V = r"\(?(\w+)(?: \| default\(\w+\))?\)?"  # var, or (per-file var | default(common var))
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
# Written even when equal to the role default: a node keeps them for life, so a
# role default changing in a later release must not change them
ALWAYS = ["cassandra_cluster_name", "cassandra_seeds", "cassandra_endpoint_snitch", "cassandra_num_tokens",
          "cassandra_partitioner", "cassandra_allocate_tokens_for_local_replication_factor",
          "cassandra_storage_compatibility_mode"]
SECRET = re.compile(r"password|passwd|secret", re.I)
# Same masking as cassandra_config's diff preview
SECRET_VALUE = re.compile(r"(?i)([\w.-]*(?:password|passwd|secret)[\w.-]*\s*[:=]\s*)([^\s#\"']+)")


def _mask(line):
    return SECRET_VALUE.sub(r"\1****", line)


ADDRESSES = ["cassandra_listen_address", "cassandra_rpc_address",
             "cassandra_broadcast_address", "cassandra_broadcast_rpc_address"]
# Per node by nature: not reported as drift
PER_NODE = ADDRESSES + ["cassandra_initial_token"]
IPV4 = "{{ ansible_facts['default_ipv4']['address'] }}"
MISSING = object()
TOP_KEY = re.compile(r"^(?:\{\{[^}]*\}\})?([a-z0-9_]+):")  # active top-level key, maybe behind a toggle
EXTRA_HEADER = "# Settings no variable covers (cassandra_extra_settings)"


def cassandra_ring_nodes(cluster_status):
    """[{address, state, dc, rack, host_id}] from cassandra_status' cluster_status."""
    return [{"state": n["status"] + n["state"], "address": n["address"], "dc": dc,
             "host_id": n["host_id"], "rack": n["rack"]}
            for dc, info in sorted((cluster_status or {}).items()) for n in info["nodes"]]


def _load_role(series, facts):
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        ctx = yaml.safe_load(f)
    with open(os.path.join(ROLE, "vars", "main.yml")) as f:
        files = yaml.safe_load(f)["_cassandra_config_files"][SERIES[series]]
    ctx["cassandra_version"] = series
    ctx["ansible_facts"] = facts
    env = jinja2.Environment(keep_trailing_newline=True, trim_blocks=True)
    env.filters["to_nice_yaml"] = lambda data, indent=2: yaml.safe_dump(data, default_flow_style=False, indent=indent)
    for dummy in range(5):  # defaults referencing other defaults
        for k, v in ctx.items():
            if isinstance(v, str) and "{{" in v:
                ctx[k] = env.from_string(v).render(**ctx)
                if ctx[k].startswith(("[", "{")):  # like Ansible, a rendered list/dict is one again
                    try:
                        ctx[k] = ast.literal_eval(ctx[k])
                    except (ValueError, SyntaxError):
                        pass
    return env, ctx, files


def _render(env, series, name, ctx):
    with open(os.path.join(ROLE, "templates", SERIES[series], name + ".j2")) as f:
        src = f.read()
    return src.split("\n"), env.from_string(src).render(**ctx).split("\n")


def _value(cap):
    parsed = yaml.safe_load(cap) if cap not in ("", "yes", "no", "on", "off") else cap
    return parsed if isinstance(parsed, (int, float, bool)) else cap


def _read_line(tpl, live):
    """Values a template line takes to render as the live line, or None."""
    parts = EXPR.split(tpl)
    exprs = parts[1::2]
    pattern = "".join(re.escape(p) if i % 2 == 0 else "(.*?)" for i, p in enumerate(parts))
    m = re.fullmatch(pattern, live)
    if not m:
        return None
    found, flags, values, gc = {}, {}, {}, {}
    for expr, cap in zip(exprs, m.groups()):
        e = expr[2:-2].strip()
        if re.fullmatch(r"(\w+)", e):
            found[e] = _value(cap)
        elif re.fullmatch(r"(\w+) \| lower", e):
            found[e.split()[0]] = cap == "true"
        elif re.fullmatch(r"'' if %s else '[^']*'" % V, e):
            flags[re.fullmatch(r"'' if %s else '[^']*'" % V, e).group(1)] = cap == ""
        elif re.fullmatch(r"'# ' if (\w+) == '' else ''", e):
            flags[e.split()[3]] = cap == ""
        elif re.fullmatch(r"%s or '[^']*'" % V, e):
            values[re.fullmatch(r"%s or '[^']*'" % V, e).group(1)] = _value(cap)
        elif re.fullmatch(r"'true' if (\w+) == '' else \(\w+ \| string \| lower\)", e):
            values[e.split()[3]] = cap == "true"
        elif re.fullmatch(r"'yes' if (\w+) else 'no'", e):
            found[e.split()[3]] = cap == "yes"
        elif re.fullmatch(r"(\w+) if \w+ is string else .*", e):
            found[e.split()[0]] = cap
        elif re.fullmatch(r"\(' ' \+ (\w+)\) if \w+ else ''", e):
            found[e.split()[2].rstrip(")")] = cap[1:]
        elif re.fullmatch(r"'[^']*' ~ %s if %s == '(\w+)' else '([^']*)'" % (V, V), e):
            m2 = re.fullmatch(r"'([^']*)' ~ %s if %s == '(\w+)' else '([^']*)'" % (V, V), e)
            prefix, var, gcvar, name, off = m2.groups()
            if cap != off and cap.startswith(prefix):
                found[var] = _value(cap[len(prefix):])
                gc.setdefault(gcvar, set()).add(name)
        elif re.fullmatch(r"'([^']*)' if %s == '(\w+)' else '([^']*)'" % V, e):
            on, gcvar, name, off = re.fullmatch(r"'([^']*)' if %s == '(\w+)' else '([^']*)'" % V, e).groups()
            if cap == on:
                gc.setdefault(gcvar, set()).add(name)
            elif cap != off:
                return None
        elif re.fullmatch(r"\('\\n' ~ .*\) if \w+ else ''", e):
            if cap:
                return None
        else:
            return None
    for var, on in flags.items():
        if not on:
            found[var] = ""
        elif var in values:
            found[var] = values[var]
    found.update({k: next(iter(v)) for k, v in gc.items() if len(v) == 1})
    return found


def _align(rendered, live):
    if live[:1] != rendered[:1]:  # not managed yet: no header line
        live = rendered[:1] + live
    return live


def _import_file(env, series, name, ctx, live):
    tpl, rendered = _render(env, series, name, ctx)
    live = _align(rendered, live)
    found = {}
    ops = difflib.SequenceMatcher(None, rendered, live, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "replace":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                got = _read_line(tpl[i], live[j]) if "{{" in tpl[i] else None
                if got is not None:
                    found.update(got)
        elif op == "insert" and i1 >= len(rendered) - 2:
            # lines appended after the last one: the file's extra options list
            tail = [t for t in tpl[-2:] if "| join(" in t]
            extra = [line for line in live[j1:j2] if line]
            if tail and extra:
                found[re.search(r"\((\w+) \| join", tail[0]).group(1)] = extra
    return found


def _default(ctx, key):
    return ctx.get(key, ctx.get(re.sub(r"^cassandra_jvm\d+_", "cassandra_jvm_", key), ""))


def _same_setting(name, role, node):
    """True when the node line means the same as the role's: commented-out
    stock value the role writes explicitly, or YAML-equal (quoting, spacing)."""
    if node.lstrip("#").strip() == role.strip():
        return True
    if name.endswith(".yaml") and ":" in role and not role.lstrip().startswith("#"):
        try:
            return yaml.safe_load(role) == yaml.safe_load(node)
        except yaml.YAMLError:
            return False
    return False


def _extra_settings(tpl, live):
    """Active top-level cassandra.yaml keys the template does not have
    (uncommented or added by hand): they belong in cassandra_extra_settings."""
    taken = {m.group(1) for m in map(TOP_KEY.match, tpl) if m}
    extras = {}
    for line in live:
        m = re.match(r"^([a-z0-9_]+):", line)
        if m and m.group(1) not in taken:
            try:
                extras.update(yaml.safe_load(line) or {})
            except yaml.YAMLError:
                pass
    return extras


def _without_extras(rendered, live, extras):
    """Both sides minus the extra settings: the role's block at the end, the
    commented stock lines of those keys, and the node's own lines for them."""
    if EXTRA_HEADER in rendered:
        rendered = rendered[:rendered.index(EXTRA_HEADER) - 1] + [""]
    keys = tuple(extras)
    rendered = [r for r in rendered if not re.match(r"^#\s?(%s):" % "|".join(keys), r)]
    live = [n for n in live if not re.match(r"^(%s):" % "|".join(keys), n)]
    return rendered, live


def _leftovers(env, series, files, ctx, live_files):
    hand, normalized = [], []
    extras = ctx.get("cassandra_extra_settings") or {}
    for name in files:
        if name not in live_files:
            continue
        rendered = _render(env, series, name, ctx)[1]
        live = _align(rendered, live_files[name].split("\n"))
        if name == "cassandra.yaml" and extras:
            rendered, live = _without_extras(rendered, live, extras)
            normalized += ["cassandra.yaml: %s kept with cassandra_extra_settings (written at the end of the file)" % k
                           for k in sorted(extras)]
        ops = difflib.SequenceMatcher(None, rendered, live, autojunk=False).get_opcodes()
        for op, i1, i2, j1, j2 in ops:
            if op == "equal":
                continue
            if op == "replace" and i2 - i1 == j2 - j1:
                pairs = list(zip(rendered[i1:i2], live[j1:j2]))
                if all(_same_setting(name, r, n) for r, n in pairs):
                    normalized += ["%s: %s  (node: %s)" % (name, _mask(r.strip()), _mask(n.strip())) for r, n in pairs]
                    continue
            hand.append("%s, line %d:" % (name, j1 + 1))
            hand += ["  - " + _mask(line) for line in rendered[i1:i2]] + ["  + " + _mask(line) for line in live[j1:j2]]
    return hand, normalized


def cassandra_config_import(live_files, cassandra_version, facts):
    """cassandra_config variables that render a node's files, and what the
    role would still change: {'vars', 'hand_edits', 'normalized'}."""
    if cassandra_version not in SERIES:
        raise AnsibleFilterError("cassandra_config_import: unsupported series %s" % cassandra_version)
    env, ctx, files = _load_role(cassandra_version, facts)
    found = {}
    for name in files:
        if name in live_files:
            found.update(_import_file(env, cassandra_version, name, ctx, live_files[name].split("\n")))
    changed = {k: v for k, v in found.items()
               if str(v).lower() != str(_default(ctx, k)).lower()
               and not (v == "" and _default(ctx, k) in ([], {}))}  # a list/dict variable left empty
    if "cassandra.yaml" in live_files:
        # JBOD: the template's single line expands to one line per directory
        try:
            dirs = (yaml.safe_load(live_files["cassandra.yaml"]) or {}).get("data_file_directories") or []
        except yaml.YAMLError:
            dirs = []
        if len(dirs) > 1:
            found["cassandra_data_dir"] = dirs[0]
            found["cassandra_data_file_directories"] = dirs
            changed["cassandra_data_dir"] = dirs[0]
            changed["cassandra_data_file_directories"] = dirs
        tpl = _render(env, cassandra_version, "cassandra.yaml", ctx)[0]
        extras = _extra_settings(tpl, live_files["cassandra.yaml"].split("\n"))
        if extras:
            changed["cassandra_extra_settings"] = extras
    hand, normalized = _leftovers(env, cassandra_version, files, dict(ctx, **changed), live_files)
    out = dict(changed)
    for key in ALWAYS:
        if key != "cassandra_storage_compatibility_mode" or cassandra_version == "50x":  # 5.0 setting
            out[key] = found.get(key, ctx.get(key))
    if isinstance(out.get("cassandra_seeds"), str):
        out["cassandra_seeds"] = [s.strip() for s in out["cassandra_seeds"].split(",") if s.strip()]
    ipv4 = (facts.get("default_ipv4") or {}).get("address")
    for key in ADDRESSES:
        if ipv4 and out.get(key) == ipv4:
            out[key] = IPV4
    return {"vars": out, "hand_edits": hand, "normalized": normalized}


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "x"
    return "c_" + s if s[0].isdigit() else s


def _place(key, nodes, levels, group_vars, host_vars):
    """Put key at the highest level where all its nodes agree (levels:
    cluster, DC, rack, then the node itself). True if that is the cluster."""
    def walk(level, members):
        values = [n["vars"].get(key, MISSING) for n in members]
        if all(v is MISSING for v in values):
            return level == 0
        if level < len(levels) and all(v == values[0] for v in values):
            group_vars.setdefault(levels[level](members[0]), {})[key] = values[0]
            return level == 0
        if level == len(levels):
            host_vars.setdefault(members[0]["name"], {})[key] = values[0]
            return False
        parts = {}
        for n in members:
            parts.setdefault(levels[level + 1](n) if level + 1 < len(levels) else n["name"], []).append(n)
        for part in parts.values():
            walk(level + 1, part)
        return False
    return walk(0, nodes)


def _show(key, value):
    if value is MISSING:
        return "(role default)"
    if _secret(key, value):
        return "****"
    # json, not yaml.safe_dump: Ansible hands filters dict/str subclasses
    return json.dumps(value, sort_keys=True, default=str)


def cassandra_inventory_layout(nodes, cluster_name):
    """nodes: [{name, dc, rack, ansible_host?, read: bool, reason?, vars,
    hand_edits, normalized, notes}] -> {'hosts', 'group_vars', 'host_vars', 'report'}."""
    cluster = _slug(cluster_name)

    def clusterg(dummy):
        return cluster

    def dcg(n):
        return "%s_%s" % (cluster, _slug(n["dc"]))

    def rackg(n):
        return "%s_%s" % (dcg(n), _slug(n["rack"]))

    levels = [clusterg, dcg, rackg]

    hosts = {"all": {"children": {cluster: {"children": {}}}}}
    group_vars, host_vars = {cluster: {}}, {}
    for n in sorted(nodes, key=lambda n: (n["dc"], n["rack"], n["name"])):
        dcs = hosts["all"]["children"][cluster]["children"]
        racks = dcs.setdefault(dcg(n), {"children": {}})["children"]
        entry = {"ansible_host": n["ansible_host"]} if n.get("ansible_host") else {}
        racks.setdefault(rackg(n), {"hosts": {}})["hosts"][n["name"]] = entry
        group_vars.setdefault(dcg(n), {})["cassandra_dc"] = n["dc"]
        group_vars.setdefault(rackg(n), {})["cassandra_rack"] = n["rack"]

    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    keys = sorted({k for n in read for k in n["vars"]} - {"cassandra_dc", "cassandra_rack"})
    drift = [k for k in keys if not _place(k, read, levels, group_vars, host_vars) and k not in PER_NODE]

    report = ["Cluster %s: %d node(s), %d read" % (cluster_name, len(nodes), len(read)),
              "Inventory group: %s (ansible-playbook ... -e cassandra_hosts=%s)" % (cluster, cluster), ""]
    unread = [n for n in nodes if n not in read]
    if unread:
        report.append("NOT READ (in the inventory, but not imported):")
        report += ["  %s: %s" % (n["name"], n.get("reason", "unreachable")) for n in unread]
        report.append("")
    if drift:
        report.append("DRIFT BETWEEN NODES (kept per group/node, check it is wanted):")
        for k in drift:
            report.append("  %s:" % k)
            report += ["    %s: %s" % (n["name"], _show(k, n["vars"].get(k, MISSING))) for n in read]
        report.append("")
    for n in read:
        report.append("== %s" % n["name"])
        report += ["  " + note for note in n.get("notes", [])]
        if n["hand_edits"]:
            report.append("  HAND EDITS no variable covers (cassandra_config would revert them):")
            report += ["    " + _mask(line) for line in n["hand_edits"]]
        else:
            report.append("  No hand edit left: cassandra_config would not change the config.")
        if n["normalized"]:
            report.append("  Normalized by the role, same setting (no effect):")
            report += ["    " + _mask(line) for line in n["normalized"]]
        report.append("")
    return {"cluster_group": cluster, "hosts": hosts, "group_vars": group_vars,
            "host_vars": host_vars, "report": "\n".join(report)}


def _secret(key, value):
    if isinstance(value, dict):
        return any(_secret(k, v) for k, v in value.items())
    return bool(SECRET.search(key))


def _split_secrets(variables):
    public, secrets = {}, {}
    for key, value in variables.items():
        (secrets if _secret(key, value) else public)[key] = value
    return public, secrets


def cassandra_inventory_files(layout):
    """[{path, content, secret}] for hosts.yml, group_vars/<group>/ and
    host_vars/<host>/: main.yml, and secrets.yml for variables named like
    passwords (and extra settings holding one)."""
    files = []
    for kind in ("group_vars", "host_vars"):
        for name, variables in sorted(layout[kind].items()):
            public, secrets = _split_secrets(variables)
            for base, data, secret in (("main.yml", public, False), ("secrets.yml", secrets, True)):
                if data:
                    files.append({"path": "%s/%s/%s" % (kind, name, base), "secret": secret,
                                  "content": yaml.safe_dump(json.loads(json.dumps(data, default=str)),
                                                            default_flow_style=False, sort_keys=True)})
    return files


def cassandra_config_ignored_vars(names, cassandra_version):
    if cassandra_version not in SERIES:
        raise AnsibleFilterError("cassandra_config_ignored_vars: unsupported series %s" % cassandra_version)
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        role_vars = set(yaml.safe_load(f))
    with open(os.path.join(ROLE, "vars", "main.yml")) as f:
        files = yaml.safe_load(f)["_cassandra_config_files"][SERIES[cassandra_version]]
    used = set()
    for name in files:
        with open(os.path.join(ROLE, "templates", SERIES[cassandra_version], name + ".j2")) as f:
            used.update(re.findall(r"\b(cassandra_\w+)", f.read()))
    # variables other variables' defaults are built from count as used
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        for value in yaml.safe_load(f).values():
            used.update(re.findall(r"\b(cassandra_\w+)", str(value)))
    tasks_dir = os.path.join(ROLE, "tasks")
    for name in os.listdir(tasks_dir):
        with open(os.path.join(tasks_dir, name)) as f:
            used.update(re.findall(r"\b(cassandra_\w+)", f.read()))
    return sorted(n for n in names if n in role_vars and n not in used)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_ring_nodes": cassandra_ring_nodes,
            "cassandra_config_import": cassandra_config_import,
            "cassandra_inventory_layout": cassandra_inventory_layout,
            "cassandra_inventory_files": cassandra_inventory_files,
            "cassandra_config_ignored_vars": cassandra_config_ignored_vars,
        }
