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
cassandra_ring_names: getent hosts on a given node -> the name to reach each
    node found in the ring by.

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
import xml.etree.ElementTree as ET
from collections import namedtuple

import jinja2
import jinja2.sandbox
import yaml

from ansible.errors import AnsibleFilterError, AnsibleUndefinedVariable
from ansible.module_utils.common.text.converters import to_bytes, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.filter.core import regex_replace
from ansible_collections.community.cassandra.plugins.filter import cassandra_jvm_args as jvm_args
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    DEFAULT_WHEN_ABSENT, NOT_COMPARED, QUANTITY_KEYS, canonical_quantity, former_keys, former_names, former_value,
    jvm_options_settings, quantity, same_settings, shell_words, xml_settings, yaml_rejected)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file, roles_with_defaults

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config")
EXPR = re.compile(r"(\{\{.*?\}\})")
V = r"\(?(\w+)(?: \| default\(\w+\))?\)?"  # var, or (per-file var | default(common var))
# A password, single-quoted unless YAML reads it back as written (_cassandra_config_quote)
QUOTED = r"""\((\w+)(?: or '[^']*')?\) \| regex_replace\("'", "''"\) \| regex_replace\(_cassandra_config_quote, "'\\\\1'"\)"""
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}
# Written even when equal to the role default: a node keeps them for life, so a
# role default changing in a later release must not change them
ALWAYS = ["cassandra_cluster_name", "cassandra_seeds", "cassandra_endpoint_snitch", "cassandra_num_tokens",
          "cassandra_partitioner", "cassandra_allocate_tokens_for_local_replication_factor",
          "cassandra_storage_compatibility_mode"]
# The node keeps these for life: read from cassandra.yaml parsed as YAML (whatever
# its quoting, indentation or comments), and the import fails without them
# rather than writing the role default
IDENTITY = {"cassandra_cluster_name": ("cluster_name",), "cassandra_num_tokens": ("num_tokens",),
            "cassandra_partitioner": ("partitioner",), "cassandra_endpoint_snitch": ("endpoint_snitch",),
            "cassandra_seeds": ("seed_provider", 0, "parameters", 0, "seeds")}
# Read the same way when the node has them, but not mandatory (allocate_tokens... absent:
# still the role default, which the template always writes; it only matters to a new node)
IDENTITY_OPTIONAL = {"cassandra_seed_provider_class_name": ("seed_provider", 0, "class_name"),
                     "cassandra_allocate_tokens_for_local_replication_factor": (
                         "allocate_tokens_for_local_replication_factor",),
                     "cassandra_storage_compatibility_mode": ("storage_compatibility_mode",)}
# Read the same way, whatever the layout of their section (moved, indented otherwise, flow style)
BY_PATH = {"cassandra_sstable_format": ("sstable", "selected_format")}
INTEGERS = ("cassandra_num_tokens", "cassandra_allocate_tokens_for_local_replication_factor")
# Same masking as cassandra_config's diff preview (plugin_utils cassandra_output)
SECRET, SECRET_VALUE, _mask, _secret = out.SECRET, out.SECRET_VALUE, out.mask, out.secret


# The node's own address or name: written as the fact that gives it
ADDRESSES = ["cassandra_listen_address", "cassandra_rpc_address",
             "cassandra_broadcast_address", "cassandra_broadcast_rpc_address", "cassandra_jmx_rmi_hostname"]
# cassandra-rackdc.properties' dc= and rack= lines, apart from the node's dc and rack
# (cassandra_dc/_rack, the ring's) under a snitch that doesn't read them
RACKDC = ("cassandra_rackdc_dc", "cassandra_rackdc_rack")
# the config files cassandra_config writes, the JMX users' too (import_cluster_keep_hand_edits names some)
CONFIG_FILE = re.compile(r"^(cassandra[.]yaml|cassandra-env[.]sh|jvm[0-9]*-server[.]options|cassandra-rackdc[.]properties"
                         r"|logback[.]xml|jmxremote[.](?:password|access))$")
# Per node by nature: not reported as drift
PER_NODE = ADDRESSES + ["cassandra_initial_token", "cassandra_medusa_fqdn", "cassandra_rackdc_rack"]
# Kept on the node that has it only (host_vars), even when every node does: a node added later must not get it
NODE_LEVEL = ("cassandra_auto_bootstrap",)
# The rights the role gives a readwrite JMX user besides readwrite
JMX_CREATE_UNREGISTER = ["create", "javax.management.monitor.*,javax.management.timer.*", "unregister"]
# The rights of an access file line the role writes, as the JVM takes them (readonly alone; the role's own check)
_JMX_CLASS = r"[A-Za-z0-9_$*]+([.][A-Za-z0-9_$*]+)*"
JMX_RIGHTS = re.compile(r"^(readonly|readwrite([ \t]+(unregister|create[ \t]+%s([ \t]*,[ \t]*%s)*))*)[ \t]*$"
                        % (_JMX_CLASS, _JMX_CLASS))
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
    "cassandra_java_set_default": "the system java (/usr/bin/java), which is not the running one",
    "cassandra_firewall_manage": "the firewall (its package, service and ports), or none",
    "cassandra_data_disk_tuning": ("the tuning of its data devices (read-ahead, scheduler, udev rule, tuned profiles),"
                                   " which cassandra_linux would change"),
}
# KEEP switches of nodes the roles set up (not another way)
SET_UP_BY_THE_ROLES = ("cassandra_data_disk_tuning",)
IPV4 = "{{ ansible_facts['default_ipv4']['address'] }}"
HOSTNAME = "{{ ansible_facts['hostname'] }}"
MISSING = object()
TOP_KEY = re.compile(r"^(?:\{\{[^}]*\}\})?([a-z0-9_]+):")  # active top-level key, maybe behind a toggle
EXTRA_HEADER = "# Settings no variable covers (cassandra_extra_settings)"
# Before it: the variables of keys the stock file has no line for (tools/gen_templates.py)
DECLARED_HEADER = "# Settings the stock file has no line for (their variables)"
# the heap options, which cassandra-env.sh adds from MAX_HEAP_SIZE / HEAP_NEWSIZE unless the options files set them
HEAP_OPTIONS = ("-Xms", "-Xmx", "-Xmn")
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
    ctx = role_file("cassandra_config")
    role_vars = role_file("cassandra_config", "vars")
    files = role_vars["_cassandra_config_files"][SERIES[series]]
    ctx["cassandra_version"] = series
    ctx["ansible_facts"] = facts
    ctx["_cassandra_config_quote"] = role_vars["_cassandra_config_quote"]
    ctx["_cassandra_config_extra_sections"] = role_vars["_cassandra_config_extra_sections"]
    ctx["_cassandra_config_plain_path"] = role_vars["_cassandra_config_plain_path"]
    env = jinja2.Environment(keep_trailing_newline=True, trim_blocks=True)
    env.filters["regex_replace"] = regex_replace
    # Ansible's, as the templates use them (the JMX users' templates too)
    env.filters["bool"] = lambda value: boolean(value, strict=False)
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
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
    """A number or true/false written as such, else the text as it is: Cassandra
    reads a text setting as text (a password 0123 is not 83, nor 1_000 1000)."""
    try:
        parsed = yaml.safe_load(cap) if cap not in ("", "yes", "no", "on", "off") else cap
    except yaml.YAMLError:
        return cap
    if isinstance(parsed, bool):
        return parsed if cap in ("true", "false") else cap
    if isinstance(parsed, (int, float)) and repr(parsed) == cap:
        return parsed
    return cap


def _yaml_string(cap):
    """The text of a YAML scalar as Cassandra reads a text setting (quotes
    undone, a comment after it left out, 0123 or yes as written), None if not
    one: null (no value, ~) is no text, and a line break would not render back."""
    try:
        if yaml.safe_load("v: " + cap)["v"] is None:
            return None
        value = yaml.load("v: " + cap, Loader=yaml.BaseLoader)["v"]
    except (yaml.YAMLError, TypeError):
        return None
    return value if isinstance(value, str) and not re.search(r"[\r\n]", value) else None


# What _read_line gathers from a line's expressions: the values read (found), the switches that turn a line on or
# off (flags, values: the value a switched line gives), the values of a choice (gc: {variable: {values}}), the
# values written for one value of another variable (chosen: {variable: (that variable, its value, the placeholder,
# the text)})
_Seen = namedtuple("_Seen", "found flags values gc chosen")


def _read_var(m, e, cap, seen):
    """{{ x }}: its value."""
    seen.found[e] = _value(cap)


def _read_lower(m, e, cap, seen):
    """{{ x | lower }}, {{ (x | bool) | lower }}: a boolean, however YAML writes it."""
    if cap.strip().lower() not in YAML_BOOLS:
        return False  # quoted, or no boolean: left to the report
    seen.found[e.strip("(").split()[0]] = YAML_BOOLS[cap.strip().lower()] == "true"  # YAML's true, however written


def _read_switch(m, e, cap, seen):
    """{{ '' if x else '# ' }}: the line switched on by x (x | default(y) too)."""
    seen.flags[m.group(1)] = cap == ""


def _read_comment_switch(m, e, cap, seen):
    """{{ '# ' if x == '' else '' }} (or x | string in ['', 'None']): the line switched off by x unset."""
    seen.flags[e.split()[3]] = cap == ""


def _read_hash_switch(m, e, cap, seen):
    """{{ '#' if x | string in ['', 'None'] else '' }}: the line switched off by x unset."""
    seen.flags[e.split()[2]] = cap == ""


def _read_set_value(m, e, cap, seen):
    """{{ x if x | string not in ['', 'None'] else '...' }}: the value of a switched line."""
    seen.values[e.split()[0]] = _value(cap)  # a number, 0 included


def _read_true_unset(m, e, cap, seen):
    """{{ 'true' if x ... else (x | string | lower) }}: the boolean of a switched line."""
    seen.values[e.split()[2]] = cap == "true"


def _read_spaced_text(m, e, cap, seen):
    """{{ (' ' ~ x) if x | string not in ['', 'None'] else '' }}: its text, quotes undone."""
    if cap:  # the text of the value, quotes undone
        seen.found[m.group(1)] = re.sub(r"^(['\"])(.*)\1$", r"\2", cap[1:].strip())


def _read_quoted(m, e, cap, seen):
    """A password, quoted as the node does (QUOTED): its text."""
    text = _yaml_string(cap)  # the password itself, however the node quotes it
    if text is None:
        return False
    (seen.values if " or '" in e else seen.found)[m.group(1)] = text


def _read_or_default(m, e, cap, seen):
    """{{ x or '...' }}: the value of a switched line."""
    seen.values[m.group(1)] = _value(cap)


def _read_yes_no(m, e, cap, seen):
    """{{ 'yes' if x else 'no' }}: a boolean."""
    seen.found[e.split()[2]] = cap == "yes"


def _read_string(m, e, cap, seen):
    """{{ x if x is string else ... }}: its text."""
    seen.found[e.split()[0]] = cap


def _read_spaced_word(m, e, cap, seen):
    """{{ (' ' + x) if x else '' }}: its text."""
    seen.found[e.split()[2].rstrip(")")] = cap[1:]


_PREFIXED_CHOICE = re.compile(r"'([^']*)' ~ %s if %s == '(\w+)' else '([^']*)'" % (V, V))


def _read_prefixed_choice(m, e, cap, seen):
    """{{ 'prefix' ~ x if y == 'name' else 'off' }}: x, and y's value name."""
    prefix, var, gcvar, name, off = _PREFIXED_CHOICE.fullmatch(e).groups()
    if cap != off and cap.startswith(prefix):
        seen.found[var] = _value(cap[len(prefix):])
        seen.gc.setdefault(gcvar, set()).add(name)


def _read_choice(m, e, cap, seen):
    """{{ 'on' if y == 'name' else 'off' }}: y's value name when on."""
    on, gcvar, name, off = m.groups()
    if cap == on:
        seen.gc.setdefault(gcvar, set()).add(name)
    elif cap != off:
        return False


_CHOSEN = re.compile(r"\w+ if (\w+) == '([^']*)' else '([^']*)'")


def _read_chosen(m, e, cap, seen):
    """{{ x if y == 'value' else 'placeholder' }}: x, once y is known."""
    seen.chosen[e.split()[0]] = _CHOSEN.fullmatch(e).groups() + (cap,)


def _read_between(m, e, cap, seen):
    """{{ ('prefix' ~ x ~ 'suffix') if x else 'off' }}: x."""
    # a value between two texts, another text when empty (4.x CASSANDRA_HEAPDUMP_DIR)
    prefix, var, suffix, off = m.groups()
    if cap == off:
        seen.found[var] = ""
    elif cap.startswith(prefix) and cap.endswith(suffix) and len(cap) > len(prefix) + len(suffix):
        seen.found[var] = cap[len(prefix):len(cap) - len(suffix)]
    else:
        return False


def _read_newline_list(m, e, cap, seen):
    """{{ ('\\n' ~ ...) if x else '' }}: lines of a list, none on the node."""
    if cap:
        return False


# the template expressions a line is read by, the first that matches: (pattern, reader); a reader adds what the
# text (cap) the node has in its place says to seen, and returns False when the line cannot be read
_LINE_EXPRESSIONS = tuple((re.compile(pattern), reader) for pattern, reader in (
    (r"(\w+)", _read_var),
    (r"(\w+) \| lower|\((\w+) \| bool\) \| lower", _read_lower),
    (r"'' if %s else '[^']*'" % V, _read_switch),
    (r"'# ' if (\w+)(?: == ''| \| string in \['', 'None'\]) else ''", _read_comment_switch),
    (r"'#' if (\w+) \| string in \['', 'None'\] else ''", _read_hash_switch),
    (r"(\w+) if \1 \| string not in \['', 'None'\] else '[^']*'", _read_set_value),
    (r"'true' if (\w+) \| string in \['', 'None'\] else \(\1 \| string \| lower\)", _read_true_unset),
    (r"\(' ' ~ (\w+)\) if \w+ \| string not in \['', 'None'\] else ''", _read_spaced_text),
    (QUOTED, _read_quoted),
    (r"%s or '[^']*'" % V, _read_or_default),
    (r"'true' if (\w+) == '' else \(\w+ \| string \| lower\)", _read_true_unset),
    (r"'yes' if (\w+) else 'no'", _read_yes_no),
    (r"(\w+) if \w+ is string else .*", _read_string),
    (r"\(' ' \+ (\w+)\) if \w+ else ''", _read_spaced_word),
    (r"'[^']*' ~ %s if %s == '(\w+)' else '([^']*)'" % (V, V), _read_prefixed_choice),
    (r"'([^']*)' if %s == '(\w+)' else '([^']*)'" % V, _read_choice),
    (r"(\w+) if (\w+) == '([^']*)' else '([^']*)'", _read_chosen),
    (r"\('([^']*)' ~ (\w+) ~ '([^']*)'\) if \2 else '([^']*)'", _read_between),
    (r"\('\\n' ~ .*\) if \w+ else ''", _read_newline_list),
))


def _read_expression(e, cap, seen):
    """Reads the text cap the node has in place of the template expression e (its {{ }} off) into seen -> False
    when the line cannot be read (an expression no reader knows, or a text it does not take)."""
    for pattern, reader in _LINE_EXPRESSIONS:
        m = pattern.fullmatch(e)
        if m:
            return reader(m, e, cap, seen) is not False
    return False


def _settled(seen, ctx):
    """What seen says once every expression is read: the values written for one value of another variable, the
    switched lines' values -> {variable: value}, None when a placeholder differs."""
    found = seen.found
    # a value written only for one value of another variable (the line on: '' if x == 'group' else '# '), else a
    # placeholder: commitlog_sync_group_window
    for var, (cond, val, off, cap) in seen.chosen.items():
        if seen.gc.get(cond) == {val}:
            found[var] = _value(cap)
        elif cap != off:
            return None
    for var, on in seen.flags.items():
        if not on:
            found[var] = ""
        elif var in seen.values:
            found[var] = seen.values[var]
        elif isinstance((ctx or {}).get(var), bool):  # a bool that only switches the line on (prefer_local=true)
            found[var] = True
        # else: a list or string that switches the line on (e.g. JMX users): its value
        # can't be read from the line, so it is left to the report as a hand edit
    found.update({k: next(iter(v)) for k, v in seen.gc.items() if len(v) == 1})
    return found


def _read_line(tpl, live, ctx=None):
    """Values a template line takes to render as the live line, or None.
    ctx: the role defaults, to tell a bool switch from other variables."""
    parts = EXPR.split(tpl)
    exprs = parts[1::2]
    pattern = "".join(re.escape(p) if i % 2 == 0 else "(.*?)" for i, p in enumerate(parts))
    m = re.fullmatch(pattern, live)
    if not m:
        return None
    seen = _Seen({}, {}, {}, {}, {})
    for expr, cap in zip(exprs, m.groups()):
        if not _read_expression(expr[2:-2].strip(), cap, seen):
            return None
    return _settled(seen, ctx)


def _align(rendered, live):
    if live[:1] != rendered[:1]:  # not managed yet: no header line
        live = rendered[:1] + live
    return live


def _import_properties(tpl, rendered, live, ctx):
    """A key=value file (the template and its rendering line for line): each templated setting read by its key,
    wherever the node has it -> {variable: value}."""
    node_kv, found = _properties(live), {}
    for t, r in zip(tpl, rendered):
        key = re.match(r"^(?:#\s*)?([\w.-]+)\s*=", r.strip())
        if "{{" not in t or not key:
            continue
        key = key.group(1)
        value = node_kv.get(key)
        if value is not None and value.lower() in ("true", "false"):
            value = value.lower()  # Java's Boolean.parseBoolean
        line = "%s=%s" % (key, value) if key in node_kv else "# " + r.lstrip("# ")
        if line == r.strip():
            continue  # as the role writes it: nothing to read
        got = _read_line(t, line, ctx)
        if got is not None:
            found.update(got)
    return found


def _extra_lines_at(name, tpl):
    """Where a file's extra lines go: (its template lines that join a list of them, the index of the line they
    follow, None when appended at the end)."""
    tail = [t for t in tpl[-2:] if "| join(" in t]
    # cassandra-env.sh and logback.xml: the line their extra lines follow (env: its last one; logback: the one
    # before </configuration>), the lines after it on the node; the JVM options files: lines appended
    after = next((i for i in range(len(tpl) - 1, max(len(tpl) - 4, -1), -1) if "| join(" in tpl[i]), None) \
        if name.endswith((".sh", ".xml")) else None
    if after is not None:
        tail = [tpl[after]]
    return tail, after


def _read_changed_lines(name, tpl, rendered, live, ctx, after):
    """The node's lines that differ from the rendering, each read against its template line -> (found: {variable:
    value}, appended: the node's lines after the line at after (cassandra-env.sh, logback.xml), extra: the JVM
    options no variable covers, or the lines appended after the last one)."""
    found, appended, extra = {}, [], []
    ops = difflib.SequenceMatcher(None, rendered, live, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op not in ("replace", "insert"):
            continue
        for k, j in enumerate(range(j1, j2)):
            i = i1 + k if op == "replace" and i1 + k < i2 else None
            got = _read_line(tpl[i], live[j], ctx) if i is not None and "{{" in tpl[i] else None
            if got is not None:
                found.update(got)
            elif name.endswith(".options") and live[j].startswith("-"):
                # bin/cassandra passes on the lines starting with '-', the last of an option counts:
                # at the end of the file, it has the same effect (unless a later line sets it again)
                if not any(_option_key(later) == _option_key(live[j]) for later in live[j + 1:]
                           if later.startswith("-")):
                    extra.append(live[j])
            elif after is not None:
                # after that line (logback.xml: still inside <configuration>), where the role writes them; a
                # blank line of the role's replaced: the node's lines in its place
                if live[j].strip() and i1 > after and (i1 == after + 1 or not name.endswith(".xml")) \
                        and all(not line.strip() for line in rendered[i1:i2]):
                    appended.append(live[j])
            elif op == "insert" and i1 >= len(rendered) - 2 and live[j]:
                extra.append(live[j])  # lines appended after the last one
    return found, appended, extra


def _import_file(env, series, name, ctx, live):
    """The values of the variables that render the template of the config file name as the node's lines live ->
    {variable: value}."""
    tpl, rendered = _render(env, series, name, ctx)
    if name.endswith(".properties") and len(tpl) == len(rendered):
        return _import_properties(tpl, rendered, live, ctx)
    # (the settings the stock file has no line for: written as by the role, however the node writes them)
    live = _align(rendered, _canonical(name, rendered + _declared_samples(series) if name.endswith(".yaml") else rendered,
                                       live))
    tail, after = _extra_lines_at(name, tpl)
    found, appended, extra = _read_changed_lines(name, tpl, rendered, live, ctx, after)
    if appended and _carried(name, series, tpl, appended):
        extra = appended  # all of them or none: a block cut in two would not run (if ... fi) or parse (<root>)
    if tail and extra:
        found[re.search(r"\((\w+) \| join", tail[0]).group(1)] = extra
    if name.endswith(".yaml"):
        # top-level settings by their key, wherever the node has them (lines
        # added or moved by hand shift the ones below out of place)
        found.update(_import_by_key(tpl, live, ctx))
    return found


def _carried(name, series, tpl, lines):
    """True when the lines a node has at the end of cassandra-env.sh or of logback.xml's <configuration> can be
    carried as its extra lines: none sets what a variable does (MAX_HEAP_SIZE=, JMX_PORT=, a JVM option of the
    series' templates, e.g. -Xmx, -Dcassandra.jmx.local.port, a GC; the logger of a variable, the root: the
    variable would then not count), and they make a whole (logback: XML elements; cassandra-env.sh: lines bash
    reads one by one, its if/fi, case/esac, do/done and braces closed). Else they are left to the report as hand
    edits."""
    templated = [re.sub(r"^\{\{.*?\}\}", "", t).strip() for t in tpl if "{{" in t]
    if name.endswith(".xml"):
        loggers = set(re.findall(r'<logger name="([^"]+)"', "\n".join(templated)))
        try:
            block = ET.fromstring("<x>%s</x>" % "\n".join(lines))
        except ET.ParseError:
            return False
        # (logback reads an element's name in any case, without its namespace)
        tags = [(e.tag.rsplit("}", 1)[-1].lower(), e.get("name")) for e in block.iter()]
        return not [t for t in tags if t[0] == "root" or t[0] == "logger" and t[1] in loggers]
    names = set()
    for t in templated:
        m = SHELL_SET.match(t.lstrip("#").strip())
        if m and m.group(2) != "JVM_OPTS":
            names.add(m.group(2))
    # the JVM options the series' cassandra-env.sh and JVM options files set, or a variable may set (the stock
    # examples commented out aside)
    options = {"-Xms", "-Xmx", "-Xmn", "-Xss"}
    folder = os.path.join(ROLE, "templates", SERIES[series])
    for f in sorted(os.listdir(folder)):
        if f == "cassandra-env.sh.j2" or f.endswith("-server.options.j2"):
            with open(os.path.join(folder, f)) as text:
                for line in text.read().split("\n"):
                    if "{{" in line or not line.lstrip().startswith("#"):
                        options.update(_option_key(w.lstrip("#")) for w in re.split(r"[\s\"']+", line)
                                       if w.lstrip("#").startswith(("-X", "-D")))
    words = []
    for line in lines:
        m = re.match(r"^\s*(?:export\s+)?(\w+)=", line)
        if m and m.group(1) in names:
            return False
        if any(_option_key(w) in options for w in re.split(r"[\s\"']+", line) if w.startswith(("-X", "-D"))):
            return False
        if line.rstrip().endswith("\\") or "<<" in line:
            return False
        try:
            words += shell_words(line)
        except ValueError:  # a quote going on to the next line
            return False
    pairs = (("if", "fi"), ("case", "esac"), ("do", "done"), ("{", "}"))
    return all(words.count(a) == words.count(b) for a, b in pairs)


# true/false as Cassandra's SnakeYAML (YAML 1.1) reads them
YAML_BOOLS = {"true": "true", "yes": "true", "on": "true", "false": "false", "no": "false", "off": "false"}
YAML_KEY = re.compile(r"^(\s*)(- )?([a-z0-9_]+):(?:[ \t]+(.*))?$")
SHELL_SET = re.compile(r"^(\s*)(\w+)=(.*)$")


def _style(value):
    value = value.strip()
    return value[0] if value[:1] in ("'", '"') else ""


def _yaml_styles(rendered):
    """The rendered cassandra.yaml's simple settings, commented ones too -> ({(indent, "- " or "", key): the quote
    the role writes the value with, "" for none}, {the boolean ones' (indent, "- " or "", key)})."""
    styles, bools = {}, set()
    for r in rendered:
        # a commented sub-key: "#  key:" ("#" in place of its first space) or "# key:"
        for m in map(YAML_KEY.match, (re.sub(r"^#\s?", "", r), re.sub(r"^#", "", r))):
            if m and m.group(4):
                styles.setdefault((m.group(1), m.group(2) or "", m.group(3)), _style(m.group(4)))
                if m.group(4).strip() in ("true", "false"):  # a boolean setting (| lower)
                    bools.add((m.group(1), m.group(2) or "", m.group(3)))
    return styles, bools


def _canonical_yaml_line(line, styles, bools):
    """A line of the node's cassandra.yaml, a setting of the template written as the role writes it (styles,
    bools: _yaml_styles') -> the line."""
    m = YAML_KEY.match(line)
    where = m and (m.group(1), m.group(2) or "", m.group(3))
    if m and m.group(4) is None and where in styles:
        return line.rstrip() + " "  # no value (null): read as the template's empty one
    if m and m.group(4) and where in styles:
        try:  # its text, quotes and comment out (not a number PyYAML would make of it: 0123 is not 83)
            value = yaml.load("v: " + m.group(4), Loader=yaml.BaseLoader)["v"]
        except (yaml.YAMLError, TypeError):
            value = None
        text = _yaml_text(value, styles[where]) if isinstance(value, str) else None
        # yes/on/TRUE, quoted or not, are true for a boolean setting only (text elsewhere)
        if isinstance(value, str) and where in bools and value.lower() in YAML_BOOLS:
            return "%s%s%s: %s" % (m.group(1), m.group(2) or "", m.group(3), YAML_BOOLS[value.lower()])
        if text is not None:
            return "%s%s%s: %s" % (m.group(1), m.group(2) or "", m.group(3), text)
        # else: as it is (the role's quoting could not write it the same)
    return line


def _last_key_counts(lines):
    """lines, a top-level key set twice: the last one counts (as for Cassandra), the others commented out (in
    place) -> lines."""
    last = {}
    for i, line in enumerate(lines):
        m = YAML_KEY.match(line)
        if m and not m.group(1) and not m.group(2):
            if m.group(3) in last:
                lines[last[m.group(3)]] = "# " + lines[last[m.group(3)]]
            last[m.group(3)] = i
    return lines


def _canonical_shell(rendered, live):
    """The node's cassandra-env.sh lines, each simple assignment of the template written in the role's quoting ->
    [line]."""
    styles = {}
    for r in rendered:
        m = SHELL_SET.match(re.sub(r"^#\s?", "", r))
        if m:
            styles.setdefault((m.group(1), m.group(2)), _style(m.group(3)))
    out = []
    for line in live:
        m = SHELL_SET.match(line)
        if m and (m.group(1), m.group(2)) in styles and not re.search(r"[$`]", m.group(3)):
            try:
                words = shlex.split(m.group(3), comments=True)
            except ValueError:
                words = []
            quote = styles[(m.group(1), m.group(2))]
            if len(words) == 1 and re.match(r"^[\w./:@%+,=-]*$" if quote != '"' else r'^[^"$`\\]*$', words[0]):
                line = "%s%s=%s" % (m.group(1), m.group(2), ('"%s"' % words[0]) if quote == '"' else words[0])
        elif m and m.group(1) and ("", m.group(2)) in styles and (m.group(1), m.group(2)) not in styles:
            line = line.lstrip()  # the role's own line before 2.x: " JVM_OPTS=..." (rmi hostname)
        out.append(line)
    return out


def _canonical(name, rendered, live):
    """The node's lines, each simple setting written the way the role writes
    that setting (its quoting, no comment after it, true/false in lower case),
    so that reading them against the template does not depend on how they
    were written; a top-level cassandra.yaml key set twice: the last one counts
    (as for Cassandra), the others are commented out."""
    if name.endswith(".yaml"):
        styles, bools = _yaml_styles(rendered)
        return _last_key_counts([_canonical_yaml_line(line, styles, bools) for line in live])
    if name.endswith(".sh"):
        return _canonical_shell(rendered, live)
    return live


def _option_key(line):
    """The JVM option a line sets (-XX:+Foo and -XX:-Foo: Foo; -Dx=1: -Dx; -Xmx4G: -Xmx)."""
    word = line.split()[0] if line.split() else ""
    m = (re.match(r"^-XX:[+-]?(\w+)", word) or re.match(r"^(-D[^=]+)", word)
         or re.match(r"^(-X(?:mx|ms|mn|ss))", word))
    return m.group(1) if m else line.strip()


def _yaml_text(value, quote):
    """value written in the template's quoting, so that YAML reads it back as
    the same text; None when that quoting cannot (a ' in '...', or plain text
    YAML would read as something else: '*x', 's #x', 'yes', '0123')."""
    if quote == "'":
        return None if "'" in value else "'%s'" % value
    if quote == '"':
        return None if re.search(r'["\\]', value) else '"%s"' % value
    try:
        same = yaml.load("v: " + value, Loader=yaml.BaseLoader)["v"] == value and \
            yaml.safe_load("v: " + value)["v"] == _value(value)
    except (yaml.YAMLError, TypeError):
        same = False
    return value if same and value.lower() not in YAML_BOOLS and value not in ("", "~", "null") else None


def _import_by_key(tpl, live, ctx):
    """The values of the template's top-level cassandra.yaml settings, each
    read from the node's line with that key."""
    lines = dict((m.group(3), line) for m, line in ((YAML_KEY.match(line), line) for line in live)
                 if m and not m.group(1) and not m.group(2))
    found = {}
    for t in tpl:
        m = TOP_KEY.match(t)
        if "{{" not in t or not m or m.group(1) not in lines:
            continue
        got = _read_line(t, lines[m.group(1)], ctx)
        if got is not None:
            found.update(got)
    return found


# cassandra.yaml directory settings: their variable, and the dir under the storage dir
STORAGE_DIRS = {"data_file_directories": ("cassandra_data_dir", "data"),
                "commitlog_directory": ("cassandra_commitlog_dir", "commitlog"),
                "saved_caches_directory": ("cassandra_saved_caches_dir", "saved_caches"),
                "hints_directory": ("cassandra_hints_dir", "hints")}


def _same_value(value, default):
    """The same as the role default: true/false in any case, else exactly
    (a password Cassandra is not cassandra)."""
    def norm(v):
        return str(v).lower() if isinstance(v, bool) or str(v).lower() in ("true", "false") else str(v)
    return norm(value) == norm(default)


def _default(ctx, key):
    return ctx.get(key, ctx.get(re.sub(r"^cassandra_jvm\d+_", "cassandra_jvm_", key), ""))


def _has_null(value):
    if isinstance(value, dict):
        value = list(value.values())
    return any(_has_null(v) for v in value) if isinstance(value, list) else value is None


def _same_setting(name, role, node):
    """True when the node line means the same as the role's: commented-out
    stock value the role writes explicitly, or YAML-equal (quoting, spacing),
    the same text included (a password 0123 or yes, which the role quotes)."""
    if name.endswith(".yaml") and node.lstrip("#").strip() == role.strip():
        return True  # elsewhere (env.sh, jvm options) a commented line is a switched-off one
    if name.endswith(".yaml") and ":" in role and not role.lstrip().startswith("#"):
        try:
            role_value, node_value = yaml.safe_load(role), yaml.safe_load(node)
            if role_value == node_value:
                return True
            if isinstance(role_value, dict) and isinstance(node_value, dict) and list(role_value) == list(node_value) \
                    and len(role_value) == 1 and str(list(role_value)[0]) in QUANTITY_KEYS:
                key = list(role_value)[0]
                if quantity(role_value[key]) is not None and quantity(role_value[key]) == quantity(node_value[key]):
                    return True  # the same amount in another unit
            # the same text, unless one of them is null (no value): Cassandra keeps null as null
            return not _has_null(role_value) and not _has_null(node_value) and \
                yaml.load(role, Loader=yaml.BaseLoader) == yaml.load(node, Loader=yaml.BaseLoader)
        except yaml.YAMLError:
            return False
    return False


def _extra_sections():
    """{section: the sub-keys of its variables}: the cassandra.yaml sections the template writes whose other
    sub-keys cassandra_extra_settings adds (the role's _cassandra_config_extra_sections)."""
    return role_file("cassandra_config", "vars")["_cassandra_config_extra_sections"]


_BLOCK_SCALAR = re.compile(r":\s+[|>][-+0-9]*\s*$")  # "key: |": the lines under it are its text


def _top_blocks(lines):
    """[(key, [line indexes])] of cassandra.yaml's active top-level keys: the key's line and the lines under it
    (indented, or a list's "- " at the start of a line), the blank and comment lines among them."""
    blocks, current, pending, scalar = [], None, [], None
    for i, line in enumerate(lines):
        m = re.match(r"^([a-z0-9_]+):", line)
        indent = len(line) - len(line.lstrip())
        if m:
            current, pending = (m.group(1), [i]), []
            blocks.append(current)
        elif current is None:
            continue
        elif scalar is not None and line.strip() and indent > scalar:
            current[1].extend(pending + [i])  # a block scalar's text, a "# ..." line too
            pending = []
            continue
        elif not line.strip() or line.lstrip().startswith("#"):
            pending.append(i)  # the block's if more of it follows
            continue
        elif line[:1] in (" ", "\t") or line.startswith("- ") or line.rstrip() == "-":
            current[1].extend(pending + [i])
            pending = []
        else:
            current, pending = None, []
        scalar = indent if current is not None and _BLOCK_SCALAR.search(line) else None
    return blocks


def _sub_key_lines(lines, indexes, keys, commented=False):
    """The indexes, among a section's (indexes: its key's line first), of the lines that set one of keys right
    under the key, with the lines under them; commented: their commented-out lines too ("  # key:")."""
    content = [i for i in indexes[1:] if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    if not keys or not content:
        return []
    child = len(lines[content[0]]) - len(lines[content[0]].lstrip())
    pattern = re.compile(r"^ {%d}(#\s?)?(%s):" % (child, "|".join(re.escape(k) for k in keys)))
    found, inside, scalar = [], False, False
    for i in indexes[1:]:
        line = lines[i]
        indent = len(line) - len(line.lstrip())
        if inside and scalar and line.strip() and indent > child:
            found.append(i)  # its block scalar's text, a "# ..." line too
            continue
        m = pattern.match(line)
        if m and (commented or not m.group(1)):
            found.append(i)
            inside = not m.group(1)
            scalar = inside and bool(_BLOCK_SCALAR.search(line))
            continue
        if inside and (not line.strip() or line.lstrip().startswith("#")):
            continue  # a blank or comment line among its lines: still under it
        if inside and (indent > child or indent == child and line.lstrip().startswith("- ")):
            found.append(i)
            continue
        inside = scalar = False
    return found


def _commented_under(lines, at):
    """The indexes of the commented-out lines right under line at, the stock sub-keys of a section ("#  selected_format:
    big", "  # log_dir:"), up to the first other line (a blank one too)."""
    found = []
    for i in range(at + 1, len(lines)):
        if not re.match(r"^(\s+#|#(\s{2,}\S|\s*- ))", lines[i]):
            break
        found.append(i)
    return found


def _extra_settings(tpl, live):
    """Active top-level cassandra.yaml keys the template does not have (uncommented or added by hand), each with
    the whole YAML block under it (a section, a list), and the sub-keys of a section of _extra_sections no
    variable covers: they belong in cassandra_extra_settings."""
    taken = {m.group(1) for m in map(TOP_KEY.match, tpl) if m}
    sections = _extra_sections()
    extras = {}
    for key, indexes in _top_blocks(live):
        if key in taken and key not in sections:
            continue
        try:  # (its last line ends with a newline: a block scalar "key: |" keeps its own)
            data = yaml.safe_load("\n".join(live[i] for i in indexes) + "\n")
        except yaml.YAMLError:
            continue  # left to the hand edits
        if not isinstance(data, dict) or key not in data:
            continue
        if key not in taken:
            extras.pop(key, None)  # set twice: the last one counts
            extras[key] = data[key]
        elif isinstance(data[key], dict):
            other = dict((k, v) for k, v in data[key].items() if k not in sections[key])
            extras.pop(key, None)
            if other:
                extras[key] = other
    return extras


def _without_extras(rendered, live, extras, rejected=()):
    """Both sides minus the extra settings: the role's block at the end, the commented stock lines of those keys
    (with their commented lines under them), and the node's own lines for them (their blocks); in a section of
    _extra_sections, the sub-keys' lines on both sides (the role's written after the logger, the node's), and their
    commented stock lines on both sides; the node's lines of the keys its Cassandra rejects (the role leaves them
    out), their indented lines too."""
    if EXTRA_HEADER in rendered:
        rendered = rendered[:rendered.index(EXTRA_HEADER) - 1] + [""]
    sections = _extra_sections()
    top = [k for k in extras if k not in sections]
    drop_rendered, drop_live = set(), set()
    if top:
        stock = re.compile(r"^#\s?(%s):" % "|".join(re.escape(k) for k in top))
        for i, line in enumerate(rendered):
            if stock.match(line):
                drop_rendered.update([i] + _commented_under(rendered, i))
    for key, indexes in _top_blocks(live):
        if key in top:
            drop_live.update(indexes + _commented_under(live, indexes[-1]))
    for key in [k for k in extras if k in sections and isinstance(extras[k], dict)]:
        for lines, drop in ((rendered, drop_rendered), (live, drop_live)):  # their commented stock lines too
            for name, indexes in _top_blocks(lines):
                if name == key:
                    drop.update(_sub_key_lines(lines, indexes + _commented_under(lines, indexes[-1]),
                                               list(extras[key]), True))
    rendered = [r for i, r in enumerate(rendered) if i not in drop_rendered]
    live = [n for i, n in enumerate(live) if i not in drop_live]
    if rejected:
        keys, kept, inside = "|".join(re.escape(k) for k in rejected), [], False
        for n in live:
            inside = bool(re.match(r"^(%s):" % keys, n)) or (inside and bool(re.match(r"^(\s|- |-$)", n)))
            if not inside:
                kept.append(n)
        live = kept
    return rendered, live


@functools.lru_cache(maxsize=None)
def _declared_lines(series):
    """The template's lines of the settings the stock file has no line for (after DECLARED_HEADER)."""
    with open(os.path.join(ROLE, "templates", SERIES[series], "cassandra.yaml.j2")) as f:
        tpl = f.read().split("\n")
    if DECLARED_HEADER not in tpl:
        return ()
    at = tpl.index(DECLARED_HEADER)
    end = next((i for i in range(at, len(tpl)) if tpl[i] == EXTRA_HEADER), len(tpl))
    return tuple(line for line in tpl[at:end] if TOP_KEY.match(line))


def _declared_keys(series):
    return [TOP_KEY.match(line).group(1) for line in _declared_lines(series)]


def _declared_samples(series):
    """Their lines with a sample value, for _canonical: a plain value, true for a boolean."""
    return [re.sub(r"\{\{ \(?\w+(?: \| bool\))?\)? \| lower \}\}", "true", re.sub(r"\{\{ \w+ \}\}", "x", line))
            for line in _declared_lines(series)]


def _without_declared(series, rendered, live):
    """Both sides minus: the settings the stock file has no line for (the role's block before the extra settings),
    with the node's line of each wherever it is when it sets the same (the import read them by key; another one
    stays, a hand edit), or when it is Cassandra's default the role leaves out (auto_bootstrap: true, a note); the
    settings the comparisons leave out where the role's file has a line for them (NOT_COMPARED, 5.0's
    heap_dump_path), wherever they are on both sides, a note for the node's line the role would not write the same."""
    role = {}
    if DECLARED_HEADER in rendered:
        at = rendered.index(DECLARED_HEADER) - 1  # its blank line
        end = next((i for i in range(at, len(rendered)) if rendered[i] == EXTRA_HEADER), len(rendered))
        end = end - 1 if end < len(rendered) else end  # the extra settings' blank line, where _without_extras cuts
        role = dict((m.group(1), r) for m, r in ((TOP_KEY.match(r), r) for r in rendered[at:end]) if m)
        rendered = rendered[:at] + rendered[end:]
    in_live = set(key for key, dummy in _top_blocks(live))
    skipped = [k for k in NOT_COMPARED if any(re.match(r"^#?\s?%s:" % k, r) for r in rendered)]
    for key in skipped:
        role[key] = next((r for r in rendered if r.startswith(key + ":")), None)
        # its commented stock line too, in place of the node's line
        rendered = [r for r in rendered if not re.match(r"^#?\s?%s:" % key if key in in_live else r"^%s:" % key, r)]
    drop, notes = set(), []
    for key, indexes in _top_blocks(live):
        if key not in role and key not in DEFAULT_WHEN_ABSENT:
            continue
        line = live[indexes[0]]
        same = len(indexes) == 1 and role.get(key) is not None and _same_setting("cassandra.yaml", role[key], line)
        default = len(indexes) == 1 and role.get(key) is None and key in DEFAULT_WHEN_ABSENT \
            and YAML_BOOLS.get(str(_yaml_string(line.split(":", 1)[1].strip())).lower()) == DEFAULT_WHEN_ABSENT[key]
        if key in skipped:
            drop.update(indexes)
            if not same:
                notes.append("cassandra.yaml: %s, not compared (%s)" % (_mask(line.strip()), NOT_COMPARED[key]))
        elif same or default:
            drop.update(indexes)
            role.pop(key, None)
            if default:
                notes.append("cassandra.yaml: %s, Cassandra's default, left out" % line.strip())
    rendered += [line for key, line in role.items() if key not in skipped and line is not None]  # the role adds them
    return rendered, [n for i, n in enumerate(live) if i not in drop], notes


def _without_former_names(rendered, live):
    """Both sides minus the node's line of a key under its former name (4.0's: 4.1+ reads either), wherever it is,
    and the role's line of the new name, when the role sets the same (as Cassandra converts it); a note for each."""
    role = dict((m.group(1), i) for i, m in enumerate(map(TOP_KEY.match, rendered)) if m)
    node = dict((key, indexes[0]) for key, indexes in _top_blocks(live) if len(indexes) == 1)
    drop_rendered, drop_live, notes = set(), set(), []
    for old, new in former_keys():
        if old not in node or new not in role or node[old] in drop_live:
            continue
        try:
            value = former_value(old, (yaml.safe_load(live[node[old]]) or {}).get(old))
            role_value = (yaml.safe_load(rendered[role[new]]) or {}).get(new)
        except (yaml.YAMLError, AttributeError):
            continue
        if value is not None and (str(value) == str(role_value) or
                                  quantity(value) is not None and quantity(value) == quantity(role_value)):
            drop_rendered.add(role[new])
            drop_live.add(node[old])
            notes.append("cassandra.yaml: %s  (node: %s)" % (rendered[role[new]].strip(), live[node[old]].strip()))
    return ([r for i, r in enumerate(rendered) if i not in drop_rendered],
            [n for i, n in enumerate(live) if i not in drop_live], notes)


def _snitch_reads_rackdc(snitch):
    """False for the snitches that take the node's dc and rack from elsewhere than
    cassandra-rackdc.properties' dc= and rack= (the role's list); a snitch of
    another class may read them."""
    unread = role_file("cassandra_config", "vars")["_cassandra_config_snitches_without_rackdc"]
    name = str(snitch or "").strip()
    prefix = "org.apache.cassandra.locator."  # a name without a dot is in that package
    return (name[len(prefix):] if name.startswith(prefix) else name) not in unread


def _same_settings(name, role, node):
    """A block that differs but sets the same: each of the role's setting lines
    paired, in order, with the node's line that means the same, every other line on both
    sides a comment or blank (# in the files read here but logback.xml).
    [(role line, node line)], else None."""
    def comment(line):
        return not line.strip() or (not name.endswith(".xml") and line.lstrip().startswith("#"))
    pairs, left, at = [], [], 0
    for r in role:
        if comment(r):
            continue
        j = next((j for j in range(at, len(node)) if _same_setting(name, r, node[j])), None)
        if j is None:
            return None
        pairs.append((r, node[j]))
        left += node[at:j]
        at = j + 1
    return pairs if all(comment(n) for n in left + node[at:]) else None


def _properties(lines):
    """The settings of a key=value (or key: value) file, comments and blank
    lines left out; a value keeps its trailing spaces, as for Java's Properties
    (prefer_local=true followed by a space is not true). A line ending with an
    odd number of backslashes goes on to the next one, as for Java (a comment
    line never does): its value is not read as another setting."""
    out, logical = {}, []
    for line in lines:
        line = line.lstrip(" \t\f").rstrip("\r")  # a CRLF file: \r ends the line, as for Java
        if logical and (len(logical[-1]) - len(logical[-1].rstrip("\\"))) % 2:
            logical[-1] = logical[-1][:-1] + line
        elif line.strip() and not line.startswith(("#", "!")):
            logical.append(line)
    for line in logical:
        # the key up to =, : or a space (key value too), as Java reads it (cassandra_settings' properties_settings)
        key, value = re.match(r"^((?:[^\\=: \t\f]|\\.)*)[ \t\f]*[=: \t\f]?[ \t\f]*(.*)$", line).groups()
        out[key] = value
    return out


class _Shown(str):
    """A line of a file with its text masked as in the whole file (shown), kept through the filters that drop lines."""
    def __new__(cls, text, shown):
        line = str.__new__(cls, text)
        line.shown = shown
        return line

    @classmethod
    def lines(cls, lines):
        return [cls(line, shown) for line, shown in zip(lines, out.mask_lines(lines))]

    @staticmethod
    def masked(lines):
        """The lines as shown: masked as in the whole file (out.mask_lines), each _Shown one as it was."""
        if all(isinstance(line, _Shown) for line in lines):
            return [line.shown for line in lines]
        return [line.shown if isinstance(line, _Shown) else shown for line, shown in zip(lines, out.mask_lines(lines))]


def _properties_leftovers(name, rendered, live_text):
    """A key=value file: only the settings count, not comments, blank lines or order -> (hand: each setting that
    differs, its values masked; normalized: the file rewritten with the same settings)."""
    hand, normalized = [], []
    role_kv, node_kv = _properties(rendered), _properties(live_text.split("\n"))
    for key in sorted(set(role_kv) | set(node_kv)):
        if role_kv.get(key) != node_kv.get(key):
            hand.append("%s: %s" % (name, key))
            hand += ["  - %s=%s" % (key, out.MASK if _secret(key, role_kv[key]) else _mask(role_kv[key]))] \
                if key in role_kv else []
            hand += ["  + %s=%s" % (key, out.MASK if _secret(key, node_kv[key]) else _mask(node_kv[key]))] \
                if key in node_kv else []
    if role_kv == node_kv and rendered != live_text.split("\n"):
        normalized.append("%s: same settings, comments and layout rewritten" % name)
    return hand, normalized


def _yaml_compared(series, rendered, live, extras, rejected):
    """cassandra.yaml's lines to compare: without the settings the stock file has no line for, the keys under their
    former names, the extra settings and the keys rejected (masked first) -> (rendered, live, normalized: notes)."""
    normalized = []
    rendered, live, notes = _without_declared(series, rendered, live)
    normalized += notes
    rendered, live, notes = _without_former_names(rendered, live)
    normalized += notes
    if extras or rejected:
        # masked before the lines of those keys go: a secret's value may go on past the lines dropped
        rendered, live = _without_extras(_Shown.lines(rendered), _Shown.lines(live), extras, rejected)
        sections = _extra_sections()
        normalized += ["cassandra.yaml: %s kept with cassandra_extra_settings (written %s)" % (
            k, "in its section" if k in sections else "at the end of the file") for k in sorted(extras)]
    return rendered, live, normalized


def _file_ops(name, rendered, live):
    """The differences of a file's lines from the rendering (difflib opcodes), a JVM options file's options moved
    only left out -> (opcodes, normalized: notes)."""
    ops = difflib.SequenceMatcher(None, rendered, live, autojunk=False).get_opcodes()
    moved = [op for op in ops if op[0] != "equal"
             and _same_settings(name, rendered[op[1]:op[2]], live[op[3]:op[4]]) is None]
    if name.endswith(".options") and moved and \
            jvm_options_settings("\n".join(rendered)) == jvm_options_settings("\n".join(live)):
        # the same options, a line at another place (the last of an option counts): cassandra_config
        # leaves such a file as it is
        return [op for op in ops if op not in moved], [
            "%s: the same options, some at another place: left as it is (cassandra_config_normalize: true"
            " rewrites it)" % name]
    return ops, []


def _difference_lines(name, rendered, live, ops):
    """Each difference of a file: the same settings written another way (normalized, its comment lines counted),
    else a hand edit, its lines masked as in the whole file -> (hand, normalized, comments: (file, line, node's
    lines, role's lines))."""
    hand, normalized, comments = [], [], []
    # shown masked as in the whole file: a line of a secret's value (a YAML block, a quote left open) is
    # masked even when its key is not in the lines that differ
    shown_rendered, shown_live = _Shown.masked(rendered), _Shown.masked(live)
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            continue
        pairs = _same_settings(name, rendered[i1:i2], live[j1:j2])
        if pairs is not None:
            at_r = _in_order(rendered[i1:i2], [r for r, n in pairs])
            at_n = _in_order(live[j1:j2], [n for r, n in pairs])
            normalized += ["%s: %s  (node: %s)" % (name, shown_rendered[i1 + r].strip(), shown_live[j1 + n].strip())
                           for r, n in zip(at_r, at_n)]
            if (i2 - i1) + (j2 - j1) > 2 * len(pairs):
                # comment lines too, e.g. the stock comments of the release the file came from
                comments.append((name, j1 + 1, j2 - j1, i2 - i1))
            continue
        hand.append("%s, line %d:" % (name, j1 + 1))
        hand += ["  - " + line for line in shown_rendered[i1:i2]] + ["  + " + line for line in shown_live[j1:j2]]
    return hand, normalized, comments


def _leftovers(env, series, files, ctx, live_files, rejected=(), skip=()):
    """What the role would still change in the node's files, rendered with ctx: rejected: the cassandra.yaml keys
    its Cassandra rejects; skip: files not compared here (the JVM's, its arguments shown to be the same) -> (hand
    edits, normalized lines, comment lines)."""
    hand, normalized, comments = [], [], []
    extras = ctx.get("cassandra_extra_settings") or {}
    for name in files:
        if name not in live_files or name in skip:
            continue
        rendered = _render(env, series, name, ctx)[1]
        if name.endswith(".properties"):
            more_hand, more_normalized = _properties_leftovers(name, rendered, live_files[name])
            hand += more_hand
            normalized += more_normalized
            continue
        if name.endswith(".xml") and _same_xml(live_files[name], "\n".join(rendered)):
            # logback.xml: its elements and attributes the same, comments and layout aside (as logback reads it)
            if live_files[name] != "\n".join(rendered):
                normalized.append("%s: same settings, written another way (comments, layout, quotes)" % name)
            continue
        live = _align(rendered, live_files[name].split("\n"))
        if name == "cassandra.yaml":
            rendered, live, notes = _yaml_compared(series, rendered, live, extras, rejected)
            normalized += notes
        ops, notes = _file_ops(name, rendered, live)
        normalized += notes
        if all(op == "equal" for op, i1, i2, j1, j2 in ops):
            continue
        more_hand, more_normalized, more_comments = _difference_lines(name, rendered, live, ops)
        hand += more_hand
        normalized += more_normalized
        comments += more_comments
    return hand, normalized, _comment_lines(comments)


def _in_order(lines, picked):
    """The indexes in lines of picked, lines of it in the same order."""
    at, out = 0, []
    for line in picked:
        at = lines.index(line, at)
        out.append(at)
        at += 1
    return out


def _template_options(env, series, ctx, name):
    """A jvm options template -> (the option lines it always writes as they
    are, the names of the options its other lines set)."""
    tpl = _render(env, series, name, ctx)[0]
    static = set(line for line in tpl if "{{" not in line and line.startswith("-") and len(line.split()) == 1)
    templated = set(jvm.option_name(EXPR.sub("", line).lstrip("#").strip()) for line in tpl if "{{" in line)
    return static, templated - {None}


def _running_heap(cmdline):
    """The heap options the running JVM got (the last of each counts), as a text."""
    last = {}
    for word in re.split(r"[\0\n]", str(cmdline or "")):
        if jvm.option_name(word) in HEAP_OPTIONS:
            last[jvm.option_name(word)] = word
    return " ".join(last[k] for k in HEAP_OPTIONS if k in last)


def _heap_source(found, read, java):
    """Where the heap the JVM runs with comes from: the options files it reads when they have both an -Xmx and an
    -Xms (cassandra-env.sh greps for Xmx and Xms; then it adds none from MAX_HEAP_SIZE), else cassandra-env.sh."""
    there = [(f, o) for f in read for o in jvm.words(found.get(jvm.options_variable(f))) if o.startswith(("-Xmx", "-Xms"))]
    if {o[:4] for dummy, o in there} == {"-Xmx", "-Xms"}:
        return ", ".join("%s (%s)" % (o, f) for f, o in there)
    if there:  # cassandra-env.sh would refuse to start ("set or unset -Xmx and -Xms flags in pairs")
        return ("%s with the other of -Xmx/-Xms given another way (cassandra-env.sh refuses one of them alone in the"
                " options files)" % ", ".join("%s (%s)" % (o, f) for f, o in there))
    if found.get("cassandra_heap_size"):
        return "cassandra_heap_size %s (cassandra-env.sh's MAX_HEAP_SIZE)" % found["cassandra_heap_size"]
    return "the unit's MAX_HEAP_SIZE if it sets one, else cassandra-env.sh's automatic sizing (Java %s)" % java


def _jvm_options(env, series, ctx, files, found, java, keep_unread, cmdline):
    """The extra options as the running Java reads them, found changed in place:
    those of a jvm<N> file it does not read dropped (keep_unread: kept), those
    the file's template writes as they are not carried again. -> (notes,
    warnings, files to leave as they are on the node)."""
    if not jvm.options_file(series, java):  # its Java not known: as read
        return [], [], []
    read = jvm.read_files(series, java)
    notes, keep, heaps = [], [], []
    for name in files:
        var = jvm.options_variable(name)
        if not var or name in read or not found.get(var):
            continue
        shown = ", ".join(_mask(o) for o in found[var])
        if keep_unread:
            notes.append("%s: %s not read by Java %s (it reads %s): imported as %s all the same"
                         " (import_cluster_keep_unread_jvm_options)" % (name, shown, java, " and ".join(read), var))
            continue
        heaps += [(name, o) for o in jvm.words(found.pop(var)) if jvm.option_name(o) in HEAP_OPTIONS]
        keep.append(name)
        notes.append("%s: %s not read by Java %s (it reads %s): not imported, nodes added later get the role's file."
                     " They stay in this node's file, left as it is (cassandra_config_keep_files): they would apply"
                     " on a move to the Java that reads it (update_java and upgrade refuse it until the file is out of"
                     " cassandra_config_keep_files). To import them anyway: -e"
                     " import_cluster_keep_unread_jvm_options=true" % (name, shown, java, " and ".join(read)))
    # an option the file's template always writes as it is, set again at another place in the file (the last
    # one counts, the same): not carried. Not one without a name (-ea, an agent) nor one that adds up: the JVM
    # gets each of them
    for name in read:
        var = jvm.options_variable(name)
        always, set_elsewhere = _template_options(env, series, ctx, name)
        names = [jvm.option_name(o.strip()) for o in found.get(var) or []]
        for line in list(found.get(var) or []):
            option = jvm.option_name(line.strip())
            if (line.strip() in always and option is not None and option not in jvm.LISTS
                    and option not in set_elsewhere and names.count(option) == 1):
                found[var] = [o for o in found[var] if o != line]
                notes.append("%s: %s, which its template writes already: not carried in %s"
                             % (name, line.strip(), var))
        if var in found and not found[var]:
            found.pop(var)
    if heaps:
        running = _running_heap(cmdline)
        notes.append("Heap: %s, in a file Java %s does not read, is not the one the JVM runs with%s: that comes from"
                     " %s" % (", ".join("%s (%s)" % (o, f) for f, o in heaps), java,
                              (" (%s)" % running) if running else "", _heap_source(found, read, java)))
    options = [(o, "%s, from %s" % (var, f), jvm.fix_it(var)) for f, var in [(f, jvm.options_variable(f)) for f in read]
               for o in jvm.words(found.get(var))]
    return notes, jvm.check(options, java), keep


def _same_xml(live, rendered):
    try:
        return xml_settings(live) == xml_settings(rendered)
    except (ET.ParseError, RecursionError):  # not XML, or nested too deep to walk: compared line by line
        return False


def _comment_lines(blocks):
    """[(file, node line, node lines, role lines)] -> one report line per file."""
    out = []
    for name in sorted({b[0] for b in blocks}):
        where = []
        for dummy, line, node, role in (b for b in blocks if b[0] == name):
            if node == 0:
                where.append("%d line%s missing before line %d" % (role, "s" if role > 1 else "", line))
            else:
                where.append("line %d" % line if node == 1 else "lines %d-%d" % (line, line + node - 1))
        out.append("%s: %s" % (name, ", ".join(where)))
    return out


def _jmx_lines(text):
    """The lines of a JMX password/access file, without comment lines: # or !
    only at the start of a line (a # further on belongs to the password)."""
    text = re.sub(r"\\\n", " ", text or "")  # access lines go on after a backslash
    return [line.split() for line in text.split("\n") if line.strip() and not line.lstrip().startswith(("#", "!"))]


def _jmx_users(password_file, access_file):
    """cassandra_jmx_users from jmxremote.password and jmxremote.access (None: no user with rights): the users of
    the password file with a plain password, then the roles with rights only (no password the role can write back:
    none, e.g. the JDK's stock controlRole, or hashed); rights other than the role's readwrite or readonly lines
    kept whole in access. What is left out (a user without rights, rights the JVM refuses): hand edits of the
    files (_jmx_leftovers)."""
    rights = dict((words[0], words[1:]) for words in _jmx_lines(access_file) if len(words) >= 2)
    rights = dict((k, v) for k, v in rights.items() if JMX_RIGHTS.match(" ".join(v)))  # (a key's last line: the JVM's)
    passwords = dict((words[0], words[1:]) for words in _jmx_lines(password_file))
    users = [{"name": name, "password": words[0]} for name, words in passwords.items()
             if len(words) == 1 and name in rights]
    users += [{"name": name} for name in rights if name not in [u["name"] for u in users]]
    for user in users:
        spec = rights[user["name"]]
        if spec in (["readonly"], ["readwrite"] + JMX_CREATE_UNREGISTER):
            user["access"] = spec[0]
        elif spec == ["readwrite"]:
            user.update(access="readwrite", create_unregister=False)  # the role's readwrite line adds them
        else:
            user["access"] = " ".join(spec)  # written as it is (e.g. other create classes)
    return users or None


def _jmx_leftovers(env, users, live_files):
    """What the role's JMX files would change on the node with these users (entries as the JVM reads them:
    comments, spacing, line continuations and the order of the rights aside), as hand edits of the file:
    "jmxremote.access: <role>", its line "  - " (the role's) and "  + " (the node's); passwords masked."""
    hand = []
    for name in ("jmxremote.password", "jmxremote.access"):
        with open(os.path.join(ROLE, "templates", name + ".j2")) as f:
            role = env.from_string(f.read()).render(cassandra_jmx_users=users)
        mine, node = [dict((w[0], w[1:]) for w in _jmx_lines(text)) for text in (role, live_files.get(name, ""))]
        for key in [k for k in list(node) + [k for k in mine if k not in node]
                    if sorted(mine.get(k, ["?"])) != sorted(node.get(k, ["?"]))]:
            hand.append("%s: %s" % (name, _mask(key)))
            for sign, entry in (("-", mine), ("+", node)):
                if key in entry:
                    words = [out.MASK] if name.endswith(".password") else entry[key]
                    hand.append("  %s %s" % (sign, _mask(" ".join([key] + words))))
    return hand


# the JVM options naming the remote JMX files, and the ones of a JMX authentication by JAAS
_JMX_FILE_OPTION = r"-Dcom\.sun\.management\.jmxremote\.%s\.file=(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
_JAAS = re.compile(r"-D(cassandra\.jmx\.remote\.login\.config|java\.security\.auth\.login\.config"
                   r"|cassandra\.jmx\.authorizer)=")
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|\S*)\s*(?:#.*)?$")
JMX_FILE_DEFAULTS = {"password": "/etc/cassandra/jmxremote.password", "access": "/etc/cassandra/jmxremote.access"}


def _shell_value(text, names):
    """A shell word with its quotes off and $NAME, ${NAME} replaced from names (not inside single quotes); None
    when one is not known."""
    if text[:1] == "'":
        return text[1:-1] if text.endswith("'") and len(text) > 1 else text[1:]
    if text[:1] == '"':
        text = text[1:-1] if text.endswith('"') and len(text) > 1 else text[1:]

    def known(match):
        name = match.group(1) or match.group(2)
        if name not in names:
            raise KeyError(name)
        return names[name]
    try:
        text = re.sub(r"\$\{(\w+)\}|\$(\w+)", known, text)
    except KeyError:
        return None
    # what only the shell knows (${X:-d}, $(..), `..`, ~): not a path
    return None if re.search(r"[$`]", text) or text.startswith("~") else text


def _shell_code(line):
    """A shell line without its comment (a # starting a word, outside quotes)."""
    quote = None
    for i, char in enumerate(line):
        if quote:
            quote = None if char == quote else quote
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def cassandra_jmx_files(env_sh, options=None, cmdline="", conf_dir="", cassandra_yaml=""):
    """Where the running JVM reads its remote JMX password and access files: its own arguments (cmdline:
    /proc/<pid>/cmdline), else cassandra-env.sh (its last line that sets them, comments aside, $CASSANDRA_CONF
    and the variables it sets expanded), else the JVM options files (options: {file name: text}). A path
    cassandra-env.sh gives through what only the shell knows (a variable it does not set, ${X:-d}, $(..)) is
    looked for in the next ones.
    -> {password, access: the paths ('' when not found), from: where, unknown: what cassandra-env.sh gives that
    no source resolves ('' when nothing), jaas: what says JMX authenticates by JAAS ('' when nothing does)}."""
    names = {"CASSANDRA_CONF": conf_dir} if conf_dir else {}
    lines = [_shell_code(line) for line in str(env_sh or "").split("\n")]
    seen = {}
    for line in lines:
        assigned = _ASSIGN.match(line)
        if assigned and assigned.group(1) != "JVM_OPTS":
            value = _shell_value(assigned.group(2), names)
            seen.setdefault(assigned.group(1), set()).add(value)
            # set twice to different values (in branches of an if): not known
            if value is not None and len(seen[assigned.group(1)]) == 1:
                names[assigned.group(1)] = value
            else:
                names.pop(assigned.group(1), None)
    # the running JVM's own arguments first: what it reads (cassandra-env.sh may set them in branches)
    sources = [("the running JVM's arguments", re.split(r"[\0\n]", str(cmdline or "")), False),
               ("cassandra-env.sh", lines, True)]
    for name, text in sorted((options or {}).items()):
        sources.append((name, [line for line in str(text or "").split("\n") if not line.lstrip().startswith("#")],
                        False))
    out = {"password": "", "access": "", "from": "", "unknown": "", "jaas": ""}
    unknown = {}
    for name, source_lines, shell in sources:
        for kind in ("password", "access"):
            value = raw = None
            for line in source_lines:
                for match in re.finditer(_JMX_FILE_OPTION % kind, line):  # the last one wins, as for the JVM
                    raw = match.group(1)
                    value = _shell_value(raw, names) if shell else raw.strip("\"'")
                    # (what a shell would run or expand, from any source: never a path the role writes)
                    value = None if value is None or re.search(r"[$`]", value) else value
            if raw is None or out[kind]:
                continue
            if value:  # each file from the first source that gives it
                out[kind] = value
                out["from"] = out["from"] or name
                unknown.pop(kind, None)
            else:
                unknown[kind] = "%s.file=%s" % (kind, raw)
        if not out["jaas"]:
            jaas = next((m.group(1) for line in source_lines for m in [_JAAS.search(line)] if m), "")
            out["jaas"] = ("-D%s in %s" % (jaas, name)) if jaas else ""
    out["unknown"] = ", ".join(unknown[k] for k in ("password", "access") if k in unknown)
    if not out["jaas"] and re.search(r"(?m)^jmx_server_options:\s*$", str(cassandra_yaml or "")) and re.search(
            r"(?m)^\s+login_config_(name|file):\s*\S", str(cassandra_yaml or "")):
        out["jaas"] = "jmx_server_options in cassandra.yaml"
    return out


def cassandra_unit_environment(text):
    """systemctl show -p Environment --value -> {name: value} (quoted entries kept whole)."""
    try:
        words = shlex.split(text or "")
    except ValueError:
        words = (text or "").split()
    return dict(w.split("=", 1) for w in words if "=" in w)


def undefined_name(exc):
    """A missing variable or field of the nodes' data (Ansible's lazy
    templating), whose message names it and shows no value."""
    source = getattr(exc, "source", None)  # 2.19+: the marker behind the error
    return (isinstance(exc, (AnsibleUndefinedVariable, jinja2.exceptions.UndefinedError))
            and (source is None or type(source).__name__ == "UndefinedMarker")
            and bool(MISSING_NAME.search(str(exc))))


def error_without_message(name, where):
    """The error being handled, without its message (it may quote a value)."""
    frames = [f for f in traceback.extract_tb(sys.exc_info()[2])
              if re.match(r"cassandra_import(_check|_nodes)?[.]py$", os.path.basename(f[0]))]
    at = " in %s(), line %d" % (frames[-1][2], frames[-1][1]) if frames else ""
    return AnsibleFilterError("%s: %s%s%s (message hidden: it may show a value read from the nodes)"
                              % (name, (where + ", ") if where else "", sys.exc_info()[0].__name__, at))


def cassandra_config_import(live_files, cassandra_version, facts, conf_target="", storage_dir="", jmx_files=None,
                            release="", java="", keep_unread_jvm_options=False, cmdline="", jvm=None):
    """cassandra_config variables that render a node's files, and what the
    role would still change: {'vars', 'hand_edits', 'normalized', 'comments'
    (comment lines only, which set nothing), 'not_settings' ({'keys': the
    cassandra.yaml keys its Cassandra rejects, not imported, 'release'}),
    'release' (the version they were judged by), 'moved' (hand edits of
    cassandra-env.sh moved to variables), 'rewritten' (the files the role
    then writes in its own layout, the JVM getting the same arguments),
    'cleanup' (options the running JVM does not use), 'not_moved' (why
    cassandra-env.sh's JVM_OPTS lines or heap were not moved, '' if moved or none)}.
    conf_target: the resolved dir the node reads its config from; storage_dir:
    the JVM's -Dcassandra.storagedir; jmx_files: cassandra_jmx_files' (the
    remote JMX files the JVM reads, under the names jmxremote.password and
    .access in live_files); release: the node's Cassandra version (4.1.5;
    '': the keys no release of the series accepts are the ones rejected); java: the
    running Java's major ('': not known), the options of a jvm<N> file it does not
    read are dropped ('jvm_notes'), unless keep_unread_jvm_options; cmdline: the
    running JVM's arguments (/proc/<pid>/cmdline), for its heap. Also 'jvm_notes',
    'jvm_warnings' (options worth a look: why, the way out) and 'keep_files'
    (the files to leave as they are on the node); jvm: the running JVM,
    {cmdline: its /proc/<pid>/cmdline, java: its major version, environment:
    what the unit gives it, newer: the config files written since it started,
    keep: import_cluster_keep_hand_edits, the JVM's files it keeps not moved}
    (java and cmdline from it when not given)."""
    if cassandra_version not in SERIES:
        raise AnsibleFilterError("cassandra_config_import: unsupported series %s" % cassandra_version)
    where = ["the role defaults"]
    # Java's Properties ends a line at \r\n, \r or \n (the other files: as their programs read them)
    live_files = dict((k, re.sub(r"\r\n?", "\n", v) if k.endswith(".properties") and isinstance(v, str) else v)
                      for k, v in live_files.items())
    try:
        return _config_import(live_files, cassandra_version, facts, where, conf_target, storage_dir, jmx_files, release,
                              java, boolean(keep_unread_jvm_options, strict=False), cmdline, jvm)
    except AnsibleFilterError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        if undefined_name(exc):
            raise
        error = error_without_message("cassandra_config_import", where[0])
    raise error  # out of the except block: no chained message either


def _changed_values(found, ctx, from_shell):
    """The values found that are not the role defaults and are values for their variable -> {variable: value}."""
    return {k: v for k, v in found.items()
            if not _same_value(v, _default(ctx, k))
            and not (v == "" and _default(ctx, k) in ([], {}))  # a list/dict variable left empty
            # a number the node does not give as one (e.g. JMX_PORT="${JMX_PORT:-7299}"): not a value
            # for the variable, left to the report (the role would write the default)
            and not (isinstance(_default(ctx, k), int) and not isinstance(_default(ctx, k), bool)
                     and not isinstance(v, int))
            # a shell expansion (CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"): not a value either,
            # unless the variable is one (cassandra_heap_dump_dir: $CASSANDRA_LOG_DIR, on 4.x too)
            and not (k in from_shell and isinstance(v, str) and re.search(r"[$`]", v)
                     and not re.search(r"[$`]", str(_default(ctx, k))) and k != "cassandra_heap_dump_dir")}


def _storage_dirs(cassandra_yaml, storage_dir, ctx, changed):
    """The directories cassandra.yaml leaves out: under the JVM's -Dcassandra.storagedir (a tarball's is
    $CASSANDRA_HOME/data), where the role writes its defaults -> ({variable: directory} not in changed, the
    storage dir to keep as cassandra_config_storage_dir or None)."""
    try:
        data = yaml.safe_load(cassandra_yaml) or {}
    except yaml.YAMLError:
        data = {}
    dirs = {}
    for key, (var, sub) in STORAGE_DIRS.items():
        path = "%s/%s" % (storage_dir.rstrip("/"), sub)
        if isinstance(data, dict) and data.get(key) is None and var not in changed and path != _default(ctx, var):
            dirs[var] = path
    # cassandra_config keeps such a file where it has the same directories, also while Cassandra is down
    if isinstance(data, dict) and any(data.get(key) is None for key in STORAGE_DIRS if key != "cdc_raw_directory"):
        return dirs, storage_dir
    # with CDC on, the role creates the cdc_raw dir cassandra.yaml leaves out there too (else under the
    # packages' /var/lib/cassandra)
    if isinstance(data, dict) and str(data.get("cdc_enabled")).lower() == "true" and data.get("cdc_raw_directory") is None \
            and storage_dir.rstrip("/") != "/var/lib/cassandra":
        return dirs, storage_dir
    return dirs, None


def _data_dirs(cassandra_yaml, ctx):
    """JBOD: the template's single line expands to one line per directory -> ({variable: value} found and changed,
    the directories to render with, None when the template's)."""
    try:
        dirs = (yaml.safe_load(cassandra_yaml) or {}).get("data_file_directories") or []
    except yaml.YAMLError:
        dirs = []
    if len(dirs) > 1:
        return {"cassandra_data_dir": dirs[0], "cassandra_data_file_directories": dirs}, None
    if dirs and dirs[0] != _default(ctx, "cassandra_data_dir"):
        # one directory, not the default one: cassandra_data_dir, the list follows it
        return {"cassandra_data_dir": dirs[0]}, dirs
    return {}, None


def _yaml_extras(env, series, ctx, cassandra_yaml, release, found, changed):
    """cassandra.yaml's settings no variable covers: a key under its former name (a file from 4.0) to its variable
    (found and changed updated), the keys Cassandra rejects left out -> (cassandra_extra_settings, the keys
    rejected)."""
    tpl = _render(env, series, "cassandra.yaml", ctx)[0]
    extras = _extra_settings(tpl, cassandra_yaml.split("\n"))
    # 4.1+: a key under its former name (a file from 4.0): its variable, which writes it under its new name
    live_keys = set(key for key, dummy in _top_blocks(cassandra_yaml.split("\n")))
    for key, var in former_names(SERIES[series]).items():
        value = former_value(key, extras[key]) if key in extras else None
        if value is not None and var[len("cassandra_"):] not in live_keys:
            del extras[key]
            found[var] = canonical_quantity(var[len("cassandra_"):], value)
            if not _same_value(found[var], _default(ctx, var)):
                changed[var] = found[var]
    # a key Cassandra rejects (e.g. added by hand since it started): not imported, the node would not restart
    rejected = yaml_rejected(extras, release)
    return dict((k, v) for k, v in extras.items() if str(k) not in rejected), rejected


def _jmx_import(env, live_files, jmx_files):
    """The remote JMX users: the password and access files the JVM reads (jmx_files: cassandra_jmx_files', read
    under the names jmxremote.password and .access; None: cassandra-env.sh's), their paths kept for the role to
    write them there; the users only when the JVM reads both files and each user's rights are in the access file
    -> (jmx_files completed, {variable: value}, notes)."""
    if jmx_files is None:  # not read by the caller: cassandra-env.sh's
        jmx_files = cassandra_jmx_files(live_files.get("cassandra-env.sh", ""),
                                        cassandra_yaml=live_files.get("cassandra.yaml", ""))
    jmx_files = dict({"password": "", "access": "", "from": "", "unknown": "", "jaas": ""}, **jmx_files)
    changed = {}
    for kind in ("password", "access"):
        if jmx_files[kind] and jmx_files[kind] != JMX_FILE_DEFAULTS[kind]:
            changed["cassandra_jmx_remote_%s_file" % kind] = jmx_files[kind]
    jmx_note = []
    if jmx_files["jaas"]:
        jmx_note = ["JMX authentication by JAAS (%s): NOT imported, the role writes the JDK's password and access"
                    " files only: keep cassandra-env.sh as it is (import_cluster_keep_hand_edits=true) and give the"
                    " playbooks a JMX login (cassandra_jmx_username, cassandra_jmx_password_file)" % jmx_files["jaas"]]
    if jmx_files["unknown"]:  # its files not read: no user taken from the role's paths
        jmx_note.append("cassandra-env.sh gives the JMX files through a variable it does not set (%s): their users"
                        " NOT imported, set cassandra_jmx_remote_password_file, _access_file and cassandra_jmx_users"
                        " by hand" % jmx_files["unknown"])
    elif live_files.get("jmxremote.password"):
        users = _jmx_users(live_files["jmxremote.password"], live_files.get("jmxremote.access"))
        if users and "jmxremote.access" in live_files and jmx_files["access"]:
            changed["cassandra_jmx_users"] = users
            jmx_note += _jmx_leftovers(env, users, live_files)
        else:
            jmx_note.append("jmxremote.password (%s): its users NOT imported (no user with rights the role"
                            " writes in the access file the JVM reads%s): set"
                            " cassandra_jmx_users by hand" % (
                                jmx_files["password"] or JMX_FILE_DEFAULTS["password"],
                                (", " + jmx_files["access"]) if jmx_files["access"] else ", none given to the JVM"))
    return jmx_files, changed, jmx_note


def _inventory_vars(changed, found, ctx, live_files, facts, conf_target, series):
    """The variables the inventory gets: changed, rackdc's dc= and rack= when the snitch does not read them, the
    ones a node keeps for life, the RPM conf dir, the seeds as a list, the node's own address or name as the fact
    that gives it -> {variable: value}."""
    out = dict((k, v) for k, v in changed.items() if k not in RACKDC)
    if "cassandra-rackdc.properties" in live_files and not _snitch_reads_rackdc(found.get("cassandra_endpoint_snitch")):
        # the file's dc= and rack= as the node has them: its snitch doesn't read them, its dc and rack
        # (the ring's) may differ (the layout drops them where they don't; a line the file lacks: the
        # role writes the ring's). Else they are the ring's.
        node_kv = _properties(live_files["cassandra-rackdc.properties"].split("\n"))
        out.update((k, found.get(k, ctx[k])) for k, key in zip(RACKDC, ("dc", "rack")) if key in node_kv)
    for key in ALWAYS:
        if key != "cassandra_storage_compatibility_mode" or series == "50x":  # 5.0 setting
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
    return out


def _config_import(live_files, cassandra_version, facts, where, conf_target="", storage_dir="", jmx_files=None,
                   release="", java="", keep_unread_jvm_options=False, cmdline="", running=None):
    """cassandra_config_import's work (where[0]: what it is reading, for its errors) -> its result."""
    running = dict(running or {})
    java, cmdline = java or running.get("java") or "", cmdline or running.get("cmdline") or ""
    env, ctx, files = _load_role(cassandra_version, facts)
    release, rejected = str(release or SERIES[cassandra_version]), []
    found, from_shell = {}, set()
    for name in files:  # (read here, not in a helper: an error in a file names _config_import)
        if name in live_files:
            where[0] = name
            got = _import_file(env, cassandra_version, name, ctx, live_files[name].split("\n"))
            found.update(got)
            if name.endswith(".sh"):
                from_shell.update(got)
    if "logback.xml" in live_files:
        where[0] = "logback.xml"
        found.update(_logback(live_files["logback.xml"]))
    where[0] = "the JVM options files"
    jvm_notes, jvm_warnings, keep_files = _jvm_options(env, cassandra_version, ctx, [f for f in files if f in live_files],
                                                       found, str(java or ""), keep_unread_jvm_options, cmdline)
    where[0] = "cassandra.yaml"
    if "cassandra.yaml" in live_files:
        found.update(_identity(live_files["cassandra.yaml"], cassandra_version))
    if cassandra_version != "40x":
        # a size, rate or duration in one unit whatever the node writes it in (100000000B/s, 2s): 200MiB/s, 2000ms
        found.update((k, canonical_quantity(k[len("cassandra_"):], v)) for k, v in found.items()
                     if k[len("cassandra_"):] in QUANTITY_KEYS)
    changed = _changed_values(found, ctx, from_shell)
    if storage_dir and "cassandra.yaml" in live_files:
        dirs, keep_storage = _storage_dirs(live_files["cassandra.yaml"], storage_dir, ctx, changed)
        found.update(dirs)
        changed.update(dirs)
        if keep_storage:
            changed["cassandra_config_storage_dir"] = keep_storage
    render_dirs = None
    if "cassandra.yaml" in live_files:
        dirs, render_dirs = _data_dirs(live_files["cassandra.yaml"], ctx)
        found.update(dirs)
        changed.update(dirs)
        extras, rejected = _yaml_extras(env, cassandra_version, ctx, live_files["cassandra.yaml"], release, found,
                                        changed)
        if extras:
            changed["cassandra_extra_settings"] = extras
    where[0] = "jmxremote.password"
    jmx_files, jmx_vars, jmx_note = _jmx_import(env, live_files, jmx_files)
    changed.update(jmx_vars)
    where[0] = "comparing the files with the role's"
    render = dict(ctx, **changed)
    if "cassandra.yaml" in live_files and render_dirs:
        render["cassandra_data_file_directories"] = render_dirs
    where[0] = "the JVM options"
    moved = _jvm_moves(env, cassandra_version, files, ctx, render, live_files, dict(running, jaas=jmx_files["jaas"]))
    for key, value in moved["vars"].items():
        render[key] = value
        if _same_value(value, _default(ctx, key)):
            changed.pop(key, None)
        else:
            changed[key] = value
    where[0] = "comparing the files with the role's"
    hand, normalized, comments = _leftovers(env, cassandra_version, files, render, live_files, rejected,
                                            moved["shown"])
    hand += jmx_note
    carried = ["%s: %d line(s) at its end kept with %s" % (name, len(changed[key]), key)
               for key, name in (("cassandra_env_extra_lines", "cassandra-env.sh"),
                                 ("cassandra_logback_extra_lines", "logback.xml")) if changed.get(key)]
    out, trusted, secrets = _move_secrets(_inventory_vars(changed, found, ctx, live_files, facts, conf_target,
                                                          cassandra_version))
    return {"vars": out, "hand_edits": hand, "normalized": normalized, "comments": comments,
            "not_settings": {"keys": rejected, "release": release} if rejected else {}, "release": release,
            "trusted": trusted, "carried": carried + secrets,
            "jvm_notes": jvm_notes, "jvm_warnings": jvm_warnings, "keep_files": keep_files,
            "moved": moved["moved"], "rewritten": moved["rewritten"], "cleanup": moved["cleanup"],
            "not_moved": moved["not_moved"]}


# The lists of lines the import fills from the hand edits a node has (cassandra-env.sh, logback.xml, the JVM
# options files): a secret in one of them goes to secrets.yml in a variable of its own, the line refers to it
SECRET_LISTS = (("cassandra_env_extra_lines", "cassandra-env.sh"), ("cassandra_logback_extra_lines", "logback.xml"),
                ("cassandra_jvm_extra_options", "jvm-server.options"), ("cassandra_jvm8_extra_options", "jvm8-server.options"),
                ("cassandra_jvm11_extra_options", "jvm11-server.options"),
                ("cassandra_jvm17_extra_options", "jvm17-server.options"))
SECRET_VAR = "cassandra_secret_"
SECRET_FILES = {"cassandra-env.sh": "env", "logback.xml": "logback", "jvm-server.options": "jvm",
                "jvm8-server.options": "jvm8", "jvm11-server.options": "jvm11", "jvm17-server.options": "jvm17"}
SECRET_NAME = re.compile(out.SECRET_NAME)
# a secret's name and the variable it went to (removed to tell whether a line holds another secret)
SECRET_MOVED_REF = re.compile(r"\{\{ (%s\w+) \}\}" % SECRET_VAR)
# the note of the lines carried (cassandra_import_nodes)
CARRIED = "hand edits carried into the inventory: "
SECRET_MOVED = re.compile(out.SECRET_NAME + r"[\"']?\{\{ %s\w+ \}\}[\"']?" % SECRET_VAR)


def _quoted_at(line, at):
    """The shell quote open at line[at] (' or "), else ''."""
    quote, i = "", 0
    while i < at:
        c = line[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 1
        elif c in "'\"" and (not quote or c == quote):
            quote = "" if quote else c
        i += 1
    return quote


def _secret_span(line, at, kind):
    """(start, end) of the value of the secret whose name ends at line[at], None when it cannot be told whole (it
    then stays in its line, the line's list goes to secrets.yml as a whole). kind: sh, xml or options."""
    if kind == "options":  # a JVM option: up to the next blank, as bin/cassandra splits them
        end = re.match(r"\S*", line[at:]).end()
        return (at, at + end) if end else None
    quote = _quoted_at(line, at) if kind == "sh" else ""
    if not quote and line[at:at + 1] in ("'", '"'):  # the value quoted: what is inside
        close = line.find(line[at], at + 1)
        inner = line[at + 1:close] if close > 0 else ""
        after = line[close + 1:close + 2] if close > 0 else "x"
        if not inner or (kind == "sh" and (line[at] == '"' and re.search(r"[$`\\]", inner) or after not in ("", " ", "\t", ";"))):
            return None
        return (at + 1, close)
    stop = {"": r"[^\s\"'$`\\;|&<>()]*", '"': r"[^\s\"$`\\]*", "'": r"[^\s']*"}[quote] if kind == "sh" else r"[^\s<>\"']*"
    end = at + re.match(stop, line[at:]).end()
    ends = {"": ("", " ", "\t", ";"), '"': ("", " ", "\t", '"'), "'": ("", " ", "\t", "'")}[quote] if kind == "sh" \
        else ("", " ", "\t", "/", ">")
    if end == at or line[end:end + 1] not in ends:
        return None
    return (at, end)


def _secret_var(name, value, taken, file_name):
    """The variable for the secret `name` (KEYSTORE_PASSWORD, -Djavax.net.ssl.keyStorePassword) of a file's lines:
    the file's short name and its name as a variable's (cassandra_secret_env_keystore_password), a number after
    it when another value of that file already took it (the same on every node that has the same lines)."""
    base = SECRET_VAR + SECRET_FILES[file_name] + "_" + (re.sub(r"[^a-z0-9]+", "_", re.sub(r"^-D|^-+", "", name).lower()).strip("_") or "value")
    var, n = base, 1
    while var in taken and taken[var] != value:
        n += 1
        var = "%s_%d" % (base, n)
    taken[var] = value
    return var


def _take_secrets(values, kind, taken, file_name, own=()):
    """A list of lines -> (the lines, each secret in them replaced by a reference to its variable, {variable:
    (its name in the line, value)}, the lines made so). A line is left as it is (its list then goes to secrets.yml
    whole) when it has a template mark ({{), a secret that cannot be told whole, one whose value the masking of
    the output would take further (a word after it: part of the secret?), or when the line made would be the text
    of one of the node's own lines (own: never two lines alike, one a template, the other not)."""
    lines, made, moved = [], [], {}
    for line in values:
        if not isinstance(line, str) or TEMPLATE_MARK.search(line):
            lines.append(line)
            continue
        spans, pos = [], 0
        for m in SECRET_NAME.finditer(line):
            if m.start() < pos:
                continue
            span = None if re.search(r"\s$", m.group(1)) else _secret_span(line, m.end(), kind)
            shown = out.SECRET_VALUE.match(line, m.start())
            # (shown: the value as the output masks it; only quotes, blanks or a ; may be left after the one taken)
            if span is None or (shown and not re.fullmatch(r"[\s\"';]*", line[span[1]:shown.end(2)])):
                spans = None
                break
            spans.append((re.sub(r"[\"'\s:=]+$", "", m.group(1)), span))
            pos = span[1]
        if not spans:
            lines.append(line)
            continue
        parts, pos, refs, mine = [], 0, {}, dict(taken)
        for name, (start, end) in spans:
            var = _secret_var(name, line[start:end], mine, file_name)
            refs[var] = (name, line[start:end])
            parts += [line[pos:start], "{{ %s }}" % var]
            pos = end
        new = "".join(parts) + line[pos:]
        if new in own or _secret_words(SECRET_MOVED.sub("", new)):
            lines.append(line)
            continue
        taken.update(mine)
        lines.append(new)
        made.append(new)
        moved.update(refs)
    return lines, moved, made


def _secret_words(text):
    """True when text may still hold a secret: a word of a secret's name (not a file's or a path's: the JMX
    password file), -pw or --password, whatever the masking of the output reads in it (a logback
    <property name="dbPassword" value="..."/>)."""
    text = re.sub(r"(?i)[\w.-]*(?:password|passwd|secret)[\w.-]*[._-](?:file|path)\b", "", text)
    return bool(out.SECRET_WORD.search(text) or re.search(r"(?:^|[\s\"'=])(?:-pw|--password)\b", text)
                or out.mask(text) != text)


def _list_secret(key, value, trusted=()):
    """True when a variable goes to secrets.yml: as the output masks it (cassandra_output.secret), the lines of
    the lists of SECRET_LISTS a secret word too; the secrets the import moved out of its own lines (trusted)
    left out."""
    if key in dict(SECRET_LISTS) and isinstance(value, list):
        return any(_secret_words(SECRET_MOVED.sub("", v) if v in trusted else v) for v in value if isinstance(v, str))
    return _secret(key, value)


def _move_secrets(variables):
    """The secrets of the lists of lines a node's variables have (SECRET_LISTS) moved to variables of their own
    (in secrets.yml): (the variables, the lines that refer to them, report lines)."""
    variables, taken, trusted, said = dict(variables), {}, [], []
    own = set(line for key, dummy in SECRET_LISTS for line in variables.get(key) or []
              if isinstance(line, str) and TEMPLATE_MARK.search(line))
    for key, name in SECRET_LISTS:
        if not isinstance(variables.get(key), list):
            continue
        kind = "sh" if name.endswith(".sh") else "xml" if name.endswith(".xml") else "options"
        lines, moved, made = _take_secrets(variables[key], kind, taken, name, own)
        if moved:
            variables[key] = lines
            variables.update((var, value) for var, (dummy, value) in moved.items())
            trusted += made
            said += ["%s: %s=%s moved to secrets.yml as %s, which %s refers to" % (name, setting, out.MASK, var, key)
                     for var, (setting, dummy) in sorted(moved.items())]
        if _list_secret(key, variables[key], made):
            said.append("%s: %s kept whole in secrets.yml (a secret in it could not be taken out alone)" % (name, key))
    return variables, trusted, said


def keep_files_option(value):
    """import_cluster_keep_hand_edits: true/false, or the config files to keep (a list, or names separated by
    commas: -e import_cluster_keep_hand_edits=logback.xml) -> True, or [names]; a name not of a config file fails."""
    if isinstance(value, str) and value.strip().lower() in ("", "yes", "no", "on", "off", "true", "false", "1", "0"):
        return boolean(value.strip(), strict=False) or []
    if isinstance(value, str):
        value = re.sub(r"[\[\]'\"]", "", value).split(",")
    if not isinstance(value, (list, tuple)):
        return boolean(value, strict=False) or []
    names = [str(v).strip() for v in value if str(v).strip()]
    wrong = [n for n in names if not CONFIG_FILE.match(n)]
    if wrong:
        raise AnsibleFilterError("import_cluster_keep_hand_edits: %s: not a config file of cassandra_config (true,"
                                 " false, or among cassandra.yaml, cassandra-env.sh, jvm*-server.options,"
                                 " cassandra-rackdc.properties, logback.xml, jmxremote.password, jmxremote.access)" % ", ".join(wrong))
    return names


def _jvm_moves(env, series, files, ctx, render, live_files, running):
    """cassandra-env.sh's hand edits moved to variables when the JVM gets the same arguments from the role's files
    (cassandra_jvm_args), and the options it does not use -> {vars, moved, rewritten: the JVM's files the role
    rewrites then, cleanup, shown: the JVM's files shown to give the same arguments (left out of the hand edits),
    not_moved: why not, '' if moved or nothing to say}; running: cassandra_config_import's, jaas: what says JMX
    authenticates by JAAS."""
    dep = jvm_args.options_file(series, running.get("java"))
    if dep is None:  # the running Java not known (Cassandra not running): nothing worked out
        return {"vars": {}, "moved": [], "rewritten": [], "cleanup": [], "shown": [], "not_moved": ""}
    jvm_files = [f for f in (jvm_args.ENV, jvm_args.COMMON, dep) if f in files]
    keep = keep_files_option(running.get("keep", False))
    kept = jvm_files if keep is True else [f for f in jvm_files if f in keep]
    if kept:  # import_cluster_keep_hand_edits: those files left as they are (said with the files kept)
        return {"vars": {}, "moved": [], "rewritten": [], "cleanup": [], "shown": [], "not_moved": ""}

    def rendered(variables):
        return dict((name, "\n".join(_render(env, series, name, variables)[1])) for name in jvm_files)
    if running.get("jaas") and jvm_args.ENV in live_files and jvm_args.ENV in jvm_files \
            and jvm_args.env_has_candidates(live_files[jvm_args.ENV], rendered(render)[jvm_args.ENV]):
        # JMX authenticated by JAAS, not imported: cassandra-env.sh to keep as it is (its note), its lines not moved
        return {"vars": {}, "moved": [], "rewritten": [], "cleanup": [], "shown": [],
                "not_moved": "JMX authenticates by JAAS (%s), not imported" % running["jaas"]}
    cmdline = str(running.get("cmdline") or "")
    if set(running.get("newer") or []) & set(jvm_files):
        cmdline = ""  # the files changed since the JVM started: its arguments are not theirs
    adopted = None
    if cmdline and dep in files:
        adopted = jvm_args.cassandra_jvm_args_adopt(
            cmdline, live_files, rendered, render, series, running.get("java"), running.get("environment") or {})
    why = (adopted or {}).get("why", "")
    adopted = adopted if adopted and "vars" in adopted else None
    found = adopted["vars"] if adopted else {}
    role = rendered(dict(render, **found))
    rewritten = [f for f in jvm_files if f in live_files and not same_settings(f, live_files[f], role[f])] \
        if adopted else []
    others = dict((name, "\n".join(_render(env, series, name, ctx)[1])) for name in files
                  if re.match(r"^jvm\d*-server\.options$", name))
    cleanup = adopted["dropped"] if adopted else []
    said = set(": ".join(line.split(": ", 2)[:2]) for line in cleanup)
    cleanup += [line for line in jvm_args.cassandra_jvm_args_cleanup(
        live_files, others, role, series, running.get("java"), cmdline, moved=bool(adopted))
        if ": ".join(line.split(": ", 2)[:2]) not in said]
    return {"vars": found, "moved": adopted["moved"] if adopted else [], "rewritten": rewritten, "cleanup": cleanup,
            "shown": jvm_files if adopted else [], "not_moved": why}


def _logback(text):
    """cassandra_config's logback.xml variables as logback reads the node's file, whatever its quoting, spacing,
    attribute order or comments (a line written another way is not read by the template's): the root logger's
    level and appenders (debug.log, the console), the org.apache.cassandra logger's level. {} when not XML."""
    try:
        conf = ET.fromstring(text)
    except ET.ParseError:
        return {}
    found = {}
    roots = conf.findall("root")
    if len(roots) == 1:
        if roots[0].get("level"):
            found["cassandra_log_level"] = roots[0].get("level")
        refs = [ref.get("ref") for ref in roots[0].findall("appender-ref")]
        found["cassandra_debug_log_enabled"] = "ASYNCDEBUGLOG" in refs
        found["cassandra_log_console"] = "STDOUT" in refs
    loggers = [logger for logger in conf.findall("logger") if logger.get("name") == "org.apache.cassandra"]
    if len(loggers) == 1 and loggers[0].get("level"):
        found["cassandra_log_level_cassandra"] = loggers[0].get("level")
    return found


def _as_text(node):
    """A YAML node with each scalar its text, as SnakeYAML reads a String
    setting (cluster_name 0123 is not 83, seeds 010 not 8); plain null/~/empty: None."""
    if isinstance(node, yaml.MappingNode):
        return dict((_as_text(k), _as_text(v)) for k, v in node.value)
    if isinstance(node, yaml.SequenceNode):
        return [_as_text(v) for v in node.value]
    if node is None or node.style is None and node.value in ("", "~", "null", "Null", "NULL"):
        return None
    return node.value


def _identity(text, series):
    """The settings a node keeps for life, from cassandra.yaml parsed as YAML.
    Fails, naming them, when a mandatory one isn't there."""
    try:
        conf = _as_text(yaml.compose(text))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)  # the line only: its text may hold a password
        raise AnsibleFilterError("cassandra_config_import: cassandra.yaml is not valid YAML%s"
                                 % (" (line %d)" % (mark.line + 1) if mark else ""))
    found, missing = {}, []
    for var, path in list(IDENTITY.items()) + list(IDENTITY_OPTIONAL.items()) + list(BY_PATH.items()):
        value = conf
        for step in path:
            try:
                value = value[step]
            except (KeyError, IndexError, TypeError):
                value = None
                break
        if var in INTEGERS and isinstance(value, str) and value.strip().isdigit():
            value = int(value)  # quoted: Cassandra reads it as a number all the same
        if var == "cassandra_num_tokens" and isinstance(conf, dict) and "num_tokens" not in conf:
            value = 1  # Cassandra's own default (e.g. a single-token node with initial_token)
        if isinstance(value, bool) or not isinstance(value, int if var in INTEGERS else (str, int)) \
                or str(value).strip() == "":
            if var in IDENTITY:
                missing.append(path[-1])
            continue
        found[var] = value
    if missing:
        raise AnsibleFilterError("cassandra_config_import: cannot read %s from cassandra.yaml (not the role default:"
                                 " set them by hand in the inventory)" % ", ".join(missing))
    if series == "50x" and "cassandra_storage_compatibility_mode" not in found and isinstance(conf, dict):
        found["cassandra_storage_compatibility_mode"] = "CASSANDRA_4"  # 5.0's own default, not the role's
    return found


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "x"
    return "c_" + s if s[0].isdigit() else s


def _same(key, a, b):
    """Equal values; for a dict written in its order (KEEP_ORDER), in the same order too."""
    if key in KEEP_ORDER and isinstance(a, dict) and isinstance(b, dict):
        return list(a.items()) == list(b.items())
    return a == b


def _place(key, nodes, levels, group_vars, host_vars):
    """Put key at the highest level where all its nodes agree (levels:
    cluster, DC, rack, then the node itself). True if that is the cluster."""
    def walk(level, members):
        values = [n["vars"].get(key, MISSING) for n in members]
        if all(v is MISSING for v in values):
            return level == 0
        if level < len(levels) and all(_same(key, v, values[0]) for v in values):
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


def values_hidden(func):
    """func, raising its unexpected errors without their message."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AnsibleFilterError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if undefined_name(exc):
                raise
            error = error_without_message(func.__name__, "")
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


# A value that follows each node's name, the same before and after it on every node (Medusa's nodetool_host
# "<node>.example.net"): one template for all, Ansible gives each node its own. The names it may follow, as
# Ansible names them, and how the layout knows them. Not ansible_host: another inventory source may set it (a
# bastion's address), the self-check could not see it. Not the node's identity (its addresses, token, Medusa's
# fqdn) nor where its data is: a renamed host must not move them
NAME_VARS = (("inventory_hostname", lambda n: n["name"]),
             ("inventory_hostname_short", lambda n: n["name"].split(".")[0]))
NOT_FROM_NAMES = tuple(PER_NODE) + ("cassandra_cluster_name", "cassandra_dc", "cassandra_rack")
# nor where its data, config, keys and backups are (a directory, a path, a keystore, Medusa's prefix or bucket)
NOT_FROM_NAMES_PATTERN = re.compile(r"_(dir|dirs|directory|directories|path|paths|file|files|prefix|home|profile_d"
                                    r"|bucket|bucket_name)$|keystore|truststore|cert")


def _literal(text):
    """True when Ansible before 2.19 would make something else of text, rendered (a number, a list, True)."""
    try:
        return not isinstance(ast.literal_eval(text.strip()), str)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return False


def _name_template(key, values):
    """values: [(node, value)] of the nodes that have key -> a template "<before>{{ <name> }}<after>" that gives
    each node exactly its value, None when there is none: fewer than 2 nodes or a single value, a value that is not
    a string (or that Ansible would read as another type), a secret, one node that does not follow the rule, two
    nodes with the same name, a { or } around it."""
    texts = [v for dummy, v in values]
    if key in NOT_FROM_NAMES or NOT_FROM_NAMES_PATTERN.search(key) or len(values) < 2 or not all(isinstance(v, str) for v in texts) \
            or len(set(texts)) < 2 or any(_secret(key, v) or TEMPLATE_MARK.search(v) or _literal(v) for v in texts):
        return None
    for var, name_of in NAME_VARS:
        names = [str(name_of(n)) for n, dummy in values]
        if not all(names) or len(set(names)) < len(names):
            continue
        first, name = texts[0], names[0]
        at = first.find(name)
        while at >= 0:
            before, after = first[:at], first[at + len(name):]
            if not re.search(r"[{}]", before + after) and all(
                    v == before + n + after for v, n in zip(texts, names)):
                return "%s{{ %s }}%s" % (before, var, after)
            at = first.find(name, at + 1)
    return None


def vars_sort_key(key):
    """Keys in the order of the vars files: by block, then as listed there."""
    titles = [title for title, dummy in BLOCKS]
    order = dict(BLOCKS)[_block(key)]
    return (titles.index(_block(key)), order.index(key) if key in order else len(order), key)


def _differences(keys, read, dcg, rackg):
    """One line per key: each value and the nodes (or DC, rack) that have it."""
    lines = []
    for key in sorted(keys, key=vars_sort_key):
        groups = {}  # by value, shown masked
        for n in read:
            value = n["vars"].get(key, MISSING)
            same = "" if value is MISSING else json.dumps(value, sort_keys=key not in KEEP_ORDER, default=str)
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


def _without_ring_rackdc(node):
    """The node's variables without cassandra-rackdc.properties' dc= and rack= where
    they are its dc and rack (the role's default writes them: cassandra_dc, cassandra_rack)."""
    ring = {"cassandra_rackdc_dc": node["dc"], "cassandra_rackdc_rack": node["rack"]}
    return dict((k, node["vars"][k]) for k in node["vars"] if k not in ring or node["vars"][k] != ring[k])


def hand_edits_kept(node):
    """A node's hand edit lines -> (the ones in files cassandra_config_keep_files keeps, the others); a
    "<file>: ..." or "<file>, line N:" line starts the lines of a file."""
    keep = (node.get("keep") or {}).get("cassandra_config_keep_files") or []
    kept, edits, current = [], [], None
    for line in node["hand_edits"]:
        if not line.startswith(" "):
            current = re.split(r"[:,]", line, maxsplit=1)[0]
        (kept if current in keep else edits).append(line)
    return kept, edits


def _cluster_group(cluster, dummy):
    """The cluster's group (whatever the node) -> its name."""
    return cluster


def _dc_group(cluster, n):
    """The group of node n's DC -> its name (<cluster>_<dc>)."""
    return "%s_%s" % (cluster, _slug(n["dc"]))


def _rack_group(cluster, n):
    """The group of node n's rack -> its name (<cluster>_<dc>_<rack>)."""
    return "%s_%s" % (_dc_group(cluster, n), _slug(n["rack"]))


def _hosts_tree(nodes, cluster, dcg, rackg):
    """The hosts file's data: the cluster's group, a group per DC, one per rack under it, the nodes in their rack
    (their ansible_host) -> (hosts, group_vars: {group: {variable: value}} with each DC's and rack's)."""
    hosts = {"all": {"children": {cluster: {"children": {}}}}}
    group_vars = {cluster: {}}
    for n in sorted(nodes, key=lambda n: (n["dc"], n["rack"], n["name"])):
        dcs = hosts["all"]["children"][cluster]["children"]
        racks = dcs.setdefault(dcg(n), {"children": {}})["children"]
        entry = {"ansible_host": n["ansible_host"]} if n.get("ansible_host") else {}
        racks.setdefault(rackg(n), {"hosts": {}})["hosts"][n["name"]] = entry
        group_vars.setdefault(dcg(n), {})["cassandra_dc"] = n["dc"]
        group_vars.setdefault(rackg(n), {})["cassandra_rack"] = n["rack"]
    return hosts, group_vars


def _read_for_layout(nodes):
    """The nodes read, their variables without rackdc's dc= and rack= where they are the ring's, Medusa's fqdn as a
    domain when one rule gives every node its value -> (read, nodes (those read replaced by theirs), the nodes with
    a Medusa fqdn, the domain or None)."""
    read = [n for n in nodes if boolean(n.get("read", False), strict=False)]
    read = [dict(n, vars=_without_ring_rackdc(n)) for n in read]
    nodes = [next((r for r in read if r["name"] == n["name"]), n) for n in nodes]
    # Medusa's fqdn is the node's folder in the backups: a rule only when it
    # gives every node its value exactly, else each node keeps its own
    medusa = [n for n in read if n["vars"].get("cassandra_medusa_fqdn")]  # "": Medusa works it out
    domain = _medusa_fqdn_domain(read) if medusa else None
    if domain is not None:
        read = [dict(n, vars=dict([(k, v) for k, v in n["vars"].items() if k != "cassandra_medusa_fqdn"]
                                  + [("cassandra_medusa_fqdn_domain", domain)])) for n in read]
        nodes = [next((r for r in read if r["name"] == n["name"]), n) for n in nodes]
    return read, nodes, medusa, domain


def _name_templates(keys, read):
    """The keys whose value comes from each node's name by one template: that template put in the nodes' variables
    (in place) -> {key: template}."""
    templates = {}
    for key in keys:
        template = _name_template(key, [(n, n["vars"][key]) for n in read if key in n["vars"]])
        if template is not None:
            templates[key] = template
            for n in read:
                if key in n["vars"]:
                    n["vars"][key] = template
    return templates


def _layout_notes(cluster_name, cluster, nodes, read, differences, host_vars, medusa, domain, templates, shared):
    """The report's head: the cluster, its group, the differences between nodes, the secrets set per node, rackdc's
    dc= and rack= kept, Medusa's fqdn, the values from each node's name, the names shared, the nodes not read ->
    [line]."""
    report = ["Cluster %s: %d node(s), %d read" % (cluster_name, len(nodes), len(read)),
              "Inventory group: %s (the playbooks find it in an inventory of this cluster alone; with other clusters:"
              " -e cassandra_hosts=%s, or CASSANDRA_CLUSTER=%s)" % (cluster, cluster, cluster), ""]
    report += differences + [""]
    per_node = sorted({k for v in host_vars.values() for k in v if k.startswith(SECRET_VAR)})
    if per_node:
        report += ["SECRETS OF HAND EDITS PER NODE: %s are set per node (host_vars/<node>/secrets.yml): a node"
                   " added later needs its own in its host_vars, or cassandra_config cannot render the lines that"
                   " refer to them" % ", ".join(per_node), ""]
    rackdc = [n for n in read if any(k in n["vars"] for k in RACKDC)]
    if rackdc:
        snitches = sorted({str(n["vars"].get("cassandra_endpoint_snitch")) for n in rackdc})
        report += ["cassandra-rackdc.properties: dc= and rack= kept as the nodes have them (cassandra_rackdc_dc,"
                   " cassandra_rackdc_rack) on %s: %s does not read them, the nodes' dc and rack are the ring's"
                   " (cassandra_dc, cassandra_rack). A node added later gets them from its groups, else its"
                   " cassandra_dc and cassandra_rack." % (", ".join(sorted(n["name"] for n in rackdc)),
                                                          " / ".join(snitches)), ""]
    if domain is not None:
        report += ["Medusa fqdn (each node's folder in the backups): <short hostname>.%s on every node,"
                   " kept as cassandra_medusa_fqdn_domain" % domain, ""]
    elif medusa:
        report += ["Medusa fqdn (each node's folder in the backups): no <short hostname>.<domain> rule gives every"
                   " node's value, kept as found (cassandra_medusa_fqdn)", ""]
    for key, template in sorted(templates.items()):
        report += ["Per node, from its name: %s = %s on every node that has it, one value in the inventory"
                   " (Ansible gives each node its own)" % (key, template), ""]
    if shared:
        report.append("SAME NAME for several nodes, named by their address instead: %s" % ", ".join(shared))
        report.append("")
    unread = [n for n in nodes if n not in read]
    if unread:
        report.append("NOT READ (in the inventory, but not imported):")
        report += ["  %s: %s" % (n["name"], n.get("reason", "unreachable")) for n in unread]
        left = sorted(set(k for n in unread for k in n.get("keep") or {}), key=lambda k: (k not in KEEP, k))
        if left:
            report.append("  The roles leave their setup as it is (host_vars: %s false)" % ", ".join(left))
        report.append("")
    return report


def _node_report(n):
    """A node read in the report: its notes, what the roles leave as it is on it, its hand edits (in files kept or
    not), its comment lines, its lines the role writes another way -> [line]."""
    report = ["== %s" % n["name"]]
    report += ["  " + note for note in n.get("notes", [])]
    other_way = dict((k, v) for k, v in (n.get("keep") or {}).items() if k not in SET_UP_BY_THE_ROLES)
    if other_way:
        report.append("  Set up another way, LEFT AS IT IS by the roles on this node (host_vars/%s/main.yml;"
                      " remove a line to let the role take it over, after --check --diff; a host"
                      " rebuilt under this name must lose them):" % n["name"])
        report += ["    %s (%s: false)" % (KEEP[k], k) for k in KEEP if k in other_way]
        report += ["    %s: %s, as this node has it" % (k, _show(k, v))
                   for k, v in sorted(other_way.items()) if k not in KEEP]
    for k in SET_UP_BY_THE_ROLES:
        if k in (n.get("keep") or {}):
            report.append("  Set up by the roles, LEFT AS IT IS on this node (host_vars/%s/main.yml): %s (%s: false;"
                          " remove the line to have it done, after --check --diff)" % (n["name"], KEEP[k], k))
    kept, edits = hand_edits_kept(n)
    if kept:
        report.append("  HAND EDITS no variable covers, in files LEFT AS THEY ARE on this node"
                      " (cassandra_config_keep_files; nodes added later get the role's):")
        report += ["    " + line for line in out.mask_hunks(kept)]
    if edits:
        report.append("  HAND EDITS no variable covers (cassandra_config would revert them):")
        report += ["    " + line for line in out.mask_hunks(edits)]
    if not n["hand_edits"]:
        report.append("  No hand edit left: cassandra_config would not change the config.")
    if n.get("comments"):
        report.append("  Comments only, no setting (e.g. the stock comments of the release the file came from):"
                      " no effect, kept unless the role rewrites the file for a setting:")
        report += ["    " + line for line in n["comments"]]
    if n["normalized"]:
        report.append("  Same setting, written another way by the role (no effect; on a node that is not blank, a"
                      " file whose settings are all the same is left as it is):")
        report += ["    " + _mask(line) for line in n["normalized"]]
    return report + [""]


@values_hidden
def cassandra_inventory_layout(nodes, cluster_name):
    """nodes: [{name, address?, hostname?, dc, rack, ansible_host?, read: bool, reason?,
    vars, hand_edits, normalized, comments?, notes}] -> {'cluster_group', 'cluster_name', 'hosts', 'group_vars', 'host_vars',
    'differences', 'report', 'names' (address -> name in the hosts file)}. Nodes sharing a name are named by their address instead."""
    cluster = _slug(cluster_name)
    names = [n["name"] for n in nodes]
    shared = sorted({name for name in names if names.count(name) > 1})
    nodes = [dict(n, name=n.get("address") or n["name"]) if n["name"] in shared else n for n in nodes]
    dcg, rackg = functools.partial(_dc_group, cluster), functools.partial(_rack_group, cluster)
    levels = [functools.partial(_cluster_group, cluster), dcg, rackg]
    hosts, group_vars = _hosts_tree(nodes, cluster, dcg, rackg)
    host_vars = {}
    read, nodes, medusa, domain = _read_for_layout(nodes)
    keys = sorted({k for n in read for k in n["vars"]} - {"cassandra_dc", "cassandra_rack"})
    templates = _name_templates(keys, read)
    for key in [k for k in keys if k in NODE_LEVEL]:
        for n in read:
            if key in n["vars"]:
                host_vars.setdefault(n["name"], {})[key] = n["vars"][key]
    drift = [k for k in keys if k not in NODE_LEVEL and not _place(k, read, levels, group_vars, host_vars)
             and k not in PER_NODE]
    differences = ["DIFFERENCES BETWEEN NODES (kept per group or node, check they are wanted):"]
    differences += _differences(drift, read, dcg, rackg) or ["  none"]
    report = _layout_notes(cluster_name, cluster, nodes, read, differences, host_vars, medusa, domain, templates,
                           shared)
    for n in nodes:
        if n.get("keep"):
            host_vars.setdefault(n["name"], {}).update(n["keep"])
        if any(k in IMPORTED_FOR for k in n.get("keep") or {}):
            host_vars[n["name"]][IMPORTED] = True
    for n in read:
        report += _node_report(n)
    report += _os_section(read)
    return {"cluster_group": cluster, "cluster_name": cluster_name, "hosts": hosts, "group_vars": group_vars,
            "host_vars": host_vars, "differences": "\n".join(differences), "report": "\n".join(report),
            "names": dict((n["address"], n["name"]) for n in nodes if n.get("address")), "nodes": nodes,
            "templates": templates, "trusted": _trusted_lines(read)}


def _marked(value):
    """The strings in value with a template mark."""
    if isinstance(value, dict):
        return [s for v in value.values() for s in _marked(v)]
    if isinstance(value, list):
        return [s for v in value for s in _marked(v)]
    return [value] if isinstance(value, str) and TEMPLATE_MARK.search(value) else []


def _trusted_lines(read):
    """The lines the import made that refer to a secret's variable, written as templates: not one a node has as
    its own text (a line with {{ read from a node is written !unsafe, never templated)."""
    made = set(line for n in read for line in n.get("trusted") or [])
    own = set(line for n in read for line in _marked(n["vars"]) if line not in (n.get("trusted") or []))
    return sorted(made - own)


ROLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles")


def _roles_defaults():
    """Every role's defaults (the variables a host gets when nothing sets them)."""
    out = {}
    for role in roles_with_defaults():
        out.update(role_file(role) or {})
    return out


def _templated(value):
    return isinstance(value, str) and not isinstance(value, _Unsafe) and re.search(r"\{[{%]", value) is not None


def _resolved(key, values, defaults):
    """key's value as the node has it: its value read (never templated), else the collection's default (a
    template of other defaults rendered in a sandbox, a list or number again as Ansible would give it); MISSING
    when unknown (a default made of facts, a filter of Ansible's)."""
    if key in values:
        return values[key]
    if key not in defaults:
        return MISSING
    value = defaults[key]
    env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
    for dummy in range(5):  # a default made of other ones
        if not _templated(value):
            return value
        try:
            value = env.from_string(value).render(**dict(defaults, **values))
        except Exception:  # pylint: disable=broad-except
            return MISSING
        try:
            value = ast.literal_eval(value)  # "['/x']" -> ['/x'], "7199" -> 7199, as Ansible's templating
        except (ValueError, SyntaxError):
            pass
    return MISSING


# not read from the nodes: a value of the user's for them is never compared with a node's
NOT_READ = ("cassandra_install_url", "cassandra_install_username", "cassandra_java_tarballs",
            "cassandra_java_package", "cassandra_offline")
_TEMPLATE_KEYS = []
# the blocks of variables the import reads from every node it reads, and writes only when not the default
_READ_BLOCKS = ("Cluster & topology", "Directories", "Account & file owners", "Network & ports", "JMX",
                "JVM & heap (cassandra-env.sh, jvm*-server.options)", "Logging (logback.xml)", "systemd unit & service")


# Medusa's install, read with its version; the OS tuning the import reads (not where the role writes it)
_MEDUSA_INSTALL = ("cassandra_medusa_enabled", "cassandra_medusa_version", "cassandra_medusa_venv",
                   "cassandra_medusa_link_dir", "cassandra_medusa_profile_d", "cassandra_medusa_python")
_OS_READ = ("cassandra_linux_sysctl", "cassandra_linux_limits", "cassandra_data_readahead_kb", "cassandra_linux_timesync")
_MEDUSA_INI = []


def _medusa_ini_keys():
    """The variables medusa.ini is made of (cassandra_medusa's template), its owners and mode, its key file."""
    if not _MEDUSA_INI:
        from ansible_collections.community.cassandra.plugins.filter.cassandra_medusa import role_settings
        mapping, dummy = role_settings()
        others = ("config_user", "config_group", "config_mode", "key_file_user", "key_file_group", "key_file_mode",
                  "s3_access_key_id", "s3_secret_access_key", "extra_settings")
        _MEDUSA_INI.append(set(mapping.values()) | set("cassandra_medusa_" + k for k in others))
    return _MEDUSA_INI[0]


def _default_when_absent(key):
    return key not in NOT_READ and any(key in keys for title, keys in BLOCKS if title in _READ_BLOCKS)


def _template_keys():
    """The variables of the cassandra_config templates: the import reads the files they make whole, a variable
    it does not write there is at the collection's default on the node."""
    if not _TEMPLATE_KEYS:
        keys = set()
        for top, dummy, files in os.walk(os.path.join(ROLE, "templates")):
            for name in files:
                with open(os.path.join(top, name)) as f:
                    keys.update(re.findall(r"\b(cassandra_\w+)", f.read()))
        _TEMPLATE_KEYS.append(keys - set(NOT_READ))
    return _TEMPLATE_KEYS[0]


def _vars_dir(path):
    """group_vars/x.yml, group_vars/x/y.yml -> group_vars/x (the group or host they are for)."""
    parts = path.split("/")
    return parts[0] + "/" + (os.path.splitext(parts[1])[0] if len(parts) == 2 else parts[1])


def group_chains(hosts):
    """The hosts file's data -> {host: [its groups, top down]}."""
    out = {}

    def walk(group, chain):
        for host in (group.get("hosts") or {}):
            out[host] = chain
        for child, sub in sorted((group.get("children") or {}).items()):
            walk(sub or {}, chain + [child])
    walk(hosts.get("all") or {}, [])
    return out


def _effective(name, chain, layout, user, mine=True):
    """The variables a host gets: the user's files and the import's (main.yml), as Ansible reads them (all, the
    groups top down, the host; for each, its dir if there is one, its files by name, else the first of
    <name>.yml, .yaml, .json) -> {key: (value, path)}. mine false: the import's dirs count, not its values."""
    out = {}
    for where in ["group_vars/all"] + ["group_vars/%s" % g for g in chain] + ["host_vars/%s" % name]:
        files = [(p, data) for p, data in user if _vars_dir(p) == where]
        written = (layout.get("group_vars") or {}).get(where.split("/", 1)[1]) if where.startswith("group_vars/") \
            else (layout.get("host_vars") or {}).get(name)
        in_dir = [f for f in files if f[0].count("/") == 2]
        if in_dir or written:  # a dir: <name>.yml next to it is not read
            files = in_dir + ([(where + "/main.yml", written)] if written and mine else [])
        else:
            files = [f for ext in ("yml", "yaml", "json") for f in files if f[0] == "%s.%s" % (where, ext)][:1]
        for path, data in sorted(files, key=lambda f: f[0]):
            for key, value in data.items():
                out[key] = (value, path)
    return out


# the variable that wins over another one in the roles (its former name)
_ALIASES = {"cassandra_config_owner": "cassandra_config_user"}


def cassandra_inventory_user_files(read, layout, password=""):
    """read: {path: text} of the vars files of the inventory dir; layout: cassandra_inventory_layout's.
    -> [{path, content}]: the user's own (not the import's) that apply to this cluster's hosts: group_vars/all,
    its groups', its hosts'; a vaulted one decrypted with password (else its content None). Not the user's: a
    file with the import's first line, a main.yml or secrets.yml laid out as an import writes it (an earlier
    release's, without that line: old_import). (The ones at the paths it writes: left out by the playbook, which
    lays out again without them.)"""
    groups = ["all"] + sorted(layout.get("group_vars") or {})
    hosts = [n["name"] for n in layout.get("nodes") or []]
    out = []
    for path, text in sorted((read or {}).items()):
        if not re.match(r"^(group_vars|host_vars)/[^/]+(/[^/]+)?[.](yml|yaml|json)$", path):
            continue
        kind, name = _vars_dir(path).split("/", 1)
        if name not in (groups if kind == "group_vars" else hosts):
            continue
        if str(text).startswith("$ANSIBLE_VAULT"):
            text = _decrypt(text, password)
            if text is None:  # not read: said in the report
                out.append({"path": path, "content": None})
                continue
        # an earlier import's, without its first line (never group_vars/all)
        earlier = name != "all" and _OWNABLE.match(path) and old_import(text)
        if written_for(text) is None and not earlier:  # the import's: written again
            out.append({"path": path, "content": text})
    return out


@values_hidden
def cassandra_inventory_layout_over(nodes, cluster_name, user_files, inventory_dir=""):
    """_layout_over (inventory_dir: where the inventory is, for a {{ inventory_dir }} in the path of your own
    template), without the user's files at the paths it writes (replaced, kept as <file>.<date>~: their
    values are not the user's any more), until they no longer meet; 'user_paths': the user's files counted."""
    files = list(user_files or [])
    for dummy in range(len(files) + 2):  # one file less each round
        layout = _layout_over(nodes, cluster_name, files, inventory_dir)
        written = set(f["path"] for f in cassandra_inventory_files(layout))
        kept = [f for f in files if f["path"] not in written]
        if len(kept) == len(files):
            break
        files = kept
    layout["user_paths"] = [f["path"] for f in files]
    return layout


def _yours_rendered(n, theirs, inventory_dir=""):
    """A node's files your own template renders (cassandra_config_template_overrides in a file of yours): not
    compared with the role's templates here (the self-check renders yours), so neither hand edits nor kept as
    they are (cassandra_config refuses a file both kept and overridden); the end lines carried from such a file
    that your template has itself left out (it would render them twice)."""
    overrides = (theirs.get("cassandra_config_template_overrides") or (None,))[0]
    if not isinstance(overrides, dict) or not overrides:
        return
    kept, edits = hand_edits_kept(dict(n, keep={"cassandra_config_keep_files": list(overrides)}))
    if kept:
        n["hand_edits"] = edits
        n["normalized"] = list(n.get("normalized") or []) + [
            "%s: your own template (cassandra_config_template_overrides), not compared with the role's here" % f
            for f in sorted(overrides) if any(line.startswith(f) for line in kept)]
    for key, f in SECRET_LISTS:
        if f in overrides and isinstance(n["vars"].get(key), list):
            _left_to_yours(n, key, f, _template_text(overrides[f], inventory_dir))
    keep = (n.get("keep") or {}).get("cassandra_config_keep_files")
    if keep:
        rest = [f for f in keep if f not in overrides]
        n["keep"] = dict((k, v) for k, v in n["keep"].items() if k != "cassandra_config_keep_files")
        if rest:
            n["keep"]["cassandra_config_keep_files"] = rest


def _template_text(path, inventory_dir):
    """The text of your template at path ({{ inventory_dir }} in it: inventory_dir), None when it cannot be read."""
    path = re.sub(r"\{\{\s*inventory_dir\s*\}\}", lambda m: inventory_dir, str(path)) if inventory_dir else str(path)
    if TEMPLATE_MARK.search(path) or not os.path.isabs(path) or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _left_to_yours(n, key, name, text):
    """The lines of key (carried from the file name) left out of n's variables when your template's text has them
    all, in their order, one after the other, blank lines aside (a JVM options file: in their order, others
    between them) (the node's lines, their moved secrets put back): it renders them
    itself. Their secrets no other line refers to too; said in its notes. (Its text as written: a line in a Jinja
    comment or a branch it does not render counts; the self-check then says what differs.)"""
    lines = n["vars"][key]
    if text is None or not lines or not all(isinstance(line, str) for line in lines):
        return
    node = [SECRET_MOVED_REF.sub(lambda m: str(n["vars"].get(m.group(1), m.group(0))), line).rstrip() for line in lines]
    node = [line for line in node if line.strip()]
    theirs = [line.rstrip() for line in text.split("\n") if line.strip()]  # (blank lines are not carried)
    if name.endswith(".options"):  # options, from anywhere in the file: in their order, others between them
        found = iter(theirs)
        if not all(line in found for line in node):
            return
    elif not any(theirs[i:i + len(node)] == node for i in range(len(theirs) - len(node) + 1)):
        return
    variables = dict((k, v) for k, v in n["vars"].items() if k != key)
    used = set(m for v in variables.values() if isinstance(v, list) for line in v if isinstance(line, str)
               for m in SECRET_MOVED_REF.findall(line))
    n["vars"] = dict((k, v) for k, v in variables.items() if not k.startswith(SECRET_VAR) or k in used)
    n["notes"] = [note for note in n.get("notes") or [] if not note.startswith(CARRIED + name + ":")] + [
        "%s: its %d line(s) at the end are in your own template (cassandra_config_template_overrides): not carried"
        % (name, len(lines))]


def _layout_over(nodes, cluster_name, user_files, inventory_dir=""):
    """cassandra_inventory_layout, with the user's own variables files of the inventory dir (user_files: [{path,
    content}], group_vars/all and those of this cluster's groups and hosts) taken into account: a node whose value
    one of them would change gets its value written (the node as it is wins), and is listed in 'yours': [{node,
    key, value, yours, path}] (so is one the import writes anyway, a value that differs between nodes); a value
    of theirs equal to the node's is not written again ('from_yours': {node: {key: {value, path}}}). 'not_compared': the keys
    of theirs not compared (a template, a vaulted value, a default not known here); 'unread_files': theirs not read
    (vaulted, no password); 'theirs_win': [{node, key, path}] a file of theirs still wins over the import's."""
    user, unread = [], []
    for f in user_files or []:
        if f.get("content") is None:
            unread.append(f["path"])
            continue
        try:
            data = yaml.load(f["content"], Loader=_UserLoader)  # nosec B506: the loader builds no object
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            user.append((f["path"], data))
    defaults = _roles_defaults()
    nodes = [dict(n, vars=dict(n.get("vars") or {})) for n in nodes]
    live_vars = dict((n["name"], dict(n["vars"])) for n in nodes)  # as found, whatever is written
    yours, not_compared, theirs_win = [], [], []

    def live_of(n, key):
        key = _ALIASES.get(key, key)
        if key in (n.get("keep") or {}):  # left as found on the node (host_vars)
            return n["keep"][key]
        return _resolved(key, live_vars[n["name"]], defaults)

    def read_on(n, key):
        """The node's value of key is known: read from it, left as found, or one the import writes only when
        it is not the default (the config files' and their owners', the unit's; Medusa's and the OS's when
        found there); not a variable the import cannot tell (install sources, policies)."""
        key = _ALIASES.get(key, key)
        live = live_vars[n["name"]]
        return (key in live or key in (n.get("keep") or {}) or key in _template_keys() or _default_when_absent(key)
                or key in _medusa_ini_keys() and boolean((n.get("medusa") or {}).get("ini", False), strict=False)
                or key in _MEDUSA_INSTALL and boolean((n.get("medusa") or {}).get("install", False), strict=False)
                or key in _OS_READ and bool(n.get("os")))

    def comparable(key, value):
        if _templated(value) or isinstance(value, _Vaulted) or (isinstance(value, (dict, list)) and _Vaulted in [
                type(v) for v in (value.values() if isinstance(value, dict) else value)]):
            if key not in not_compared:
                not_compared.append(key)
            return False
        return True

    layout = cassandra_inventory_layout(nodes, cluster_name)
    # a value theirs gives already, as the node has it: left to theirs (where Ansible reads it once written)
    chains = group_chains(layout["hosts"])
    from_yours, differ = {}, []
    for n in nodes:
        name = layout["names"].get(n.get("address"), n["name"])
        theirs = _effective(name, chains.get(name, []), layout, user, mine=False)
        _yours_rendered(n, theirs, inventory_dir)
        for key in [k for k in n["vars"] if k in theirs and k not in ALWAYS and k not in ("cassandra_dc", "cassandra_rack")]:
            if comparable(key, theirs[key][0]) and _same(key, theirs[key][0], n["vars"][key]):
                from_yours.setdefault(name, {})[key] = {"value": n["vars"].pop(key), "path": theirs[key][1]}
        # a value of theirs the node does not have: kept as found (written by the import), listed
        if boolean(n.get("read", False), strict=False):
            for key, (value, path) in sorted(theirs.items()):
                if key in ALWAYS or key in ("cassandra_dc", "cassandra_rack") or not read_on(n, key) \
                        or not comparable(key, value):
                    continue
                live = live_of(n, key)
                if live is not MISSING and not _same(key, value, live):
                    differ.append({"node": name, "key": key, "value": live, "yours": value, "path": path})
    layout = cassandra_inventory_layout(nodes, cluster_name)
    for dummy in range(3):  # each round writes the values theirs would change; the layout again
        chains = group_chains(layout["hosts"])
        changed, pins = False, []
        for n in nodes:
            if not boolean(n.get("read", False), strict=False):
                continue
            name = layout["names"].get(n.get("address"), n["name"])
            own = ["group_vars/%s/main.yml" % g for g in chains.get(name, [])] + ["host_vars/%s/main.yml" % name]
            for key, (value, path) in sorted(_effective(name, chains.get(name, []), layout, user).items()):
                if path in own or not read_on(n, key) or not comparable(key, value):
                    continue
                live = live_of(n, key)
                if live is MISSING:
                    if key not in not_compared:
                        not_compared.append(key)
                    continue
                if _same(key, value, live):
                    continue
                if _same(key, n["vars"].get(key, MISSING), live):  # written, theirs wins (a host file of theirs)
                    pins.append((name, key, live))
                else:
                    n["vars"][key] = live
                    changed = True
                if not any(y["node"] == name and y["key"] == key for y in yours):
                    yours.append({"node": name, "key": key, "value": live, "yours": value, "path": path})
        if not changed:
            for name, key, live in pins:
                layout["host_vars"].setdefault(name, {})[key] = live
            break
        layout = cassandra_inventory_layout(nodes, cluster_name)
    # a file of theirs that still wins (sorted after main.yml in a host's dir): said, not hidden
    chains = group_chains(layout["hosts"])
    for y in yours:
        n = next((n for n in nodes if layout["names"].get(n.get("address"), n["name"]) == y["node"]), None)
        got = _effective(y["node"], chains.get(y["node"], []), layout, user).get(y["key"])
        if n is not None and got and not _same(y["key"], got[0], y["value"]):
            theirs_win.append({"node": y["node"], "key": y["key"], "path": got[1]})
    for d in differ:  # the ones the import writes anyway (a value that differs between nodes) too
        if not any(y["node"] == d["node"] and y["key"] == d["key"] for y in yours):
            yours.append(d)
    layout["yours"] = sorted(yours, key=lambda y: (y["key"], y["node"]))
    layout["from_yours"] = from_yours
    layout["not_compared"] = sorted(not_compared)
    layout["unread_files"] = unread
    layout["theirs_win"] = theirs_win
    return layout


def _by_nodes(pairs):
    """pairs: [(node name, lines)] -> report lines, each line once under the
    nodes that have it: the ones every node has first."""
    order, where = [], {}
    for name, lines in pairs:
        for line in lines:
            if line not in where:
                where[line] = []
                order.append(line)
            if name not in where[line]:
                where[line].append(name)
    groups = []
    for line in order:
        names = tuple(sorted(where[line]))
        group = next((g for g in groups if g[0] == names), None)
        if group is None:
            groups.append((names, [line]))
        else:
            group[1].append(line)
    out = []
    for names, lines in sorted(groups, key=lambda g: len(g[0]) != len(pairs)):
        out.append("  On every node read:" if len(names) == len(pairs) and len(pairs) > 1 else "  On %s:" % ", ".join(names))
        out += ["    " + line for line in lines]
    return out


def _os_section(read):
    """The OS tuning found on the nodes (import_cluster's cassandra_os_import)."""
    nodes = [n for n in read if n.get("os")]
    if not nodes:
        return []
    out = ["OS TUNING FOUND ON THE NODES (read only, compared with what cassandra_linux and cassandra_service set):"]
    out += _by_nodes([(n["name"], n["os"].get("lines") or []) for n in nodes])
    carried = _by_nodes([(n["name"], n["os"].get("carried") or []) for n in nodes])
    if carried:
        out += ["", "OS TUNING CARRIED INTO THE VARIABLES, for the nodes added later (the nodes above with"
                " cassandra_linux_manage false are left as they are):"] + carried
    return out + [""]


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
        "cassandra_version", "cassandra_package_version", "cassandra_install_method", "cassandra_packages",
        "cassandra_install_tools", "cassandra_install_jemalloc", "cassandra_package_hold",
        "cassandra_java_version",
        "cassandra_java_home", "cassandra_java_package", "cassandra_java_tarball", "cassandra_java_tarball_checksum",
        "cassandra_java_tarball_dir", "cassandra_install_java", "cassandra_java_set_default"]),
    ("Directories", [
        "cassandra_conf_dir", "cassandra_rpm_conf_alternative", "cassandra_data_dir", "cassandra_data_file_directories", "cassandra_commitlog_dir",
        "cassandra_hints_dir", "cassandra_saved_caches_dir", "cassandra_cdc_raw_dir", "cassandra_log_dir",
        "cassandra_heap_dump_dir"]),
    ("Account & file owners", [
        "cassandra_user", "cassandra_group", "cassandra_config_user", "cassandra_config_group", "cassandra_config_mode",
        "cassandra_config_public_mode", "cassandra_config_file_permissions"]),
    ("Network & ports", [
        "cassandra_listen_address", "cassandra_broadcast_address", "cassandra_rpc_address",
        "cassandra_broadcast_rpc_address", "cassandra_storage_port", "cassandra_ssl_storage_port",
        "cassandra_start_native_transport", "cassandra_native_transport_port",
        "cassandra_native_transport_allow_older_protocols", "cassandra_rpc_keepalive", "cassandra_internode_compression",
        "cassandra_inter_dc_tcp_nodelay"]),
    ("JMX", [
        "cassandra_jmx_port", "cassandra_local_jmx", "cassandra_jmx_rmi_hostname", "cassandra_jmx_username",
        "cassandra_jmx_password_file", "cassandra_jmx_password", "cassandra_jmx_users"]),
    ("CQL login (the operations that read the replication)", ["cassandra_cql_username", "cassandra_cql_password"]),
    ("JVM & heap (cassandra-env.sh, jvm*-server.options)", [
        "cassandra_heap_size", "cassandra_heap_newsize", "cassandra_max_direct_memory_size", "cassandra_jvm_gc",
        "cassandra_jvm_max_gc_pause_millis", "cassandra_jvm_g1_heap_region_size", "cassandra_jvm_g1_new_size_percent",
        "cassandra_jvm_initiating_heap_occupancy_percent", "cassandra_jvm_cms_initiating_occupancy_fraction",
        "cassandra_jvm_max_tenuring_threshold", "cassandra_jvm_parallel_gc_threads", "cassandra_jvm_conc_gc_threads",
        "cassandra_jvm_extra_options", "cassandra_jvm8_extra_options", "cassandra_jvm11_extra_options",
        "cassandra_jvm17_extra_options", "cassandra_env_extra_lines"]),
    ("Other cassandra.yaml settings", []),
    ("cassandra.yaml settings no variable covers", ["cassandra_extra_settings"]),
    ("Logging (logback.xml)", [
        "cassandra_log_level", "cassandra_log_level_cassandra", "cassandra_debug_log_enabled", "cassandra_log_console",
        "cassandra_logback_extra_lines"]),
    ("Secrets of the lines above (hand edits the import carried)", []),
    ("OS tuning (found on the nodes, for the nodes added later)", [
        "cassandra_linux_sysctl_file", "cassandra_linux_sysctl", "cassandra_linux_limits", "cassandra_data_readahead_kb",
        "cassandra_linux_timesync"]),
    ("systemd unit & service", [
        "cassandra_service_restart", "cassandra_service_environment", "cassandra_service_limit_nofile",
        "cassandra_service_limit_nproc", "cassandra_service_limit_memlock", "cassandra_service_limit_as"]),
    ("Medusa", [
        "cassandra_medusa_version", "cassandra_medusa_venv", "cassandra_medusa_link_dir", "cassandra_medusa_profile_d",
        "cassandra_medusa_python", "cassandra_medusa_storage_provider", "cassandra_medusa_bucket_name",
        "cassandra_medusa_region", "cassandra_medusa_host", "cassandra_medusa_port", "cassandra_medusa_base_path",
        "cassandra_medusa_prefix", "cassandra_medusa_key_file", "cassandra_medusa_fqdn"]),
    ("Left as it is on this node (set up another way)", [k for k in KEEP if k not in SET_UP_BY_THE_ROLES] + [IMPORTED]),
    ("Left as it is on this node (set up by the roles)", list(SET_UP_BY_THE_ROLES)),
    ("Other", []),
]
BLOCK_PATTERNS = [
    (re.compile(r"^%s" % SECRET_VAR), "Secrets of the lines above (hand edits the import carried)"),
    (re.compile(r"^cassandra_medusa_"), "Medusa"),
    (re.compile(r"^cassandra_service_"), "systemd unit & service"),
    (re.compile(r"^cassandra_jmx_"), "JMX"),
    (re.compile(r"^cassandra_java_"), "Versions & packages"),
    (re.compile(r"^cassandra_(?:jvm\d*|heap)_"), "JVM & heap (cassandra-env.sh, jvm*-server.options)"),
    (re.compile(r"^cassandra_\w+_(?:dir|directory|directories)$"), "Directories"),
    (re.compile(r"^cassandra_\w+_(?:address|port)$"), "Network & ports"),
]
# Dicts a role writes in their order (a file written whole): not sorted
KEEP_ORDER = ("cassandra_linux_limits",)
TEMPLATE_BLOCKS = [("cassandra.yaml", "Other cassandra.yaml settings"), ("logback.xml", "Logging (logback.xml)")]


@functools.lru_cache(maxsize=None)
def template_vars(name):
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
        if key in template_vars(name):
            return title
    return "Other"


class Unsafe(str):
    """A value read from a node that looks like a template (a password with
    {{, {% or {#): written !unsafe, so Ansible uses it as it is."""


class Dumper(yaml.SafeDumper):
    pass


Dumper.add_representer(Unsafe, lambda dumper, value: dumper.represent_scalar("!unsafe", str(value), style="'"))
TEMPLATE_MARK = re.compile(r"\{\{|\{%|\{#")


def _plain(value):
    """value with plain types only: Ansible hands filters str subclasses
    (AnsibleUnsafeText, whose str() is itself), which yaml.safe_dump rejects."""
    if isinstance(value, dict):
        return dict((_plain(k), _plain(v)) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, bytes):
        return bytes.decode(value, "utf-8", "surrogateescape")
    if isinstance(value, str):
        return "".join([value])  # a copy as a str (str() of AnsibleUnsafeText is itself)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


def _unsafe(value, trusted=()):
    """trusted: the lines the import made that refer to a secret's variable (written as templates)."""
    if isinstance(value, dict):
        return dict((k, _unsafe(v, trusted)) for k, v in value.items())
    if isinstance(value, list):
        return [_unsafe(v, trusted) for v in value]
    if isinstance(value, str) and TEMPLATE_MARK.search(value) and value not in (IPV4, HOSTNAME) and value not in trusted:
        return Unsafe(value)
    return value


def _vars_yaml(variables, templates=None, trusted=()):
    """variables as YAML, grouped by subject (BLOCKS): a comment line per block,
    a blank line between blocks. Each key is dumped on its own, the same way
    a whole dict would be (same values and quoting). templates: {key: the
    import's template of the nodes' names} written as a template; trusted: see _unsafe."""
    data = _unsafe(json.loads(json.dumps(variables, default=str)), frozenset(trusted))
    for key, template in (templates or {}).items():
        if variables.get(key) == template:
            data[key] = _plain(template)
    grouped = {}
    for key in data:
        grouped.setdefault(_block(key), []).append(key)
    out = []
    for title, order in BLOCKS:
        keys = grouped.get(title)
        if not keys:
            continue
        keys.sort(key=lambda k: (order.index(k), "") if k in order else (len(order), k))
        out.append("# %s\n" % title + "".join(yaml.dump({k: data[k]}, Dumper=Dumper, default_flow_style=False,
                                                        sort_keys=k not in KEEP_ORDER) + _note(k, data[k]) for k in keys))
    return "\n".join(out)


# What the nodes can't tell, written under the variable that needs it
NOTES = {
    ("cassandra_install_method", "packages"): (
        "# TODO: cassandra_install_url: the directory of the package files, for new nodes (not read from the nodes)\n"),
}


def _note(key, value):
    return NOTES.get((key, value), "") if isinstance(value, str) else ""


def _split_secrets(variables, trusted=()):
    public, secrets = {}, {}
    for key, value in variables.items():
        (secrets if _list_secret(key, value, trusted) else public)[key] = value
    return public, secrets


@values_hidden
def cassandra_inventory_files(layout):
    """[{path, content, secret}] for group_vars/<group>/ and
    host_vars/<host>/: main.yml, and secrets.yml for variables named like
    passwords (and extra settings holding one)."""
    layout = _plain(layout)
    files = []
    for kind in ("group_vars", "host_vars"):
        for name, variables in sorted(layout[kind].items()):
            public, secrets = _split_secrets(variables, frozenset(layout.get("trusted") or ()))
            for base, data, secret in (("main.yml", public, False), ("secrets.yml", secrets, True)):
                if data:
                    files.append({"path": "%s/%s/%s" % (kind, name, base), "secret": secret,
                                  "content": _vars_yaml(data, layout.get("templates"), layout.get("trusted") or ())})
    return files


# First line of every file the import writes: a re-import replaces or removes
# only the files that start with it (and the vaulted secrets.yml next to one),
# and only those of its own cluster, which the line names
GENERATED = "# Written by community.cassandra.import_cluster for %s: the next import replaces it. Your own settings: other files."
# the line of the releases before it named the cluster
GENERATED_UNNAMED = "# Written by community.cassandra.import_cluster: the next import replaces it. Your own settings: other files."
_HEADER = re.compile(r"# Written by community[.]cassandra[.]import_cluster(?: for ([^\s:]+))?: ")
_OWNABLE = re.compile(r"^(?:group_vars|host_vars)/[^/]+/(?:main|secrets)\.yml$")


def cassandra_inventory_generated(content, cluster):
    """content, marked as written by the import of the cluster (its group)."""
    return GENERATED % cluster + "\n" + content


def written_for(text):
    """The cluster group named by the import's first line of text, '' when the
    line names none (an earlier release), None when text is not the import's."""
    match = _HEADER.match(text or "")
    return (match.group(1) or "") if match else None


def leading_comments(text):
    """The lines of text before its first line of data (comments, blank lines, ---): what tells an earlier
    import's file without its first line, and shows no value."""
    out = []
    for line in str(text or "").split("\n"):
        if line.strip() and line.strip() != "---" and not line.lstrip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out)


def old_import(text):
    """True when text, without the import's first line, starts as an import wrote its main.yml or secrets.yml:
    (---, blank lines,) then the comment of one of its blocks, a title of its own as it is (# Cluster & topology,
    # Directories; any case), and nothing else first."""
    titles = set(title.lower() for title, dummy in BLOCKS)
    for line in str(text or "").split("\n"):
        line = line.strip()
        if not line or line == "---":
            continue
        return line.startswith("#") and line[1:].strip().lower() in titles
    return False


def _decrypt(text, password):
    """text decrypted with password, or None (no password, another one, damaged)."""
    if not password:
        return None
    try:
        from ansible.parsing.vault import VaultLib, VaultSecret
        return to_text(VaultLib([("default", VaultSecret(to_bytes(password)))]).decrypt(to_bytes(text)))
    except Exception:
        return None


class _TagsLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors
    """A YAML inventory's tags read as nothing (!vault), or as text (!unsafe): only names count here."""


class _Unsafe(str):
    """A value written !unsafe: its text, not a template."""


_TagsLoader.add_multi_constructor("!", lambda loader, suffix, node: None)
_TagsLoader.add_constructor("!unsafe", lambda loader, node: _Unsafe(loader.construct_scalar(node)))


class _Vaulted(object):
    """An inline !vault value of the user's: not read."""


class _UserLoader(_TagsLoader):  # pylint: disable=too-many-ancestors
    pass


_UserLoader.add_constructor("!vault", lambda loader, node: _Vaulted())
# the files Ansible reads as inventory in a dir (not its own ignored extensions), other than YAML: INI
_INVENTORY_IGNORED = re.compile(r"(\.(pyc|pyo|swp|bak|rpm|md|txt|rst|orig|cfg|retry)|~)$")
_INI_GROUP = re.compile(r"^\[([^:\]\s]+)(?::(\w+))?\]\s*(?:#.*)?$")  # a section, as Ansible's ini plugin reads it


def _inventory_names(data):
    """A YAML inventory (parsed) -> (its groups but all, its hosts)."""
    groups, hosts, seen = set(), set(), set()

    def walk(level):
        if not isinstance(level, dict) or id(level) in seen:  # an anchor that loops back
            return
        seen.add(id(level))
        for name, group in level.items():
            groups.add(name)
            if isinstance(group, dict):
                hosts.update(group["hosts"] if isinstance(group.get("hosts"), dict) else {})
                walk(group.get("children"))
    walk(data)
    groups.difference_update(["all", "ungrouped"])
    return groups, hosts


def _yaml_names(text):
    """A YAML inventory's text -> (its groups but all, its hosts); none when it does not parse."""
    try:
        return _inventory_names(yaml.load(text, Loader=_TagsLoader))  # nosec B506: the loader builds no object
    except yaml.YAMLError:
        return set(), set()


def _defined_groups(level, seen=None):
    """The groups a YAML inventory gives hosts, children or vars (not those it only names as a child)."""
    seen = set() if seen is None else seen
    out = set()
    if not isinstance(level, dict) or id(level) in seen:  # an anchor that loops back
        return out
    seen.add(id(level))
    for name, group in level.items():
        if isinstance(group, dict):
            if any(group.get(k) for k in ("hosts", "children", "vars")):
                out.add(name)
            out |= _defined_groups(group.get("children"), seen)
    return out


def _ini_groups(text):
    """The sections of an INI inventory with something in them, as Ansible's ini plugin reads them."""
    out, section = set(), None
    for line in text.splitlines():
        line = line.strip()
        match = _INI_GROUP.match(line)
        if match:
            section = match.group(1)
        elif section and line and not line.startswith((";", "#")):
            out.add(section)
    return out


def _own_names(path, text):
    """An inventory file of the user's -> the groups it gives hosts, children or vars: as Ansible reads it, JSON
    or YAML (a .yml, .yaml, .json or no extension), else INI."""
    data = None
    if os.path.splitext(path)[1] in ("", ".yml", ".yaml", ".json"):
        try:
            data = json.loads(text)
        except ValueError:
            try:
                data = yaml.load(text, Loader=_TagsLoader)  # nosec B506: the loader builds no object
            except yaml.YAMLError:
                data = None
    groups = _defined_groups(data) if isinstance(data, dict) else _ini_groups(text)
    return groups - set(["all", "ungrouped"]), set()


def _cluster_tree(text, cluster):
    """True when a YAML inventory holds the group cluster alone (and what is under it): this cluster's hosts file,
    without the import's first line (from before that line, or edited by hand)."""
    try:
        data = yaml.load(text, Loader=_TagsLoader)  # nosec B506: the loader builds no object
    except yaml.YAMLError:
        return False
    if isinstance(data, dict) and list(data) == ["all"] and isinstance(data["all"], dict) and list(data["all"]) == ["children"]:
        data = data["all"]["children"]
    return isinstance(data, dict) and list(data) == [cluster] and isinstance(data[cluster], dict)


def _clusters_groups(text, cluster):
    """True when the groups of a YAML inventory are all this cluster's (its group, <cluster group>_...)."""
    groups, dummy = _yaml_names(text)
    return bool(groups) and all(g == cluster or g.startswith(cluster + "_") for g in groups)


def cassandra_inventory_leftovers(paths, read, written, cluster, password="", inventory=None, cluster_name=None,
                                  adopt=False):
    """paths: the files in the inventory dir, which holds every cluster, each
    with its <cluster group>.yml (relative paths); read: {path: its first line
    (a vaulted file, and an inventory file: all of it)} for those read;
    written: the paths this import writes; cluster: its cluster group;
    password: the vault password, if any; inventory: the hosts this import
    writes; cluster_name: its cassandra_cluster_name. Returns {'stale': the
    files an earlier import of this cluster wrote that this one does not (to
    remove), 'kept': the files no import wrote (left as they are), 'replaced':
    the files at a path it writes that no import wrote, 'unsure': the files an
    earlier import wrote that may be another cluster's (left as they are),
    'conflicts': why this import would write over another cluster's files, or
    share its hosts or groups}. A file whose first line names no cluster (an
    earlier release) is this cluster's when this cluster's hosts file alone
    names its group or host (for a file without the first line laid out as
    the import writes it: the one there, else the one it writes). A <cluster group>.yml without the import's
    first line that holds this cluster's group alone is its hosts file (an
    earlier release's, or edited by hand). Two clusters whose names make the
    same group (the cassandra_cluster_name of group_vars/<cluster group>/main.yml,
    with the import's first line or none), the user's own inventory files with
    a group of this cluster's, a hosts file of an import into a dir of its own
    (an earlier layout: hosts.yml, at the top or in a subdir) and a cluster
    group all, ungrouped or cassandra are conflicts too. A file without the
    import's first line laid out as the import writes it (old_import: read
    gives its leading comments) is an earlier release's too, replaced with a
    backup. adopt (import_cluster_adopt): the files of an earlier import not
    known to be this cluster's, and a <cluster group>.yml of no import, at
    the paths it writes or in its groups' and hosts' dirs, are this
    cluster's (the replaced ones kept as <file>.<date>~); 'adoptable': the
    conflicts it would solve; 'backup': the stale files to keep as
    <file>.<date>~ when removed (known by their layout only, or adopted)."""
    found = dict((p, read.get(p)) for p in paths)
    old_layout = set()

    def header(path):
        text = found.get(path)
        if text is not None and text.startswith("$ANSIBLE_VAULT"):  # the import's only if it decrypts to its line
            text = _decrypt(text, password)
        name = written_for(text)
        if name is None and text is not None and _OWNABLE.match(path) and _vars_dir(path) != "group_vars/all" \
                and old_import(text):
            old_layout.add(path)
            return ""  # an earlier release's, without its first line
        return name

    headers = dict((p, header(p)) for p in found)
    # the clusters' hosts files (the import's), by cluster, and the user's own inventory files
    clusters, own, conflicts = {}, {}, []
    if cluster in ("all", "ungrouped"):
        conflicts.append("cluster group %s (from the cluster name): a group Ansible takes on its own,"
                         " not one cluster's of an inventory dir" % cluster)
    for path, name in sorted(headers.items()):
        if any(p in ("group_vars", "host_vars", "vars_plugins") or p.startswith(".") or _INVENTORY_IGNORED.search(p)
               for p in path.split("/")):
            continue  # not read by Ansible as inventory
        if name is None or path in old_layout and "/" not in path:
            if path == cluster + ".yml" and found[path] is not None and (
                    _cluster_tree(found[path], cluster) or adopt and _clusters_groups(found[path], cluster)):
                clusters[path] = (cluster, _yaml_names(found[path]))  # replaced, the old one kept as <file>.<date>~
            elif found[path] is not None:
                own[path] = _own_names(path, found[path])
        elif "/" in path or (path == "hosts.yml" and name != "hosts"):  # an import into a dir of its own
            conflicts.append("%s: an import's hosts file of a dir of its own (an earlier layout), read by Ansible too:"
                             " move it out of the inventory dir" % path)
        elif path.endswith(".yml"):
            clusters[path] = (name or path[:-len(".yml")], _yaml_names(found[path]))
    # the same group for another cluster (names that differ only by case or punctuation)
    previous = "group_vars/%s/main.yml" % cluster
    if cluster_name is not None and found.get(previous) is not None and headers[previous] in (cluster, "", None):
        try:
            data = yaml.load(found[previous], Loader=_TagsLoader)  # nosec B506: the loader builds no object
        except yaml.YAMLError:
            data = None
        theirs = data.get("cassandra_cluster_name") if isinstance(data, dict) else None
        templated = not isinstance(theirs, _Unsafe) and re.search(r"\{[{%]", str(theirs))  # the user's: not told
        if theirs is not None and str(theirs) != str(cluster_name) and not templated:
            conflicts.append("%s: cluster '%s', not '%s' (two clusters, one group %s)" % (previous, theirs, cluster_name, cluster))

    new_names = _inventory_names(inventory) if inventory else (set(), set())

    def named_by(path, new=False):
        """The clusters whose hosts file names the group or host of a group_vars or host_vars path (new: the
        hosts this import writes count as this cluster's hosts file)."""
        parts = path.split("/")
        if len(parts) < 2 or parts[0] not in ("group_vars", "host_vars"):
            return set()
        item = os.path.splitext(parts[1])[0] if len(parts) == 2 else parts[1]  # group_vars/<group>.yml too
        pick = 0 if parts[0] == "group_vars" else 1
        return set(c for c, names in ([(cluster, new_names)] if new else []) + list(clusters.values())
                   if item in names[pick])

    def owner(path):
        name = headers[path]
        if name or name is None:
            return name
        if path in clusters:  # a hosts file
            return clusters[path][0]
        if "/" not in path:
            return cluster if path in written else ""
        owners = named_by(path)
        if not owners and path in old_layout:  # from before the first line (one cluster a dir): the hosts written
            owners = named_by(path, new=True)
        if not owners and adopt and (path in written or named_by(path, new=True)):
            return cluster
        return owners.pop() if len(owners) == 1 else ""

    stale, kept, unsure, adoptable = [], [], [], []
    for path in sorted(found):
        who = owner(path)
        if path in written:
            if who not in (None, cluster):
                conflicts.append("%s: written for %s" % (path, who) if who
                                 else "%s: written by an earlier import, not known to be %s's" % (path, cluster))
                if not who and not named_by(path):  # (named by several clusters' hosts files: not solved)
                    adoptable.append(conflicts[-1])
        elif who is None:
            if not named_by(path) - set([cluster]):  # not in another cluster's group_vars or host_vars
                kept.append(path)
        elif who == cluster:
            (stale if _OWNABLE.match(path) else kept).append(path)
        elif who == "":
            unsure.append(path)
    if inventory:
        groups, hosts = _inventory_names(inventory)
        for path, (who, (their_groups, their_hosts)) in sorted(clusters.items()):
            if who != cluster:
                conflicts += ["%s: in %s too" % (name, path) for name in sorted((groups & their_groups) | (hosts & their_hosts))]
        for path, (their_groups, dummy) in sorted(own.items()):  # a host may be in groups of the user's too
            conflicts += ["group %s: in %s too" % (name, path) for name in sorted(groups & their_groups)]
            if path == cluster + ".yml" and _clusters_groups(found[path] or "", cluster):  # its hosts file
                adoptable += conflicts[len(conflicts) - len(groups & their_groups):]
    replaced = sorted(p for p in found if p in written and (headers[p] is None or p in old_layout
                                                            or adopt and headers[p] == ""))
    return {"stale": stale, "kept": kept, "replaced": replaced, "unsure": unsure, "conflicts": conflicts,
            "adoptable": adoptable, "backup": sorted(p for p in stale if p in old_layout or adopt and headers[p] == "")}


def cassandra_inventory_same_secret(existing, content, password):
    """True when existing, a vaulted file, decrypts with password to content:
    written again, it would only change its encryption."""
    if not existing or not str(existing).startswith("$ANSIBLE_VAULT"):
        return False
    return _decrypt(existing, password) == content


@values_hidden
def cassandra_inventory_own_values(read, cluster_group, keys, password=""):
    """read: {path: text} of the vars files of the inventory dir -> {key: value} of keys as an earlier import of
    this cluster wrote them in its own files of the cluster group (group_vars/<cluster group>/main.yml and
    secrets.yml, with the import's first line naming the group; a vaulted one decrypted with password). What a
    re-import keeps when nothing else gives it (the CQL login: no node gives it)."""
    out = {}
    for name in ("main.yml", "secrets.yml"):
        text = (read or {}).get("group_vars/%s/%s" % (cluster_group, name))
        if text and str(text).startswith("$ANSIBLE_VAULT"):
            text = _decrypt(text, password)
        if not text or written_for(text) != cluster_group:
            continue
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            out.update((k, data[k]) for k in keys if k in data and isinstance(data[k], (str, int)))
    return out


# a host's own vars file, as Ansible reads them: host_vars/<host>.yml, or any file of host_vars/<host>/
_HOST_VARS_FILE = re.compile(r"^host_vars/([^/]+?)(?:[.](?:yml|yaml|json)|/[^/]+[.](?:yml|yaml|json))$")


def _host_entries(data):
    """A YAML inventory (parsed) -> {host: the variables it gives the host itself ({} for none)}."""
    out, seen = {}, set()

    def walk(level):
        if not isinstance(level, dict) or id(level) in seen:  # an anchor that loops back
            return
        seen.add(id(level))
        for group in level.values():
            if isinstance(group, dict):
                for host, variables in (group["hosts"] if isinstance(group.get("hosts"), dict) else {}).items():
                    out.setdefault(host, {}).update(variables if isinstance(variables, dict) else {})
                walk(group.get("children"))
    walk(data)
    return out


@values_hidden
def cassandra_import_kept_ansible_host(nodes, read, hosts_file, cluster_group, names, password=""):
    """nodes: cassandra_import_nodes' -> the same, without ansible_host for the hosts the inventory already there
    reaches without one: the hosts file of this cluster an earlier import wrote (<cluster group>.yml, its first
    line naming the group, or holding that group alone) and the host_vars files there (a vaulted one decrypted
    with password; one that can't be read may set it). What was chosen then (import_cluster_set_ansible_host:
    false, or ansible_host taken out by hand) stays; a node new to it gets the import's own choice (its name
    may not be known to the controller).
    read: {path: text} of the inventory dir; hosts_file: the cluster's hosts file there (<cluster group>.yml);
    names: {address: the node's name there} (the layout's). For a
    re-import that is not given import_cluster_set_ansible_host."""
    text = (read or {}).get(hosts_file)
    if not text or (written_for(text) != cluster_group and not _cluster_tree(text, cluster_group)):
        return nodes
    try:
        hosts = _host_entries(yaml.load(text, Loader=_TagsLoader))  # nosec B506: the loader builds no object
    except yaml.YAMLError:
        return nodes
    if not hosts:
        return nodes
    for path, content in sorted((read or {}).items()):
        match = _HOST_VARS_FILE.match(path)
        if not match or match.group(1) not in hosts:
            continue
        if str(content or "").startswith("$ANSIBLE_VAULT"):
            content = _decrypt(content, password)
            if content is None:
                hosts[match.group(1)]["ansible_host"] = None  # not read: it may set it
                continue
        try:
            variables = yaml.load(content or "", Loader=_TagsLoader)  # nosec B506: the loader builds no object
        except yaml.YAMLError:
            continue
        if isinstance(variables, dict) and "ansible_host" in variables:
            hosts[match.group(1)]["ansible_host"] = variables["ansible_host"]
    without = set(h for h, v in hosts.items() if "ansible_host" not in v)
    out = []
    for n in nodes:
        name = (names or {}).get(n.get("address"), n.get("name"))
        if n.get("ansible_host") and name in without:
            # (its note on the address it was reached at, which ansible_host would replace: not said either)
            n = dict(n, ansible_host="", notes=[x for x in n.get("notes") or [] if not str(x).startswith("reached at ")])
        out.append(n)
    return out


def cassandra_config_ignored_vars(names, cassandra_version):
    if cassandra_version not in SERIES:
        raise AnsibleFilterError("cassandra_config_ignored_vars: unsupported series %s" % cassandra_version)
    role_vars = set(role_file("cassandra_config"))
    files = role_file("cassandra_config", "vars")["_cassandra_config_files"][SERIES[cassandra_version]]
    used = set()
    for name in files:
        with open(os.path.join(ROLE, "templates", SERIES[cassandra_version], name + ".j2")) as f:
            used.update(re.findall(r"\b(cassandra_\w+)", f.read()))
    # variables other variables' defaults are built from count as used
    for value in role_file("cassandra_config").values():
        used.update(re.findall(r"\b(cassandra_\w+)", str(value)))
    tasks_dir = os.path.join(ROLE, "tasks")
    for name in os.listdir(tasks_dir):
        with open(os.path.join(tasks_dir, name)) as f:
            used.update(re.findall(r"\b(cassandra_\w+)", f.read()))
    return sorted(n for n in names if n in role_vars and n not in used)


# Parts of an error message that show no value: this filter's own messages,
# and the name of a missing field or variable
SAFE_REASONS = [
    (re.compile(r"(cassandra_(?:config_import|inventory_layout|inventory_files|import_nodes|import_self_check(?:_nodes)?): [^\n]*?"
                r"\(message hidden: "
                r"it may show a value read from the nodes\))"), r"\1"),
    (re.compile(r"(cassandra_import_nodes: [0-9A-Fa-f.:]+: \w+)"), r"\1"),
    (re.compile(r"(cassandra_config_import: unsupported series \w+)"), r"\1"),
    # the user's own -e value
    (re.compile(r"(import_cluster_keep_hand_edits: [^\n]*?: not a config file of cassandra_config \([^)]*\))"), r"\1"),
    (re.compile(r"(cassandra_config_import: cannot read [\w, ]+ from cassandra\.yaml \([^)]*\))"), r"\1"),
    (re.compile(r"(cassandra_config_import: cassandra\.yaml is not valid YAML(?: \(line \d+\))?)"), r"\1"),
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


_IPV4 = re.compile(r"^[0-9]+(?:[.][0-9]+){3}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?(?:[.][A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9])?)*$")


def _getent(text):
    """getent hosts output -> {address: [names]} and {name: [addresses]}."""
    names, addresses = {}, {}
    for line in (text or "").splitlines():
        fields = line.split()
        for name in fields[1:]:
            names.setdefault(fields[0], []).append(name)
            addresses.setdefault(name, []).append(fields[0])
    return names, addresses


def cassandra_ring_names(on_node, addresses, given):
    """on_node: getent hosts <addresses> on the given node that read the ring;
    addresses: the nodes found in the ring; given: that given node's
    inventory name. Returns {address: the name to reach it by}: the name the
    given node knows it by, in the form the given name has (its short name,
    or the fqdn when the given name has a domain); else the address (a given
    node named by its address: the others too)."""
    if _IPV4.match(str(given)) or ":" in str(given):
        return dict((a, a) for a in addresses)
    names, dummy = _getent(on_node)
    out = {}
    for address in addresses:
        found = [n for n in names.get(address) or [] if _HOSTNAME.match(n)]
        fqdn = next((n for n in found if "." in n), "")
        out[address] = (fqdn if "." in str(given) else (found[0].split(".")[0] if found else "")) or address
    return out


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_ring_nodes": cassandra_ring_nodes,
            "cassandra_config_import": cassandra_config_import,
            "cassandra_jmx_files": cassandra_jmx_files,
            "cassandra_inventory_layout": cassandra_inventory_layout,
            "cassandra_inventory_layout_over": cassandra_inventory_layout_over,
            "cassandra_inventory_user_files": cassandra_inventory_user_files,
            "cassandra_inventory_files": cassandra_inventory_files,
            "cassandra_inventory_generated": cassandra_inventory_generated,
            "cassandra_inventory_leftovers": cassandra_inventory_leftovers,
            "cassandra_inventory_same_secret": cassandra_inventory_same_secret,
            "cassandra_inventory_own_values": cassandra_inventory_own_values,
            "cassandra_import_kept_ansible_host": cassandra_import_kept_ansible_host,
            "cassandra_config_ignored_vars": cassandra_config_ignored_vars,
            "cassandra_unit_environment": cassandra_unit_environment,
            "cassandra_import_error": cassandra_import_error,
            "cassandra_ring_names": cassandra_ring_names,
            "cassandra_leading_comments": leading_comments,
        }
