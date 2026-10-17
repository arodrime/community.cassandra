# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Read a task file as Ansible parses it: its static import_tasks replaced by the tasks they import, so a
test finds a task by name whichever file of the role it was moved to."""
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
