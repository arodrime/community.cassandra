# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_differs: {node: value} of a setting that must be the same on every
    node -> why the run stops, and where to fix it.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def cassandra_differs(values, setting, group=""):
    """values: {node: value} of a setting that must be the same on every node
    (preflight). Why the run stops, the values by nodes, where to fix it.
    -> the lines that stop the run: why, then each value with its nodes."""
    by_value, shown_as = {}, {}
    for node, value in (values or {}).items():
        by_value.setdefault(str(value), []).append(node)  # as the assert compares them
        shown_as[str(value)] = (", ".join(str(v) for v in value) if isinstance(value, (list, tuple))
                                else str(value) if str(value) else '""')
    ranked = sorted(by_value.items(), key=lambda kv: (-len(kv[1]), out.nodes(kv[1])))
    shown = "; ".join("%s: %s" % (out.nodes(names), shown_as[value] if value != "None" else "(none)")
                      for value, names in ranked)
    where = "group_vars/%s" % group if group else "the cluster's group_vars"
    others = sorted((n for value, names in ranked[1:] for n in names),
                    key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    hosts = ", ".join("host_vars/%s" % n for n in others[:3]) + (", ..." if len(others) > 3 else "")
    if setting == "_cassandra_service_seeds":
        what = "The seed list (cassandra_seeds) is not the same on every node%s (%s)" % (
            " of %s" % group if group else "", shown)
        fix = ("Set cassandra_seeds once in %s (or leave it out: the collection picks the seeds), and remove it "
               "from %s." % (where, hosts or "the host_vars"))
    else:
        what = "%s is not the same on every node%s (%s)" % (setting, " of %s" % group if group else "", shown)
        fix = "Set it once in %s, and remove it from %s." % (where, hosts or "the host_vars")
    return "%s: every node needs the same. %s Nothing was changed." % (what, fix)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_differs": cassandra_differs,
        }
