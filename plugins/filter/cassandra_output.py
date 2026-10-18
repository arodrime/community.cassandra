# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output blocks of plugin_utils/cassandra_output.py, as filters.
Each returns text (a line, or lines), which a debug task marked cassandra_output: true prints.

cassandra_node_list: names -> "node1..node3, node5".
cassandra_duration: seconds -> "1h02m".
cassandra_normal_wait_line: the progress line of the wait for a node to be NORMAL.
cassandra_phase, cassandra_check_what: a phase line, preflight's check line.
cassandra_command, cassandra_todo, cassandra_seeds_todo: what is left to do, and the commands for it.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os

from ansible.utils.display import Display

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

SCREEN_WIDTH = 160  # the screen's width when no width is known (Ansible's display gives one: 79 in a log)


def terminal_width():
    """The width of the terminal the run shows in: Ansible's (its display's, as the ops callback; a worker has no
    terminal of its own from ansible-core 2.19 on), else the terminal's (a column spared), else COLUMNS, else
    SCREEN_WIDTH."""
    try:
        columns = int(getattr(Display(), "columns", 0) or 0)
    except Exception:  # pylint: disable=broad-except  # no display to ask (outside a run): the other sources
        columns = 0
    if columns:
        return max(60, columns)
    for fd in (1, 2):
        try:
            return max(60, os.get_terminal_size(fd).columns - 1)
        except (OSError, ValueError):
            continue
    try:
        return max(60, int(os.environ.get("COLUMNS", "")) - 1)
    except ValueError:
        return SCREEN_WIDTH


def cassandra_node_list(names, keep_order=False, full=False):
    """Node names -> one text, runs of names as ranges (full: every name; keep_order: as given, else sorted the
    natural way): ["node1", "node2", "node3", "node5"] -> "node1..node3, node5"."""
    return out.nodes(names, keep_order=keep_order, full=full)


def cassandra_duration(seconds, short=False):
    """Seconds -> a duration: 3725 -> "1h02m"."""
    return out.duration(seconds, short=short)


# the modes (netstats) nodetool status shows as a state of its own: as its letters (UJ, then UN once NORMAL)
_MODE_STATE = {"JOINING": "UJ", "MOVING": "UM", "LEAVING": "UL"}


def cassandra_normal_wait_line(mode, waited=0, timeout=0, index=0, steps=0, node="", operation="", done=False):
    """The progress line of the wait for a node to be NORMAL (wait_normal.yml): "[2/5] node2 restart  waiting for
    NORMAL (UJ\u2192UN)  1m10s/10m"; mode: netstats' (JOINING, MOVING, LEAVING as their status letters, another one
    as itself: "(mode STARTING)", empty: "(no JMX answer yet)"); done: "[2/5] node2 restart  NORMAL after 1m20s".
    e.g. ("JOINING", waited=70, timeout=600, index=2, steps=5, node="node2", operation="restart") -> "[2/5] node2
    restart  waiting for NORMAL (UJ\u2192UN)  1m10s/10m"."""
    mode = str(mode or "").upper()
    head = ("[%d/%d] %s %s" % (int(index), int(steps), node, operation) if int(steps or 0)
            else ("%s %s" % (node, operation)).strip())
    if done:
        return "  ".join(x for x in [head, "NORMAL after %s" % out.duration(waited)] if x)
    state = (u"%s\u2192UN" % _MODE_STATE[mode]) if mode in _MODE_STATE else ("mode %s" % mode) if mode else "no JMX answer yet"
    return "  ".join(x for x in [head, "waiting for NORMAL (%s)" % state,
                                 "%s/%s" % (out.duration(waited), out.duration(timeout, short=True))] if x)


def _same_sources(a, b):
    def norm(sources):
        sources = [sources] if isinstance(sources, str) else list(sources or [])
        return sorted(os.path.realpath(os.path.expanduser(str(s))) for s in sources if s)
    return norm(a) == norm(b)


def cassandra_command(playbook, inventory=None, hosts=None, limit=None, extra=None, options=None, cwd=None,
                      default_inventory=None):
    """A playbook and the run's options -> the command line that runs it: 'rolling_restart' |
    cassandra_command(inventory='inv.yml', limit=['n1']) -> "ansible-playbook -i inv.yml
    community.cassandra.rolling_restart --limit n1". default_inventory: ansible.cfg's (lookup('config',
    'DEFAULT_HOST_LIST')): no -i when the run's inventory is that one."""
    if default_inventory and _same_sources(inventory, default_inventory):
        inventory = None
    return out.command(playbook, inventory=inventory, hosts=hosts or None, limit=limit or None, extra=extra,
                       options=options, cwd=cwd)


def cassandra_seeds_todo(change, group=""):
    """cassandra_seed_change's result -> the TO DO item ({text, command}) when it picked seeds the inventory does
    not have, None otherwise."""
    return out.seeds_todo(change, group=group)


def cassandra_phase(name, names=None, node="", what="", count=None):
    """A phase line (plugin_utils cassandra_output PHASES): 'decommission' |
    cassandra_phase(node='node3') -> "Decommissioning node3..."."""
    return out.phase(name, names=names, node=node, what=what, count=count)


def cassandra_check_what(present, kind="", targets=None, replacing="", run=None, absent=None):
    """The nodes present and the operation's targets -> what preflight's check line says of the cluster
    (plugin_utils cassandra_output check_what): ["n1", "n2", "n3"] | cassandra_check_what(kind='add',
    targets=['n4']) -> "3 nodes; adding n4"."""
    return out.check_what(present, kind=kind or "", targets=targets, replacing=replacing or "", run=run, absent=absent)


def cassandra_todo(items, title="TO DO"):
    """[{text, command}] -> the TO DO block: [{"text": "restart n1", "command": "ansible-playbook x"}] ->
    ["TO DO", "  1. restart n1:", "     ansible-playbook x"]."""
    return out.todo(items, title=title)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_node_list": cassandra_node_list,
            "cassandra_duration": cassandra_duration,
            "cassandra_normal_wait_line": cassandra_normal_wait_line,
            "cassandra_command": cassandra_command,
            "cassandra_todo": cassandra_todo,
            "cassandra_phase": cassandra_phase,
            "cassandra_check_what": cassandra_check_what,
            "cassandra_seeds_todo": cassandra_seeds_todo,
        }
