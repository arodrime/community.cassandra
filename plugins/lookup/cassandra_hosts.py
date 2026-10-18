# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: cassandra_hosts
short_description: The inventory group of the cluster the operation playbooks run on
version_added: 3.0.0
description:
  - Returns the variable C(cassandra_hosts) when it is set (C(-e cassandra_hosts=<group>)). It must name a
    group of the inventory with hosts, else the lookup fails. Under C(ansible-playbook), it must not come from the
    inventory (C(group_vars), C(host_vars)), which the plays' hosts do not see, and a C(-e) one not be empty.
  - Else the cluster named by the environment variable C(CASSANDRA_CLUSTER) when it is set
    (C(CASSANDRA_CLUSTER=<group> ansible-playbook ...)), the group of that name with hosts, else the group
    whose hosts are the hosts with that C(cassandra_cluster_name) (exact, case-sensitive), laid out as a
    cluster's (a group with its C(<group>_...) groups). A value that matches no group, a
    cluster name that several groups have, or one hosts whose name the inventory alone does not give may
    have, fails.
  - When both are set, they must name the same group (C(CASSANDRA_CLUSTER) the group itself or its
    cluster name), else the lookup fails, naming both.
  - Neither set, the cluster group when the inventory holds one cluster laid out as the import writes it
    (C(all) > C(<cluster>) > C(<cluster>_<dc>) > C(<cluster>_<dc>_<rack>)), that is the group whose
    name starts every other group's (C(prod) for C(prod_dc1) and C(prod_dc1_rack1)), holds
    their hosts and has one at least (a lone group, C([linux]), is no cluster's). C(all), C(ungrouped) and the
    groups the playbooks make while they run are left out.
  - Else fails, naming the top groups (one per cluster) and the two ways to name one. No group name is
    taken by default, not C(cassandra) either. Any other group (C(monitoring), C(linux), a second cluster)
    needs C(-e cassandra_hosts=<group>) or C(CASSANDRA_CLUSTER), rather than a guess that could run on other hosts.
  - The groups are read at each call, so a group your own plays add (C(group_by), C(add_host)) before
    an operation playbook in the same run makes the next calls fail the same way.
  - It fails with a C(REFUSED  ...) block that says how to name the cluster, printed as a verdict by the
    C(community.cassandra.ops) callback.
options:
  explain:
    description: Returns the group and how it was picked (C(-e cassandra_hosts), C(CASSANDRA_CLUSTER) (environment),
      C(the only cluster in the inventory)).
    type: bool
    default: false
  banner:
    description:
      - "Returns the line the playbooks start with, the group, its nodes (without the hosts marked
        C(cassandra_node_state=absent)), its datacenters and how it was picked
        ('CLUSTER prod (5 nodes, dc1) | chosen by: -e cassandra_hosts')."
    type: bool
    default: false
  refusal:
    description:
      - Returns the refusal block the lookup would fail with, or an empty string, instead of failing on it. The operation
        playbooks' first play (on localhost) stops on it with that message, before a play's C(hosts) calls the
        lookup (where its failure is a raw Ansible error).
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
  description: The name of the cluster's group; with O(explain), C(<group> (<how it was picked>)); with O(banner),
    the line naming it; with O(refusal), the refusal or an empty string.
  type: list
  elements: str
"""

import os
import re

from ansible import context
from ansible.errors import AnsibleError, AnsibleLookupError
from ansible.plugins.lookup import LookupBase
from ansible.utils.vars import load_extra_vars
from jinja2 import Undefined

# a host's cassandra_cluster_name that the inventory alone does not give (a fact of the node)
UNKNOWN = object()
# the environment variable that names the cluster (the same one as cassandra_hosts when both are set)
ENV = "CASSANDRA_CLUSTER"
# the group the playbooks took by default before: a group as any other now (its name in the refusal's hint)
DEFAULT = "cassandra"
# how the cluster was picked, as the playbooks' first line says it
BY_GIVEN = "-e cassandra_hosts"
BY_ENV = "%s (environment)" % ENV
BY_ONLY = "the only cluster in the inventory"
# an empty -e cassandra_hosts=: refused, even on an inventory of one cluster
EMPTY = "Give the cluster's group, -e cassandra_hosts=<group>, or leave -e cassandra_hosts out"
NAME_ONE = "-e cassandra_hosts=<group>, or %s=<group or cluster name> in your shell" % ENV

# groups the playbooks make with group_by/add_host while they run: never the
# cluster group, even when they hold every host
RUNTIME = re.compile(r"^cassandra_(target_rack_nodes|create_start_order"
                     r"|(seed|apply_config|apply_config_batch|apply_config_os|apply_config_os_window_any"
                     r"|apply_config_os_window_only)_(True|False))$")


def _candidates(groups):
    return dict((name, frozenset(hosts)) for name, hosts in groups.items()
                if hosts and name not in ("all", "ungrouped") and not RUNTIME.match(name))


def _others(groups):
    return ", ".join(sorted(g for g in groups if g not in ("all", "ungrouped")))


def _block(verdict, *lines):
    """A refusal: its verdict line, a blank line (under the ops callback, the verdict line alone red), what to do."""
    return "\n".join(("REFUSED  " + verdict, "") + lines + ("Nothing was changed.",))


def _found(groups):
    """The clusters of the inventory (its top groups, when they are not all laid out as clusters)."""
    tops = top_groups(groups)
    clusters = [name for name in tops if any(o.startswith(name + "_") for o in _candidates(groups))]
    return ("this inventory has %d clusters: %s" % (len(tops), ", ".join(tops)) if len(tops) > 1 and clusters == tops
            else "top groups of this inventory: %s" % (", ".join(tops) or "none"))


# how to name the cluster, never one of them to paste: an operation on a cluster not chosen
WAY_OUT = ("Name the one to run on:", "  export %s=<cluster>" % ENV, "or add -e cassandra_hosts=<cluster> to the command.")


def cluster_group(given, groups, env=None, cluster_name=None):
    """given: cassandra_hosts or None; groups: {name: [hosts]}; env: the value
    of ENV or None; cluster_name(host): its cassandra_cluster_name. Returns
    the group name, or raises ValueError naming the candidates."""
    return picked(given, groups, env, cluster_name)[0]


def picked(given, groups, env=None, cluster_name=None):
    """cluster_group, and how it was picked (BY_GIVEN, BY_ENV, BY_ONLY)."""
    if given:
        # a typo matches no host: under --limit (no localhost play to check it), every play would be
        # skipped and the run end ok
        if groups and not groups.get(given):
            why = "that group has no host" if given in groups else "no group of that name in the inventory"
            raise ValueError(_block("cassandra_hosts=%s: %s" % (given, why),
                                    "Its groups: %s" % _others(groups),
                                    "%s the cluster's group: -e cassandra_hosts=<group>"
                                    % ("Add its hosts to the inventory, or name" if given in groups else "Name")))
        if env and env != given:
            _same(given, env, groups, cluster_name)
        return given, BY_GIVEN
    if env:
        return _named(env, groups, cluster_name), BY_ENV
    cands = _candidates(groups)
    if not cands:
        raise ValueError(_block("no cluster: the inventory has no group with hosts",
                                "Put the nodes in a group (all > <cluster> > <dc> > <rack>, as the import writes it)."))
    # the import's layout: every other group is <cluster>_..., within <cluster>, and there is one at least (a
    # lone [linux] or [web] group is no cluster's)
    heads = [name for name, hosts in sorted(cands.items())
             if all(o == name or (o.startswith(name + "_") and h <= hosts) for o, h in cands.items())] if len(cands) > 1 else []
    if len(heads) == 1:
        return heads[0], BY_ONLY
    found = clusters_found(groups)
    others = other_groups(groups, found)
    # a group named DEFAULT is no default any more (never a name to paste: an operation on a cluster not chosen)
    hint = (("A group %s is no longer taken by default: name it as any other." % DEFAULT,)
            if DEFAULT in _candidates(groups) else ())
    empty = " (-e cassandra_hosts is empty)" if given == "" else ""
    if len(found) > 1:
        verdict = "no cluster named%s: this inventory has %d clusters: %s%s" % (
            empty, len(found), ", ".join(found), " (other groups: %s)" % ", ".join(others) if others else "")
    elif found:
        verdict = "no cluster named%s: the inventory is not the cluster %s alone (other groups: %s)" % (
            empty, found[0], ", ".join(others))
    else:
        verdict = ("no cluster named%s: the inventory holds no cluster laid out as the import writes it (<cluster>,"
                   " <cluster>_<dc>, <cluster>_<dc>_<rack>; groups: %s)" % (empty, ", ".join(others)))
    raise ValueError(_block(verdict, *(WAY_OUT + hint)))


def _same(given, env, groups, cluster_name):
    """-e cassandra_hosts and CASSANDRA_CLUSTER both set: they name the same group, else ValueError naming both."""
    why = ""
    try:
        other = _named(env, groups, cluster_name)
    except ValueError as exc:
        other, why = None, str(exc).split("\n", maxsplit=1)[0].replace("REFUSED  ", "")
    if other == given:
        return
    said = ("it names the group %s" % other if other == env else "it is the cluster of the group %s" % other) if other \
        else "it names no cluster of the inventory (%s)" % why.rstrip(".")
    raise ValueError(_block("-e cassandra_hosts=%s and %s=%s (from your shell) disagree: %s" % (given, ENV, env, said),
                            "Name one: unset %s, or run without -e cassandra_hosts." % ENV))


def _named(value, groups, cluster_name):
    """The group ENV names: a group with hosts, else the one whose hosts are those with that cluster name."""
    cands = _candidates(groups)
    if value in cands:
        return value
    names = dict((h, cluster_name(h) if cluster_name else None) for members in cands.values() for h in members)
    named = frozenset(h for h, name in names.items() if name == value)
    if not named:
        found = _found(groups)
        unknown = sum(1 for name in names.values() if name is UNKNOWN)
        raise ValueError(_block("%s=%s: no group of that name with hosts, and no host with that cassandra_cluster_name%s"
                                % (ENV, value, (" (%d hosts have one only the node knows: give the group instead)"
                                                % unknown) if unknown else ""),
                                found[0].upper() + found[1:] + ".", *WAY_OUT))
    # the largest groups of those hosts only, laid out as a cluster's (<cluster> with groups <cluster>_...:
    # not linux or monitoring when the name is set for all); prod, not prod_dc1 of the same hosts
    inside = dict((name, members) for name, members in cands.items() if members <= named and
                  any(o.startswith(name + "_") and h <= members for o, h in cands.items()))
    tops = [name for name, members in inside.items() if not any(members < other for other in inside.values())]
    tops = sorted(n for n in tops if not any(n.startswith(o + "_") and inside[o] == inside[n] for o in tops))
    if not inside:
        # no group laid out as a cluster's among them ([prod] p1 p2): the one group of exactly those hosts (not
        # linux, holding a cluster's hosts and more)
        exact = [name for name, members in cands.items() if members == named]
        if len(exact) == 1:
            tops, inside = exact, {exact[0]: named}
    unread = frozenset(h for h, name in names.items() if name is UNKNOWN)
    if len(tops) == 1 and inside[tops[0]] == named:
        # a host whose name is not known could be the rest of a larger group: no guess
        if not any(members > named and members <= named | unread for members in cands.values()):
            return tops[0]
    raise ValueError(_block("%s=%s: the hosts with that cassandra_cluster_name are not one group's (groups: %s)"
                            % (ENV, value, ", ".join(tops) or "none"),
                            "Name the group instead:", "  export %s=<group>" % ENV,
                            "or add -e cassandra_hosts=<group> to the command."))


def top_groups(groups):
    """The groups with hosts that no other group holds (all, ungrouped and
    the runtime groups left out), sorted: one per cluster in an inventory of
    several clusters laid out as the import writes them."""
    cands = _candidates(groups)
    top = [name for name, hosts in cands.items() if not any(hosts < other for other in cands.values())]
    return sorted(n for n in top if not any(n.startswith(o + "_") for o in top))  # orders, not orders_dc1 too


def clusters_found(groups):
    """The groups laid out as a cluster's (<cluster> holding groups <cluster>_...), sorted, not the datacenter
    or rack groups of another: the clusters the refusal names (not monitoring, linux, seeds)."""
    cands = _candidates(groups)
    laid = dict((n, h) for n, h in cands.items() if any(o.startswith(n + "_") and oh <= h for o, oh in cands.items()))
    return sorted(n for n, h in laid.items() if not any(n.startswith(o + "_") and h <= laid[o] for o in laid))


def other_groups(groups, found):
    """The groups that are no cluster's of found nor in one by name (monitoring, seeds, dc1), sorted: why the
    inventory is not one cluster."""
    return sorted(n for n in _candidates(groups) if n not in found and not any(n.startswith(c + "_") for c in found))


def cluster_line(group, why, hosts, state, dc):
    """banner() for these hosts of the group (summary() alone when why is None): state(host) its
    cassandra_node_state, dc(host) its cassandra_dc (UNKNOWN when only the node knows it: dc?; none: the
    role's default, dc1)."""
    gone = [h for h in hosts if str(state(h) or "").strip().lower() == "absent"]
    dcs = [dc(h) for h in hosts if h not in gone]
    dcs = ["dc?" if d is UNKNOWN else (d if d not in (None, "") else "dc1") for d in dcs]
    if why is None:
        return summary(group, len(hosts) - len(gone), dcs, len(gone))
    return banner(group, why, len(hosts) - len(gone), dcs, len(gone))


def summary(group, nodes, dcs, absent=0):
    """"prod (5 nodes, dc1)": nodes, how many hosts of the group are part of the cluster; dcs: their datacenters;
    absent: the hosts marked cassandra_node_state=absent."""
    parts = ["%d node%s" % (nodes, "" if nodes == 1 else "s")]
    if absent:
        parts[0] += " + %d marked absent" % absent
    parts += sorted(set(str(dc) for dc in dcs))
    return "%s (%s)" % (group, ", ".join(parts))


def banner(group, why, nodes, dcs, absent=0):
    """The line the playbooks start with: "CLUSTER prod (5 nodes, dc1) | chosen by: -e cassandra_hosts"."""
    return "CLUSTER %s | chosen by: %s" % (summary(group, nodes, dcs, absent), why)


def not_given(given, loader, inventory=False):
    """Why a cassandra_hosts is refused (None when it is not): from the inventory (group_vars, host_vars:
    inventory, not -e), the plays' hosts would not see it, the play running on another group than its tasks
    name (a playbook's vars, an import_playbook's, reach them: taken). Only under ansible-playbook (its command
    line known); an empty -e one (EMPTY) would leave the cluster to CASSANDRA_CLUSTER or the inventory unsaid."""
    if given is None or not context.CLIARGS.get("args"):
        return None
    extra = load_extra_vars(loader)
    if inventory and "cassandra_hosts" not in extra:
        return ("cassandra_hosts is set in the inventory (%s, group_vars or host_vars): the plays' hosts do not see it."
                " Give it on the command line, -e cassandra_hosts=%s, or %s=%s in your shell." % (given, given, ENV, given))
    if not given and "cassandra_hosts" in extra:
        return EMPTY
    return None


def in_inventory(hostvars, host):
    """Whether the host's hostvars (its inventory variables and the -e ones, not the play's) set cassandra_hosts."""
    try:
        return host in hostvars and "cassandra_hosts" in hostvars[host]
    except AnsibleError:
        return False


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        variables = variables or {}
        given = variables.get("cassandra_hosts")
        if isinstance(given, str):
            given = self._templar.template(given)
        self.set_options(var_options=variables, direct=kwargs)
        refusal = self.get_option("refusal")
        if given is not None and not isinstance(given, str):
            msg = _block("cassandra_hosts must be a group name, not %r" % (given,), "Name the cluster's group: -e cassandra_hosts=<group>")
            if refusal:
                return [msg]
            raise AnsibleLookupError(msg)
        hostvars = variables.get("hostvars") or {}
        refused = not_given(given, self._loader, in_inventory(hostvars, variables.get("inventory_hostname")))

        def read(host, name):
            """A host's variable as the inventory gives it, UNKNOWN when it does not (a fact of the node)."""
            try:
                value = hostvars[host].get(name) if host in hostvars else None
                if isinstance(value, str) and "{{" in value:
                    value = self._templar.template(value)
            except AnsibleError:
                return UNKNOWN
            # a value from a fact or a variable the controller lacks: an undefined marker (ansible-core 2.19+),
            # that fails only where it is shown
            return UNKNOWN if isinstance(value, Undefined) else value

        groups = dict(variables.get("groups") or {})
        try:
            if refused == EMPTY:  # said with the clusters found, as when none is named
                picked("", groups, None, None)  # (several: refused, "(-e cassandra_hosts is empty)" in it)
                raise ValueError(_block("no cluster named (-e cassandra_hosts is empty)", EMPTY + "."))
            if refused:
                raise ValueError(_block(refused))
            group, why = picked(given, groups, os.environ.get(ENV), lambda host: read(host, "cassandra_cluster_name"))
        except ValueError as exc:
            if refusal:
                return [str(exc)]
            raise AnsibleLookupError(str(exc))
        if refusal:
            return [""]
        if self.get_option("banner"):
            return [cluster_line(group, why, list(groups.get(group) or []), lambda h: read(h, "cassandra_node_state"),
                                 lambda h: read(h, "cassandra_dc"))]
        return ["%s (%s)" % (group, why) if self.get_option("explain") else group]
