# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output blocks of plugin_utils/cassandra_output.py, as filters.
Each returns text (a line, or lines), which a debug task marked cassandra_output: true prints.

cassandra_normal_wait_line: the progress line of the wait for a node to be NORMAL.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

# the modes (netstats) nodetool status shows as a state of its own: as its letters (UJ, then UN once NORMAL)
_MODE_STATE = {"JOINING": "UJ", "MOVING": "UM", "LEAVING": "UL"}


def cassandra_normal_wait_line(mode, waited=0, timeout=0, node="", operation="", done=False):
    """The progress line of the wait for a node to be NORMAL (wait_normal.yml): "node2 restart  waiting for
    NORMAL (UJ\u2192UN)  1m10s/10m"; mode: netstats' (JOINING, MOVING, LEAVING as their status letters, another one
    as itself: "(mode STARTING)", empty: "(no JMX answer yet)"); done: "node2 restart  NORMAL after 1m20s"."""
    mode = str(mode or "").upper()
    head = ("%s %s" % (node, operation)).strip()
    if done:
        return "  ".join(x for x in [head, "NORMAL after %s" % out.duration(waited)] if x)
    state = (u"%s\u2192UN" % _MODE_STATE[mode]) if mode in _MODE_STATE else ("mode %s" % mode) if mode else "no JMX answer yet"
    return "  ".join(x for x in [head, "waiting for NORMAL (%s)" % state,
                                 "%s/%s" % (out.duration(waited), out.duration(timeout, short=True))] if x)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_normal_wait_line": cassandra_normal_wait_line,
        }
