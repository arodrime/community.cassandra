# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_close_names: the variables set that look like a misspelt
variable of the playbooks (cassandra_host for cassandra_hosts).
cassandra_unknown_names: the cassandra_* variables set that the collection
does not know, each with the known variable it is close to (or "").
cassandra_unknown_note: the warning of one of them.
cassandra_renamed_names: the variables set under a former name, each with its
name now.
cassandra_unknown_extra_vars: the -e variables under a prefix of the collection
(cassandra_, import_, help_, status_, drift_) that it does not know, each with the known
variable it is close to (or "")."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import difflib
import functools
import glob
import os
import re

import yaml

from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file, roles_with_defaults

_TOP = os.path.join(os.path.dirname(__file__), "..", "..")
_ROLES = os.path.join(_TOP, "roles")
# the files used_names reads names from (relative to the collection), each check its own (help's: not the roles'
# files/ nor molecule/, but every template, not only the .j2 ones); the playbooks' task files and templates hold
# text that was in playbooks/*.yml, read by both
_PLAYBOOK_FILES = ("playbooks/*.yml", "playbooks/tasks/*.yml", "playbooks/templates/*.j2")
_READ = {"unknown": ("roles/*/*/*.yml", "roles/*/templates/**/*.j2") + _PLAYBOOK_FILES,
         "help": ("roles/*/defaults/*.yml", "roles/*/meta/*.yml", "roles/*/tasks/*.yml", "roles/*/vars/*.yml",
                  "roles/*/handlers/*.yml", "roles/*/templates/**") + _PLAYBOOK_FILES}
# the names the roles and playbooks use: import_cluster's own facts (import_cluster_*), not every import_ word
# (import_tasks, import_playbook, a file's name): its options are the playbook variables
_NAME = re.compile(r"\b(?:cassandra|import_cluster|help)_\w+")
# the prefixes of the collection's variables (cas+andra: a missing or doubled s, casandra_hosts, is ours too;
# CASSANDRA_HOSTS too, a variable name being case-sensitive)
_OURS = re.compile(r"(?:cas+andra|import|help|status|drift)_", re.IGNORECASE)
# a comment: its names are examples (a typo shown, a module), not variables read (text after " #" in a
# string goes too: a name only there is then unknown, never a typo let through)
_COMMENT = re.compile(r"(?m)(?:^|\s)#.*$")
# Why the two lists below: the 3.0.0 releases keep the behaviour of the first 3.0.0 release candidate (3.0.0-rc1,
# "rc1" here), so a -e name it accepted is still accepted and one it refused is still refused (the tests compare
# with its names: tests/unit/plugins/filter/fixtures/known_names_rc1.txt). rc1 took every name its playbooks wrote for
# a known name, the names of the filters they call included. Since, some of that Jinja moved into Python and new
# filters came. The lists can go when a release no longer keeps rc1's behaviour (the fixture and its tests with them).
# - names rc1 read that the playbooks no longer write: still known, though none is read as a variable (filters rc1's
#   Jinja called that Python calls now, and cassandra_jmx_ssl_, a prefix rc1 wrote)
RC1_NAMES_NOW_IN_PYTHON = frozenset((
    "cassandra_apply_config_down_ok", "cassandra_apply_config_outcomes", "cassandra_clock_lines",
    "cassandra_drift_dirs", "cassandra_drift_paths", "cassandra_drift_shared",
    "cassandra_import_keep_parts", "cassandra_jmx_ssl_", "cassandra_leading_comments",
    "cassandra_prometheus_config_error", "cassandra_storage_mode_back_stop", "cassandra_storage_mode_given_wrong",
    "cassandra_storage_mode_into_none_stop", "cassandra_storage_mode_jump_stop", "cassandra_storage_mode_kept_stop",
    "cassandra_storage_mode_stopped_unsure", "cassandra_storage_mode_unsure", "cassandra_storage_mode_written",
    "cassandra_config_answer"))  # (cassandra_config's question registered it; the shared one asks it now)
# - names the playbooks write since rc1 that are no variables, so unknown as in rc1 (cassandra_seed_hosts given with
#   -e is refused as a typo of cassandra_seeds)
NOT_VARIABLES = frozenset((
    # the filters added since rc1 that the playbooks call (a new one goes here too: a test says so)
    "cassandra_apply_config_axonops_steps", "cassandra_apply_config_down_ok_all", "cassandra_apply_config_view",
    "cassandra_apply_gate_lines", "cassandra_apply_only", "cassandra_apply_only_stops", "cassandra_check_groups", "cassandra_apply_resume_refusal",
    "cassandra_config_user_files", "cassandra_config_user_request", "cassandra_cqlsh_credential_refusals",
    "cassandra_axonops_changes", "cassandra_axonops_config",
    "cassandra_axonops_enabled_hosts", "cassandra_axonops_files", "cassandra_axonops_java_agent",
    "cassandra_axonops_loader_refusals", "cassandra_axonops_read_problems", "cassandra_axonops_repository_items",
    "cassandra_apply_config_changes", "cassandra_apply_config_diff_text", "cassandra_apply_config_diff_warning",
    "cassandra_apply_config_happens",
    "cassandra_import_newer_files",
    "cassandra_axonops_problems", "cassandra_axonops_unavailable",
    "cassandra_cleanup_batches", "cassandra_clock_plan", "cassandra_config_audit_parameter_refusals",
    "cassandra_config_exporter_refusals", "cassandra_config_jmx_tls_refusals", "cassandra_config_logback_refusals",
    "cassandra_config_override_refusals", "cassandra_config_rackdc_refusals", "cassandra_config_snippet_refusals",
    "cassandra_cql_login_outcome", "cassandra_dead_address", "cassandra_dead_node_refusal",
    "cassandra_drift_screen", "cassandra_drift_verdict", "cassandra_import_cql_todo",
    "cassandra_import_cqlsh_todo", "cassandra_installed_packages", "cassandra_inventory_file_heads",
    "cassandra_node_reset_dir_lines", "cassandra_node_reset_lines", "cassandra_package_query", "cassandra_rack_infos",
    "cassandra_seed_hold_lines", "cassandra_seed_hosts", "cassandra_service_account_refusal", "cassandra_service_changes",
    "cassandra_service_other_seeds", "cassandra_staying_node", "cassandra_storage_mode_given_rows", "cassandra_storage_mode_guard",
    "cassandra_sysctl_todo",
    "cassandra_too_few_nodes", "cassandra_topology_done", "cassandra_topology_nothing_to_do",
    "cassandra_topology_refusals", "cassandra_unknown_note", "cassandra_upgrade_copy_refusal",
    # other words: a file the roles read (plugin_utils/cassandra_config_record.py), a module a template's comment
    # names (cassandra_os.py)
    "cassandra_config_record", "cassandra_os"))
# former names of the variables: every node an operation targets is cassandra_target_nodes; the others, renamed
# since the 2.1.0 release candidates (an inventory written for one is refused, not silently ignored)
RENAMED = dict([(name, "cassandra_target_nodes") for name in (
    "cassandra_new_nodes", "cassandra_leaving_nodes", "cassandra_reset_nodes", "cassandra_dead_node_address",
    "cassandra_upgrade_canary", "cassandra_status_from")] + [
    ("cassandra_repository_deb_url", "cassandra_install_url"), ("cassandra_repository_rpm_url", "cassandra_install_url"),
    ("cassandra_repository_username", "cassandra_install_username"),
    ("cassandra_repository_password", "cassandra_install_password"),
    ("cassandra_medusa_config_owner", "cassandra_medusa_config_user"),
    ("cassandra_medusa_key_file_owner", "cassandra_medusa_key_file_user"),
    ("cassandra_config_owner", "cassandra_config_user")] + [
    # import_cluster's options, import_<x> now (given with -e: variables_check.yml reads them from the -e ones)
    ("import_cluster_" + name, "import_" + name) for name in (
        "dir", "report_dir", "force", "host_names", "set_ansible_host", "vault_password_file", "vault_id", "medusa_path",
        "keep_hand_edits", "allow_unread", "carry_exporter_config", "report_shareable", "adopt", "strict", "debug")])
# variables of the 2.1.0 release candidates no longer read: {name: the file whose lines they gave}. An inventory that
# still sets one is refused (its lines would be left out of the file, every node restarted without them)
REMOVED = {"cassandra_env_extra_lines": "cassandra-env.sh", "cassandra_logback_extra_lines": "logback.xml"}
# options of the 2.1.0 release candidates that are gone: {name: what is done instead}
DROPPED = {"import_cluster_keep_unread_jvm_options":
           "the options of a jvm<N>-server.options the running Java does not read are always imported now"}


# the secrets import_cluster moves out of a config file's lines, which refer to them (a snippet's lines...)
SECRET_VARS = "cassandra_secret_"


@functools.lru_cache(maxsize=None)
def _role_variables():
    """The variables of the collection's roles: their argument specs and defaults."""
    names = set()
    for path in glob.glob(os.path.join(_ROLES, "*", "meta", "argument_specs.yml")):
        with open(path, encoding="utf-8") as f:
            specs = (yaml.safe_load(f) or {}).get("argument_specs") or {}
        for entry in specs.values():
            names.update((entry or {}).get("options") or {})
    for role in roles_with_defaults():
        names.update(role_file(role) or {})
    return frozenset(names)


def cassandra_close_names(names, known, cutoff=0.9):
    """names: the variables set; known: the playbooks' own. Returns {name:
    the known name it is close to} for the names that are neither known nor
    a variable of the collection's roles (cassandra_rpc_address is not a
    misspelt cassandra_replace_address).
    Example: ["cassandra_heap_sise"], ["cassandra_heap_size"] ->
    {"cassandra_heap_sise": "cassandra_heap_size"}."""
    close = {}
    ours = _role_variables()
    for name in names or []:
        if name in known or name in ours or name.startswith(SECRET_VARS):
            continue
        match = difflib.get_close_matches(name, known, n=1, cutoff=cutoff)
        if match:
            close[name] = match[0]
    return close


@functools.lru_cache(maxsize=None)
def used_names(for_help=False):
    """Every cassandra_*, import_cluster_* and help_* name the roles and playbooks (their task files and
    templates too) use, their own facts and results too, as rc1 knew them (RC1_NAMES_NOW_IN_PYTHON,
    NOT_VARIABLES). for_help: those help's typo check reads (its files, and the names their comments write too);
    else the unknown-variable check's (comments left out)."""
    names = set()
    for pattern in _READ["help" if for_help else "unknown"]:
        for path in glob.glob(os.path.join(_TOP, pattern), recursive=True):
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                names.update(_NAME.findall(text if for_help else _COMMENT.sub("", text)))
    return frozenset((names | RC1_NAMES_NOW_IN_PYTHON) - NOT_VARIABLES)


def cassandra_unknown_names(names, known, cutoff=0.8):
    """names: the cassandra_* variables a host has; known: the playbooks'
    own (-e). Returns {name: the documented variable it is close to, or ""}
    for the names no role (argument specs, defaults) or playbook knows nor
    uses (e.g. cassandra_config_usr, a typo, or a name from another version)."""
    documented = sorted(_role_variables() | set(known or []))
    used = used_names()
    out = {}
    for name in names or []:
        if name in documented or name in used or name.startswith(SECRET_VARS):
            continue
        low = name.lower()  # CASSANDRA_HOSTS: cassandra_hosts
        match = ([RENAMED[low]] if low in RENAMED else [low] if low in documented
                 else ["cassandra_hosts"] if low == "cassandra_cluster"  # the CASSANDRA_CLUSTER environment variable
                 else difflib.get_close_matches(low, documented, n=1, cutoff=cutoff) or _pasted(low, documented))
        out[name] = match[0] if match else ""
    return out


def _pasted(low, documented):
    """A name with text pasted into it (a comment: "cassandra_# Logging (logback.xml)log_console"): the documented
    variable its last word makes ("cassandra_log_console"), as a list; [] when none."""
    tail = re.search(r"\W(\w+)$", low)
    name = "cassandra_" + tail.group(1).lstrip("_") if tail else ""
    return [name] if name in documented else []


def cassandra_unknown_note(name, close="", where="", close_set=False):
    """The warning of a variable no role or playbook knows (cassandra_unknown_names): a block of its own, one fact
    per line, its lines under its first; close: the documented variable it is close to; where: the nodes it is set
    for; close_set: close is set too (else it keeps its default).
    Example: "cassandra_log_consol", "cassandra_log_console", "all 3 nodes" -> "WARNING  probably a typo in a
    variable name, not known to this collection:", "         cassandra_log_consol", "         set for: all 3 nodes",
    "         no role or playbook reads it", "         closest known variable: cassandra_log_console",
    "         cassandra_log_console not set: its default applies meanwhile" (joined)."""
    under = " " * 9
    lines = ["WARNING  probably a typo in a variable name, not known to this collection:", under + name]
    lines += [under + "set for: " + where] if where else []
    lines.append(under + "no role or playbook reads it")
    if close:
        lines.append(under + "closest known variable: " + close)
        lines.append(under + close + (" is set too" if close_set else " not set: its default applies meanwhile"))
    return "\n".join(lines)


def cassandra_renamed_names(names):
    """names: the variables set. Returns {name: its name now} for those set under a former name, {name: ""} for
    those no longer read (REMOVED).
    Example:
    ["cassandra_new_nodes"] -> {"cassandra_new_nodes": "cassandra_target_nodes"}."""
    return dict((name, RENAMED.get(name, "")) for name in names or []
                if name in RENAMED or name in REMOVED or name in DROPPED)


def cassandra_former_name_text(name, now="", where=None):
    """The way out for a variable under a former name (now: its name now) or no longer read (REMOVED; where: the
    inventory files that set it) -> text.
    Example:
    "cassandra_env_extra_lines", "", ["group_vars/prod/main.yml"] -> "is no longer read (...): re-import ..."."""
    if now and name.startswith("import_cluster_"):  # (an option of import_cluster, given with -e)
        return "is renamed %s: use -e %s=... instead." % (now, now)
    if now:
        return "is renamed %s: set %s instead." % (now, now)
    if name in DROPPED:
        return "is removed (%s): remove -e %s from the command." % (DROPPED[name], name)
    files = ", ".join(where or []) or "the inventory files that set it"
    return ("is no longer read: %s would be written without its lines. Re-import the cluster (import_cluster), which"
            " writes these lines into snippet files (cassandra_config_template_snippets), then remove %s from %s."
            % (REMOVED.get(name, "the file"), name, files))


def cassandra_files_setting(name, sources=None, playbook_dir=""):
    """The group_vars and host_vars files next to the inventory sources (and the playbooks') that set the top-level
    variable name, read as text -> [path]; a vaulted one that cannot be read here: "<path> (vaulted, not read)"."""
    dirs = []
    for source in list(sources or []) + ([playbook_dir] if playbook_dir else []):
        base = str(source) if os.path.isdir(str(source)) else os.path.dirname(str(source))
        if base and base not in dirs:
            dirs.append(base)
    key = re.compile(r"(?m)^%s\s*:" % re.escape(name))
    found = []
    for base in dirs:
        for kind in ("group_vars", "host_vars"):
            for top, dummy, files in sorted(os.walk(os.path.join(base, kind))):
                for f in sorted(files):
                    if f.startswith(".") or not f.endswith((".yml", ".yaml", ".json")) and "." in f:
                        continue
                    path = os.path.join(top, f)
                    try:
                        with open(path, encoding="utf-8", errors="replace") as text:
                            content = text.read()
                    except OSError:
                        continue
                    if content.startswith("$ANSIBLE_VAULT"):
                        found.append("%s (vaulted, not read)" % path)
                    elif key.search(content):
                        found.append(path)
    return found


def _allowed(allow):
    """cassandra_allow_unknown_variables: true (every name), false, or the names let through (a list, or
    comma-separated)."""
    if isinstance(allow, (list, tuple, set, frozenset)):
        return set(str(name).strip() for name in allow)
    text = str(allow if allow is not None else "").strip().strip("[]")
    if text.lower() in ("", "true", "false", "yes", "no", "on", "off", "1", "0", "y", "n", "t", "f"):
        return boolean(text or False, strict=False)
    return set(name.strip(" '\"") for name in re.split(r"[,\s]+", text) if name.strip(" '\""))


def cassandra_unknown_extra_vars(names, known, allow=False):
    """names: the -e variables; known: the playbooks' own options; allow: cassandra_allow_unknown_variables.
    Returns {name: the documented variable it is close to, or ""} for those under a prefix of the collection that
    no role or playbook knows (cassandra_unknown_names), the allowed ones left out. The former names are left to
    cassandra_renamed_names, the other prefixes to their owners.
    Example: ["cassandra_heap_sise"], [] ->
    {"cassandra_heap_sise": "cassandra_heap_size"}."""
    allowed = _allowed(allow)
    if allowed is True:
        return {}
    skip = set(known or []) | set(RENAMED) | set(DROPPED) | (allowed or set())
    ours = [n for n in names or [] if _OURS.match(n) and n not in skip]
    # the usual run (-e cassandra_hosts=...): nothing to look up in the roles' files
    return cassandra_unknown_names(ours, known) if ours else {}


class FilterModule(object):
    def filters(self):
        return {"cassandra_close_names": cassandra_close_names, "cassandra_unknown_names": cassandra_unknown_names,
                "cassandra_renamed_names": cassandra_renamed_names, "cassandra_unknown_extra_vars": cassandra_unknown_extra_vars,
                "cassandra_former_name_text": cassandra_former_name_text, "cassandra_unknown_note": cassandra_unknown_note,
                "cassandra_files_setting": cassandra_files_setting}
