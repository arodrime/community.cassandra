# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import (import_cluster): the files it writes, and the ones an earlier import wrote.

The secrets of the hand edits (each in a variable of its own, in secrets.yml), import_keep_hand_edits (the parts of
a node kept as the node has them), the group_vars and host_vars files (the variables grouped by subject, a value
that looks like a template written !unsafe), the first line naming the cluster, and the inventory dir's files of
an earlier import (to remove, keep or replace).

Filters (plugins/filter/cassandra_import.py names the filters):
cassandra_import_kept_last: the inventory dir's files -> the parts the last
    import kept as each node had them.
cassandra_import_keep_parts: import_keep_hand_edits -> the parts it names.
cassandra_inventory_files: that layout -> the files to write, passwords apart,
    variables grouped by subject.
cassandra_inventory_generated: a file's content -> the same with the import's
    first line (naming the cluster).
cassandra_leading_comments: a file's text -> its lines before the first line
    of data (what tells an earlier import's file).
cassandra_inventory_leftovers: the inventory dir's files -> the ones of an
    earlier import to remove, keep or replace, and the conflicts with others.
cassandra_inventory_same_secret: a vaulted file, a content -> whether writing
    it again would only change its encryption.
cassandra_inventory_own_values: the inventory dir's vars files -> values an
    earlier import of this cluster wrote (the CQL login).
cassandra_import_kept_ansible_host: the import's nodes -> the same, without the
    ansible_host the inventory already there does without.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import functools
import json
import os
import re
from collections import namedtuple

import yaml

from ansible.errors import AnsibleFilterError
from ansible.module_utils.common.text.converters import to_bytes, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.filter.cassandra_repository_import import SERIES_TOKEN
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import (
    APPENDERS, ENV_END, HOSTNAME, IMPORTED, IPV4, KEEP, LOGBACK_END, ROLE, _secret, SERIES, SET_UP_BY_THE_ROLES,
    values_hidden)


# the config files cassandra_config writes, the JMX users' too (import_keep_hand_edits names some)
CONFIG_FILE = re.compile(r"^(cassandra[.]yaml|cassandra-env[.]sh|jvm[0-9]*-server[.]options|cassandra-rackdc[.]properties"
                         r"|logback[.]xml|jmxremote[.](?:password|access))$")
# The lists of lines the import fills from the hand edits a node has (cassandra-env.sh, logback.xml, the JVM
# options files): a secret in one of them goes to secrets.yml in a variable of its own, the line refers to it
SECRET_LISTS = ((ENV_END, "cassandra-env.sh"), (LOGBACK_END, "logback.xml"), (APPENDERS, "logback.xml:appenders"),
                ("cassandra_jvm_extra_options", "jvm-server.options"), ("cassandra_jvm8_extra_options", "jvm8-server.options"),
                ("cassandra_jvm11_extra_options", "jvm11-server.options"),
                ("cassandra_jvm17_extra_options", "jvm17-server.options"))
SECRET_VAR = "cassandra_secret_"
SECRET_FILES = {"cassandra-env.sh": "env", "logback.xml": "logback", "logback.xml:appenders": "logback_appenders",
                "jvm-server.options": "jvm",
                "jvm8-server.options": "jvm8", "jvm11-server.options": "jvm11", "jvm17-server.options": "jvm17"}
SECRET_NAME = re.compile(cassandra_secrets.SECRET_NAME)
# a secret's name and the variable it went to (removed to tell whether a line holds another secret)
SECRET_MOVED_REF = re.compile(r"\{\{ (%s\w+) \}\}" % SECRET_VAR)
# the note of the lines carried (cassandra_import_nodes)
CARRIED = "hand edits carried into the inventory: "
SECRET_MOVED = re.compile(cassandra_secrets.SECRET_NAME + r"[\"']?\{\{ %s\w+ \}\}[\"']?" % SECRET_VAR)


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


# a URL's password with one of these is not taken out of its line (the line goes to secrets.yml whole; a / or a
# quote: USERINFO took it to the last @ of its word, maybe further than the password)
URL_PASSWORD_UNSAFE = re.compile(r"[\s'\"{}<>\\$`;&|()?#/]")


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
            shown = cassandra_secrets.SECRET_VALUE.match(line, m.start())
            # (shown: the value as the output masks it; only quotes, blanks or a ; may be left after the one taken)
            if span is None or (shown and not re.fullmatch(r"[\s\"';]*", line[span[1]:shown.end(2)])):
                spans = None
                break
            spans.append((re.sub(r"[\"'\s:=]+$", "", m.group(1)), span))
            pos = span[1]
        urls = list(cassandra_secrets.USERINFO.finditer(line))
        if spans is not None and [m for m in urls if URL_PASSWORD_UNSAFE.search(m.group("password"))]:
            spans = None  # a quote, a brace...: where the password ends is not sure, the line stays whole
        if spans is not None:  # a URL's password (scheme://user:password@host), named by its user
            spans = sorted(spans + [("%s_password" % m.group("user"), m.span("password")) for m in urls
                                    if not [s for s in spans if s[1][0] < m.end("password") and m.start("password") < s[1][1]]],
                           key=lambda s: s[1][0])
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
        if new in own or _secret_words(SECRET_MOVED_REF.sub("", SECRET_MOVED.sub("", new))):
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
    return bool(cassandra_secrets.SECRET_WORD.search(text) or re.search(r"(?:^|[\s\"'=])(?:-pw|--password)\b", text)
                or cassandra_secrets.mask(text) != text)


def _list_secret(key, value, trusted=()):
    """True when a variable goes to secrets.yml: as the output masks it (cassandra_secrets.hidden), the lines of
    the lists of SECRET_LISTS a secret word too; the secrets the import moved out of its own lines (trusted)
    left out."""
    if key in dict(SECRET_LISTS) and isinstance(value, list):
        return any(_secret_words(SECRET_MOVED_REF.sub("", SECRET_MOVED.sub("", v)) if v in trusted else v) for v in value if isinstance(v, str))
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
        kind = "sh" if name.endswith(".sh") else "xml" if name.startswith("logback.xml") else "options"
        lines, moved, made = _take_secrets(variables[key], kind, taken, name, own)
        if moved:
            variables[key] = lines
            variables.update((var, value) for var, (dummy, value) in moved.items())
            trusted += made
            said += ["%s: %s=%s moved to secrets.yml as %s, which %s refers to" % (
                name, setting, cassandra_secrets.MASK, var, "its snippet" if key in dict(SNIPPET_LISTS) else key)
                for var, (setting, dummy) in sorted(moved.items())]
    return variables, trusted, said


# The parts of a node import_keep_hand_edits keeps as the node has them (besides config files): {part: the values that
# leave it so}, the finest switch of each (os: all of cassandra_linux)
KEPT_PARTS = {
    "os": {"cassandra_linux_manage": False},
    "sysctl": {"cassandra_linux_sysctl": {}},
    "limits": {"cassandra_linux_limits": {}, "cassandra_linux_nproc_all": False},
    "thp": {"cassandra_linux_thp_enabled": "keep", "cassandra_linux_thp_defrag": "keep"},
    "swap": {"cassandra_linux_swap_off": False},
    "timesync": {"cassandra_linux_timesync": False},
    "disks": {"cassandra_data_disk_tuning": False},
    "unit": {"cassandra_service_unit_manage": False},
    "repo": {"cassandra_repository_manage": False},
    "cqlsh": {"cassandra_cqlsh_python_manage": False},
    "firewall": {"cassandra_firewall_manage": False},
}
# what the import writes with them: the parts kept (the next import reads it back)
KEPT_PARTS_VAR = "cassandra_import_kept"
# the variables the import carries for each part (left out when the part is kept as the node has it)
_OS_VARS = {"sysctl": ("cassandra_linux_sysctl", "cassandra_linux_sysctl_file"),
            "limits": ("cassandra_linux_limits", "cassandra_linux_nproc_all"),
            "thp": ("cassandra_linux_thp_enabled", "cassandra_linux_thp_defrag"),
            "swap": ("cassandra_linux_swap_off",),
            "timesync": ("cassandra_linux_timesync", "cassandra_linux_timesync_servers"),
            "disks": ("cassandra_data_readahead_kb", "cassandra_data_scheduler", "cassandra_data_lvm_readahead",
                      "cassandra_data_disk_tuning")}
KEPT_PART_VARS = dict(_OS_VARS, os=tuple(v for names in _OS_VARS.values() for v in names),
                      unit=("cassandra_service_start",), repo=(),
                      cqlsh=("cassandra_cqlsh_source", "cassandra_cqlsh_command"),
                      firewall=("open_ports", "cassandra_firewall_enabled", "cassandra_firewall_port_sources",
                                "cassandra_prometheus_exporter_sources"))
# the finer parts os keeps too
OS_PARTS = tuple(_OS_VARS)
# what each part is, for the report and the stops
KEPT_PART_WORDS = {"os": "the OS settings (kernel, limits, THP, swap, time sync, disks)",
                   "sysctl": "the kernel settings", "limits": "the limits files", "thp": "transparent huge pages",
                   "swap": "swap", "timesync": "time sync", "disks": "the tuning of the data devices",
                   "unit": "the unit (or init script) Cassandra is started by", "repo": "the package repositories",
                   "cqlsh": "cqlsh's Python", "firewall": "the firewall"}


def _keep_names(value):
    """import_keep_hand_edits -> True/False, or [names]."""
    if isinstance(value, str) and value.strip().lower() in ("", "yes", "no", "on", "off", "true", "false", "1", "0"):
        return boolean(value.strip(), strict=False)
    if isinstance(value, str):
        value = re.sub(r"[\[\]'\"]", "", value).split(",")
    if not isinstance(value, (list, tuple)):
        return boolean(value, strict=False)
    names = [str(v).strip() for v in value if str(v).strip()]
    # (true among parts: every hand-edited config file, with them)
    names = ["true" if n.lower() in ("true", "yes") else n for n in names]
    wrong = [n for n in names if not CONFIG_FILE.match(n) and n not in KEPT_PARTS and n != "true"]
    if wrong:
        raise AnsibleFilterError("import_keep_hand_edits: %s: neither a config file of cassandra_config (cassandra.yaml,"
                                 " cassandra-env.sh, jvm*-server.options, cassandra-rackdc.properties, logback.xml,"
                                 " jmxremote.password, jmxremote.access) nor a part (%s); or true (every hand-edited"
                                 " file), false"
                                 % (", ".join(wrong), ", ".join(KEPT_PARTS)))
    return names


def keep_files_option(value):
    """import_keep_hand_edits: true/false, or the config files and parts to keep (a list, or names separated by
    commas: -e import_keep_hand_edits=logback.xml,os) -> True, or [the config files named]; a name neither a config
    file nor a part (KEPT_PARTS) fails. true keeps the hand-edited config files only."""
    names = _keep_names(value)
    if names is True or names is False:
        return names or []
    return True if "true" in names else [n for n in names if CONFIG_FILE.match(n)]


# the switches an import before rc10 wrote for a part set up another way: {switch: part}
OLD_KEPT_SWITCHES = {"cassandra_linux_manage": "os", "cassandra_service_unit_manage": "unit",
                     "cassandra_repository_manage": "repo", "cassandra_cqlsh_python_manage": "cqlsh",
                     "cassandra_firewall_manage": "firewall"}
# every one of them, and these, on a node it could not read (not a choice: that node is imported now)
_OLD_NOT_READ = ("cassandra_java_set_default", "cassandra_install_tools", "cassandra_install_jemalloc")


def _covered(part, parts):
    """part is kept by parts: named, or a finer part of os."""
    return part in parts or (part in OS_PARTS and "os" in parts)


def keep_option_with(keep_hand_edits, parts):
    """The import_keep_hand_edits value that keeps what this one does and these parts too (in KEPT_PARTS' order)."""
    names = _keep_names(keep_hand_edits)
    names = ["true"] if names is True else [] if names is False else [str(n) for n in names]
    return ",".join(names + [p for p in KEPT_PARTS if p in parts and p not in names])


def cassandra_import_kept_last(texts, keep_hand_edits=False):
    """texts: {path in the inventory: text} of the files there (an earlier import's among them); keep_hand_edits:
    this import's option -> {host: {parts: [the parts the last import left as that node had them (its
    KEPT_PARTS_VAR, or the *_manage: false of an import before rc10 on a node it read, OLD_KEPT_SWITCHES) that this
    one does not name: it imports them (a part is kept only when the run names it, as a file)], again: the option
    that keeps them} or {unreadable: "vaulted", or "not YAML" (written by the import)} (its host_vars main.yml cannot
    be read)}: the report says each."""
    now = keep_parts_option(keep_hand_edits)
    out = {}
    for path, text in sorted((texts or {}).items()):
        m = re.match(r"^host_vars/([^/]+)/main\.yml$", path)
        vaulted = str(text).startswith("$ANSIBLE_VAULT")
        if not m or not (vaulted or IMPORT_MARK in str(text)):
            continue
        try:
            data = {} if vaulted else (yaml.safe_load(text) or {})
        except yaml.YAMLError:
            data = None
        if vaulted or not isinstance(data, dict):
            # (vaulted: whose it is not known, the import vaults secrets.yml alone)
            out[m.group(1)] = {"unreadable": "vaulted" if vaulted else "not YAML"}
            continue
        last = [str(p) for p in data.get(KEPT_PARTS_VAR) or []]
        if KEPT_PARTS_VAR not in data and not all(data.get(k) is False for k in list(OLD_KEPT_SWITCHES) + list(_OLD_NOT_READ)):
            last = [part for k, part in OLD_KEPT_SWITCHES.items() if data.get(k) is False]
        dropped = [p for p in KEPT_PARTS if p in last and not _covered(p, now)]
        if dropped:
            out[m.group(1)] = {"parts": dropped, "again": keep_option_with(keep_hand_edits, now + dropped)}
    return out


def keep_parts_option(value):
    """import_keep_hand_edits -> [the parts named (KEPT_PARTS)], in their order there; true or false: none."""
    names = _keep_names(value)
    return [] if names is True or names is False else [n for n in KEPT_PARTS if n in names]


# The lines the import carries at the end of a file (cassandra-env.sh, logback.xml's <configuration>): written in a
# template piece of their own next to the variables (cassandra_config_template_snippets), not in the inventory's
# YAML: (the list they come in, the file)
SNIPPET_LISTS = ((LOGBACK_END, "logback.xml"), (ENV_END, "cassandra-env.sh"), (APPENDERS, "logback.xml:appenders"))
SNIPPETS = "cassandra_config_template_snippets"


def _vars_dir(path):
    """group_vars/x.yml, group_vars/x/y.yml -> group_vars/x (the group or host they are for)."""
    parts = path.split("/")
    return parts[0] + "/" + (os.path.splitext(parts[1])[0] if len(parts) == 2 else parts[1])


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
        "cassandra_java_home", "cassandra_java_link", "cassandra_java_package", "cassandra_java_tarball", "cassandra_java_tarball_checksum",
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
        "cassandra_heap_size", "cassandra_heap_newsize", "cassandra_heap_in_jvm_options",
        "cassandra_max_direct_memory_size", "cassandra_jvm_gc", "cassandra_jvm_max_gc_pause_millis",
        "cassandra_jvm_g1_heap_region_size", "cassandra_jvm_g1_new_size_percent",
        "cassandra_jvm_g1_rset_updating_pause_time_percent",
        "cassandra_jvm_initiating_heap_occupancy_percent", "cassandra_jvm_cms_initiating_occupancy_fraction",
        "cassandra_jvm_max_tenuring_threshold", "cassandra_jvm_parallel_gc_threads", "cassandra_jvm_conc_gc_threads",
        "cassandra_jvm_extra_options", "cassandra_jvm8_extra_options", "cassandra_jvm11_extra_options",
        "cassandra_jvm17_extra_options"]),
    ("Prometheus exporter (its Java agent)", [
        "cassandra_prometheus_exporter_enabled", "cassandra_prometheus_exporter_version",
        "cassandra_prometheus_exporter_checksum", "cassandra_prometheus_exporter_dir", "cassandra_prometheus_exporter_port",
        "cassandra_prometheus_exporter_address", "cassandra_prometheus_exporter_config_file"]),
    ("Other cassandra.yaml settings", []),
    ("cassandra.yaml settings no variable covers", ["cassandra_extra_settings"]),
    ("Logging (logback.xml)", [
        "cassandra_log_level", "cassandra_log_level_cassandra", "cassandra_debug_log_enabled", "cassandra_log_console",
        "cassandra_log_audit_file", "cassandra_log_audit_dir", "cassandra_log_system_dir", "cassandra_log_debug_dir",
        "cassandra_log_compress", "cassandra_log_audit_compress",
        "cassandra_log_jmx_configurator"]),
    ("Template pieces next to the inventory (hand edits the import carried)", [SNIPPETS]),
    ("Secrets of the lines above (hand edits the import carried)", []),
    ("OS tuning (found on the nodes, for the nodes added later)", [
        "cassandra_linux_sysctl_file", "cassandra_linux_sysctl", "cassandra_linux_limits", "cassandra_linux_nproc_all",
        "cassandra_linux_swap_off", "cassandra_linux_thp_enabled", "cassandra_linux_thp_defrag",
        "cassandra_data_readahead_kb", "cassandra_data_scheduler", "cassandra_data_lvm_readahead",
        "cassandra_linux_timesync", "cassandra_linux_timesync_servers"]),
    ("systemd unit & service", [
        "cassandra_service_restart", "cassandra_service_environment", "cassandra_service_limit_nofile",
        "cassandra_service_limit_nproc", "cassandra_service_limit_memlock", "cassandra_service_limit_as"]),
    ("Medusa", [
        "cassandra_medusa_version", "cassandra_medusa_venv", "cassandra_medusa_link_dir", "cassandra_medusa_profile_d",
        "cassandra_medusa_python", "cassandra_medusa_storage_provider", "cassandra_medusa_bucket_name",
        "cassandra_medusa_region", "cassandra_medusa_host", "cassandra_medusa_port", "cassandra_medusa_base_path",
        "cassandra_medusa_prefix", "cassandra_medusa_key_file", "cassandra_medusa_fqdn"]),
    ("Kept by hand (import_keep_hand_edits): the roles leave these parts as the node has them", [KEPT_PARTS_VAR]),
    ("Left as it is on this node (set up another way)", [k for k in KEEP if k not in SET_UP_BY_THE_ROLES] + [IMPORTED]),
    ("Left as it is on this node (set up by the roles)", list(SET_UP_BY_THE_ROLES)),
    ("Other", []),
]
BLOCK_PATTERNS = [
    (re.compile(r"^%s" % SECRET_VAR), "Secrets of the lines above (hand edits the import carried)"),
    (re.compile(r"^cassandra_medusa_"), "Medusa"),
    (re.compile(r"^cassandra_prometheus_exporter_"), "Prometheus exporter (its Java agent)"),
    (re.compile(r"^cassandra_service_"), "systemd unit & service"),
    (re.compile(r"^cassandra_jmx_"), "JMX"),
    (re.compile(r"^cassandra_java_"), "Versions & packages"),
    (re.compile(r"^cassandra_(?:jvm\d*|heap)_"), "JVM & heap (cassandra-env.sh, jvm*-server.options)"),
    (re.compile(r"^cassandra_\w+_(?:dir|directory|directories)$"), "Directories"),
    (re.compile(r"^cassandra_\w+_(?:address|port)$"), "Network & ports"),
]
# Values a role writes in their order (a file written whole, the audit logger's parameters): their mappings not sorted
KEEP_ORDER = ("cassandra_linux_limits", "cassandra_audit_logger_parameters")
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


try:  # ansible-core 2.19+ templates trusted strings only: a filter's output is used as it is
    from ansible.template import trust_as_template  # noqa: F401 pylint: disable=unused-import

    def _as_is(value):
        return value
except ImportError:  # before, any string not marked unsafe is templated again where it is used (a loop's item)
    from ansible.utils.unsafe_proxy import wrap_var as _as_is


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


def _vars_yaml(variables, templates=None, trusted=(), given=()):
    """variables as YAML, grouped by subject (BLOCKS): a comment line per block,
    a blank line between blocks. Each key is dumped on its own, the same way
    a whole dict would be (same values and quoting). templates: {key: the
    import's template of the nodes' names} written as a template; trusted: see _unsafe; given: the variables your
    files set (no note asks for one of them)."""
    data = _unsafe(json.loads(json.dumps(variables, default=str)), frozenset(trusted))
    for key, template in (templates or {}).items():
        if variables.get(key) == template:
            data[key] = _plain(template)
    # a mirror URL that names the series (cassandra_repository_import): written as a template, not !unsafe
    url = data.get("cassandra_install_url")
    if isinstance(url, str) and not isinstance(url, Unsafe) and SERIES_TOKEN in url:
        data["cassandra_install_url"] = _plain(url.replace(SERIES_TOKEN, "{{ cassandra_version }}"))
    grouped = {}
    for key in data:
        grouped.setdefault(_block(key), []).append(key)
    out = []
    for title, order in BLOCKS:
        keys = grouped.get(title)
        if not keys:
            continue
        keys.sort(key=lambda k: (order.index(k), "") if k in order else (len(order), k))
        dumped = [yaml.dump({k: data[k]}, Dumper=Dumper, default_flow_style=False, sort_keys=k not in KEEP_ORDER)
                  + _note(k, data[k], given) for k in keys]
        out.append("# %s\n" % title + "".join(dumped))
    return "\n".join(out)


# What the nodes can't tell, written under the variable that needs it: (the variable it asks for, the note)
NOTES = {
    ("cassandra_install_method", "packages"): ("cassandra_install_url", (
        "# TODO: cassandra_install_url: the directory of the package files, for new nodes (not read from the nodes)\n")),
}


def _note(key, value, given=()):
    """The note under key's value, '' when none or when your files set the variable it asks for (given)."""
    asked, note = NOTES.get((key, value), (None, "")) if isinstance(value, str) else (None, "")
    return "" if asked in given else note


def _split_secrets(variables, trusted=()):
    public, secrets = {}, {}
    for key, value in variables.items():
        (secrets if _list_secret(key, value, trusted) else public)[key] = value
    return public, secrets


@values_hidden
def cassandra_inventory_files(layout):
    """layout: cassandra_inventory_layout's -> [{path, content, secret}] for group_vars/<group>/ and
    host_vars/<host>/: main.yml, and secrets.yml for variables named like
    passwords (and extra settings holding one); then the snippets (<file>.snippet.j2).
    e.g. -> [{"path": "group_vars/prod/main.yml", "content": "<its YAML>", "secret": False},
    {"path": "group_vars/prod/secrets.yml", "content": "<its YAML>", "secret": True}, ...]."""
    layout = _plain(layout)
    files = []
    for kind in ("group_vars", "host_vars"):
        for name, variables in sorted(layout[kind].items()):
            public, secrets = _split_secrets(variables, frozenset(layout.get("trusted") or ()))
            for base, data, secret in (("main.yml", public, False), ("secrets.yml", secrets, True)):
                if data:
                    files.append({"path": "%s/%s/%s" % (kind, name, base), "secret": secret,
                                  "content": _as_is(_vars_yaml(data, layout.get("templates"), layout.get("trusted") or (),
                                                               layout.get("user_keys") or ()))})
    for path, text in sorted((layout.get("snippets") or {}).items()):
        files.append({"path": path, "secret": False, "content": _as_is(text)})
    return files


# First line of every file the import writes: a re-import replaces or removes
# only the files that start with it (and the vaulted secrets.yml next to one),
# and only those of its own cluster, which the line names
GENERATED = "# Written by community.cassandra.import_cluster for %s: the next import replaces it. Your own settings: other files."
# the line of the releases before it named the cluster
GENERATED_UNNAMED = "# Written by community.cassandra.import_cluster: the next import replaces it. Your own settings: other files."
# what both start with: a file an import wrote
IMPORT_MARK = os.path.commonprefix([GENERATED, GENERATED_UNNAMED])
# (a template's: in a Jinja comment, {# ... #})
_HEADER = re.compile(r"\{?# Written by community[.]cassandra[.]import_cluster(?: for ([^\s:]+))?: ")
_OWNABLE = re.compile(r"^(?:group_vars|host_vars)/[^/]+/(?:main\.yml|secrets\.yml|[^/]+\.j2)$")


def cassandra_inventory_generated(content, cluster, path=""):
    """content, cluster (its group), path: the file's (a template, .j2: the line in a Jinja comment, which renders
    nothing) -> content, marked as written by the import of the cluster (GENERATED first).
    e.g. ("x: 1\n", "prod") -> "# Written by community.cassandra.import_cluster for prod: ...\nx: 1\n"."""
    # the templates it holds (a URL's {{ cassandra_version }}, a node's address) written as they are
    if str(path).endswith(".j2"):
        return _as_is("{%s #}\n%s" % (GENERATED % cluster, content))
    return _as_is(GENERATED % cluster + "\n" + content)


def written_for(text):
    """The cluster group named by the import's first line of text, '' when the
    line names none (an earlier release), None when text is not the import's."""
    match = _HEADER.match(text or "")
    return (match.group(1) or "") if match else None


def leading_comments(text):
    """text -> the lines of text before its first line of data (comments, blank lines, ---): what tells an earlier
    import's file without its first line, and shows no value. e.g. "# a\n\nx: 1" -> "# a\n"."""
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
    except Exception:  # pylint: disable=broad-except  # a wrong password, a damaged file: not decrypted
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


def _file_header(path, text, password, old_layout):
    """The cluster a file's first line names (a vaulted one: once decrypted with password), "" for an earlier
    release's file without that line laid out as the import writes it (its path added to old_layout), None for
    none -> the cluster's name, "" or None."""
    if text is not None and text.startswith("$ANSIBLE_VAULT"):  # the import's only if it decrypts to its line
        text = _decrypt(text, password)
    name = written_for(text)
    if name is None and text is not None and _OWNABLE.match(path) and _vars_dir(path) != "group_vars/all" \
            and old_import(text):
        old_layout.add(path)
        return ""  # an earlier release's, without its first line
    return name


def _inventory_files(headers, found, cluster, adopt, old_layout):
    """The inventory files of the dir: the clusters' hosts files (the import's) and the user's own -> (clusters:
    {path: (cluster, its names)}, own: {path: its names}, conflicts)."""
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
    return clusters, own, conflicts


def _same_group_conflicts(found, headers, cluster, cluster_name):
    """The same group for another cluster (names that differ only by case or punctuation): the cluster name of
    group_vars/<cluster group>/main.yml (the import's or no one's) -> [conflict]."""
    previous = "group_vars/%s/main.yml" % cluster
    if cluster_name is None or found.get(previous) is None or headers[previous] not in (cluster, "", None):
        return []
    try:
        data = yaml.load(found[previous], Loader=_TagsLoader)  # nosec B506: the loader builds no object
    except yaml.YAMLError:
        data = None
    theirs = data.get("cassandra_cluster_name") if isinstance(data, dict) else None
    templated = not isinstance(theirs, _Unsafe) and re.search(r"\{[{%]", str(theirs))  # the user's: not told
    if theirs is not None and str(theirs) != str(cluster_name) and not templated:
        return ["%s: cluster '%s', not '%s' (two clusters, one group %s)" % (previous, theirs, cluster_name, cluster)]
    return []


# What tells whose a file of the inventory dir is: its first line's cluster (headers), the clusters' hosts files,
# the files laid out as an earlier release wrote them, this cluster's group, the paths this import writes, adopt,
# the groups and hosts it writes (new_names)
_Owners = namedtuple("_Owners", "headers clusters old_layout cluster written adopt new_names")


def _named_by(owners, path, new=False):
    """The clusters whose hosts file names the group or host of a group_vars or host_vars path (new: the
    hosts this import writes count as this cluster's hosts file)."""
    parts = path.split("/")
    if len(parts) < 2 or parts[0] not in ("group_vars", "host_vars"):
        return set()
    item = os.path.splitext(parts[1])[0] if len(parts) == 2 else parts[1]  # group_vars/<group>.yml too
    pick = 0 if parts[0] == "group_vars" else 1
    return set(c for c, names in ([(owners.cluster, owners.new_names)] if new else []) + list(owners.clusters.values())
               if item in names[pick])


def _owner(owners, path):
    """Whose a file is: a cluster's name, "" when not known (an earlier import's), None for no import's."""
    name = owners.headers[path]
    if name or name is None:
        return name
    if path in owners.clusters:  # a hosts file
        return owners.clusters[path][0]
    if "/" not in path:
        return owners.cluster if path in owners.written else ""
    found = _named_by(owners, path)
    if not found and path in owners.old_layout:  # from before the first line (one cluster a dir): the hosts written
        found = _named_by(owners, path, new=True)
    if not found and owners.adopt and (path in owners.written or _named_by(owners, path, new=True)):
        return owners.cluster
    return found.pop() if len(found) == 1 else ""


def _by_owner(owners, paths):
    """The files sorted by whose they are -> (stale, kept, unsure, conflicts: the paths this import writes over
    another's, adoptable: those of them adopt solves)."""
    cluster, written = owners.cluster, owners.written
    stale, kept, unsure, conflicts, adoptable = [], [], [], [], []
    for path in sorted(paths):
        who = _owner(owners, path)
        if path in written:
            if who not in (None, cluster):
                conflicts.append("%s: written for %s" % (path, who) if who
                                 else "%s: written by an earlier import, not known to be %s's" % (path, cluster))
                if not who and not _named_by(owners, path):  # (named by several clusters' hosts files: not solved)
                    adoptable.append(conflicts[-1])
        elif who is None:
            if not _named_by(owners, path) - set([cluster]):  # not in another cluster's group_vars or host_vars
                kept.append(path)
        elif who == cluster:
            (stale if _OWNABLE.match(path) else kept).append(path)
        elif who == "":
            unsure.append(path)
    return stale, kept, unsure, conflicts, adoptable


def _shared_names(inventory, clusters, own, found, cluster):
    """The groups and hosts this import writes that another cluster's hosts file or a file of the user's has too ->
    (conflicts, adoptable: those of this cluster's own hosts file of no import)."""
    conflicts, adoptable = [], []
    groups, hosts = _inventory_names(inventory)
    for path, (who, (their_groups, their_hosts)) in sorted(clusters.items()):
        if who != cluster:
            conflicts += ["%s: in %s too" % (name, path) for name in sorted((groups & their_groups) | (hosts & their_hosts))]
    for path, (their_groups, dummy) in sorted(own.items()):  # a host may be in groups of the user's too
        conflicts += ["group %s: in %s too" % (name, path) for name in sorted(groups & their_groups)]
        if path == cluster + ".yml" and _clusters_groups(found[path] or "", cluster):  # its hosts file
            adoptable += conflicts[len(conflicts) - len(groups & their_groups):]
    return conflicts, adoptable


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
    backup. adopt (import_adopt): the files of an earlier import not
    known to be this cluster's, and a <cluster group>.yml of no import, at
    the paths it writes or in its groups' and hosts' dirs, are this
    cluster's (the replaced ones kept as <file>.<date>~); 'adoptable': the
    conflicts it would solve; 'backup': the stale files to keep as
    <file>.<date>~ when removed (known by their layout only, or adopted).
    e.g. -> {"stale": ["host_vars/db9/main.yml"], "kept": ["group_vars/all.yml"], "replaced": [], "unsure": [],
    "conflicts": [], "adoptable": [], "backup": []}."""
    found = dict((p, read.get(p)) for p in paths)
    old_layout = set()
    headers = dict((p, _file_header(p, found.get(p), password, old_layout)) for p in found)
    clusters, own, conflicts = _inventory_files(headers, found, cluster, adopt, old_layout)
    conflicts += _same_group_conflicts(found, headers, cluster, cluster_name)
    new_names = _inventory_names(inventory) if inventory else (set(), set())
    owners = _Owners(headers, clusters, old_layout, cluster, written, adopt, new_names)
    stale, kept, unsure, more, adoptable = _by_owner(owners, found)
    conflicts += more
    if inventory:
        more, more_adoptable = _shared_names(inventory, clusters, own, found, cluster)
        conflicts += more
        adoptable += more_adoptable
    replaced = sorted(p for p in found if p in written and (headers[p] is None or p in old_layout
                                                            or adopt and headers[p] == ""))
    return {"stale": stale, "kept": kept, "replaced": replaced, "unsure": unsure, "conflicts": conflicts,
            "adoptable": adoptable, "backup": sorted(p for p in stale if p in old_layout or adopt and headers[p] == "")}


def cassandra_inventory_same_secret(existing, content, password):
    """existing (a file's text), content, password -> True when existing, a vaulted file, decrypts with password to
    content: written again, it would only change its encryption. e.g. ("$ANSIBLE_VAULT;1.1;AES256...", "x: 1\n",
    "secret") -> True."""
    if not existing or not str(existing).startswith("$ANSIBLE_VAULT"):
        return False
    return _decrypt(existing, password) == content


@values_hidden
def cassandra_inventory_own_values(read, cluster_group, keys, password=""):
    """read: {path: text} of the vars files of the inventory dir -> {key: value} of keys as an earlier import of
    this cluster wrote them in its own files of the cluster group (group_vars/<cluster group>/main.yml and
    secrets.yml, with the import's first line naming the group; a vaulted one decrypted with password). What a
    re-import keeps when nothing else gives it (the CQL login: no node gives it).
    e.g. (read, "prod", ["cassandra_cql_username", "cassandra_cql_password"]) -> {"cassandra_cql_username": "admin",
    "cassandra_cql_password": "..."}."""
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
    with password; one that can't be read may set it). What was chosen then (import_set_ansible_host:
    false, or ansible_host taken out by hand) stays; a node new to it gets the import's own choice (its name
    may not be known to the controller).
    read: {path: text} of the inventory dir; hosts_file: the cluster's hosts file there (<cluster group>.yml);
    names: {address: the node's name there} (the layout's). For a
    re-import that is not given import_set_ansible_host.
    e.g. [{"name": "db1", "ansible_host": "10.0.0.1", ...}] -> [{"name": "db1", "ansible_host": "", ...}] when the
    hosts file there gives db1 no ansible_host."""
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
