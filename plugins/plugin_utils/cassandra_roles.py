# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The collection's roles as the filters read them: their defaults and vars files."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import copy
import functools
import os

import yaml

ROLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles")


@functools.lru_cache(maxsize=None)
def _loaded(role, kind):
    with open(os.path.join(ROLES, role, kind, "main.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def role_file(role, kind="defaults"):
    """A role's defaults/main.yml (kind: defaults) or vars/main.yml (vars) as YAML reads it, a copy of its own
    (the caller may change it): each file is read once. role_file("cassandra_config")["cassandra_heap_size"] -> ''."""
    return copy.deepcopy(_loaded(role, kind))


def roles_with_defaults():
    """The roles that have a defaults/main.yml, sorted."""
    return [r for r in sorted(os.listdir(ROLES)) if os.path.exists(os.path.join(ROLES, r, "defaults", "main.yml"))]
