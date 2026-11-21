from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Readability guardrails of the playbooks and roles: no Jinja string longer than 300 characters (a filter of
# plugins/filter instead; a bare expression of when, that, until... too), no inline shell longer than 20 lines (a
# script in files/ instead; any shell, command or raw task, named or not). The ones there already
# are listed in readability_allowed.txt with their size: the list may only shrink. A listed string may not
# grow; once it is short enough (or gone), its line is removed from the list.
#
# A string is named by its file and its place in it: the names of the plays, blocks and tasks above it, then its
# keys ("playbooks/topology.yml :: Plan the topology change / Show the plan :: vars._lines").

import glob
import os

import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import TOP

JINJA_MAX = 300  # characters
SHELL_MAX = 20  # lines
ALLOWED = os.path.join(os.path.dirname(__file__), "readability_allowed.txt")


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!vault", lambda loader, node: loader.construct_scalar(node))
_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _files():
    found = set()
    for pattern in ("playbooks/**/*.yml", "roles/**/*.yml"):
        found.update(path for path in glob.glob(os.path.join(TOP, pattern), recursive=True) if "/molecule/" not in path)
    return sorted(found)


def _strings(node, place, keys):
    """(place, keys, text) of each string in node. place: the names of the plays, blocks and tasks above it."""
    if isinstance(node, str):
        yield place, keys, node
    elif isinstance(node, dict):
        if isinstance(node.get("name"), str):
            place, keys = place + [node["name"]], []
        for key, value in node.items():
            yield from _strings(value, place, keys + [str(key)])
    elif isinstance(node, list):
        for i, value in enumerate(node):
            named = isinstance(value, dict) and isinstance(value.get("name"), str)
            yield from _strings(value, place, keys if named else keys + [str(i)])


# a bare Jinja expression (no {{ }}): the value of these keys, or an item of their list
BARE = ("when", "that", "until", "failed_when", "changed_when")
# a task's inline script: the free form of these modules, their cmd, or the task's args.cmd
SCRIPT_MODULES = [prefix + module for prefix in ("", "ansible.builtin.", "ansible.legacy.")
                  for module in ("shell", "command", "raw")]


def _kinds(keys, text):
    """[(kind, size)] of text at keys (the keys under its task) that is over its limit."""
    sizes = []
    named = [key for key in keys if not key.isdigit()]  # (an item of a list: the key of the list)
    bare = bool(named) and named[-1] in BARE
    if ("{{" in text or "{%" in text or bare) and len(text) > JINJA_MAX:
        sizes.append(("jinja", len(text)))
    free_form = bool(keys) and keys[-1] in SCRIPT_MODULES
    cmd = len(keys) > 1 and keys[-2] in SCRIPT_MODULES + ["args"] and keys[-1] == "cmd"
    if (free_form or cmd) and len(text.splitlines()) > SHELL_MAX:
        sizes.append(("shell", len(text.splitlines())))
    return sizes


def measured():
    """{"<file> :: <place> :: <keys> (jinja|shell)": size} of every string over its limit (size: characters for a
    Jinja string, lines for an inline shell). The same name twice in a file: #2, #3... after it, numbered among
    every string of that name, so that the first one going under its limit does not rename the others."""
    over = {}
    for path in _files():
        with open(path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=_Loader)
        rel = os.path.relpath(path, TOP)
        seen = {}
        for place, keys, text in _strings(data, [], []):
            name = "%s :: %s :: %s" % (rel, " / ".join(place), ".".join(keys))
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name += " #%d" % seen[name]
            for kind, size in _kinds(keys, text):
                over["%s (%s)" % (name, kind)] = size
    return over


def allowed():
    listed = {}
    with open(ALLOWED, encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                size, name = line.rstrip("\n").split("\t", 1)
                listed[name] = int(size)
    return listed


def test_no_new_long_jinja_string_or_inline_shell():
    new = sorted(name for name in measured() if name not in allowed())
    assert not new, "longer than %d characters (Jinja) or %d lines (shell), a filter or a script instead:\n%s" % (
        JINJA_MAX, SHELL_MAX, "\n".join(new))


def test_a_listed_one_does_not_grow():
    now, listed = measured(), allowed()
    grown = sorted("%s: %d, listed %d" % (name, now[name], listed[name]) for name in listed
                   if name in now and now[name] > listed[name])
    assert not grown, "\n".join(grown)


def test_the_list_only_shrinks():
    now = measured()
    gone = sorted(name for name in allowed() if name not in now)
    assert not gone, "short enough now, or gone: remove it from %s:\n%s" % (ALLOWED, "\n".join(gone))
