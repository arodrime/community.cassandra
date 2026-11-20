from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Readability guardrails of the playbooks and roles: no Jinja string longer than 300 characters (a filter of
# plugins/filter instead), no inline shell longer than 20 lines (a script in files/ instead). The ones there already
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


def measured():
    """{"<file> :: <place> :: <keys> (jinja|shell)": size} of every string over its limit (size: characters for a
    Jinja string, lines for an inline shell). The same name twice in a file: #2, #3... after it."""
    over = {}
    for path in _files():
        with open(path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=_Loader)
        rel = os.path.relpath(path, TOP)
        for place, keys, text in _strings(data, [], []):
            name = "%s :: %s :: %s" % (rel, " / ".join(place), ".".join(keys))
            sizes = []
            if ("{{" in text or "{%" in text) and len(text) > JINJA_MAX:
                sizes.append(("jinja", len(text)))
            if keys[:1] in (["shell"], ["ansible.builtin.shell"]) and keys[1:] in ([], ["cmd"]) \
                    and len(text.splitlines()) > SHELL_MAX:
                sizes.append(("shell", len(text.splitlines())))
            for kind, size in sizes:
                key = "%s (%s)" % (name, kind)
                n = 2
                while key in over:
                    key = "%s #%d (%s)" % (name, n, kind)
                    n += 1
                over[key] = size
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
