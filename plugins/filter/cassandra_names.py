# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_close_names: the variables set that look like a misspelt
variable of the playbooks (cassandra_host for cassandra_hosts).
cassandra_unknown_names: the cassandra_* variables set that the collection
does not know, each with the known variable it is close to (or "").
cassandra_renamed_names: the variables set under a former name, each with its
name now."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import difflib
import functools
import glob
import os
import re

import yaml

_ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "roles")
_PLAYBOOKS = os.path.join(os.path.dirname(__file__), "..", "..", "playbooks")
_NAME = re.compile(r"\bcassandra_\w+")
# former names of the playbooks' variables: every node an operation targets is cassandra_target_nodes
RENAMED = dict((name, "cassandra_target_nodes") for name in (
    "cassandra_new_nodes", "cassandra_leaving_nodes", "cassandra_reset_nodes", "cassandra_dead_node_address",
    "cassandra_upgrade_canary", "cassandra_status_from"))


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
    return names


def cassandra_close_names(names, known, cutoff=0.9):
    """names: the variables set; known: the playbooks' own. Returns {name:
    the known name it is close to} for the names that are neither known nor
    a variable of the collection's roles (cassandra_rpc_address is not a
    misspelt cassandra_replace_address)."""
    close = {}
    ours = _role_variables()
    for name in names or []:
        if name in known or name in ours:
            continue
        match = difflib.get_close_matches(name, known, n=1, cutoff=cutoff)
        if match:
            close[name] = match[0]
    return close


@functools.lru_cache(maxsize=None)
def _used_names():
    """Every cassandra_* name the roles and playbooks use (their own facts and results too)."""
    names = set()
    paths = (glob.glob(os.path.join(_ROLES, "*", "*", "*.yml"))
             + glob.glob(os.path.join(_ROLES, "*", "templates", "**", "*.j2"), recursive=True)
             + glob.glob(os.path.join(_PLAYBOOKS, "*.yml")))
    for path in paths:
        with open(path, encoding="utf-8") as f:
            names.update(_NAME.findall(f.read()))
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
        if name in documented or name in used:
            continue
        match = difflib.get_close_matches(name, documented, n=1, cutoff=cutoff)
        out[name] = match[0] if match else ""
    return out


def cassandra_renamed_names(names):
    """names: the variables set. Returns {name: its name now} for those set under a former name."""
    return dict((name, RENAMED[name]) for name in names or [] if name in RENAMED)


class FilterModule(object):
    def filters(self):
        return {"cassandra_close_names": cassandra_close_names, "cassandra_unknown_names": cassandra_unknown_names,
                "cassandra_renamed_names": cassandra_renamed_names}
