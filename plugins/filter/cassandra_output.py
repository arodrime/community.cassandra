# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output blocks of module_utils/cassandra_output.py as
filters, for the playbooks (docs/docsite/rst/guide_output.rst). Each one
returns text lines, which a debug task marked cassandra_output: true prints.

cassandra_node_list: names -> "node1..node5, node7".
cassandra_size, cassandra_rate, cassandra_duration: bytes, bytes/s, seconds as text.
cassandra_setting_lines: {node: value} -> "setting: value  all" and the other values.
cassandra_settings_lines: several settings, their names, values and nodes in aligned columns.
cassandra_by_nodes: [[node, [lines]]] -> each line once under the nodes that have it.
cassandra_plan: {operation, cluster, version, summary, steps, facts, warnings,
    question} -> the plan screen.
cassandra_progress_line: a cassandra_stream_progress state -> the progress line
    and the line of the other ends.
cassandra_prepare_lines, cassandra_left_system_warning: what the prepare of new nodes did.
cassandra_recap: [{node, outcome, status, seconds, reason}] -> the end of run recap.
cassandra_perm_lines, cassandra_diff_lines, cassandra_changed_lines: what changed.
cassandra_command, cassandra_todo, cassandra_inventory_steps,
cassandra_in_git_work_tree: what is left to do.
cassandra_mask: text with its secrets as ****.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

# a peer with data left is called stalled once it has moved nothing for this long, seconds
_PEER_STALL = 60


def cassandra_node_list(names, keep_order=False, full=False):
    return out.nodes(names, keep_order=keep_order, full=full)


def cassandra_size(count, scale=None):
    return out.size(count, scale)


def cassandra_rate(per_second):
    return out.rate(per_second)


def cassandra_duration(seconds, short=False):
    return out.duration(seconds, short=short)


def cassandra_setting_lines(values, setting, all_nodes=None, expected=None, notes=None, where=None, full=False,
                            indent=""):
    return out.setting_lines(setting, values or {}, all_nodes=all_nodes, expected=expected, notes=notes, where=where,
                             full=full, indent=indent)


def cassandra_settings_lines(settings, indent="  ", max_value=30, max_label=40, max_nodes=30):
    """settings: [[setting, [[value, nodes, note]]] or a heading str] -> aligned lines."""
    entries = [s if isinstance(s, str) else (s[0], [tuple(r) for r in s[1]]) for s in settings or []]
    return out.settings_lines(entries, indent=indent, max_value=max_value, max_label=max_label, max_nodes=max_nodes)


def cassandra_by_nodes(pairs, full=False, every="all"):
    return out.by_nodes([(p[0], p[1]) for p in pairs or []], full=full, every=every)


def cassandra_plan(spec, check=False):
    """spec: the arguments of module_utils cassandra_output.plan as a dict."""
    spec = dict(spec or {})
    return out.plan(spec.pop("operation", ""), check=check, **spec)


def cassandra_progress_line(state, index=0, steps=0, node="", operation="", mode="", names=None, status="going"):
    """state: a cassandra_stream_progress state; names: {address: inventory
    name} for the other ends. The other ends: their progress summed per node,
    stalled when they have bytes left and moved nothing at this check."""
    state = state or {}
    peers = {}
    for stream in (state.get("streams") or {}).values():
        name = (names or {}).get(stream["other"], stream["other"])
        if name == node:  # a node's own tasks (cleanup)
            continue
        peer = peers.setdefault((stream.get("way", "with"), name), {"name": name, "way": stream.get("way", "with"),
                                                                    "done": 0, "total": 0, "stalled": False})
        peer["done"] += stream.get("done", 0)
        peer["total"] += stream.get("total", 0)
        # still to send, and nothing moved for a while (a single quiet check is no stall)
        if stream.get("done", 0) < stream.get("total", 0) and not stream.get("moved", True) \
                and state.get("now", 0) - stream.get("since", 0) >= _PEER_STALL:
            peer["stalled"] = True
    return out.progress_line(index, steps, node, operation, mode=mode, done=state.get("bytes_done", 0),
                             total=state.get("bytes_total", 0), speed=state.get("rate"), now=state.get("now", 0),
                             start=state.get("start"), idle=state.get("idle", 0) if state.get("idle_checks") else 0,
                             limit=state.get("limit", 0), peers=[p for p in peers.values() if p["total"]],
                             status=status)


def cassandra_recap(outcomes, operation="", cluster="", check=False, seconds=None, extra=None, full=False):
    return out.recap(operation, cluster, [dict(o) for o in outcomes or []], check=check, seconds=seconds,
                     extra=extra, full=full)


def cassandra_perm_lines(changes, indent="  "):
    return out.perm_lines(changes, indent=indent)


def cassandra_diff_lines(before, after, indent="  "):
    return out.diff_lines(before, after, indent=indent)


def cassandra_changed_lines(diff, indent="  "):
    return out.changed_lines(diff, indent=indent)


def _same_sources(a, b):
    def norm(sources):
        sources = [sources] if isinstance(sources, str) else list(sources or [])
        return sorted(os.path.realpath(os.path.expanduser(str(s))) for s in sources if s)
    return norm(a) == norm(b)


def cassandra_command(playbook, inventory=None, hosts=None, limit=None, extra=None, options=None, cwd=None,
                      default_inventory=None):
    """default_inventory: ansible.cfg's (lookup('config', 'DEFAULT_HOST_LIST')):
    no -i when the run's inventory is that one."""
    if default_inventory and _same_sources(inventory, default_inventory):
        inventory = None
    return out.command(playbook, inventory=inventory, hosts=hosts or None, limit=limit or None, extra=extra,
                       options=options, cwd=cwd)


def cassandra_removed_todo(removed, reimport_from="", inventory=None, hosts=None, inventory_dir=None, cwd=None,
                           default_inventory=None, in_git=None, unreachable=None, imported=True, reimport_hosts="",
                           first=None):
    """default_inventory: ansible.cfg's (lookup('config', 'DEFAULT_HOST_LIST')):
    no -i when the run's inventory is that one. reimport_hosts: see
    cassandra_reimport_hosts."""
    if default_inventory and _same_sources(inventory, default_inventory):
        inventory = None
    return out.removed_todo(removed, reimport_from, inventory=inventory, hosts=hosts or None,
                            inventory_dir=inventory_dir or None, cwd=cwd, in_git=in_git, unreachable=unreachable,
                            imported=imported, reimport_hosts=reimport_hosts, first=first)


def cassandra_seeds_todo(change, group=""):
    """cassandra_seed_change's -> the TO DO item when it picked seeds the
    inventory does not have (None otherwise)."""
    return out.seeds_todo(change, group=group)


def cassandra_seeds_reload_todo(failed, command_line):
    """The nodes whose nodetool reloadseeds failed -> the TO DO item (None
    when none)."""
    return out.seeds_reload_todo(failed, command_line)


def cassandra_reimport_hosts(group, hosts="", env_cluster="", files=True, others=0):
    """The -e cassandra_hosts of a re-import (import_cluster reading the
    cluster from the inventory), '' when it takes the cluster without it:
    group: the cluster's group (lookup community.cassandra.cassandra_hosts);
    hosts: the run's -e cassandra_hosts; env_cluster: CASSANDRA_CLUSTER;
    files: the inventory sources are files (the import reads
    CASSANDRA_CLUSTER only then); others: how many hosts of the inventory
    are not in that group (none: the import reads every host, this cluster's)."""
    if hosts:
        return hosts
    if env_cluster and files:
        return ""
    return group if others else ""


def cassandra_phase(name, names=None, node="", what="", count=None):
    """A phase line (module_utils cassandra_output PHASES): 'decommission' |
    cassandra_phase(node='node3') -> "Decommissioning node3..."."""
    return out.phase(name, names=names, node=node, what=what, count=count)


def cassandra_prepare_lines(hosts, check=False):
    """What the prepare of new nodes did, a line per step and group of nodes
    (module_utils cassandra_output prepare_lines)."""
    return out.prepare_lines(hosts, check=boolean(check, strict=False))


def cassandra_left_system_warning(hosts):
    """The WARNING about an old system keyspace left outside the data
    directories of new nodes (module_utils cassandra_output left_system_warning)."""
    return out.left_system_warning(hosts)


def cassandra_check_what(present, kind="", targets=None, replacing="", run=None, absent=None):
    """What preflight's check line says of the cluster (module_utils
    cassandra_output check_what): "3 nodes; adding node4, node5"."""
    return out.check_what(present, kind=kind or "", targets=targets, replacing=replacing or "", run=run, absent=absent)


def cassandra_path_from(path, cwd=None):
    """path relative to cwd when it is under it, else as given."""
    return out.path_from(str(path), cwd)


def cassandra_todo(items, title="TO DO"):
    return out.todo(items, title=title)


def cassandra_inventory_steps(inventory_file, in_git=None, message="", cwd=None):
    return out.inventory_steps(inventory_file, in_git=in_git, message=message, cwd=cwd)


def cassandra_in_git_work_tree(path):
    return out.in_git_work_tree(path)


def cassandra_mask(text):
    return out.mask(text)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_node_list": cassandra_node_list,
            "cassandra_size": cassandra_size,
            "cassandra_rate": cassandra_rate,
            "cassandra_duration": cassandra_duration,
            "cassandra_setting_lines": cassandra_setting_lines,
            "cassandra_settings_lines": cassandra_settings_lines,
            "cassandra_by_nodes": cassandra_by_nodes,
            "cassandra_plan": cassandra_plan,
            "cassandra_progress_line": cassandra_progress_line,
            "cassandra_recap": cassandra_recap,
            "cassandra_perm_lines": cassandra_perm_lines,
            "cassandra_diff_lines": cassandra_diff_lines,
            "cassandra_changed_lines": cassandra_changed_lines,
            "cassandra_command": cassandra_command,
            "cassandra_seeds_reload_todo": cassandra_seeds_reload_todo,
            "cassandra_todo": cassandra_todo,
            "cassandra_phase": cassandra_phase,
            "cassandra_check_what": cassandra_check_what,
            "cassandra_prepare_lines": cassandra_prepare_lines,
            "cassandra_left_system_warning": cassandra_left_system_warning,
            "cassandra_path_from": cassandra_path_from,
            "cassandra_removed_todo": cassandra_removed_todo,
            "cassandra_reimport_hosts": cassandra_reimport_hosts,
            "cassandra_seeds_todo": cassandra_seeds_todo,
            "cassandra_inventory_steps": cassandra_inventory_steps,
            "cassandra_in_git_work_tree": cassandra_in_git_work_tree,
            "cassandra_mask": cassandra_mask,
        }
