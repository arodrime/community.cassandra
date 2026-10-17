# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output blocks of plugin_utils/cassandra_output.py the playbooks use, as filters
(docs/docsite/rst/guide_output.rst). Each returns text (a line, or lines), which a debug task marked
cassandra_output: true prints.

cassandra_node_list: names -> "node1..node3, node5".
cassandra_size, cassandra_duration: bytes, seconds -> "1.5 GiB", "1h02m".
cassandra_progress_line: a cassandra_stream_progress state -> the progress line and the line of the other ends.
cassandra_phase, cassandra_check_what: a phase line, preflight's check line.
cassandra_prepare_lines, cassandra_left_system_warning: what the prepare of new nodes did.
cassandra_command, cassandra_todo, cassandra_removed_todo, cassandra_seeds_todo, cassandra_seeds_reload_todo,
cassandra_reimport_hosts: what is left to do, and the commands for it.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os

from ansible.module_utils.parsing.convert_bool import boolean
from ansible.utils.display import Display

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

SCREEN_WIDTH = 160  # the screen's width when no width is known (Ansible's display gives one: 79 in a log)


def terminal_width():
    """The width of the terminal the run shows in: Ansible's (its display's, as the ops callback; a worker has no
    terminal of its own from ansible-core 2.19 on), else the terminal's (a column spared), else COLUMNS, else
    SCREEN_WIDTH."""
    try:
        columns = int(getattr(Display(), "columns", 0) or 0)
    except Exception:  # pylint: disable=broad-except
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


# a peer with data left is called stalled once it has moved nothing for as long as the main line says STALLED
# (the state's warn), this long at least, seconds
_PEER_STALL = 60


def cassandra_node_list(names, keep_order=False, full=False):
    """Node names -> one text, runs of names as ranges (full: every name; keep_order: as given, else sorted the
    natural way): ["node1", "node2", "node3", "node5"] -> "node1..node3, node5"."""
    return out.nodes(names, keep_order=keep_order, full=full)


def cassandra_size(count, scale=None):
    """Bytes -> a size (scale: the bytes of the unit to write it in, e.g. the total's): 1610612736 -> "1.5 GiB"."""
    return out.size(count, scale)


def cassandra_duration(seconds, short=False):
    """Seconds -> a duration: 3725 -> "1h02m"."""
    return out.duration(seconds, short=short)


def cassandra_progress_line(state, index=0, steps=0, node="", operation="", mode="", names=None, status="going"):
    """state: a cassandra_stream_progress state -> the progress line and the line of the other ends (as lines of
    one text, plugin_utils cassandra_output progress_line); names: {address: inventory name} for the other ends. The other ends: their progress summed per node,
    with the time a stream with bytes left moved nothing (the longest),
    stalled from the time the main line says STALLED (the state's warn). The
    peers in columns as many as fit in the terminal."""
    state = state or {}
    peers = {}
    warn = state.get("warn", state.get("limit", 0))
    stall = max(_PEER_STALL, warn)
    # the receiving node's disk grew meanwhile: a file on its way, from one of them
    writing = state.get("disk_at") is not None and state.get("now", 0) - state["disk_at"] < stall
    for stream in (state.get("streams") or {}).values():
        name = (names or {}).get(stream["other"], stream["other"])
        if name == node:  # a node's own tasks (cleanup)
            continue
        peer = peers.setdefault((stream.get("way", "with"), name), {"name": name, "way": stream.get("way", "with"),
                                                                    "done": 0, "total": 0, "stalled": False,
                                                                    "idle": 0})
        peer["done"] += stream.get("done", 0)
        peer["total"] += stream.get("total", 0)
        # still to send, and nothing moved for a while (a single quiet check is no news)
        if stream.get("done", 0) < stream.get("total", 0) and not stream.get("moved", True):
            peer["idle"] = max(peer["idle"], state.get("now", 0) - stream.get("since", 0))
            peer["stalled"] = peer["stalled"] or (peer["idle"] >= stall and not writing)
    return out.progress_line(index, steps, node, operation, mode=mode, done=state.get("bytes_done", 0),
                             total=state.get("bytes_total", 0), speed=state.get("rate"), now=state.get("now", 0),
                             start=state.get("start"), idle=state.get("idle", 0) if state.get("idle_checks") else 0,
                             limit=state.get("limit", 0), peers=[p for p in peers.values() if p["total"]],
                             status=status, warn=warn, disk=state.get("disk_grown", 0),
                             frozen=state.get("now", 0) - state.get("netstats_at", state.get("now", 0)),
                             width=terminal_width())


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


def cassandra_removed_todo(removed, reimport_from="", inventory=None, hosts=None, inventory_dir=None, cwd=None,
                           default_inventory=None, in_git=None, unreachable=None, imported=True, reimport_hosts="",
                           first=None):
    """The nodes removed from the cluster (removed), a node that stays (reimport_from) -> the TO DO lines that
    take them out of the inventory (a re-import, else by hand), with their commands. default_inventory:
    ansible.cfg's (lookup('config', 'DEFAULT_HOST_LIST')): no -i when the run's inventory is that one.
    reimport_hosts: see cassandra_reimport_hosts."""
    if default_inventory and _same_sources(inventory, default_inventory):
        inventory = None
    return out.removed_todo(removed, reimport_from, inventory=inventory, hosts=hosts or None,
                            inventory_dir=inventory_dir or None, cwd=cwd, in_git=in_git, unreachable=unreachable,
                            imported=imported, reimport_hosts=reimport_hosts, first=first)


def cassandra_seeds_todo(change, group=""):
    """cassandra_seed_change's result -> the TO DO item ({text, command}) when it picked seeds the inventory does
    not have, None otherwise."""
    return out.seeds_todo(change, group=group)


def cassandra_seeds_reload_todo(failed, command_line):
    """The nodes whose nodetool reloadseeds failed, its command line -> the TO DO item, None when none:
    ["n1"], "nodetool reloadseeds" -> {"text": "reload the seed list on n1: ...", "command": "nodetool reloadseeds"}."""
    return out.seeds_reload_todo(failed, command_line)


def cassandra_reimport_hosts(group, hosts="", env_cluster="", files=True, others=0):
    """The -e cassandra_hosts of a re-import (import_cluster reading the
    cluster from the inventory), '' when it takes the cluster without it:
    group: the cluster's group (lookup community.cassandra.cassandra_hosts);
    hosts: the run's -e cassandra_hosts; env_cluster: CASSANDRA_CLUSTER;
    files: the inventory sources are files (the import reads
    CASSANDRA_CLUSTER only then); others: how many hosts of the inventory
    are not in that group (none: the import reads every host, this cluster's). 'c1' |
    cassandra_reimport_hosts(others=2) -> 'c1'."""
    if hosts:
        return hosts
    if env_cluster and files:
        return ""
    return group if others else ""


def cassandra_phase(name, names=None, node="", what="", count=None):
    """A phase line (plugin_utils cassandra_output PHASES): 'decommission' |
    cassandra_phase(node='node3') -> "Decommissioning node3..."."""
    return out.phase(name, names=names, node=node, what=what, count=count)


def cassandra_prepare_lines(hosts, check=False):
    """hosts: [{name, ran, repository, packages, cassandra, ...}] (plugin_utils cassandra_output prepare_lines) -> a
    line per step and group of nodes: "node4, node5  repository  ok (already set)"."""
    return out.prepare_lines(hosts, check=boolean(check, strict=False))


def cassandra_left_system_warning(hosts):
    """hosts: [{name, left: [directories holding a system keyspace], dirs: [its data directories]}] -> the WARNING
    lines about an old system keyspace left outside the data directories of new nodes (plugin_utils
    cassandra_output left_system_warning)."""
    return out.left_system_warning(hosts)


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
            "cassandra_size": cassandra_size,
            "cassandra_duration": cassandra_duration,
            "cassandra_progress_line": cassandra_progress_line,
            "cassandra_command": cassandra_command,
            "cassandra_seeds_reload_todo": cassandra_seeds_reload_todo,
            "cassandra_todo": cassandra_todo,
            "cassandra_phase": cassandra_phase,
            "cassandra_check_what": cassandra_check_what,
            "cassandra_prepare_lines": cassandra_prepare_lines,
            "cassandra_left_system_warning": cassandra_left_system_warning,
            "cassandra_removed_todo": cassandra_removed_todo,
            "cassandra_reimport_hosts": cassandra_reimport_hosts,
            "cassandra_seeds_todo": cassandra_seeds_todo,
        }
