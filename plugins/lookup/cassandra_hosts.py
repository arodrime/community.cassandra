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
  - Else the group C(cassandra) when the inventory has one with hosts.
  - Else the cluster group, when the inventory holds one cluster laid out as the import writes it
    (C(all) > C(<cluster>) > C(<cluster>_<dc>) > C(<cluster>_<dc>_<rack>)), that is the group whose
    name starts every other group's (C(prod) for C(prod_dc1) and C(prod_dc1_rack1)) and holds
    their hosts. C(all), C(ungrouped) and the groups the playbooks make while they run are left out.
  - Else fails, naming the top groups. Any other group (C(monitoring), C(linux), a second cluster)
    needs C(-e cassandra_hosts=<group>), rather than a guess that could run on other hosts.
  - The groups are read at each call, so a group your own plays add (C(group_by), C(add_host)) before
    an operation playbook in the same run makes the next calls fail the same way.
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
                     r"|create_start_order|apply_config_\w+|update_java_\w+|upgrade_nodes)$")


def cluster_group(given, groups):
    """given: cassandra_hosts or None; groups: {name: [hosts]}. Returns the
    group name, or raises ValueError naming the candidates."""
    if given:
        return given
    if groups.get(DEFAULT):
        return DEFAULT
    cands = dict((name, frozenset(hosts)) for name, hosts in groups.items()
                 if hosts and name not in ("all", "ungrouped") and not RUNTIME.match(name))
    if not cands:
        raise ValueError("cassandra_hosts is not set and the inventory has no group with hosts."
                         " Put the nodes in a group (all > <cluster> > <dc> > <rack>, as the import writes it)")
    # the import's layout: every other group is <cluster>_..., within <cluster>
    heads = [name for name, hosts in sorted(cands.items())
             if all(o == name or (o.startswith(name + "_") and h <= hosts) for o, h in cands.items())]
    if len(heads) == 1:
        return heads[0]
    top = [name for name, hosts in cands.items() if not any(hosts < other for other in cands.values())]
    top = sorted(n for n in top if not any(n.startswith(o + "_") for o in top))  # orders, not orders_dc1 too
    raise ValueError("cassandra_hosts is not set and the inventory's groups are not one cluster's"
                     " (<cluster>, <cluster>_<dc>, <cluster>_<dc>_<rack>; top groups: %s)."
                     " Run with -e cassandra_hosts=<the cluster's group>" % ", ".join(top))


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
