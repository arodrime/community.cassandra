# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import (import_cluster): a node's config file read against the role's template of it.

_load_role, _render, _import_file, _read_changed_lines: the variables that give the node's lines, the hand edits
left over, and the settings compared as Cassandra reads them (YAML, shell, properties, XML). Also the constants
the other import modules share, and the import's error rules (values_hidden, error_without_message): an
unexpected error never shows its message, which may quote a value read from a node.

Filter (plugins/filter/cassandra_import.py names the filters):
cassandra_import_error: a failed task's result -> why it failed, without the
    values (the import's no_log tasks hold passwords).
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
import textwrap
import traceback
import xml.etree.ElementTree as ET
from collections import namedtuple

import jinja2
import jinja2.sandbox
import yaml

from ansible.errors import AnsibleFilterError, AnsibleUndefinedVariable
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.filter.core import regex_replace
from ansible_collections.community.cassandra.plugins.filter import cassandra_jvm_args as jvm_args
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    DEFAULT_WHEN_ABSENT, FORMER_SUB_KEYS, JVM_UNLOCK, NOT_COMPARED, QUANTITY_KEYS, STOCK_WHEN_ABSENT, former_keys,
    former_value, jvm_options_settings, quantity, stock_when_absent, without_later_stock, shell_words, xml_settings,
    yaml_settings)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file


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
BY_PATH = {"cassandra_sstable_format": ("sstable", "selected_format"),
           "cassandra_audit_logger_class_name": ("audit_logging_options", "logger", 0, "class_name"),
           "cassandra_audit_logs_dir": ("audit_logging_options", "audit_logs_dir"),
           "cassandra_audit_included_categories": ("audit_logging_options", "included_categories"),
           "cassandra_audit_excluded_keyspaces": ("audit_logging_options", "excluded_keyspaces"),
           "cassandra_audit_excluded_users": ("audit_logging_options", "excluded_users"),
           "cassandra_audit_max_queue_weight": ("audit_logging_options", "max_queue_weight"),
           "cassandra_audit_max_log_size": ("audit_logging_options", "max_log_size")}
# The audit logger's parameters: the one mapping of its list (Cassandra reads the first, as text), read as text
AUDIT_PARAMETERS = ("cassandra_audit_logger_parameters", ("audit_logging_options", "logger", 0, "parameters"))
INTEGERS = ("cassandra_num_tokens", "cassandra_allocate_tokens_for_local_replication_factor",
            "cassandra_audit_max_queue_weight", "cassandra_audit_max_log_size")
# Same masking as cassandra_config's diff preview (plugin_utils cassandra_secrets)
_mask, _secret = cassandra_secrets.mask, cassandra_secrets.hidden


# The node's own address or name: written as the fact that gives it
ADDRESSES = ["cassandra_listen_address", "cassandra_rpc_address",
             "cassandra_broadcast_address", "cassandra_broadcast_rpc_address", "cassandra_jmx_rmi_hostname"]
# cassandra-rackdc.properties' dc= and rack= lines, apart from the node's dc and rack
# (cassandra_dc/_rack, the ring's) under a snitch that doesn't read them
RACKDC = ("cassandra_rackdc_dc", "cassandra_rackdc_rack")
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


def _load_role(series, facts):
    ctx = role_file("cassandra_config")
    role_vars = role_file("cassandra_config", "vars")
    files = role_vars["_cassandra_config_files"][SERIES[series]]
    ctx["cassandra_version"] = series
    ctx["ansible_facts"] = facts
    ctx["_cassandra_config_quote"] = role_vars["_cassandra_config_quote"]
    ctx["_cassandra_config_extra_sections"] = role_vars["_cassandra_config_extra_sections"]
    ctx["_cassandra_config_plain_path"] = role_vars["_cassandra_config_plain_path"]
    ctx["_cassandra_config_log_patterns"] = role_vars["_cassandra_config_log_patterns"]
    # (the extra lines read as such: a snippet of yours renders nothing here)
    ctx["_cassandra_config_snippet_logback"] = ctx["_cassandra_config_snippet_appenders"] = ""
    ctx["_cassandra_config_snippet_env"] = ""
    env = jinja2.Environment(keep_trailing_newline=True, trim_blocks=True)
    env.filters["regex_replace"] = regex_replace
    # Ansible's, as the templates use them (the JMX users' templates too)
    env.filters["bool"] = lambda value: boolean(value, strict=False)
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    env.filters["to_nice_yaml"] = lambda data, indent=2: yaml.safe_dump(data, default_flow_style=False, indent=indent)
    env.filters["to_json"] = json.dumps
    env.filters["zip"] = lambda *lists: list(zip(*lists))
    for dummy in range(5):  # defaults referencing other defaults
        for k, v in ctx.items():
            if isinstance(v, str) and "{{" in v:
                ctx[k] = env.from_string(v).render(**ctx)
                if ctx[k] in ("True", "False"):  # (cassandra_log_audit_file) like Ansible, a boolean again
                    ctx[k] = ctx[k] == "True"
                elif ctx[k].startswith(("[", "{")):  # like Ansible, a rendered list/dict is one again
                    try:
                        ctx[k] = ast.literal_eval(ctx[k])
                    except (ValueError, SyntaxError):
                        pass
    return env, ctx, files


# cassandra_config's vars made of other variables, as lines of logback.xml: worked out again from each rendering's
DERIVED = ("_cassandra_config_logback_refs", "_cassandra_config_log_levels")
# a node's whole <appender> elements before <root> (no variable of their own): its snippet logback.xml:appenders
# once laid out (the import's own key, never in the inventory)
APPENDERS = "(logback.xml:appenders)"
# the lines a node has at the end of cassandra-env.sh, of logback.xml's <configuration>: their snippets once laid out
ENV_END = "(cassandra-env.sh)"
LOGBACK_END = "(logback.xml)"
END_KEYS = {"cassandra-env.sh": ENV_END, "logback.xml": LOGBACK_END}
# the role's variable each of these renders in (its snippet's)
SNIPPET_TEXT = ((APPENDERS, "_cassandra_config_snippet_appenders"), (ENV_END, "_cassandra_config_snippet_env"),
                (LOGBACK_END, "_cassandra_config_snippet_logback"))


def _render(env, series, name, ctx):
    with open(os.path.join(ROLE, "templates", SERIES[series], name + ".j2")) as f:
        src = f.read()
    ctx = dict(ctx)
    for key in DERIVED:  # (lists of lines, as Ansible gives them)
        ctx[key] = ast.literal_eval(env.from_string(role_file("cassandra_config", "vars")[key]).render(**ctx).strip())
    for key, var in SNIPPET_TEXT:
        if ctx.get(key):
            ctx[var] = "\n".join(ctx[key])
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


# G1's tuning lines: commented out too when their value is "" (... and value | string != '')
WHEN_SET = r"(?: and \(?\w+(?: \| default\(\w+\))?\)? \| string != '')?"
_PREFIXED_CHOICE = re.compile(r"'([^']*)' ~ %s if %s == '(\w+)'%s else '([^']*)'" % (V, V, WHEN_SET))


def _read_prefixed_choice(m, e, cap, seen):
    """{{ 'prefix' ~ x if y == 'name' else 'off' }}: x, and y's value name."""
    prefix, var, gcvar, name, off = _PREFIXED_CHOICE.fullmatch(e).groups()
    if cap != off and cap.startswith(prefix):
        seen.found[var] = _value(cap[len(prefix):])
        seen.gc.setdefault(gcvar, set()).add(name)
    elif cap != off:  # another option in its place (-Xmx8G for -XX:MaxTenuringThreshold=2): not this line's
        return False


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


REMOVED = re.compile(r"^cassandra_jvm\d*_removed_options$")
REMOVED_TOGGLE = re.compile(r"^\{\{ '#' if '([^']*)' in (cassandra_jvm\d*_removed_options) else '' \}\}(.*)$")


def _read_removed(m, e, cap, seen):
    """{{ '#' if 'line' in cassandra_jvm<N>_removed_options else '' }}: a stock option line commented out."""
    if re.match(r"^#\s*$", cap):
        seen.found[m.group(2)] = [m.group(1)]
    elif cap:
        return False


def _merge(found, got):
    """found updated with got, the stock option lines switched off added to found's."""
    for key, value in got.items():
        if REMOVED.match(key) and isinstance(found.get(key), list):
            found[key] = found[key] + [line for line in value if line not in found[key]]
        else:
            found[key] = value


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
    (r"'[^']*' ~ %s if %s == '(\w+)'%s else '([^']*)'" % (V, V, WHEN_SET), _read_prefixed_choice),
    (r"'([^']*)' if %s == '(\w+)'%s else '([^']*)'" % (V, WHEN_SET), _read_choice),
    (r"(\w+) if (\w+) == '([^']*)' else '([^']*)'", _read_chosen),
    (r"\('([^']*)' ~ (\w+) ~ '([^']*)'\) if \2 else '([^']*)'", _read_between),
    (r"\('\\n' ~ .*\) if \w+ else ''", _read_newline_list),
    (r"\(\w+ ~ '\\n'\) if \w+ else ''", _read_newline_list),  # (lines before it: a snippet's)
    # the heap in cassandra-env.sh unless in the JVM options (cassandra_heap_in_jvm_options; there: read as extra
    # options, then as the heap when -Xms and -Xmx are alike, _heap_in_options)
    (r"'' if (\w+) and not \(\w+ \| bool\) else '[^']*'", _read_switch),
    (r"(\w+) if \1 and not \(\w+ \| bool\) else '[^']*'", _read_or_default),
    (r"\('\\n-Xms' ~ .*\) if .* else ''", _read_newline_list),
    (r"'#' if '([^']*)' in (cassandra_jvm\d*_removed_options) else ''", _read_removed),
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
    # its other settings (cassandra_rackdc_extra_settings), when the role writes them back the same: a plain key, a
    # value on one line, not starting with a blank (else hand edits)
    keys = set(m.group(1) for t, r in zip(tpl, rendered) if "{{" in t
               for m in [re.match(r"^(?:#\s*)?([\w.-]+)\s*=", r.strip())] if m)
    extra = dict((k, v) for k, v in node_kv.items() if k not in keys and re.match(r"^[A-Za-z0-9_.-]+$", k)
                 and not re.search(r"(^[ \t\f]|\\$)", v) and not _secret(k, v))
    if extra and "cassandra_rackdc_extra_settings" in ctx:
        found["cassandra_rackdc_extra_settings"] = extra
    return found


def _end_lines_at(name, tpl):
    """Where a file's extra lines go: (the template lines they are read after: a JVM options file's line that joins
    its extra options, or the line that renders the snippet of cassandra-env.sh or logback.xml; the index of that
    snippet's line, None for a JVM options file, its extra lines appended at the end)."""
    tail = [t for t in tpl[-2:] if "| join(" in t]
    # cassandra-env.sh and logback.xml: the line their snippet follows (env: its last one; logback: the one
    # before </configuration>), the lines after it on the node; the JVM options files: lines appended
    after = next((i for i in range(len(tpl) - 1, max(len(tpl) - 4, -1), -1)
                  if "_cassandra_config_snippet_" in tpl[i]), None) if name.endswith((".sh", ".xml")) else None
    if after is not None:
        tail = [tpl[after]]
    return tail, after


def _read_changed_lines(name, tpl, rendered, live, ctx, after):
    """The node's lines that differ from the rendering, each read against its template line -> (found: {variable:
    value}, appended: the node's lines after the line at after (cassandra-env.sh, logback.xml), extra: the JVM
    options no variable covers, or the lines appended after the last one)."""
    found, appended, extra = {}, [], []
    ops = difflib.SequenceMatcher(None, rendered, live, autojunk=False).get_opcodes()
    if name.endswith(".options"):
        # a stock option line the node has removed: switched off, unless the node sets that option elsewhere, or the
        # file predates it (LATER_STOCK: the role adds it)
        options = set(_option_key(line) for line in live if line.startswith("-"))
        later = without_later_stock(name, "\n".join(rendered), "\n".join(live))[1]
        for op, i1, i2, j1, j2 in ops:
            gone = range(i1, i2) if op == "delete" else range(i1 + (j2 - j1), i2) if op == "replace" else ()
            for i in gone:
                m = REMOVED_TOGGLE.match(tpl[i])
                if m and rendered[i] == m.group(1) and _option_key(m.group(1)) not in options and m.group(1) not in later:
                    _merge(found, {m.group(2): [m.group(1)]})
    for op, i1, i2, j1, j2 in ops:
        if op not in ("replace", "insert"):
            continue
        # cassandra-env.sh: a line the node commented out beside its own (#LOCAL_JMX=yes above LOCAL_JMX=no) does
        # not run; the node's lines that run paired with the role's in order (the role's comments kept: a line its
        # template comments out or not, #MAX_HEAP_SIZE, is still that line's)
        pairs = {}
        if op == "replace" and name.endswith(".sh"):
            code = [n for n in range(j1, j2) if not live[n].lstrip().startswith("#")]
            if len(code) == i2 - i1:
                pairs = dict(zip(code, range(i1, i2)))
        for k, j in enumerate(range(j1, j2)):
            i = pairs.get(j, i1 + k if op == "replace" and i1 + k < i2 else None)
            got = _read_line(tpl[i], live[j], ctx) if i is not None and "{{" in tpl[i] else None
            if got is not None:
                _merge(found, got)
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
    tail, after = _end_lines_at(name, tpl)
    found, appended, extra = _read_changed_lines(name, tpl, rendered, live, ctx, after)
    if appended and _carried(name, series, tpl, appended):
        extra = appended  # all of them or none: a block cut in two would not run (if ... fi) or parse (<root>)
    if tail and extra:
        found[END_KEYS.get(name) or re.search(r"\((cassandra_\w+) \| join", tail[0]).group(1)] = extra
    if name.endswith(".yaml"):
        # top-level settings by their key, wherever the node has them (lines
        # added or moved by hand shift the ones below out of place)
        found.update(_import_by_key(tpl, live, ctx))
    if name.endswith(".options"):
        _custom_gc(env, series, name, ctx, tpl, live, found)
    if name.endswith(".sh"):
        found.update(_env_lines_elsewhere(tpl, live, ctx, found.get("cassandra_local_jmx") is False))
    return found


# The earlier series whose stock cassandra-env.sh a node of a series may still have (the package keeps a changed file
# on upgrade): their stock code differs as cassandra_jvm_args models it (a test checks it on the stock files). 4.0's
# is 4.1's
EARLIER_ENV = {"50x": ("41x",)}
# a line of the later series a node may have put in the earlier file (5.0's jamm agent: 4.x's does not start 5.0)
LATER_ENV_LINE = re.compile(r'^JVM_OPTS="\$JVM_OPTS ')


def earlier_env(series, live, role, facts):
    """The node's cassandra-env.sh (live: its text) as an earlier series' stock file (EARLIER_ENV), its lines added
    to JVM_OPTS or setting the heap aside (cassandra_jvm_args.env_differences): {series: that one, found: its
    variables, read by that series' template (but at that series' defaults), text: that file rendered with them, a
    JVM_OPTS line outside any block taken from the later series' (role: the role's rendering) in place of its own}.
    None when it is not one."""
    for old in EARLIER_ENV.get(series, ()):
        env, ctx, dummy = _load_role(old, facts)
        found = _import_file(env, old, jvm_args.ENV, ctx, live.split("\n"))
        found = dict((k, v) for k, v in found.items() if not _same_value(v, _default(ctx, k)))
        lines = _render(env, old, jvm_args.ENV, dict(ctx, **found))[1]
        node, later = live.split("\n"), set(line.strip() for line in role.split("\n"))
        matcher = difflib.SequenceMatcher(None, [x.strip() for x in lines], [x.strip() for x in node], autojunk=False)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "replace" and i2 - i1 == 1 and j2 - j1 == 1 and node[j1].strip() in later \
                    and lines[i1].strip() not in later and LATER_ENV_LINE.match(lines[i1]) and LATER_ENV_LINE.match(node[j1]):
                lines[i1] = node[j1]
        text = "\n".join(lines)
        try:
            jvm_args.env_differences(jvm_args.env_lines(live), jvm_args.env_lines(text))
        except jvm_args.NotSame:
            continue
        return {"series": old, "found": found, "text": text}
    return None


# why a node's earlier series' stock cassandra-env.sh is not shown to give the JVM the same arguments, with no JVM to
# work it out from
NOT_RUNNING_OR_CHANGED = "Cassandra not running, or its files changed since it started"


def earlier_env_not_shown(earlier, series, why):
    """The one line said for a node's earlier series' stock cassandra-env.sh (earlier: earlier_env's) whose values
    are read but that is not shown to give the JVM the same arguments as the series' one (why) -> the line."""
    return ("cassandra-env.sh: the stock file of %s.x (kept on upgrade), its values read; %s's not shown to give the"
            " JVM the same arguments: %s" % (SERIES[earlier["series"]].split(".")[0], SERIES.get(series, series), why))


def env_cmdline(running):
    """The running JVM's arguments (running: {cmdline, newer}) as what cassandra-env.sh gave it: none when that file
    was written since it started (its arguments are not that file's)."""
    running = running or {}
    return "" if jvm_args.ENV in (running.get("newer") or []) else running.get("cmdline") or ""


def _without_file_hunks(hand, name):
    """Hand edits (_difference_lines') without the ones of the file name."""
    out, mine = [], False
    for line in hand:
        header = re.match(r"^(\S.*), line \d+:$", line)
        if header:
            mine = header.group(1) == name
        if not mine:
            out.append(line)
    return out


# cassandra-env.sh's lines of a variable a node may have switched on at another place than the stock one (below the
# stock comment, at the end) outside any block, or in the remote JMX branch: bash runs them as well there
ELSEWHERE = ("-Djava.rmi.server.hostname=",)
# the options of cassandra-env.sh's remote JMX TLS lines (cassandra_jmx_ssl_*, off by default): read where the stock
# file has them; set by a node's own lines elsewhere (the JVM's default TLS settings, for another client too), those
# lines are its own, carried as such
JMX_TLS_OPTIONS = ("-Dcom.sun.management.jmxremote.ssl", "-Djavax.net.ssl.")
LOCAL_JMX_TEST = 'if [ "$LOCAL_JMX" = yes ]; then'  # (as env_lines writes it)


def _env_lines_elsewhere(tpl, live, ctx, remote_jmx=False):
    """The values of cassandra-env.sh's lines of ELSEWHERE that the node has on outside any block, or in the else
    branch of its LOCAL_JMX test when it sets remote JMX (remote_jmx: LOCAL_JMX=no read), wherever they are (the last
    one counts) -> {variable: value}."""
    try:
        lines = jvm_args.env_lines("\n".join(live))
    except jvm_args.NotSame:
        return {}
    ran = set(line["n"] for line in lines if not line["depth"])
    test = next((i for i, line in enumerate(lines) if line["text"] == LOCAL_JMX_TEST and not line["depth"]), None)
    if remote_jmx and test is not None:
        branch = lines[test + 1:]
        branch = branch[:next((i for i, line in enumerate(branch) if not line["depth"]), len(branch))]  # (to its fi)
        at_else = next((i for i, line in enumerate(branch) if line["text"] == "else" and line["depth"] == 1), None)
        if at_else is not None:
            ran |= set(line["n"] for line in branch[at_else + 1:] if line["depth"] == 1 and line["text"] != "fi")
    found = {}
    for t in [t for t in tpl if "{{" in t and any(word in t for word in ELSEWHERE)]:
        for n, line in enumerate(live):
            got = _read_line(t, line.strip(), ctx) if n + 1 in ran else None
            if got and all(got.values()):
                found.update(got)
    return found


# G1's tuning variables: "" comments their stock line out
G1_TUNING = re.compile(r"^cassandra_jvm\d*_(?:max_tenuring_threshold|g1_heap_region_size|g1_new_size_percent"
                       r"|g1_rset_updating_pause_time_percent|max_gc_pause_millis|initiating_heap_occupancy_percent)$")
# a JVM options file's line written for one GC (the stock block of G1 or CMS): its file's GC variable
GC_LINE = re.compile(r"\((cassandra_jvm\d+_gc) \| default\(cassandra_jvm_gc\)\) == ")


def _custom_gc(env, series, name, ctx, tpl, live, found):
    """A jvm<N>-server.options whose GC options the role's stock block cannot write as the node has them -> found
    changed in place, first of these that gives the JVM the node's GC options and differs in nothing more than the
    reading as is (a hand edit elsewhere in the file stays one):
    - G1 with the tuning lines the node has not on commented out (their variables ""), its other values as read;
    - cassandra_jvm<N>_gc custom (the role's blocks commented out), the node's GC lines (any GC, its threads, the
      unlock options) in its extra options, in the node's order with them.
    Else as read: what differs stays a hand edit."""
    gc_lines = [t for t in tpl if GC_LINE.search(t)]
    if not gc_lines:
        return
    var = jvm.options_variable(name)
    node = jvm_options_settings("\n".join(live))
    block = gc_lines + [t for t in tpl if "_gc_threads" in t]
    names = set(jvm.option_name(w.lstrip("#")) for t in gc_lines for w in re.findall(r"'(#?-[^']*)'", t))
    names |= {"ParallelGCThreads", "ConcGCThreads"} | set(JVM_UNLOCK)

    def is_gc(option):
        return option in names or bool(re.match(r"^Use\w*GC$", option or "")) or str(option).startswith("(set before")

    def differences(variables):
        role = jvm_options_settings("\n".join(_render(env, series, name, dict(ctx, **variables))[1]))
        return set(k for k in set(role) | set(node) if role.get(k) != node.get(k))
    as_read = differences(found)
    if not [k for k in as_read if is_gc(k)]:
        return

    def better(variables):  # the GC's differences gone, none added
        found_now = differences(variables)
        return found_now <= as_read and not [k for k in found_now if is_gc(k)]
    # G1 with the tuning lines the node has not on commented out (their value ""): the node's own G1 block
    tuned = {}
    for t in [t for t in gc_lines if "== 'G1'" in t]:
        value = re.search(r"\((cassandra_jvm\d+_\w+) \| default\(cassandra_jvm_\w+\)\) if", t)
        option = re.match(r"\{\{ '(-[^'=]+)=' ~", t)
        if value and option and not node.get(jvm.option_name(option.group(1) + "=")):
            tuned[value.group(1)] = ""
    if tuned and node.get("UseG1GC") == ["-XX:+UseG1GC"] and better(dict(found, **tuned)):
        found.update(tuned)
        return
    extras, lines = list(found.get(var) or []), []
    for line in live:
        if line in extras:
            extras.remove(line)
            lines.append(line)
        elif any(is_gc(jvm.option_name(o)) for o in jvm.words(line)):
            lines.append(line)
    custom = dict((k, v) for k, v in found.items()  # (less what the block's lines gave)
                  if k not in re.findall(r"\b(cassandra_jvm\d+_\w+)\b", " ".join(block)))
    custom[GC_LINE.search(gc_lines[0]).group(1)] = "custom"
    custom[var] = lines
    if better(custom):
        found.clear()
        found.update((k, v) for k, v in custom.items() if v != [] or k != var)


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
                    if any(o in line for o in JMX_TLS_OPTIONS):
                        continue
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


def _indents(lines, indexes):
    """The indents of a block's setting lines (comments and blank lines aside), in order, each once."""
    out = []
    for i in indexes[1:]:
        line = lines[i]
        indent = len(line) - len(line.lstrip(" "))
        if line.strip() and not line.lstrip().startswith("#") and indent not in out:
            out.append(indent)
    return sorted(out)


def _reindented(rendered, live):
    """The node's cassandra.yaml lines, each section indented as the role's (4.0's stock file indents by 4, 4.1's by
    2: a 4.0 file on a later release) when it has as many levels, and reads the same with them (YAML) -> lines."""
    role = dict((key, _indents(rendered, indexes)) for key, indexes in _top_blocks(rendered))
    live = list(live)
    for key, indexes in _top_blocks(live):
        mine = _indents(live, indexes)
        if key not in role or not mine or mine == role[key] or len(mine) != len(role[key]):
            continue
        levels = dict(zip(mine, role[key]))
        block = []
        for i in indexes:
            line = live[i]
            indent = len(line) - len(line.lstrip(" "))
            block.append(" " * levels[indent] + line.lstrip(" ") if i != indexes[0] and indent in levels else line)
        try:
            same = yaml.safe_load("\n".join(live[i] for i in indexes)) == yaml.safe_load("\n".join(block))
        except yaml.YAMLError:
            same = False
        if same:
            for i, line in zip(indexes, block):
                live[i] = line
    return live


def _former_sub_keys_renamed(rendered, live):
    """The node's cassandra.yaml lines, a section's sub-key under its former name (FORMER_SUB_KEYS: 4.0's) under the
    name the role's section writes it (Cassandra reads either), the section having no line of that name -> (lines,
    notes)."""
    role = dict((key, [rendered[i] for i in indexes]) for key, indexes in _top_blocks(rendered))
    live, notes = list(live), []
    for key, indexes in _top_blocks(live):
        for old, new in FORMER_SUB_KEYS.get(key, {}).items():
            written = re.compile(r"^\s+%s:" % new)
            at = [i for i in indexes if re.match(r"^\s+%s:" % old, live[i])]
            if len(at) == 1 and any(written.match(r) for r in role.get(key, [])) \
                    and not any(written.match(live[i]) for i in indexes):
                line = live[at[0]]
                live[at[0]] = line.replace(old + ":", new + ":", 1)
                notes.append("cassandra.yaml: %s: %s  (node: %s)" % (key, live[at[0]].strip(), _mask(line.strip())))
    return live, notes


def _canonical(name, rendered, live):
    """The node's lines, each simple setting written the way the role writes
    that setting (its quoting, no comment after it, true/false in lower case),
    so that reading them against the template does not depend on how they
    were written; a top-level cassandra.yaml key set twice: the last one counts
    (as for Cassandra), the others are commented out."""
    if name.endswith(".yaml"):
        styles, bools = _yaml_styles(rendered)
        live = _former_sub_keys_renamed(rendered, _reindented(rendered, live))[0]
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
        if value is not None and (str(value) == str(role_value) or value == "" and role_value is None or
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
        return [cls(line, shown) for line, shown in zip(lines, cassandra_secrets.mask_lines(lines))]

    @staticmethod
    def masked(lines):
        """The lines as shown: masked as in the whole file (cassandra_secrets.mask_lines), each _Shown one as it was."""
        if all(isinstance(line, _Shown) for line in lines):
            return [line.shown for line in lines]
        return [line.shown if isinstance(line, _Shown) else shown for line, shown in zip(lines, cassandra_secrets.mask_lines(lines))]


def _properties_leftovers(name, rendered, live_text):
    """A key=value file: only the settings count, not comments, blank lines or order -> (hand: each setting that
    differs, its values masked; normalized: the file rewritten with the same settings)."""
    hand, normalized = [], []
    role_kv, node_kv = _properties(rendered), _properties(live_text.split("\n"))
    for key in sorted(set(role_kv) | set(node_kv)):
        if role_kv.get(key) != node_kv.get(key):
            hand.append("%s: %s" % (name, key))
            hand += ["  - %s=%s" % (key, cassandra_secrets.MASK if _secret(key, role_kv[key]) else _mask(role_kv[key]))] \
                if key in role_kv else []
            hand += ["  + %s=%s" % (key, cassandra_secrets.MASK if _secret(key, node_kv[key]) else _mask(node_kv[key]))] \
                if key in node_kv else []
    if role_kv == node_kv and rendered != live_text.split("\n"):
        normalized.append("%s: same settings, comments and layout rewritten" % name)
    return hand, normalized


@functools.lru_cache(maxsize=None)
def _parameter_patterns():
    """(a key the role writes plain, a character no value may hold) of cassandra_audit_logger_parameters: the role's
    own (_cassandra_config_plain_key, _cassandra_config_unwritable_text), which its checks refuse."""
    role_vars = role_file("cassandra_config", "vars")
    return re.compile(role_vars["_cassandra_config_plain_key"]), re.compile(role_vars["_cassandra_config_unwritable_text"])


def _set_parameters(item):
    """A mapping of the logger's parameters without its null values (for Cassandra, as if not there)."""
    return dict((k, v) for k, v in item.items() if v is not None) if isinstance(item, dict) else item


def _without_null_parameters(logger):
    """The logger's entries, the null values of their parameters left out, and their parameters when only null
    values were there."""
    if not isinstance(logger, list):
        return logger
    out = []
    for entry in logger:
        if isinstance(entry, dict) and isinstance(entry.get("parameters"), list):
            parameters = [_set_parameters(p) for p in entry["parameters"]]
            gone = parameters == [{}] and entry["parameters"] != [{}]  # (an empty mapping written as such stays)
            entry = dict((k, v) for k, v in entry.items() if k != "parameters")
            if not gone:
                entry["parameters"] = parameters
        out.append(entry)
    return out


def _logger_lines(lines):
    """The indexes of the lines of audit_logging_options' logger, and its value, its scalars as text (Cassandra
    reads its parameters as text: 7 and "7" are the same), the null values of its parameters left out (None: none,
    or not YAML)."""
    block = next((indexes for key, indexes in _top_blocks(lines) if key == "audit_logging_options"), None)
    at = _sub_key_lines(lines, block, ["logger"]) if block else []
    try:
        data = _as_text(yaml.compose(textwrap.dedent("\n".join(lines[i] for i in at)))) if at else None
    except yaml.YAMLError:
        data = None
    return at, _without_null_parameters(data.get("logger")) if isinstance(data, dict) else None


def _without_same_logger(rendered, live):
    """Both sides minus the audit logger's lines when they set the same, however the node writes them (flow style,
    quotes) -> (rendered, live, normalized: a note when written another way)."""
    (at_r, role), (at_n, node) = _logger_lines(rendered), _logger_lines(live)
    if role is None or role != node:
        return rendered, live, []
    note = [] if [rendered[i] for i in at_r] == [live[i] for i in at_n] else [
        "cassandra.yaml: audit_logging_options.logger, the same written another way"]
    return ([r for i, r in enumerate(rendered) if i not in at_r], [n for i, n in enumerate(live) if i not in at_n],
            note)


def _sub_key_values(lines, section, keys):
    """{key: (its value as text, the indexes of its lines and of its commented-out ones)} of the keys set once in
    section's block layout (not YAML: left out)."""
    block = next((indexes for key, indexes in _top_blocks(lines) if key == section), None)
    block = block + _commented_under(lines, block[-1]) if block else None  # its stock commented lines last
    out = {}
    for key in keys if block else []:
        active = _sub_key_lines(lines, block, [key])
        if len([i for i in active if re.match(r"^\s*%s:" % re.escape(key), lines[i])]) != 1:
            continue
        try:
            data = _as_text(yaml.compose(textwrap.dedent("\n".join(lines[i] for i in active))))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and data.get(key) is not None:
            out[key] = (data[key], _sub_key_lines(lines, block, [key], True))
    return out


def _without_same_sub_keys(rendered, live):
    """Both sides minus the lines (and the commented-out stock ones) of a section's sub-keys that have a variable
    (_extra_sections, but its enabled and logger) when they set the same, wherever the node writes them in the
    section (e.g. after the logger, where the role wrote them as extra settings) -> (rendered, live, normalized: a
    note when written another way)."""
    drop_rendered, drop_live, notes = set(), set(), []
    for section, own in _extra_sections().items():
        keys = [k for k in own if k not in ("enabled", "logger")]
        role, node = _sub_key_values(rendered, section, keys), _sub_key_values(live, section, keys)
        for key in sorted(k for k in role if k in node and role[k][0] == node[k][0]):
            drop_rendered.update(role[key][1])
            drop_live.update(node[key][1])
            if [rendered[i] for i in role[key][1]] != [live[i] for i in node[key][1]]:
                notes.append("cassandra.yaml: %s.%s, the same written another way" % (section, key))
    return ([r for i, r in enumerate(rendered) if i not in drop_rendered],
            [n for i, n in enumerate(live) if i not in drop_live], notes)


def _without_stock_when_absent(rendered, live):
    """The role's lines minus its block of each key of STOCK_WHEN_ABSENT the node's file has no line for, when the role
    sets it at the value Cassandra takes without it (the stock line of a later series), a note for each."""
    in_live = set(key for key, dummy in _top_blocks(live))
    drop, notes = set(), []
    for key, indexes in _top_blocks(rendered):
        if key in STOCK_WHEN_ABSENT and key not in in_live:
            try:
                same = stock_when_absent(key, yaml_settings("\n".join(rendered[i] for i in indexes)))
            except yaml.YAMLError:
                same = False
            if same:
                drop.update(indexes)
                notes.append("cassandra.yaml: %s: not in the node's file, which Cassandra reads as the role's %s"
                             " (the role writes it)" % (key, rendered[indexes[0]].strip() if len(indexes) == 1
                                                        else "stock section"))
    return [r for i, r in enumerate(rendered) if i not in drop], live, notes


def _yaml_compared(series, rendered, live, extras, rejected):
    """cassandra.yaml's lines to compare: without the settings the stock file has no line for, the keys under their
    former names, the keys of a later series' stock file Cassandra reads the same without, the extra settings and the
    keys rejected (masked first) -> (rendered, live, normalized: notes)."""
    normalized = []
    rendered, live, notes = _without_declared(series, rendered, live)
    normalized += notes
    rendered, live, notes = _without_stock_when_absent(rendered, live)
    normalized += notes
    rendered, live, notes = _without_former_names(rendered, live)
    normalized += notes
    live, notes = _former_sub_keys_renamed(rendered, live)
    normalized += notes
    rendered, live, notes = _without_same_logger(rendered, live)
    normalized += notes
    rendered, live, notes = _without_same_sub_keys(rendered, live)
    normalized += notes
    if extras or rejected:
        # masked before the lines of those keys go: a secret's value may go on past the lines dropped
        rendered, live = _without_extras(_Shown.lines(rendered), _Shown.lines(live), extras, rejected)
        sections = _extra_sections()
        normalized += ["cassandra.yaml: %s kept with cassandra_extra_settings (written %s)" % (
            k, "in its section" if k in sections else "at the end of the file") for k in sorted(extras)]
    return rendered, live, normalized


def _file_ops(name, rendered, live, proven=()):
    """The differences of a file's lines from the rendering (difflib opcodes), a JVM options file's options moved
    only left out (proven: the names the JVM is shown to get the same of, wherever they are) -> (opcodes,
    normalized: notes)."""
    ops = difflib.SequenceMatcher(None, rendered, live, autojunk=False).get_opcodes()
    moved = [op for op in ops if op[0] != "equal"
             and _same_settings(name, rendered[op[1]:op[2]], live[op[3]:op[4]]) is None]
    if not (name.endswith(".options") and moved):
        return ops, []
    role, node = jvm_options_settings("\n".join(rendered)), jvm_options_settings("\n".join(live))
    if role == node:
        # the same options, a line at another place (the last of an option counts): cassandra_config
        # leaves such a file as it is
        return [op for op in ops if op not in moved], [
            "%s: the same options, some at another place: left as it is (cassandra_config_normalize: true"
            " rewrites it)" % name]
    # other edits in the file: still, an option the JVM gets the same from the role's lines (one carried in the
    # extra options) is no hand edit where the node has it
    same, notes = [], []
    for op in moved:
        options = jvm.words(rendered[op[1]:op[2]] + live[op[3]:op[4]])
        names = set(jvm.option_name(o) for o in options)
        if options and None not in names and all(role.get(k) == node.get(k) or k in proven for k in names) \
                and all(role.get(k) == node.get(k) for k in set(role) | set(node) if k.startswith("(set before")):
            same.append(op)
            notes.append("%s: %s, at another place than the role writes it: the same for the JVM (the last one"
                         " counts)" % (name, ", ".join(sorted(names))))
    return [op for op in ops if op not in same], notes


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


# the node's JVM_OPTS lines in the branch of the LOCAL_JMX test its running JVM does not take (jvm_args.jmx_branch_not_run)
BRANCH_NOT_RUN_NOTE = ("cassandra-env.sh: %s in the %s JMX branch these nodes do not run (LOCAL_JMX=%s): no effect;"
                       " apply_config writes the stock branch without %s, no restart")


def branch_not_run_note(count, runs):
    """BRANCH_NOT_RUN_NOTE for count lines, the JVM running the runs branch ("local" or "remote")."""
    return BRANCH_NOT_RUN_NOTE % (out.plural(count, "line"), "remote" if runs == "local" else "local",
                                  "yes" if runs == "local" else "no", "it" if count == 1 else "them")


# the role's JMX password file line on a node with that line commented out, its JVM never reading the file
# (jvm_args.password_file_line_inert)
PASSWORD_FILE_NOTE = ("cassandra-env.sh: -Dcom.sun.management.jmxremote.password.file commented out, the JVM never reads"
                      " it (JMX authentication off, no JMX agent, JAAS login or authorizer): no effect; apply_config"
                      " writes the stock line, no restart")


# the node's JVM_OPTS lines in the remote JMX branch, the role's outside it (jvm_args.remote_branch_moved)
REMOTE_BRANCH_NOTE = ("cassandra-env.sh: %s adding options to JVM_OPTS in the remote JMX branch (the one the running"
                      " Cassandra started with: its remote JMX port), the role's outside it: the same for the JVM")

# a node's line of an ELSEWHERE option at another place, the role's at the stock one (jvm_args.named_lines_moved)
ELSEWHERE_NOTE = ("cassandra-env.sh: %s adding an option the role writes at its stock place, at another place outside"
                  " any block on the node (no other line sets it): the same for the JVM")

# a node's line in a block written again the same below it in that block (jvm_args.repeated_lines)
REPEATED_NOTE = ("cassandra-env.sh: %s adding options to JVM_OPTS written again the same below in its block: the"
                 " last one counts, the same for the JVM")


def _leftovers(env, series, files, ctx, live_files, rejected=(), skip=(), proven=(), environment=None, cmdline=""):
    """What the role would still change in the node's files, rendered with ctx: rejected: the cassandra.yaml keys
    its Cassandra rejects; skip: files not compared here (the JVM's, its arguments shown to be the same); environment:
    the one cassandra-env.sh gets; cmdline: the running JVM's arguments (none when cassandra-env.sh was written since
    it started) -> (hand edits, normalized lines, comment lines)."""
    hand, normalized, comments = [], [], []
    extras = ctx.get("cassandra_extra_settings") or {}
    for name in files:
        if name not in live_files or name in skip:
            continue
        # (a stock line of a later release the node's file predates: not a hand edit, said apart)
        rendered = without_later_stock(name, "\n".join(_render(env, series, name, ctx)[1]), live_files[name])[0].split("\n")
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
        live_text = live_files[name]
        if name == jvm_args.ENV:  # (the node's lines in the remote JMX branch it takes, the role's outside it)
            inert = jvm_args.password_file_line_inert(live_text, "\n".join(rendered), cmdline, environment)
            if inert:  # (the stock password file line commented out, the JVM never reading it)
                rendered = jvm_args.without_lines("\n".join(rendered), inert).split("\n")
                normalized.append(PASSWORD_FILE_NOTE)
            mine, theirs = jvm_args.remote_branch_moved(live_text, "\n".join(rendered), environment, cmdline)
            if mine:
                normalized.append(REMOTE_BRANCH_NOTE % out.plural(len(mine), "line"))
            # (a line of an option a node may switch on anywhere, outside any block on both sides)
            named, named_role = jvm_args.named_lines_moved(live_text, "\n".join(rendered), ELSEWHERE, environment)
            if named:
                normalized.append(ELSEWHERE_NOTE % out.plural(len(named), "line"))
            mine, theirs = mine + named, theirs + named_role
            # (and a line its block writes again the same below it: the last one counts)
            again = jvm_args.repeated_lines(live_text, "\n".join(rendered))
            again_rendered = jvm_args.repeated_lines("\n".join(rendered), live_text)
            if again:
                normalized.append(REPEATED_NOTE % out.plural(len(again), "line"))
            live_text = jvm_args.without_lines(live_text, mine + again)
            rendered = jvm_args.without_lines("\n".join(rendered), theirs + again_rendered).split("\n")
        live = _align(rendered, live_text.split("\n"))
        if name == "cassandra.yaml":
            rendered, live, notes = _yaml_compared(series, rendered, live, extras, rejected)
            normalized += notes
        ops, notes = _file_ops(name, rendered, live, proven)
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
              if re.match(r"cassandra_import(_check|_nodes|_read|_files|_jvm|_exporter|_config|_layout)?[.]py$", os.path.basename(f[0]))]
    at = " in %s(), line %d" % (frames[-1][2], frames[-1][1]) if frames else ""
    return AnsibleFilterError("%s: %s%s%s (message hidden: it may show a value read from the nodes)"
                              % (name, (where + ", ") if where else "", sys.exc_info()[0].__name__, at))


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


# Parts of an error message that show no value: this filter's own messages,
# and the name of a missing field or variable
SAFE_REASONS = [
    (re.compile(r"(cassandra_(?:config_import|inventory_layout|inventory_files|import_nodes|import_self_check(?:_nodes)?): [^\n]*?"
                r"\(message hidden: "
                r"it may show a value read from the nodes\))"), r"\1"),
    (re.compile(r"(cassandra_import_nodes: [0-9A-Fa-f.:]+: \w+)"), r"\1"),
    (re.compile(r"(cassandra_config_import: unsupported series \w+)"), r"\1"),
    # the user's own -e value
    (re.compile(r"(import_keep_hand_edits: [^\n]*?: neither a config file of cassandra_config \([^)]*\) nor a part"
                r" \([^)]*\); or true \(every hand-edited file\), false)"), r"\1"),
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
    of the error shows no value, else ''.
    e.g. {"failed": True, "results": [{"failed": True, "item": {"address": "10.0.0.1"}, "msg": "..."}]}
    -> "10.0.0.1: failed" (its message shows a value: left out)."""
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
