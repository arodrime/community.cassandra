# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_config_ignored_vars: variable names, series -> the cassandra_config
    variables among them that series' templates don't use (e.g. 4.0 names after
    an upgrade to 4.1).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import re

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_config")
SERIES = {"40x": "4.0", "41x": "4.1", "50x": "5.0"}


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
    tasks_dir = os.path.join(ROLE, "tasks")
    for name in os.listdir(tasks_dir):
        with open(os.path.join(tasks_dir, name)) as f:
            used.update(re.findall(r"\b(cassandra_\w+)", f.read()))
    return sorted(n for n in names if n in role_vars and n not in used)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_config_ignored_vars": cassandra_config_ignored_vars,
        }
