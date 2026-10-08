# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output blocks of module_utils/cassandra_output.py as
filters, for the playbooks (docs/docsite/rst/guide_output.rst). Each one
returns text lines, which a debug task marked cassandra_output: true prints.

cassandra_node_list: names -> "node1..node5, node7".
cassandra_size, cassandra_rate, cassandra_duration: bytes, bytes/s, seconds as text.
cassandra_setting_lines: {node: value} -> "setting:  value  all" and the other values.
cassandra_by_nodes: [[node, [lines]]] -> each line once under the nodes that have it.
cassandra_plan: {operation, cluster, version, summary, steps, facts, warnings,
    question} -> the plan screen.
cassandra_progress_line: a cassandra_stream_progress state -> the progress line
    and the line of the other ends.
cassandra_recap: [{node, outcome, status, seconds, reason}] -> the end of run recap.
cassandra_perm_lines, cassandra_diff_lines, cassandra_changed_lines: what changed.
cassandra_command, cassandra_todo, cassandra_inventory_steps,
cassandra_in_git_work_tree: what is left to do.
cassandra_mask: text with its secrets as ****.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out


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
        if stream.get("done", 0) < stream.get("total", 0) and not stream.get("moved", True):
            peer["stalled"] = True
    return out.progress_line(index, steps, node, operation, mode=mode, done=state.get("bytes_done", 0),
                             total=state.get("bytes_total", 0), speed=state.get("rate"), now=state.get("now", 0),
                             start=state.get("start"), idle_checks=state.get("idle_checks", 0),
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
            "cassandra_by_nodes": cassandra_by_nodes,
            "cassandra_plan": cassandra_plan,
            "cassandra_progress_line": cassandra_progress_line,
            "cassandra_recap": cassandra_recap,
            "cassandra_perm_lines": cassandra_perm_lines,
            "cassandra_diff_lines": cassandra_diff_lines,
            "cassandra_changed_lines": cassandra_changed_lines,
            "cassandra_command": cassandra_command,
            "cassandra_todo": cassandra_todo,
            "cassandra_inventory_steps": cassandra_inventory_steps,
            "cassandra_in_git_work_tree": cassandra_in_git_work_tree,
            "cassandra_mask": cassandra_mask,
        }
