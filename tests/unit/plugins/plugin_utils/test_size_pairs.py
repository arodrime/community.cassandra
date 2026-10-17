"""Every done/total size pair goes through cassandra_output.amount or sizes:
no other code formats two sizes in one string or a size in the unit of
another (the unit of the pair is picked once, from the total)."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import ast
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
# the functions that print one size
SIZE = {"size", "_size", "_gib", "cassandra_size"}
# where the helpers live, and the filter that exposes size(count, scale) to templates
ALLOWED = {os.path.join("plugins", "plugin_utils", "cassandra_output.py"): None,
           os.path.join("plugins", "filter", "cassandra_output.py"): {"cassandra_size"}}
# two sizes side by side that are not a done/total pair: (file, text of the line)
NOT_PAIRS = {(os.path.join("plugins", "filter", "cassandra_new_node.py"), "on average (nodetool status), this host has")}


# a number formatted with a size unit, not a rate: "%.1f GiB", "%d MiB" (a setting in KB is not a size shown)
_BY_HAND = re.compile(r"%[-+ 0#]*[0-9]*(?:\.[0-9]+)?[dfg]\s*[KMGTP]iB\b(?!/s)")


def _name(call):
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _size_calls(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and _name(n) in SIZE]


def _python_files():
    for top, _dirs, files in os.walk(os.path.join(ROOT, "plugins")):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(top, name)
                yield os.path.relpath(path, ROOT), path


def _bypasses(source, allowed_functions=()):
    """The lines that format a size pair without the helpers."""
    found = []
    tree = ast.parse(source)
    skip = set()
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef) and func.name in allowed_functions:
            skip.update(id(n) for n in ast.walk(func))
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Call) and _name(node) in SIZE and len(node.args) + len(node.keywords) > 1:
            found.append(node.lineno)  # a size in the unit of another one: a pair
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and len(_size_calls(node.right)) > 1:
            found.append(node.lineno)
        elif isinstance(node, ast.Call) and _name(node) == "format" and \
                len([c for a in node.args for c in _size_calls(a)]) > 1:
            found.append(node.lineno)
        elif isinstance(node, ast.JoinedStr) and len(_size_calls(node)) > 1:
            found.append(node.lineno)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and len(_size_calls(node)) > 1:
            found.append(node.lineno)  # _size(a) + "/" + _size(b)
        elif isinstance(node, ast.Call) and _name(node) == "join" and \
                len([c for a in node.args for c in _size_calls(a)]) > 1:
            found.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _BY_HAND.search(node.value):
            found.append(node.lineno)  # "%.1f GiB": a size formatted by hand
    return found


def test_the_scan_finds_a_pair():
    assert _bypasses('x = "%s / %s" % (_size(done), _size(total))') == [1]
    assert _bypasses('x = "%s free of %s" % (_gib(free), _gib(total))') == [1]
    assert _bypasses('x = _size(done, total)') == [1]
    assert _bypasses('x = "{0}/{1}".format(out.size(a), out.size(b))') == [1]
    assert _bypasses('x = _size(a) + "/" + _size(b)') == [1]
    assert _bypasses('x = "/".join([_size(a), _size(b)])') == [1]
    assert _bypasses('x = "%.1f/%.1f GiB" % (a / G, b / G)') == [1]
    assert _bypasses('x = "%d MiB" % a') == [1]
    assert _bypasses('x = "%d MiB/s" % a') == []
    assert _bypasses('x = "%s/%s" % out.sizes(a, b)') == []
    assert _bypasses('x = "%s, %s" % (_size(done), _duration(t))') == []


def test_no_size_pair_bypasses_the_helpers():
    found = []
    for rel, path in _python_files():
        if rel in ALLOWED and ALLOWED[rel] is None:
            continue
        with open(path) as f:
            source = f.read()
        lines = source.split("\n")
        found.extend("%s:%d" % (rel, line) for line in _bypasses(source, ALLOWED.get(rel) or ())
                     if not any(rel == file and text in lines[line - 1] for file, text in NOT_PAIRS))
    assert not found, "a done/total size pair outside cassandra_output.amount/sizes: %s" % found


def test_no_template_prints_two_sizes_on_a_line():
    found = []
    for top in ("roles", "playbooks"):
        for base, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if name.endswith((".yml", ".yaml", ".j2")):
                    path = os.path.join(base, name)
                    with open(path) as f:
                        for number, line in enumerate(f, 1):
                            if len(re.findall(r"cassandra_size\b", line)) > 1:
                                found.append("%s:%d" % (os.path.relpath(path, ROOT), number))
    assert not found
