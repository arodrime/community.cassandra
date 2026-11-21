# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Read a task file as Ansible parses it: its static import_tasks replaced by the tasks they import, so a
test finds a task by name whichever file of the role it was moved to; and find a task or a play by name, wherever
it is (any play, inside a block, its rescue or always), so a test reads what a task does, not where it is."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import yaml

_IMPORT = ("ansible.builtin.import_tasks", "import_tasks")


def load_tasks(path):
    """path: a task file. -> its tasks, each static import_tasks (a file name with no Jinja) replaced by the
    tasks of that file (read from the same directory, recursively), as Ansible applies the import's keywords:
    its when conditions before the task's own, its vars under the task's. Blocks keep their shape."""
    with open(path, encoding="utf-8") as f:
        tasks = yaml.safe_load(f) or []
    return _inline(tasks, os.path.dirname(path))


def _listed(when):
    return [] if when is None else list(when) if isinstance(when, list) else [when]


def _inline(tasks, where):
    out = []
    for task in tasks:
        name = next((task[k] for k in _IMPORT if isinstance(task, dict) and k in task), None)
        if name is not None and "{{" not in name:
            other = set(task) - set(_IMPORT) - {"name", "when", "vars"}
            if other:  # not applied here: a test would read the tasks without them
                raise ValueError("import of %s: %s not supported by load_tasks" % (name, ", ".join(sorted(other))))
            for imported in load_tasks(os.path.join(where, name)):
                if "when" in task:
                    imported["when"] = _listed(task["when"]) + _listed(imported.get("when"))
                if "vars" in task:
                    imported["vars"] = dict(task["vars"], **imported.get("vars") or {})
                out.append(imported)
            continue
        if isinstance(task, dict):
            task = dict(task, **dict((k, _inline(task[k], where)) for k in ("block", "rescue", "always") if k in task))
        out.append(task)
    return out


_SECTIONS = ("pre_tasks", "tasks", "post_tasks", "handlers", "block", "rescue", "always")


def find_task(source, name):
    """The task named name in source: a playbook's plays, a play, a task list (load_tasks) or a task. Looked for
    in every play and inside blocks (their rescue and always too), the outer tasks first: a task file imported at
    the top and again inside a block gives the top one. Fails when there is none, or two different ones at the
    same depth (then give the play or the block as source)."""
    level = [source] if isinstance(source, dict) else list(source)
    while level:
        found = []
        for item in level:
            if isinstance(item, dict) and item.get("name") == name and "hosts" not in item and item not in found:
                found.append(item)  # (a play is no task)
        if len(found) > 1:
            raise AssertionError("%d different tasks named %r" % (len(found), name))
        if found:
            return found[0]
        level = [task for item in level if isinstance(item, dict) for key in _SECTIONS for task in item.get(key) or []]
    raise AssertionError("no task named %r" % name)


def find_play(plays, name):
    """The play named name."""
    found = [play for play in plays if play.get("name") == name]
    if len(found) != 1:
        raise AssertionError("%d plays named %r" % (len(found), name))
    return found[0]
