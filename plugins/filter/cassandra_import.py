# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters behind the import_cluster playbook.

cassandra_ring_nodes: cassandra_status module's cluster_status -> list of nodes.
cassandra_config_import: a node's config files -> cassandra_config variables,
    plus what the role would still change (hand edits / normalized lines).
cassandra_inventory_layout: every node's variables -> inventory groups,
    group_vars, host_vars, drift between nodes and the report.
cassandra_inventory_files: that layout -> the files to write, passwords apart,
    variables grouped by subject.
cassandra_unit_environment: systemctl's Environment of a unit -> dict.
cassandra_config_ignored_vars: variable names, series -> the cassandra_config
    variables among them that series' templates don't use (e.g. 4.0 names after
    an upgrade to 4.1).
cassandra_import_error: a failed task's result -> why it failed, without the
    values (the import's no_log tasks hold passwords).

Their unexpected errors do not quote the error message, which may show a value
read from the config (a password): its type and where it happened only.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ast
import difflib
import functools
import json
import os
import re
import shlex
import sys
import traceback

import jinja2
import yaml

from ansible.errors import AnsibleFilterError, AnsibleUndefinedVariable
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
SECRET = re.compile(r"password|passwd|secret|sse_c_key|access_key", re.I)  # sse_c_key, access_key: Medusa's
# Same masking as cassandra_config's diff preview
SECRET_VALUE = re.compile(r"(?i)([\w.-]*(?:password|passwd|secret)[\w.-]*\s*[:=]\s*)([^\s#\"']+)")


def _mask(line):
    return SECRET_VALUE.sub(r"\1****", line)


# The node's own address or name: written as the fact that gives it
ADDRESSES = ["cassandra_listen_address", "cassandra_rpc_address",
             "cassandra_broadcast_address", "cassandra_broadcast_rpc_address", "cassandra_jmx_rmi_hostname"]
# Per node by nature: not reported as drift
PER_NODE = ADDRESSES + ["cassandra_initial_token", "cassandra_medusa_fqdn"]
# The rights the role gives a readwrite JMX user besides readwrite
JMX_CREATE_UNREGISTER = ["create", "javax.management.monitor.*,javax.management.timer.*", "unregister"]
# What the roles leave as it is on a node set up another way (host_vars only:
# a node added later gets it from the roles)
# Written next to the switches below when one of those a blank host would miss
# is set (all but the repositories): add_node refuses a blank host rebuilt
# under that node's name
IMPORTED = "cassandra_imported_host"
IMPORTED_FOR = ("cassandra_cqlsh_python_manage", "cassandra_linux_manage", "cassandra_service_unit_manage")
KEEP = {
    "cassandra_repository_manage": "the package repositories",
    "cassandra_cqlsh_python_manage": "cqlsh's Python (python3.11, cqlshlib link, wrapper)",
    "cassandra_linux_manage": "the OS settings (kernel, limits, THP, swap, time sync, disks)",
    "cassandra_service_unit_manage": "the systemd unit (or init script) Cassandra is started by",
}
IPV4 = "{{ ansible_facts['default_ipv4']['address'] }}"
HOSTNAME = "{{ ansible_facts['hostname'] }}"
MISSING = object()
TOP_KEY = re.compile(r"^(?:\{\{[^}]*\}\})?([a-z0-9_]+):")  # active top-level key, maybe behind a toggle
EXTRA_HEADER = "# Settings no variable covers (cassandra_extra_settings)"
# The undefined errors naming a field or a variable, nothing else of the data.
# Jinja words a missing dict key (d[key]) the same way: nothing here or in the
# playbook may index a dict with a key read from the nodes.
MISSING_NAME = re.compile(r"(?:object'?|object of type '\w+') has no attribute '(\w+)'\s*$|^'(\w+)' is undefined\s*$")


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


def _read_line(tpl, live, ctx=None):
    """Values a template line takes to render as the live line, or None.
    ctx: the role defaults, to tell a bool switch from other variables."""
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
            values[e.split()[2]] = cap == "true"
        elif re.fullmatch(r"'yes' if (\w+) else 'no'", e):
            found[e.split()[2]] = cap == "yes"
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
        elif isinstance((ctx or {}).get(var), bool):  # a bool that only switches the line on (prefer_local=true)
            found[var] = True
        # else: a list or string that switches the line on (e.g. JMX users): its value
        # can't be read from the line, so it is left to the report as a hand edit
    found.update({k: next(iter(v)) for k, v in gc.items() if len(v) == 1})
    return found


def _align(rendered, live):
    if live[:1] != rendered[:1]:  # not managed yet: no header line
        live = rendered[:1] + live
    return live


def _import_file(env, series, name, ctx, live):
    tpl, rendered = _render(env, series, name, ctx)
    if name.endswith(".properties") and len(tpl) == len(rendered):
        # key=value files: read each templated setting by its key, wherever the node has it
        node_kv, found = _properties(live), {}
        for t, r in zip(tpl, rendered):
            key = re.match(r"^(?:#\s*)?([\w.-]+)\s*=", r.strip())
            if "{{" not in t or not key:
                continue
            key = key.group(1)
            line = "%s=%s" % (key, node_kv[key]) if key in node_kv else "# " + r.lstrip("# ")
            if line == r.strip():
                continue  # as the role writes it: nothing to read
            got = _read_line(t, line, ctx)
            if got is not None:
                found.update(got)
        return found
    live = _align(rendered, live)
    found = {}
    ops = difflib.SequenceMatcher(None, rendered, live, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "replace":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                got = _read_line(tpl[i], live[j], ctx) if "{{" in tpl[i] else None
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
    if name.endswith(".yaml") and node.lstrip("#").strip() == role.strip():
        return True  # elsewhere (env.sh, jvm options) a commented line is a switched-off one
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


def _properties(lines):
    """The settings of a key=value (or key: value) file, comments and blank lines left out."""
    out = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            key, sep, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _leftovers(env, series, files, ctx, live_files):
    hand, normalized = [], []
    extras = ctx.get("cassandra_extra_settings") or {}
    for name in files:
        if name not in live_files:
            continue
        rendered = _render(env, series, name, ctx)[1]
        if name.endswith(".properties"):
            # key=value files: only the settings count, not comments, blank lines or order
            role_kv, node_kv = _properties(rendered), _properties(live_files[name].split("\n"))
            for key in sorted(set(role_kv) | set(node_kv)):
                if role_kv.get(key) != node_kv.get(key):
                    hand.append("%s: %s" % (name, key))
                    hand += ["  - %s=%s" % (key, _mask(role_kv[key]))] if key in role_kv else []
                    hand += ["  + %s=%s" % (key, _mask(node_kv[key]))] if key in node_kv else []
            if role_kv == node_kv and rendered != live_files[name].split("\n"):
                normalized.append("%s: same settings, comments and layout rewritten" % name)
            continue
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


def _jmx_lines(text):
    """The lines of a JMX password/access file, without comment lines: # or !
    only at the start of a line (a # further on belongs to the password)."""
    text = re.sub(r"\\\n", " ", text or "")  # access lines go on after a backslash
    return [line.split() for line in text.split("\n") if line.strip() and not line.lstrip().startswith(("#", "!"))]


def _jmx_users(password_file, access_file):
    """cassandra_jmx_users from jmxremote.password and jmxremote.access, None
    when they can't be read back as the role writes them (e.g. hashed passwords)."""
    rights = dict((words[0], words[1:]) for words in _jmx_lines(access_file) if len(words) >= 2)
    access = dict((name, words[0]) for name, words in rights.items())
    users = []
    for words in _jmx_lines(password_file):
        if len(words) != 2 or access.get(words[0]) not in ("readwrite", "readonly"):
            return None
        user = {"name": words[0], "password": words[1], "access": access[words[0]]}
        extra = rights[words[0]][1:]
        if user["access"] == "readwrite" and not extra:
            user["create_unregister"] = False  # the role's readwrite line adds them
        elif user["access"] == "readwrite" and extra != JMX_CREATE_UNREGISTER:
            return None  # other rights: the role would change them
        users.append(user)
    return users


def _jmx_access_file_on(env_sh):
    """cassandra-env.sh points the JVM at /etc/cassandra/jmxremote.access (where the role writes it)."""
    return re.search(r"^\s*JVM_OPTS=.*-Dcom\.sun\.management\.jmxremote\.access\.file=/etc/cassandra/jmxremote\.access\b",
                     env_sh or "", re.M) is not None


def cassandra_unit_environment(text):
    """systemctl show -p Environment --value -> {name: value} (quoted entries kept whole)."""
    try:
        words = shlex.split(text or "")
    except ValueError:
        words = (text or "").split()
    return dict(w.split("=", 1) for w in words if "=" in w)


def _undefined(exc):
    """A missing variable or field of the nodes' data (Ansible's lazy
    templating), whose message names it and shows no value."""
    source = getattr(exc, "source", None)  # 2.19+: the marker behind the error
    return (isinstance(exc, (AnsibleUndefinedVariable, jinja2.exceptions.UndefinedError))
            and (source is None or type(source).__name__ == "UndefinedMarker")
            and bool(MISSING_NAME.search(str(exc))))


def _hidden(name, where):
    """The error being handled, without its message (it may quote a value)."""
    frames = [f for f in traceback.extract_tb(sys.exc_info()[2])
              if os.path.basename(f[0]).startswith("cassandra_import.py")]
    at = " in %s(), line %d" % (frames[-1][2], frames[-1][1]) if frames else ""
    return AnsibleFilterError("%s: %s%s%s (message hidden: it may show a value read from the nodes)"
                              % (name, (where + ", ") if where else "", sys.exc_info()[0].__name__, at))


def cassandra_config_import(live_files, cassandra_version, facts, conf_target=""):
    """cassandra_config variables that render a node's files, and what the
    role would still change: {'vars', 'hand_edits', 'normalized'}.
    conf_target: the resolved dir the node reads its config from."""
    if cassandra_version not in SERIES:
        raise AnsibleFilterError("cassandra_config_import: unsupported series %s" % cassandra_version)
    where = ["the role defaults"]
    try:
        return _config_import(live_files, cassandra_version, facts, where, conf_target)
    except AnsibleFilterError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        if _undefined(exc):
            raise
        error = _hidden("cassandra_config_import", where[0])
    raise error  # out of the except block: no chained message either


def _config_import(live_files, cassandra_version, facts, where, conf_target=""):
    env, ctx, files = _load_role(cassandra_version, facts)
    found = {}
    for name in files:
        if name in live_files:
            where[0] = name
            found.update(_import_file(env, cassandra_version, name, ctx, live_files[name].split("\n")))
    where[0] = "cassandra.yaml"
    changed = {k: v for k, v in found.items()
               if str(v).lower() != str(_default(ctx, k)).lower()
               and not (v == "" and _default(ctx, k) in ([], {}))}  # a list/dict variable left empty
    render_dirs = None
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
        elif dirs and dirs[0] != _default(ctx, "cassandra_data_dir"):
            # one directory, not the default one: cassandra_data_dir, the list follows it
            found["cassandra_data_dir"] = changed["cassandra_data_dir"] = dirs[0]
            render_dirs = dirs
        tpl = _render(env, cassandra_version, "cassandra.yaml", ctx)[0]
        extras = _extra_settings(tpl, live_files["cassandra.yaml"].split("\n"))
        if extras:
            changed["cassandra_extra_settings"] = extras
    # remote JMX users (the role writes /etc/cassandra/jmxremote.password and .access), only
    # when the JVM reads both files there and each user's rights are in it
    where[0] = "jmxremote.password"
    jmx_note = []
    if live_files.get("jmxremote.password"):
        users = _jmx_users(live_files["jmxremote.password"], live_files.get("jmxremote.access"))
        if users and "jmxremote.access" in live_files and _jmx_access_file_on(live_files.get("cassandra-env.sh")):
            changed["cassandra_jmx_users"] = users
        else:
            jmx_note = ["jmxremote.password: its users NOT imported (not every user has plain password and"
                        " readwrite/readonly rights in /etc/cassandra/jmxremote.access, the file cassandra-env.sh"
                        " points at): set cassandra_jmx_users by hand"]
    where[0] = "comparing the files with the role's"
    render = dict(ctx, **changed)
    if "cassandra.yaml" in live_files and render_dirs:
        render["cassandra_data_file_directories"] = render_dirs
    hand, normalized = _leftovers(env, cassandra_version, files, render, live_files)
    hand += jmx_note
    out = dict(changed)
    for key in ALWAYS:
        if key != "cassandra_storage_compatibility_mode" or cassandra_version == "50x":  # 5.0 setting
            out[key] = found.get(key, ctx.get(key))
    # RPM: the config stays where the node reads it (e.g. default.conf), rather
    # than moving to the role's own alternative conf dir
    if (facts.get("os_family") == "RedHat" and conf_target
            and conf_target != ctx.get("cassandra_rpm_conf_alternative")):
        out["cassandra_rpm_conf_alternative"] = ""
    if isinstance(out.get("cassandra_seeds"), str):
        out["cassandra_seeds"] = [s.strip() for s in out["cassandra_seeds"].split(",") if s.strip()]
    ipv4 = (facts.get("default_ipv4") or {}).get("address")
    for key in ADDRESSES:
        if ipv4 and out.get(key) == ipv4:
            out[key] = IPV4
        elif facts.get("hostname") and out.get(key) == facts["hostname"]:
            out[key] = HOSTNAME
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


def _values_hidden(func):
    """func, raising its unexpected errors without their message."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AnsibleFilterError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if _undefined(exc):
                raise
            error = _hidden(func.__name__, "")
        raise error
    return wrapper


def _medusa_fqdn_domain(read):
    """The domain D when every read node with a Medusa fqdn has exactly
    "<its short hostname>.D", the same D everywhere (what the cassandra_medusa
    default then writes back), else None."""
    domains = set()
    for n in read:
        if "cassandra_medusa_fqdn" not in n["vars"]:
            continue
        fqdn, hostname = str(n["vars"]["cassandra_medusa_fqdn"]), str(n.get("hostname") or "")
        if not hostname or not fqdn.startswith(hostname + ".") or len(fqdn) == len(hostname) + 1:
            return None
        domains.add(fqdn[len(hostname) + 1:])
    return domains.pop() if len(domains) == 1 else None


def _sort_key(key):
    """Keys in the order of the vars files: by block, then as listed there."""
    titles = [title for title, dummy in BLOCKS]
    order = dict(BLOCKS)[_block(key)]
    return (titles.index(_block(key)), order.index(key) if key in order else len(order), key)


def _differences(keys, read, dcg, rackg):
    """One line per key: each value and the nodes (or DC, rack) that have it."""
    lines = []
    for key in sorted(keys, key=_sort_key):
        groups = {}  # by value, shown masked
        for n in read:
            value = n["vars"].get(key, MISSING)
            same = "" if value is MISSING else json.dumps(value, sort_keys=True, default=str)
            groups.setdefault(same, (_show(key, value), []))[1].append(n)
        parts = []
        for value, members in sorted(groups.values(), key=lambda g: (-len(g[1]), g[0], sorted(n["name"] for n in g[1]))):
            names = sorted(n["name"] for n in members)
            dc = [n for n in read if dcg(n) == dcg(members[0])]
            rack = [n for n in read if rackg(n) == rackg(members[0])]
            if dc == [n for n in read if n in members] and len(dc) > 1:
                where = "DC %s" % members[0]["dc"]
            elif rack == [n for n in read if n in members] and len(rack) > 1:
                where = "rack %s/%s" % (members[0]["dc"], members[0]["rack"])
            elif not parts and len(members) > 3:  # the most common value: the others are listed
                where = "%d nodes" % len(members)
            else:
                where = ", ".join(names)
            parts.append("%s on %s" % (value, where))
        lines.append("  %s: %s" % (key, "; ".join(parts)))
    return lines


@_values_hidden
def cassandra_inventory_layout(nodes, cluster_name):
    """nodes: [{name, address?, hostname?, dc, rack, ansible_host?, read: bool, reason?,
    vars, hand_edits, normalized, notes}] -> {'hosts', 'group_vars', 'host_vars',
    'differences', 'report'}. Nodes sharing a name are named by their address instead."""
    cluster = _slug(cluster_name)
    names = [n["name"] for n in nodes]
    shared = sorted({name for name in names if names.count(name) > 1})
    nodes = [dict(n, name=n.get("address") or n["name"]) if n["name"] in shared else n for n in nodes]

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
    # Medusa's fqdn is the node's folder in the backups: a rule only when it
    # gives every node its value exactly, else each node keeps its own
    medusa = [n for n in read if n["vars"].get("cassandra_medusa_fqdn")]  # "": Medusa works it out
    domain = _medusa_fqdn_domain(read) if medusa else None
    if domain is not None:
        read = [dict(n, vars=dict([(k, v) for k, v in n["vars"].items() if k != "cassandra_medusa_fqdn"]
                                  + [("cassandra_medusa_fqdn_domain", domain)])) for n in read]
        nodes = [next((r for r in read if r["name"] == n["name"]), n) for n in nodes]
    keys = sorted({k for n in read for k in n["vars"]} - {"cassandra_dc", "cassandra_rack"})
    drift = [k for k in keys if not _place(k, read, levels, group_vars, host_vars) and k not in PER_NODE]
    differences = ["DIFFERENCES BETWEEN NODES (kept per group or node, check they are wanted):"]
    differences += _differences(drift, read, dcg, rackg) or ["  none"]

    report = ["Cluster %s: %d node(s), %d read" % (cluster_name, len(nodes), len(read)),
              "Inventory group: %s (ansible-playbook ... -e cassandra_hosts=%s)" % (cluster, cluster), ""]
    report += differences + [""]
    if domain is not None:
        report += ["Medusa fqdn (each node's folder in the backups): <short hostname>.%s on every node,"
                   " kept as cassandra_medusa_fqdn_domain" % domain, ""]
    elif medusa:
        report += ["Medusa fqdn (each node's folder in the backups): no <short hostname>.<domain> rule gives every"
                   " node's value, kept as found (cassandra_medusa_fqdn)", ""]
    if shared:
        report.append("SAME NAME for several nodes, named by their address instead: %s" % ", ".join(shared))
        report.append("")
    unread = [n for n in nodes if n not in read]
    if unread:
        report.append("NOT READ (in the inventory, but not imported):")
        report += ["  %s: %s" % (n["name"], n.get("reason", "unreachable")) for n in unread]
        if any(n.get("keep") for n in unread):
            report.append("  The roles leave their setup as it is (host_vars: %s false)" % ", ".join(KEEP))
        report.append("")
    for n in nodes:
        if n.get("keep"):
            host_vars.setdefault(n["name"], {}).update(n["keep"])
        if any(k in IMPORTED_FOR for k in n.get("keep") or {}):
            host_vars[n["name"]][IMPORTED] = True
    for n in read:
        report.append("== %s" % n["name"])
        report += ["  " + note for note in n.get("notes", [])]
        if n.get("keep"):
            report.append("  Set up another way, LEFT AS IT IS by the roles on this node (host_vars/%s/main.yml;"
                          " remove a line to let the role take it over, after --check --diff; a host"
                          " rebuilt under this name must lose them):" % n["name"])
            report += ["    %s (%s: false)" % (KEEP[k], k) for k in KEEP if k in n["keep"]]
            report += ["    %s: %s, as this node has it" % (k, _show(k, v))
                       for k, v in sorted(n["keep"].items()) if k not in KEEP]
        if n["hand_edits"]:
            report.append("  HAND EDITS no variable covers (cassandra_config would revert them):")
            report += ["    " + _mask(line) for line in n["hand_edits"]]
        else:
            report.append("  No hand edit left: cassandra_config would not change the config.")
        if n["normalized"]:
            report.append("  Same setting, written another way by the role (no effect; on an initialized node, a"
                          " file whose settings are all the same is left as it is):")
            report += ["    " + _mask(line) for line in n["normalized"]]
        report.append("")
    return {"cluster_group": cluster, "hosts": hosts, "group_vars": group_vars,
            "host_vars": host_vars, "differences": "\n".join(differences), "report": "\n".join(report)}


def _secret(key, value):
    if isinstance(value, dict):
        return any(_secret(k, v) for k, v in value.items())
    if isinstance(value, list):
        return bool(SECRET.search(key)) or any(_secret(key, v) for v in value if isinstance(v, (dict, str)))
    if value == "":
        return False  # e.g. a password variable set to "" to leave it out
    if isinstance(value, str) and SECRET_VALUE.search(value):
        return True  # e.g. a unit's JVM_EXTRA_OPTS=-Djavax.net.ssl.keyStorePassword=...
    return bool(SECRET.search(key)) and not key.endswith("_file")  # a path, e.g. cassandra_jmx_password_file


# The blocks of a main.yml/secrets.yml, in this order: a key goes to the block
# that lists it, else to the first pattern it matches, else to the block of the
# cassandra_config template that uses it (TEMPLATE_BLOCKS), else to "Other".
# In a block, the listed keys in their order, then the others alphabetically.
BLOCKS = [
    ("Cluster & topology", [
        "cassandra_cluster_name", "cassandra_dc", "cassandra_rack", "cassandra_prefer_local", "cassandra_seeds",
        "cassandra_seed_provider_class_name", "cassandra_endpoint_snitch", "cassandra_num_tokens",
        "cassandra_allocate_tokens_for_local_replication_factor", "cassandra_initial_token", "cassandra_partitioner",
        "cassandra_storage_compatibility_mode"]),
    ("Versions & packages", [
        "cassandra_version", "cassandra_package_version", "cassandra_packages", "cassandra_java_version",
        "cassandra_java_home", "cassandra_java_package", "cassandra_java_tarball", "cassandra_java_tarball_checksum",
        "cassandra_java_tarball_dir", "cassandra_install_java", "cassandra_java_set_default"]),
    ("Directories", [
        "cassandra_conf_dir", "cassandra_rpm_conf_alternative", "cassandra_data_dir", "cassandra_data_file_directories", "cassandra_commitlog_dir",
        "cassandra_hints_dir", "cassandra_saved_caches_dir", "cassandra_cdc_raw_dir", "cassandra_log_dir",
        "cassandra_heap_dump_dir"]),
    ("Network & ports", [
        "cassandra_listen_address", "cassandra_broadcast_address", "cassandra_rpc_address",
        "cassandra_broadcast_rpc_address", "cassandra_storage_port", "cassandra_ssl_storage_port",
        "cassandra_start_native_transport", "cassandra_native_transport_port",
        "cassandra_native_transport_allow_older_protocols", "cassandra_rpc_keepalive", "cassandra_internode_compression",
        "cassandra_inter_dc_tcp_nodelay"]),
    ("JMX", [
        "cassandra_jmx_port", "cassandra_local_jmx", "cassandra_jmx_rmi_hostname", "cassandra_jmx_username",
        "cassandra_jmx_password_file", "cassandra_jmx_password", "cassandra_jmx_users"]),
    ("JVM & heap (cassandra-env.sh, jvm*-server.options)", [
        "cassandra_heap_size", "cassandra_heap_newsize", "cassandra_max_direct_memory_size", "cassandra_jvm_gc",
        "cassandra_jvm_max_gc_pause_millis", "cassandra_jvm_g1_heap_region_size", "cassandra_jvm_g1_new_size_percent",
        "cassandra_jvm_initiating_heap_occupancy_percent", "cassandra_jvm_cms_initiating_occupancy_fraction",
        "cassandra_jvm_max_tenuring_threshold", "cassandra_jvm_parallel_gc_threads", "cassandra_jvm_conc_gc_threads",
        "cassandra_jvm_extra_options", "cassandra_jvm8_extra_options", "cassandra_jvm11_extra_options",
        "cassandra_jvm17_extra_options"]),
    ("Other cassandra.yaml settings", []),
    ("cassandra.yaml settings no variable covers", ["cassandra_extra_settings"]),
    ("Logging (logback.xml)", [
        "cassandra_log_level", "cassandra_log_level_cassandra", "cassandra_debug_log_enabled", "cassandra_log_console"]),
    ("systemd unit & service", ["cassandra_service_restart", "cassandra_service_environment"]),
    ("Medusa", [
        "cassandra_medusa_version", "cassandra_medusa_venv", "cassandra_medusa_link_dir", "cassandra_medusa_profile_d",
        "cassandra_medusa_python", "cassandra_medusa_storage_provider", "cassandra_medusa_bucket_name",
        "cassandra_medusa_region", "cassandra_medusa_host", "cassandra_medusa_port", "cassandra_medusa_base_path",
        "cassandra_medusa_prefix", "cassandra_medusa_key_file", "cassandra_medusa_fqdn"]),
    ("Left as it is on this node (set up another way)", list(KEEP) + [IMPORTED]),
    ("Other", []),
]
BLOCK_PATTERNS = [
    (re.compile(r"^cassandra_medusa_"), "Medusa"),
    (re.compile(r"^cassandra_service_"), "systemd unit & service"),
    (re.compile(r"^cassandra_jmx_"), "JMX"),
    (re.compile(r"^cassandra_java_"), "Versions & packages"),
    (re.compile(r"^cassandra_(?:jvm\d*|heap)_"), "JVM & heap (cassandra-env.sh, jvm*-server.options)"),
    (re.compile(r"^cassandra_\w+_(?:dir|directory|directories)$"), "Directories"),
    (re.compile(r"^cassandra_\w+_(?:address|port)$"), "Network & ports"),
]
TEMPLATE_BLOCKS = [("cassandra.yaml", "Other cassandra.yaml settings"), ("logback.xml", "Logging (logback.xml)")]


@functools.lru_cache(maxsize=None)
def _template_vars(name):
    """The variables a cassandra_config template uses, whatever the series."""
    used = set()
    for series in SERIES.values():
        path = os.path.join(ROLE, "templates", series, name + ".j2")
        if os.path.exists(path):
            with open(path) as f:
                used.update(re.findall(r"\b(cassandra_\w+)", f.read()))
    return frozenset(used)


def _block(key):
    for title, keys in BLOCKS:
        if key in keys:
            return title
    for regex, title in BLOCK_PATTERNS:
        if regex.search(key):
            return title
    for name, title in TEMPLATE_BLOCKS:
        if key in _template_vars(name):
            return title
    return "Other"


def _vars_yaml(variables):
    """variables as YAML, grouped by subject (BLOCKS): a comment line per block,
    a blank line between blocks. Each key is dumped on its own, the same way
    a whole dict would be (same values and quoting)."""
    data = json.loads(json.dumps(variables, default=str))
    grouped = {}
    for key in data:
        grouped.setdefault(_block(key), []).append(key)
    out = []
    for title, order in BLOCKS:
        keys = grouped.get(title)
        if not keys:
            continue
        keys.sort(key=lambda k: (order.index(k), "") if k in order else (len(order), k))
        out.append("# %s\n" % title + "".join(yaml.safe_dump({k: data[k]}, default_flow_style=False, sort_keys=True)
                                              for k in keys))
    return "\n".join(out)


def _split_secrets(variables):
    public, secrets = {}, {}
    for key, value in variables.items():
        (secrets if _secret(key, value) else public)[key] = value
    return public, secrets


@_values_hidden
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
                                  "content": _vars_yaml(data)})
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


# Parts of an error message that show no value: this filter's own messages,
# and the name of a missing field or variable
SAFE_REASONS = [
    (re.compile(r"(cassandra_(?:config_import|inventory_layout|inventory_files): [^\n]*?\(message hidden: "
                r"it may show a value read from the nodes\))"), r"\1"),
    (re.compile(r"(cassandra_config_import: unsupported series \w+)"), r"\1"),
    (re.compile(r"(?:object'?|object of type '\w+') has no attribute '(\w+)'(?:\.|$)"), r"missing field '\1'"),
    (re.compile(r"(?:^|: )'(\w+)' is undefined(?:\.|$)"), r"undefined variable '\1'"),
]


def _safe_reason(msg):
    for regex, template in SAFE_REASONS:
        match = regex.search(msg)
        if match:
            return match.expand(template)
    return ""


def cassandra_import_error(result, label="address"):
    """A failed task's result (ansible_failed_result) -> why it failed, for the
    import's no_log tasks: the failed loop items (their `label` key) and what
    of the error shows no value, else ''."""
    out = []
    for res in (result or {}).get("results") or [result or {}]:
        if not isinstance(res, dict) or not boolean(res.get("failed", False), strict=False):
            continue
        item = res.get("item")
        where = item.get(label) if isinstance(item, dict) and label else None
        why = _safe_reason(str(res.get("msg", "")))
        text = ("%s: %s" % (where, why or "failed") if where else why)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_ring_nodes": cassandra_ring_nodes,
            "cassandra_config_import": cassandra_config_import,
            "cassandra_inventory_layout": cassandra_inventory_layout,
            "cassandra_inventory_files": cassandra_inventory_files,
            "cassandra_config_ignored_vars": cassandra_config_ignored_vars,
            "cassandra_unit_environment": cassandra_unit_environment,
            "cassandra_import_error": cassandra_import_error,
        }
