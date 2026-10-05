# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: cassandra_hosts
short_description: The inventory group of the cluster the operation playbooks run on
version_added: 2.1.0
description:
  - Returns the variable C(cassandra_hosts) when it is set (C(-e cassandra_hosts=<group>)).
  - Else the group C(cassandra) when the inventory has one.
  - Else the cluster group of the inventory, when there is only one, as the import writes it
    (C(all) > C(<cluster>) > C(<cluster>_<dc>) > C(<cluster>_<dc>_<rack>)) or as written by hand.
    The cluster group is the group no other group holds all the hosts of, leaving out C(all),
    C(ungrouped) and the groups the playbooks make while they run. Among groups with the same
    hosts (one datacenter, one rack), the one whose name starts the others' (C(prod) for
    C(prod_dc1) and C(prod_dc1_rack1)).
  - Else fails, naming the candidate groups.
options: {}
author: Alain Rodriguez (@arodrime)
"""

EXAMPLES = r"""
- name: Run on the cluster of the inventory
  hosts: "{{ lookup('community.cassandra.cassandra_hosts') }}"
  tasks:
    - name: Show it
      ansible.builtin.debug:
        msg: "{{ groups[lookup('community.cassandra.cassandra_hosts')] }}"
"""

RETURN = r"""
_raw:
  description: The name of the cluster's group.
  type: list
  elements: str
"""

import re

from ansible.errors import AnsibleLookupError
from ansible.plugins.lookup import LookupBase

DEFAULT = "cassandra"

# groups the playbooks make with group_by/add_host while they run: never the
# cluster group, even when they hold every host
RUNTIME = re.compile(r"^cassandra_(target_rack_nodes|move_left_going\w*|move_order|leaving_dc|seed_\w+"
                     r"|create_start_order|apply_config_\w+|update_java_\w+|upgrade_nodes|reset_\w+)$")


def cluster_group(given, groups):
    """given: cassandra_hosts or None; groups: {name: [hosts]}. Returns the
    group name, or raises ValueError naming the candidates."""
    if given:
        return given
    if groups.get(DEFAULT):
        return DEFAULT
    cands = dict((name, frozenset(hosts)) for name, hosts in groups.items()
                 if hosts and name not in ("all", "ungrouped") and not RUNTIME.match(name))
    top = dict((name, hosts) for name, hosts in cands.items()
               if not any(hosts < other for other in cands.values()))
    by_hosts = {}
    for name, hosts in top.items():
        by_hosts.setdefault(hosts, []).append(name)
    # the groups with the same hosts (one datacenter, one rack: <cluster>,
    # <cluster>_<dc>, <cluster>_<dc>_<rack>): the one whose name starts the others'
    heads = []
    for names in by_hosts.values():
        head = [n for n in sorted(names) if all(o == n or o.startswith(n + "_") for o in names)]
        heads.append(head[0] if head else None)
    if len(heads) == 1 and heads[0]:
        return heads[0]
    if not heads:
        raise ValueError("cassandra_hosts is not set and the inventory has no group with hosts."
                         " Put the nodes in a group (all > <cluster> > <dc> > <rack>, as the import writes it)")
    if len(heads) == 1:
        raise ValueError("cassandra_hosts is not set and these groups hold the same hosts: %s."
                         " Run with -e cassandra_hosts=<the cluster's group>" % ", ".join(sorted(top)))
    listed = sorted(h if h else "/".join(sorted(names)) for h, names in zip(heads, by_hosts.values()))
    raise ValueError("cassandra_hosts is not set and the inventory has several clusters (groups: %s)."
                     " Run with -e cassandra_hosts=<one of them>" % ", ".join(listed))


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        variables = variables or {}
        given = variables.get("cassandra_hosts")
        if isinstance(given, str):
            given = self._templar.template(given)
        if given is not None and not isinstance(given, str):
            raise AnsibleLookupError("cassandra_hosts must be a group name, not %r" % (given,))
        try:
            return [cluster_group(given, dict(variables.get("groups") or {}))]
        except ValueError as exc:
            raise AnsibleLookupError(str(exc))
