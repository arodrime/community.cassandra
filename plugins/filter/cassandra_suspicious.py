# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_suspicious_settings: {node: its variables} -> a WARNING line per
setting of SUSPICIOUS some nodes have (the preflight playbook and the
import_cluster report say them; nothing is refused): the nodes, the setting as
cassandra.yaml has it, and why it is risky."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

DASH = u"—"


def _false(value):
    """A boolean variable set to false; anything else (null, unset, a template not resolved, "") is not."""
    try:
        return value is not None and not boolean(value, strict=True)
    except (TypeError, ValueError):
        return False


# (variable, what makes it suspicious, the setting as written, why); add a row for another one
SUSPICIOUS = [
    ("cassandra_auto_bootstrap", _false, "auto_bootstrap: false",
     "a node that loses its data restarts empty without streaming it back; remove the setting (default true) unless"
     " you know why it is there"),
]


def suspicious(nodes):
    """nodes: {name: {variable: value}} (a node's hostvars will do) -> [(the names, the setting, why)], in the order
    of SUSPICIOUS."""
    found = []
    for var, test, setting, why in SUSPICIOUS:
        names = [str(name) for name, values in (nodes or {}).items() if var in (values or {}) and test(values[var])]
        if names:
            found.append((names, setting, why))
    return found


def cassandra_suspicious_settings(nodes):
    """Filter: a "WARNING  node4: auto_bootstrap: false - ..." line per row of suspicious(nodes)."""
    return ["WARNING  %s: %s %s %s" % (out.nodes(names), setting, DASH, why) for names, setting, why in suspicious(nodes)]


class FilterModule(object):
    def filters(self):
        return {"cassandra_suspicious_settings": cassandra_suspicious_settings}
