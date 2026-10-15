# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_close_names: the variables set that look like a misspelt
variable of the playbooks (cassandra_host for cassandra_hosts).
cassandra_unknown_names: the cassandra_* variables set that the collection
does not know, each with the known variable it is close to (or "").
cassandra_renamed_names: the variables set under a former name, each with its
name now.
cassandra_unknown_extra_vars: the -e variables under a prefix of the collection
(cassandra_, import_cluster_, help_) that it does not know, each with the known
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

_ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "roles")
_PLAYBOOKS = os.path.join(os.path.dirname(__file__), "..", "..", "playbooks")
_NAME = re.compile(r"\b(?:cassandra|import_cluster|help)_\w+")
# the prefixes of the collection's variables (cas+andra: a missing or doubled s, casandra_hosts, is ours too;
# CASSANDRA_HOSTS too, a variable name being case-sensitive)
_OURS = re.compile(r"(?:cas+andra|import_cluster|help)_", re.IGNORECASE)
# a comment: its names are examples (a typo shown, a module), not variables read (text after " #" in a
# string goes too: a name only there is then unknown, never a typo let through)
_COMMENT = re.compile(r"(?m)(?:^|\s)#.*$")
# former names of the playbooks' variables: every node an operation targets is cassandra_target_nodes
RENAMED = dict((name, "cassandra_target_nodes") for name in (
    "cassandra_new_nodes", "cassandra_leaving_nodes", "cassandra_reset_nodes", "cassandra_dead_node_address",
    "cassandra_upgrade_canary", "cassandra_status_from"))


# the secrets import_cluster moves out of a config file's lines, which refer to them (cassandra_env_extra_lines...)
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
    for path in glob.glob(os.path.join(_ROLES, "*", "defaults", "main.yml")):
        with open(path, encoding="utf-8") as f:
            names.update(yaml.safe_load(f) or {})
    return frozenset(names)


def cassandra_close_names(names, known, cutoff=0.9):
    """names: the variables set; known: the playbooks' own. Returns {name:
    the known name it is close to} for the names that are neither known nor
    a variable of the collection's roles (cassandra_rpc_address is not a
    misspelt cassandra_replace_address)."""
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
def _used_names():
    """Every cassandra_*, import_cluster_* and help_* name the roles and playbooks use outside comments (their
    own facts and results too)."""
    names = set()
    paths = (glob.glob(os.path.join(_ROLES, "*", "*", "*.yml"))
             + glob.glob(os.path.join(_ROLES, "*", "templates", "**", "*.j2"), recursive=True)
             + glob.glob(os.path.join(_PLAYBOOKS, "*.yml")))
    for path in paths:
        with open(path, encoding="utf-8") as f:
            names.update(_NAME.findall(_COMMENT.sub("", f.read())))
    return frozenset(names)


def cassandra_unknown_names(names, known, cutoff=0.8):
    """names: the cassandra_* variables a host has; known: the playbooks'
    own (-e). Returns {name: the documented variable it is close to, or ""}
    for the names no role (argument specs, defaults) or playbook knows nor
    uses (e.g. cassandra_config_usr, a typo, or a name from another version)."""
    documented = sorted(_role_variables() | set(known or []))
    used = _used_names()
    out = {}
    for name in names or []:
        if name in documented or name in used or name.startswith(SECRET_VARS):
            continue
        low = name.lower()  # CASSANDRA_HOSTS: cassandra_hosts
        match = ([RENAMED[low]] if low in RENAMED else [low] if low in documented
                 else ["cassandra_hosts"] if low == "cassandra_cluster"  # the CASSANDRA_CLUSTER environment variable
                 else difflib.get_close_matches(low, documented, n=1, cutoff=cutoff))
        out[name] = match[0] if match else ""
    return out


def cassandra_renamed_names(names):
    """names: the variables set. Returns {name: its name now} for those set under a former name."""
    return dict((name, RENAMED[name]) for name in names or [] if name in RENAMED)


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
    cassandra_renamed_names, the other prefixes to their owners."""
    allowed = _allowed(allow)
    if allowed is True:
        return {}
    skip = set(known or []) | set(RENAMED) | (allowed or set())
    ours = [n for n in names or [] if _OURS.match(n) and n not in skip]
    # the usual run (-e cassandra_hosts=...): nothing to look up in the roles' files
    return cassandra_unknown_names(ours, known) if ours else {}


class FilterModule(object):
    def filters(self):
        return {"cassandra_close_names": cassandra_close_names, "cassandra_unknown_names": cassandra_unknown_names,
                "cassandra_renamed_names": cassandra_renamed_names, "cassandra_unknown_extra_vars": cassandra_unknown_extra_vars}
