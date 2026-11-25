# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Read a task file as Ansible parses it: its static import_tasks replaced by the tasks they import, and the tasks
of its static include_tasks under them (a part of the role behind one include), so a test finds a task by name
whichever file of the role it was moved to; and find a task or a play by name, wherever
it is (any play, inside a block, its rescue or always), so a test reads what a task does, not where it is; and the
script a shell task runs, inline or in a template of the playbook."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re

import yaml

_IMPORT = ("ansible.builtin.import_tasks", "import_tasks")
_INCLUDE = ("ansible.builtin.include_tasks", "include_tasks")


def load_tasks(path, _loading=()):
    """path: a task file. -> its tasks, each static import_tasks (a file name with no Jinja) replaced by the
    tasks of that file (read from the same directory, recursively), as Ansible applies the import's keywords:
    its when conditions before the task's own, its vars under the task's. Blocks keep their shape. A static
    include_tasks (a file name, no other keyword than name, when and vars) is kept, the tasks of its file under
    it as a block's (its when and vars on it, as Ansible applies them to the include); not a file that includes
    itself (left as it is)."""
    with open(path, encoding="utf-8") as f:
        tasks = yaml.safe_load(f) or []
    return _inline(tasks, os.path.dirname(path), _loading + (os.path.abspath(path),))


def _listed(when):
    return [] if when is None else list(when) if isinstance(when, list) else [when]


def _inline(tasks, where, loading):
    out = []
    for task in tasks:
        name = next((task[k] for k in _IMPORT if isinstance(task, dict) and k in task), None)
        if name is not None and "{{" not in name:
            other = set(task) - set(_IMPORT) - {"name", "when", "vars"}
            if other:  # not applied here: a test would read the tasks without them
                raise ValueError("import of %s: %s not supported by load_tasks" % (name, ", ".join(sorted(other))))
            for imported in load_tasks(os.path.join(where, name), loading):
                if "when" in task:
                    imported["when"] = _listed(task["when"]) + _listed(imported.get("when"))
                if "vars" in task:
                    imported["vars"] = dict(task["vars"], **imported.get("vars") or {})
                out.append(imported)
            continue
        included = next((task[k] for k in _INCLUDE if isinstance(task, dict) and k in task), None)
        if (isinstance(included, str) and "{{" not in included and os.path.abspath(os.path.join(where, included)) not in loading
                and not set(task) - set(_INCLUDE) - {"name", "when", "vars"}):
            out.append(dict(task, block=load_tasks(os.path.join(where, included), loading)))
            continue
        if isinstance(task, dict):
            task = dict(task, **dict((k, _inline(task[k], where, loading)) for k in ("block", "rescue", "always") if k in task))
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


# a shell task whose script is a template or a file of the playbook: shell: "{{ lookup('ansible.builtin.template', ...)
# }}", or "{{ lookup('ansible.builtin.file', ..., rstrip=false) }}"
_SCRIPT_LOOKUP = re.compile(r"^\{\{ lookup\('ansible\.builtin\.(template|file)', playbook_dir ~ '/([^']+)'(?:, rstrip=false)?\) \}\}$")
_LEADING_COMMENT = re.compile(r"\A\{#.*?#\}\n?", re.S)
PLAYBOOKS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "playbooks"))


def shell_script(task):
    """The script a shell task of a playbook runs, as written: its inline text, the playbook's file it looks up, or
    the playbook's template it looks up without the Jinja comment that starts it; its Jinja ({{ }}) left as it is."""
    text = task["ansible.builtin.shell"]
    looked_up = _SCRIPT_LOOKUP.match(text)
    if not looked_up:
        return text
    with open(os.path.join(PLAYBOOKS, looked_up.group(2)), encoding="utf-8") as f:
        script = f.read()
    return _LEADING_COMMENT.sub("", script, count=1) if looked_up.group(1) == "template" else script
