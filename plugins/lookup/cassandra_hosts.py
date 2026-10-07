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
  - Else the cluster named by the environment variable C(CASSANDRA_CLUSTER) when it is set
    (C(CASSANDRA_CLUSTER=<group> ansible-playbook ...)), the group of that name with hosts, else the group
    whose hosts are the hosts with that C(cassandra_cluster_name) (exact, case-sensitive), laid out as a
    cluster's (C(cassandra), or a group with its C(<group>_...) groups). A value that matches no group, a
    cluster name that several groups have, or one hosts whose name the inventory alone does not give may
    have, fails.
  - Else the group C(cassandra) when the inventory has one with hosts.
  - Else the cluster group, when the inventory holds one cluster laid out as the import writes it
    (C(all) > C(<cluster>) > C(<cluster>_<dc>) > C(<cluster>_<dc>_<rack>)), that is the group whose
    name starts every other group's (C(prod) for C(prod_dc1) and C(prod_dc1_rack1)) and holds
    their hosts. C(all), C(ungrouped) and the groups the playbooks make while they run are left out.
  - Else fails, naming the top groups. Any other group (C(monitoring), C(linux), a second cluster)
    needs C(-e cassandra_hosts=<group>), rather than a guess that could run on other hosts.
  - The groups are read at each call, so a group your own plays add (C(group_by), C(add_host)) before
    an operation playbook in the same run makes the next calls fail the same way.
options:
  explain:
    description: Returns the group and how it was picked (C(-e cassandra_hosts), C(CASSANDRA_CLUSTER), the inventory).
    type: bool
    default: false
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
  description: The name of the cluster's group; with O(explain), C(<group> (<how it was picked>)).
  type: list
  elements: str
"""

import os
import re

from ansible.errors import AnsibleError, AnsibleLookupError
from ansible.plugins.lookup import LookupBase

DEFAULT = "cassandra"
# a host's cassandra_cluster_name that the inventory alone does not give (a fact of the node)
UNKNOWN = object()
# the environment variable that picks the cluster when cassandra_hosts is not set
ENV = "CASSANDRA_CLUSTER"

# groups the playbooks make with group_by/add_host while they run: never the
# cluster group, even when they hold every host
RUNTIME = re.compile(r"^cassandra_(target_rack_nodes|move_left_going|move_order|leaving_dc|create_start_order|topology_(add|remove)"
                     r"|upgrade_nodes|(seed|apply_config|update_java)_(True|False))$")


def _candidates(groups):
    return dict((name, frozenset(hosts)) for name, hosts in groups.items()
                if hosts and name not in ("all", "ungrouped") and not RUNTIME.match(name))


def cluster_group(given, groups, env=None, cluster_name=None):
    """given: cassandra_hosts or None; groups: {name: [hosts]}; env: the value
    of ENV or None; cluster_name(host): its cassandra_cluster_name. Returns
    the group name, or raises ValueError naming the candidates."""
    return picked(given, groups, env, cluster_name)[0]


def picked(given, groups, env=None, cluster_name=None):
    """cluster_group, and how it was picked."""
    if given:
        return given, "cassandra_hosts"
    if env:
        return _named(env, groups, cluster_name), "%s=%s" % (ENV, env)
    if groups.get(DEFAULT):
        return DEFAULT, "the group %s" % DEFAULT
    cands = _candidates(groups)
    if not cands:
        raise ValueError("cassandra_hosts is not set and the inventory has no group with hosts."
                         " Put the nodes in a group (all > <cluster> > <dc> > <rack>, as the import writes it)")
    # the import's layout: every other group is <cluster>_..., within <cluster>
    heads = [name for name, hosts in sorted(cands.items())
             if all(o == name or (o.startswith(name + "_") and h <= hosts) for o, h in cands.items())]
    if len(heads) == 1:
        return heads[0], "the inventory's cluster group"
    raise ValueError("cassandra_hosts is not set and the inventory's groups are not one cluster's"
                     " (<cluster>, <cluster>_<dc>, <cluster>_<dc>_<rack>; top groups: %s)."
                     " Run with -e cassandra_hosts=<the cluster's group> (or %s=<the cluster's group or name>)"
                     % (", ".join(top_groups(groups)), ENV))


def _named(value, groups, cluster_name):
    """The group ENV names: a group with hosts, else the one whose hosts are those with that cluster name."""
    cands = _candidates(groups)
    if value in cands:
        return value
    names = dict((h, cluster_name(h) if cluster_name else None) for members in cands.values() for h in members)
    named = frozenset(h for h, name in names.items() if name == value)
    if not named:
        raise ValueError("%s=%s: no group of that name with hosts, and no host with that cassandra_cluster_name"
                         " (top groups: %s)" % (ENV, value, ", ".join(top_groups(groups))))
    # the largest groups of those hosts only, laid out as a cluster's (cassandra, or <cluster> with groups
    # <cluster>_...: not linux or monitoring when the name is set for all); prod, not prod_dc1 of the same hosts
    inside = dict((name, members) for name, members in cands.items() if members <= named and (
        name == DEFAULT or any(o.startswith(name + "_") and h <= members for o, h in cands.items())))
    tops = [name for name, members in inside.items() if not any(members < other for other in inside.values())]
    tops = sorted(n for n in tops if not any(n.startswith(o + "_") and inside[o] == inside[n] for o in tops))
    unread = frozenset(h for h, name in names.items() if name is UNKNOWN)
    if len(tops) == 1 and inside[tops[0]] == named:
        # a host whose name is not known could be the rest of a larger group: no guess
        if not any(members > named and members <= named | unread for members in cands.values()):
            return tops[0]
    raise ValueError("%s=%s: the hosts with that cassandra_cluster_name are not one group's (groups: %s):"
                     " give the group instead (%s=<group>, or -e cassandra_hosts=<group>)"
                     % (ENV, value, ", ".join(tops) or "none", ENV))


def top_groups(groups):
    """The groups with hosts that no other group holds (all, ungrouped and
    the runtime groups left out), sorted: one per cluster in an inventory of
    several clusters laid out as the import writes them."""
    cands = _candidates(groups)
    top = [name for name, hosts in cands.items() if not any(hosts < other for other in cands.values())]
    return sorted(n for n in top if not any(n.startswith(o + "_") for o in top))  # orders, not orders_dc1 too


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        variables = variables or {}
        given = variables.get("cassandra_hosts")
        if isinstance(given, str):
            given = self._templar.template(given)
        if given is not None and not isinstance(given, str):
            raise AnsibleLookupError("cassandra_hosts must be a group name, not %r" % (given,))
        self.set_options(var_options=variables, direct=kwargs)
        hostvars = variables.get("hostvars") or {}

        def cluster_name(host):
            try:
                value = hostvars[host].get("cassandra_cluster_name") if host in hostvars else None
                if isinstance(value, str) and "{{" in value:
                    value = self._templar.template(value)
            except AnsibleError:  # not known from the inventory alone (a fact of the node)
                return UNKNOWN
            return value

        try:
            group, why = picked(given, dict(variables.get("groups") or {}), os.environ.get(ENV), cluster_name)
        except ValueError as exc:
            raise AnsibleLookupError(str(exc))
        return ["%s (%s)" % (group, why) if self.get_option("explain") else group]
