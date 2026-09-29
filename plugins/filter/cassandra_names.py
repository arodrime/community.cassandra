# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_close_names: the variables set that look like a misspelt
variable of the playbooks (cassandra_host for cassandra_hosts)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import difflib
import glob
import os

import yaml

_ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "roles")


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


class FilterModule(object):
    def filters(self):
        return {"cassandra_close_names": cassandra_close_names}
