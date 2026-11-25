# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The import (import_cluster): a node's config files -> cassandra_config variables.

cassandra_config_import puts the other import modules together: cassandra.yaml (first the settings a node keeps
for life), cassandra-env.sh, the JVM options files, JMX, logback.xml, the exporter, and what the role would still
change (hand edits, normalized lines).

Filters (plugins/filter/cassandra_import.py names the filters):
cassandra_config_import: a node's config files -> cassandra_config variables,
    plus what the role would still change (hand edits / normalized lines).
cassandra_unit_environment: systemctl's Environment of a unit -> dict.
cassandra_config_ignored_vars: variable names, series -> the cassandra_config
    variables among them that series' templates don't use (e.g. 4.0 names after
    an upgrade to 4.1).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import re
import shlex

import yaml

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.filter import cassandra_jvm_args as jvm_args
from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import (
    LATER_STOCK, QUANTITY_KEYS, canonical_quantity, former_names, former_value, without_later_stock, yaml_rejected)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_jvm_options as jvm
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_read import (
    ALWAYS, APPENDERS, as_text, AUDIT_PARAMETERS, branch_not_run_note, BY_PATH, role_default, earlier_env,
    earlier_env_not_shown, END_KEYS, env_cmdline, extra_settings, G1_TUNING, HEAP_OPTIONS, IDENTITY, IDENTITY_OPTIONAL,
    import_file, INTEGERS, leftovers, load_role, merge, NOT_RUNNING_OR_CHANGED, parameter_patterns, properties, RACKDC,
    render_template, same_value, set_parameters, snitch_reads_rackdc, STORAGE_DIRS, top_blocks, without_file_hunks)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import (
    ADDRESSES, error_without_message, HOSTNAME, IPV4, ROLE, SERIES, undefined_name)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_files import (
    keep_files_option, move_secrets)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_jvm import (
    audit_file_default, cassandra_jmx_files, heap_in_options, JMX_FILE_DEFAULTS, jmx_leftovers, jmx_users,
    jvm_moves, jvm_options, logback, logback_carried)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_exporter import (
    exporter_import, exporter_rewritten)


# the layout note of the heap of an options file the role writes in jvm-server.options (the report reads it)
HEAP_MOVED = "moved to jvm-server.options (cassandra_heap_in_jvm_options), the same for the JVM"


def cassandra_unit_environment(text):
    """systemctl show -p Environment --value -> {name: value} (quoted entries kept whole).
    e.g. 'A=1 "B=x y"' -> {"A": "1", "B": "x y"}."""
    try:
        words = shlex.split(text or "")
    except ValueError:
        words = (text or "").split()
    return dict(w.split("=", 1) for w in words if "=" in w)


class _Reading(object):
    """What cassandra_config_import is reading (what: a file, a part of the work): an unexpected error names it in its
    message, which shows nothing else of the node's files. _config_import sets it before each step."""

    def __init__(self, what):
        self.what = what


def cassandra_config_import(live_files, cassandra_version, facts, conf_target="", storage_dir="", jmx_files=None,
                            release="", java="", cmdline="", running_jvm=None):
    """cassandra_config variables that render a node's files, and what the role would still change.
    live_files: {file name: text} of the node's config files; facts: the node's.
    conf_target: the resolved dir the node reads its config from; storage_dir: the JVM's -Dcassandra.storagedir;
    jmx_files: cassandra_jmx_files' (the remote JMX files the JVM reads, under the names jmxremote.password and
    .access in live_files); release: the node's Cassandra version (4.1.5; '': the keys no release of the series
    accepts are the ones rejected); java: the running Java's major ('': not known), for the options files it reads;
    cmdline: the running JVM's arguments (/proc/<pid>/cmdline), for its heap (java and cmdline: given by the tests;
    the playbook gives them in running_jvm); running_jvm: the running JVM, {cmdline:
    its /proc/<pid>/cmdline, java: its major version, environment: what the unit gives it, newer: the config files
    written since it started, keep: import_keep_hand_edits, the JVM's files it keeps not moved} (java and cmdline
    from it when not given).
    Returns {
    - vars: the variables;
    - hand_edits: what the role would still change in the node's files (lines no variable gives, with notes);
    - normalized: what the role writes another way with the same effect (layout, the heap's file, comments);
    - comments: comment lines only, which set nothing;
    - not_settings: {keys: the cassandra.yaml keys its Cassandra rejects, not imported, release};
    - release: the version they were judged by;
    - trusted: its lines (of the variables) that refer to a secret's variable, written as templates;
    - carried: the notes of the lines carried into the inventory (a file's lines at its end, your appenders, the
      secrets moved to variables of their own);
    - jvm_notes; jvm_warnings: options worth a look (why, the way out);
    - moved: hand edits of cassandra-env.sh moved to variables; rewritten: the files the role then writes in its
      own layout, the JVM getting the same arguments; cleanup: options the running JVM does not use; not_moved:
      why cassandra-env.sh's JVM_OPTS lines or heap were not moved ('' if moved or none)}.
    e.g. cassandra_config_import({"cassandra.yaml": text, ...}, "41x", ansible_facts)
    -> {"vars": {"cassandra_cluster_name": "Prod", ...}, "hand_edits": [], "normalized": [], ...}."""
    if cassandra_version not in SERIES:
        raise AnsibleFilterError("cassandra_config_import: unsupported series %s" % cassandra_version)
    reading = _Reading("the role defaults")
    # Java's Properties ends a line at \r\n, \r or \n (the other files: as their programs read them)
    live_files = dict((k, re.sub(r"\r\n?", "\n", v) if k.endswith(".properties") and isinstance(v, str) else v)
                      for k, v in live_files.items())
    try:
        return _config_import(live_files, cassandra_version, facts, reading, conf_target, storage_dir, jmx_files,
                              release, java, cmdline, running_jvm)
    except AnsibleFilterError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        if undefined_name(exc):
            raise
        error = error_without_message("cassandra_config_import", reading.what)
    raise error  # out of the except block: no chained message either


def _audit_file(found, changed):
    """cassandra_log_audit_file in changed (in place) only when the node's is not its default there: on with
    FileAuditLogger (cassandra_audit_logging_enabled, cassandra_audit_logger_class_name as found)."""
    if "cassandra_log_audit_file" not in found:
        return
    derived = audit_file_default(found)
    changed.pop("cassandra_log_audit_file", None)
    if bool(found["cassandra_log_audit_file"]) != derived:
        changed["cassandra_log_audit_file"] = bool(found["cassandra_log_audit_file"])


def _changed_values(found, ctx, from_shell):
    """The values found that are not the role defaults and are values for their variable -> {variable: value}."""
    return {k: v for k, v in found.items()
            if not same_value(v, role_default(ctx, k))
            and not (v == "" and role_default(ctx, k) in ([], {}))  # a list/dict variable left empty
            # a number the node does not give as one (e.g. JMX_PORT="${JMX_PORT:-7299}"): not a value
            # for the variable, left to the report (the role would write the default)
            and not (isinstance(role_default(ctx, k), int) and not isinstance(role_default(ctx, k), bool)
                     and not isinstance(v, int) and not (v == "" and G1_TUNING.match(k)))
            # a shell expansion (CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"): not a value either,
            # unless the variable is one (cassandra_heap_dump_dir: $CASSANDRA_LOG_DIR, on 4.x too)
            and not (k in from_shell and isinstance(v, str) and re.search(r"[$`]", v)
                     and not re.search(r"[$`]", str(role_default(ctx, k))) and k != "cassandra_heap_dump_dir")}


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
        if isinstance(data, dict) and data.get(key) is None and var not in changed and path != role_default(ctx, var):
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
    if dirs and dirs[0] != role_default(ctx, "cassandra_data_dir"):
        # one directory, not the default one: cassandra_data_dir, the list follows it
        return {"cassandra_data_dir": dirs[0]}, dirs
    return {}, None


def _yaml_extras(env, series, ctx, cassandra_yaml, release, found, changed):
    """cassandra.yaml's settings no variable covers: a key under its former name (a file from 4.0) to its variable
    (found and changed updated), the keys Cassandra rejects left out -> (cassandra_extra_settings, the keys
    rejected)."""
    tpl = render_template(env, series, "cassandra.yaml", ctx)[0]
    extras = extra_settings(tpl, cassandra_yaml.split("\n"))
    # 4.1+: a key under its former name (a file from 4.0): its variable, which writes it under its new name
    live_keys = set(key for key, dummy in top_blocks(cassandra_yaml.split("\n")))
    for key, var in former_names(SERIES[series]).items():
        value = former_value(key, extras[key]) if key in extras else None
        if value is not None and var[len("cassandra_"):] not in live_keys:
            del extras[key]
            found[var] = canonical_quantity(var[len("cassandra_"):], value)
            if not same_value(found[var], role_default(ctx, var)):
                changed[var] = found[var]
    # a key Cassandra rejects (e.g. added by hand since it started): not imported, the node would not restart
    rejected = yaml_rejected(extras, release)
    return dict((k, v) for k, v in extras.items() if str(k) not in rejected), rejected


# a node whose running JVM runs local JMX and reads neither JMX file (jvm_args.jmx_files_unread)
JMX_FILES_UNREAD_NOTE = ("jmxremote.* users not imported: this JVM runs local JMX and reads neither file; the files are"
                         " left as they are")


def _jmx_import(env, live_files, jmx_files, running=None):
    """The remote JMX users: the password and access files the JVM reads (jmx_files: cassandra_jmx_files', read
    under the names jmxremote.password and .access; None: cassandra-env.sh's), their paths kept for the role to
    write them there; the users only when the JVM reads both files and each user's rights are in the access file,
    not when the running JVM reads neither (running: {cmdline, environment, newer}, jvm_args.jmx_files_unread)
    -> (jmx_files completed, {variable: value}, notes, notes of what has no effect)."""
    if jmx_files is None:  # not read by the caller: cassandra-env.sh's
        jmx_files = cassandra_jmx_files(live_files.get("cassandra-env.sh", ""),
                                        yaml_text=live_files.get("cassandra.yaml", ""))
    jmx_files = dict({"password": "", "access": "", "from": "", "unknown": "", "jaas": ""}, **jmx_files)
    changed = {}
    for kind in ("password", "access"):
        if jmx_files[kind] and jmx_files[kind] != JMX_FILE_DEFAULTS[kind]:
            changed["cassandra_jmx_remote_%s_file" % kind] = jmx_files[kind]
    jmx_note = []
    if jmx_files["jaas"]:
        jmx_note = ["JMX authentication by JAAS (%s): NOT imported, the role writes the JDK's password and access"
                    " files only: keep cassandra-env.sh as it is (import_keep_hand_edits=true) and give the"
                    " playbooks a JMX login (cassandra_jmx_username, cassandra_jmx_password_file)" % jmx_files["jaas"]]
    if jmx_files["unknown"]:  # its files not read: no user taken from the role's paths
        jmx_note.append("cassandra-env.sh gives the JMX files through a variable it does not set (%s): their users"
                        " NOT imported, set cassandra_jmx_remote_password_file, _access_file and cassandra_jmx_users"
                        " by hand" % jmx_files["unknown"])
    elif live_files.get("jmxremote.password") and jvm_args.jmx_files_unread(
            live_files.get(jvm_args.ENV, ""), env_cmdline(running), (running or {}).get("environment")):
        return jmx_files, changed, jmx_note, [JMX_FILES_UNREAD_NOTE]
    elif live_files.get("jmxremote.password"):
        users = jmx_users(live_files["jmxremote.password"], live_files.get("jmxremote.access"))
        if users and "jmxremote.access" in live_files and jmx_files["access"]:
            changed["cassandra_jmx_users"] = users
            jmx_note += jmx_leftovers(env, users, live_files)
        else:
            jmx_note.append("jmxremote.password (%s): its users NOT imported (no user with rights the role"
                            " writes in the access file the JVM reads%s): set"
                            " cassandra_jmx_users by hand" % (
                                jmx_files["password"] or JMX_FILE_DEFAULTS["password"],
                                (", " + jmx_files["access"]) if jmx_files["access"] else ", none given to the JVM"))
    return jmx_files, changed, jmx_note, []


def _inventory_vars(changed, found, ctx, live_files, facts, conf_target, series):
    """The variables the inventory gets: changed, rackdc's dc= and rack= when the snitch does not read them, the
    ones a node keeps for life, the RPM conf dir, the seeds as a list, the node's own address or name as the fact
    that gives it -> {variable: value}."""
    out = dict((k, v) for k, v in changed.items() if k not in RACKDC)
    if "cassandra-rackdc.properties" in live_files and not snitch_reads_rackdc(found.get("cassandra_endpoint_snitch")):
        # the file's dc= and rack= as the node has them: its snitch doesn't read them, its dc and rack
        # (the ring's) may differ (the layout drops them where they don't; a line the file lacks: the
        # role writes the ring's). Else they are the ring's.
        node_kv = properties(live_files["cassandra-rackdc.properties"].split("\n"))
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


def _config_import(live_files, cassandra_version, facts, reading, conf_target="", storage_dir="", jmx_files=None,
                   release="", java="", cmdline="", running=None):
    """cassandra_config_import's work (reading.what: the part it is reading, for its errors) -> its result."""
    running = dict(running or {})
    java, cmdline = java or running.get("java") or "", cmdline or running.get("cmdline") or ""
    env, ctx, files = load_role(cassandra_version, facts)
    # the Prometheus exporter's agent: its variables; the node read as without it from here on
    reading.what = "the Prometheus exporter's agent"
    live_as_found = live_files
    keep = keep_files_option(running.get("keep", False))
    live_files, cmdline, exporter = _exporter_taken_out(live_files, cmdline, ctx, running, keep, conf_target,
                                                        cassandra_version, java)
    running["cmdline"] = cmdline
    release, rejected = str(release or SERIES[cassandra_version]), []
    live_files, read_as_notes = _read_as_the_role(env, cassandra_version, ctx, live_files, running, keep)
    found, from_shell = {}, set()
    reading.what = "cassandra-env.sh"
    earlier = earlier_env(cassandra_version, live_files[jvm_args.ENV], "\n".join(
        render_template(env, cassandra_version, jvm_args.ENV, ctx)[1]), facts) if jvm_args.ENV in live_files else None
    for name in files:  # (in _config_import itself: an error raised below it, outside these files, names this function)
        if name in live_files:
            reading.what = name
            got = earlier["found"] if earlier and name == jvm_args.ENV else \
                import_file(env, cassandra_version, name, ctx, live_files[name].split("\n"))
            merge(found, got)
            if name.endswith(".sh"):
                from_shell.update(got)
    if "logback.xml" in live_files:
        reading.what = "logback.xml"
        found, logback_note = _logback_found(env, cassandra_version, ctx, found, live_files["logback.xml"])
    else:
        logback_note = []
    reading.what = "the JVM options files"
    jvm_notes, jvm_warnings = jvm_options(env, cassandra_version, ctx, found, str(java or ""))
    heap_moved = _heap_moved(env, cassandra_version, ctx, found, java, cmdline, running, keep, live_files)
    jvm_warnings += _later_stock_warnings(env, cassandra_version, ctx, files, live_files)
    reading.what = "cassandra.yaml"
    changed, render_dirs, rejected = _yaml_changed(env, cassandra_version, ctx, live_files, found, from_shell,
                                                   storage_dir, release)
    reading.what = "jmxremote.password"
    jmx_files, jmx_vars, jmx_note, jmx_unread_note = _jmx_import(env, live_files, jmx_files, running)
    changed.update(jmx_vars)
    reading.what = "comparing the files with the role's"
    render = _render_values(ctx, changed, found, live_files, render_dirs)
    reading.what = "the JVM options"
    moved = jvm_moves(env, cassandra_version, files, ctx, render, live_files, dict(running, jaas=jmx_files["jaas"]),
                      earlier["text"] if earlier else None)
    _take_moved(moved["vars"], ctx, render, changed)
    reading.what = "comparing the files with the role's"
    # (the heap shown the same from the running JVM's arguments wherever its file has it: not a hand edit there)
    hand, normalized, comments = leftovers(env, cassandra_version, files, render, live_files, rejected,
                                           moved["shown"], HEAP_OPTIONS if moved["shown"] else (),
                                           running.get("environment"), env_cmdline(running))
    if earlier:
        hand = _earlier_env_said(earlier, cassandra_version, moved, cmdline, render, hand, normalized, jvm_warnings)
    normalized += read_as_notes + jmx_unread_note + _jvm_normalized(heap_moved, moved, earlier)
    changed.update(exporter["vars"])
    rewritten = moved["rewritten"] + [f for f in exporter_rewritten(env, cassandra_version, render, exporter,
                                                                    live_as_found) if f not in moved["rewritten"]]
    hand += logback_note + jmx_note
    out, trusted, secrets = move_secrets(_inventory_vars(changed, found, ctx, live_files, facts, conf_target,
                                                         cassandra_version))
    return {"vars": out, "hand_edits": hand, "normalized": normalized, "comments": comments,
            "not_settings": {"keys": rejected, "release": release} if rejected else {}, "release": release,
            "trusted": trusted, "carried": _carried_lines(changed) + secrets,
            "jvm_notes": jvm_notes + exporter["notes"], "jvm_warnings": jvm_warnings,
            "moved": moved["moved"] + exporter["moved"], "rewritten": rewritten, "cleanup": moved["cleanup"],
            "not_moved": moved["not_moved"]}


def _exporter_taken_out(live_files, cmdline, ctx, running, keep, conf_target, cassandra_version, java):
    """The node's files and the JVM's arguments without the Prometheus exporter's agent (exporter_import), but when
    the file that gives it is kept as it is (import_keep_hand_edits) -> (live_files, cmdline, exporter)."""
    as_read = (live_files, cmdline)
    live_files, cmdline, exporter = exporter_import(
        live_files, cmdline, ctx, running.get("exporter"), [ctx.get("cassandra_conf_dir"), conf_target],
        jvm.read_files_known(cassandra_version, java))
    if exporter["found"] and (keep is True or exporter["found"]["file"] in keep or jvm_args.COMMON in keep):
        # the agent's file kept as it is (import_keep_hand_edits): it stays there, the role writes none (nor
        # could it in a kept jvm-server.options, where it writes it: cassandra_config refuses that)
        live_files, cmdline = as_read
        agent_file = exporter["found"]["file"]
        why = ("that file is kept as it is (import_keep_hand_edits) and gives it as it does"
               if keep is True or agent_file in keep else "jvm-server.options, where the role writes it, is kept as it"
               " is (import_keep_hand_edits); %s gives it as it does" % agent_file)
        exporter = {"vars": {}, "moved": [], "found": None,
                    "notes": ["%s: the Prometheus exporter's agent NOT imported: %s" % (agent_file, why)]}
    return live_files, cmdline, exporter


def _read_as_the_role(env, cassandra_version, ctx, live_files, running, keep):
    """The node's files with what the role writes otherwise for the same effect read as the role's: a plain
    CASSANDRA_HEAPDUMP_DIR, cassandra-env.sh's lines in the JMX branch its JVM does not take -> (live_files, [the
    notes that say so])."""
    heap_dump_note = []
    if cassandra_version in ("40x", "41x") and "cassandra-env.sh" in live_files:
        text, folder = jvm_args.heap_dump_dir_as_role(live_files["cassandra-env.sh"], running.get("environment"),
                                                      running.get("cmdline"), running.get("heapdump_before"))
        if folder:  # (the same dir as the role's line, the JVM's environment giving none)
            live_files = dict(live_files, **{"cassandra-env.sh": text})
            heap_dump_note = ["cassandra-env.sh: CASSANDRA_HEAPDUMP_DIR=%s read as cassandra_heap_dump_dir (the running"
                              " JVM's environment has no CASSANDRA_HEAPDUMP_DIR: the same dir)" % folder]
    branch_note = []
    if env_cmdline(running) and jvm_args.ENV in live_files and not (keep is True or jvm_args.ENV in keep):
        # the node's lines in the JMX branch its JVM does not take: no effect, not read (the role's: stock)
        text, count, runs = jvm_args.without_jmx_branch_not_run(
            live_files[jvm_args.ENV], "\n".join(render_template(env, cassandra_version, jvm_args.ENV, ctx)[1]),
            running.get("environment"), env_cmdline(running))
        if count:
            live_files = dict(live_files, **{jvm_args.ENV: text})
            branch_note = [branch_not_run_note(count, runs)]
    # (the log dir written as itself in logback.xml: imported as it is, cassandra_log_system_dir and
    # cassandra_log_debug_dir, so that cassandra_config writes the file the node has)
    return live_files, heap_dump_note + branch_note


def _logback_found(env, cassandra_version, ctx, found, text):
    """found with logback.xml's variables (logback: None drops one), then the ones the carried lines give
    (logback_carried) -> (found, [hand edit lines])."""
    for key, value in logback(text).items():
        if value is None:
            found.pop(key, None)
        else:
            found[key] = value
    return logback_carried(env, cassandra_version, ctx, found, text)


def _heap_moved(env, cassandra_version, ctx, found, java, cmdline, running, keep, live_files):
    """The heap of the JVM's arguments read as cassandra_config writes it, in the options files
    (heap_in_options) -> {file: [its heap options]}; {} when not worked out."""
    # (not with a JVM file kept as it is: cassandra-env.sh, jvm-server.options where the role writes it, the options
    # file the heap is in; not with a heap of cassandra-env.sh's own: a line not the stock file's that sets
    # MAX_HEAP_SIZE or HEAP_NEWSIZE (=, :=), or has an -Xms, -Xmx or -Xmn, however written)
    jvm_files = {jvm_args.ENV, jvm_args.COMMON, jvm.options_file(cassandra_version, str(java or ""))}
    stock_env = set(line.strip() for line in render_template(env, cassandra_version, jvm_args.ENV, ctx)[1])
    env_heap = [line for line in live_files.get(jvm_args.ENV, "").split("\n") if line.strip() not in stock_env
                and re.search(r"^[^#]*(\b(MAX_HEAP_SIZE|HEAP_NEWSIZE):?=|-Xm[sxn])", line)]
    if cmdline and not set(running.get("newer") or []) & {jvm_args.ENV, jvm_args.COMMON} \
            and not (keep is True or set(keep) & jvm_files) and not env_heap:
        return heap_in_options(found, cassandra_version, str(java or ""), live_files)
    return {}


def _later_stock_warnings(env, cassandra_version, ctx, files, live_files):
    """The stock lines of a later release (LATER_STOCK) a node's file predates: one warning each."""
    warnings = []
    for name in [n for n in LATER_STOCK if n in live_files and n in files]:
        for line in without_later_stock(name, "\n".join(render_template(env, cassandra_version, name, ctx)[1]), live_files[name])[1]:
            warnings.append("%s (%s): absent, kept so (%s): %s; missing because this file predates it" % (
                line, name, "cassandra_%s_removed_options" % name.split("-")[0], LATER_STOCK[name][line][0]))
    return warnings


def _yaml_changed(env, cassandra_version, ctx, live_files, found, from_shell, storage_dir, release):
    """found with cassandra.yaml's settings a node keeps for life, a size, rate or duration in one unit; then the values
    that differ from the role's, its storage and data dirs and its extra settings among them -> (changed, the data
    dirs to render the role's file with (None: the role's), the keys its Cassandra rejects)."""
    if "cassandra.yaml" in live_files:
        found.update(_identity(live_files["cassandra.yaml"], cassandra_version))
    if cassandra_version != "40x":
        # a size, rate or duration in one unit whatever the node writes it in (100000000B/s, 2s): 200MiB/s, 2000ms
        found.update((k, canonical_quantity(k[len("cassandra_"):], v)) for k, v in found.items()
                     if k[len("cassandra_"):] in QUANTITY_KEYS)
    changed = _changed_values(found, ctx, from_shell)
    _audit_file(found, changed)
    if storage_dir and "cassandra.yaml" in live_files:
        dirs, keep_storage = _storage_dirs(live_files["cassandra.yaml"], storage_dir, ctx, changed)
        found.update(dirs)
        changed.update(dirs)
        if keep_storage:
            changed["cassandra_config_storage_dir"] = keep_storage
    render_dirs, rejected = None, []
    if "cassandra.yaml" in live_files:
        dirs, render_dirs = _data_dirs(live_files["cassandra.yaml"], ctx)
        found.update(dirs)
        changed.update(dirs)
        extras, rejected = _yaml_extras(env, cassandra_version, ctx, live_files["cassandra.yaml"], release, found,
                                        changed)
        if extras:
            changed["cassandra_extra_settings"] = extras
    return changed, render_dirs, rejected


def _render_values(ctx, changed, found, live_files, render_dirs):
    """The variables the role's files are rendered with to compare them with the node's."""
    render = dict(ctx, **changed)
    if "cassandra_log_audit_file" in found:  # (its default made of other values: the node's whatever it is)
        render["cassandra_log_audit_file"] = bool(found["cassandra_log_audit_file"])
    if "cassandra.yaml" in live_files and render_dirs:
        render["cassandra_data_file_directories"] = render_dirs
    return render


def _take_moved(variables, ctx, render, changed):
    """The variables cassandra-env.sh's lines moved to (jvm_moves): rendered with, written when not the default."""
    for key, value in variables.items():
        render[key] = value
        if same_value(value, role_default(ctx, key)):
            changed.pop(key, None)
        else:
            changed[key] = value


def _earlier_env_said(earlier, cassandra_version, moved, cmdline, render, hand, normalized, jvm_warnings):
    """cassandra-env.sh the stock file of an earlier series: said once (normalized, a warning), or one hand edit line
    for the file when the JVM's arguments are not shown the same -> hand (normalized, jvm_warnings and
    moved["not_moved"] changed in place)."""
    old = SERIES[earlier["series"]].split(".")[0] + ".x"
    if jvm_args.ENV in moved["shown"]:
        normalized.append("cassandra-env.sh: the stock file of %s (kept on upgrade), its values read: cassandra_config"
                          " writes %s's, which gives the JVM the same arguments (worked out from the running JVM's)"
                          % (old, SERIES[cassandra_version]))
        if "-XX:HeapDumpPath=" not in cmdline:
            jvm_warnings.append("-XX:HeapDumpPath (cassandra-env.sh): added by apply_config: %s's sets it always, in"
                                " %s (cassandra_heap_dump_dir), %s's only with CASSANDRA_HEAPDUMP_DIR; missing because"
                                " this file predates it" % (SERIES[cassandra_version],
                                                            render.get("cassandra_heap_dump_dir"), old))
        return hand
    # one line for the file: its values are read, the JVM's arguments are what is not shown the same
    hand = without_file_hunks(hand, jvm_args.ENV) + [earlier_env_not_shown(
        earlier, cassandra_version, moved["not_moved"] or (NOT_RUNNING_OR_CHANGED if not cmdline
                                                           else "not worked out"))]
    moved["not_moved"] = ""
    return hand


def _jvm_normalized(heap_moved, moved, earlier):
    """The JVM's files' layout notes: the heap moved to jvm-server.options, cassandra-env.sh's lines at other
    places only."""
    notes = ["%s: %s %s" % (f, "/".join(options), HEAP_MOVED) for f, options in sorted(heap_moved.items())
             if f != jvm_args.COMMON]
    if jvm_args.ENV in moved["rewritten"] and not moved["moved"] and not earlier:  # its lines at other places only
        notes.append("cassandra-env.sh: lines at another place than the role writes them (e.g. in the remote JMX"
                     " branch): the same arguments for the JVM (worked out from the running JVM's), no effect")
    return notes


def _carried_lines(changed):
    """The lines carried at the end of a file (END_KEYS) and the appenders before <root>: one note each."""
    carried = ["%s: %d line(s) at its end kept with %s" % (name, len(changed[key]), key)
               for name, key in END_KEYS.items() if changed.get(key)]
    if changed.get(APPENDERS):
        carried.append("logback.xml:appenders: %d line(s) of its own appenders before <root> kept with %s"
                       % (len(changed[APPENDERS]), APPENDERS))
    return carried


def _logger_parameters(conf):
    """The audit logger's parameters (AUDIT_PARAMETERS) of cassandra.yaml read as text (as_text), their null values
    left out -> {variable: the mapping} when its list holds one mapping of keys the role writes plain and values it
    can write (parameter_patterns), else {} (left to the comparison: a hand edit; nothing when none is left)."""
    value = conf
    for step in AUDIT_PARAMETERS[1]:
        try:
            value = value[step]
        except (KeyError, IndexError, TypeError):
            return {}
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return {}  # several (Cassandra reads the first), none, or not a mapping
    plain_key, unwritable = parameter_patterns()
    parameters = set_parameters(value[0])
    if not all(isinstance(k, str) and plain_key.match(k) and isinstance(v, str) and not unwritable.search(v)
               for k, v in parameters.items()):
        return {}  # a key read otherwise written plain, a value that is a list, a mapping or on several lines
    return {AUDIT_PARAMETERS[0]: parameters} if parameters else {}


def _identity(text, series):
    """The settings a node keeps for life, from cassandra.yaml parsed as YAML, and the ones read by their path
    (BY_PATH, the audit logger's parameters). Fails, naming them, when a mandatory one isn't there."""
    try:
        conf = as_text(yaml.compose(text))
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
    found.update(_logger_parameters(conf))
    return found


# cassandra_config's vars/ that name variables by series: not a use of them
# (and the list of what the checks read, every series' names: _cassandra_config_check_vars)
VARS_BY_SERIES = ("_cassandra_config_since", "_cassandra_config_old_names", "_cassandra_config_old_names_of",
                  "_cassandra_config_check_vars")


def cassandra_config_ignored_vars(names, cassandra_version):
    """names: variable names; cassandra_version: a series (40x, 41x, 50x) -> the cassandra_config variables among
    them that series' templates, tasks and defaults do not use, sorted (e.g. 4.0 names after an upgrade to 4.1).
    e.g. (["cassandra_column_index_size_in_kb", "cassandra_cluster_name"], "41x")
    -> ["cassandra_column_index_size_in_kb"]."""
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
    # and its vars/ (the config files' owner, group and modes), but for the maps of names by series
    for key, value in role_file("cassandra_config", "vars").items():
        if key not in VARS_BY_SERIES:
            used.update(re.findall(r"\b(cassandra_\w+)", str(value)))
    tasks_dir = os.path.join(ROLE, "tasks")
    for name in os.listdir(tasks_dir):
        with open(os.path.join(tasks_dir, name)) as f:
            used.update(re.findall(r"\b(cassandra_\w+)", f.read()))
    return sorted(n for n in names if n in role_vars and n not in used)
