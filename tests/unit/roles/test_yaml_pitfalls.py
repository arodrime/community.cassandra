from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Mistakes that pass the syntax check and fail (or silently misbehave) at run
# time. Each was found in this collection once.

import glob
import os
import re

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
FILES = sorted(set(
    f for pattern in ("playbooks/**/*.y*ml", "roles/**/*.y*ml", "tests/integration/**/*.y*ml")
    for f in glob.glob(os.path.join(ROOT, pattern), recursive=True)
))
CONDITIONS = ("when", "that", "failed_when", "changed_when", "until", "var")
ASSERTS = ("ansible.builtin.assert", "ansible.legacy.assert", "assert")
MESSAGES = ("fail_msg", "msg", "success_msg")


def ids(path):
    return os.path.relpath(path, ROOT)


class Loader(yaml.SafeLoader):
    """Safe loader that also reads Ansible's tags (!unsafe, !vault)."""


def construct_tagged(loader, suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


Loader.add_multi_constructor("!", construct_tagged)


def load(path):
    with open(path, encoding="utf-8") as f:
        return [doc for doc in yaml.load_all(f, Loader=Loader) if doc is not None]


def scalars(node, key=None):
    """(key the value belongs to, scalar node) pairs."""
    if isinstance(node, yaml.ScalarNode):
        yield key, node
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from scalars(item, key)
    elif isinstance(node, yaml.MappingNode):
        for key_node, value in node.value:
            yield from scalars(value, key_node.value if isinstance(key_node, yaml.ScalarNode) else None)


def mappings(data):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from mappings(value)
    elif isinstance(data, list):
        for item in data:
            yield from mappings(item)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def where(mapping):
    return mapping.get("name", "(unnamed)")


@pytest.mark.parametrize("path", FILES, ids=ids)
def test_no_doubled_backslash_in_templates(path):
    # YAML unescapes \\ to \ only in double-quoted strings, and Jinja inside
    # Ansible does not unescape again: a regex written '\\S' in a plain,
    # single-quoted or block scalar reaches Python as \\S, a literal
    # backslash, and never matches. Conditions are templates without braces.
    # A literal backslash that is really wanted can be written \x5c.
    with open(path, encoding="utf-8") as f:
        pairs = [pair for doc in yaml.compose_all(f, Loader=Loader) if doc is not None for pair in scalars(doc)]
    bad = ["line %d: %s" % (node.start_mark.line + 1, node.value.strip()[:80])
           for key, node in pairs
           if "\\\\" in node.value and ("{{" in node.value or "{%" in node.value or key in CONDITIONS)]
    assert bad == []


def jinja_code(template):
    """The Jinja code of a template, without what can't be a variable read."""
    code = " ".join(a + " " + b for a, b in re.findall(r"{{(.*?)}}|{%(.*?)%}", template, re.S))
    code = re.sub(r"\\.", "", code)                           # escaped quotes
    code = re.sub(r"'[^']*'|\"[^\"]*\"", "''", code)          # string literals
    code = re.sub(r"\b\w+\s*=(?!=)", "", code)               # keyword arguments, set x =
    code = re.sub(r"\bfor\s+[\w\s,]+\s+in\b", "", code)       # loop variables
    return re.sub(r"\|\s*\w+", "", code)                      # filter names


@pytest.mark.parametrize("path", FILES, ids=ids)
def test_no_variable_defined_from_itself(path):
    # vars: {x: "{{ x | combine(...) }}"} is a recursive template: compute
    # the new value under another name first (set_fact may reuse the name).
    docs = load(path)
    if "/defaults/" in ids(path) or "/vars/" in ids(path):
        scopes = [("defaults/vars", doc) for doc in docs if isinstance(doc, dict)]
    else:
        scopes = [(where(m), m["vars"]) for m in mappings(docs) if isinstance(m.get("vars"), dict)]
    bad = ["%s: %s" % (task, name) for task, scope in scopes for name, value in scope.items()
           for text in strings(value)
           if re.search(r"(?<![\w.])%s\b" % re.escape(name), jinja_code(text))]
    assert bad == []


@pytest.mark.parametrize("path", FILES, ids=ids)
def test_assert_messages_read_only_what_exists(path):
    # An assert's messages are templated whatever the result: a stat result's
    # attributes other than 'exists' are missing when the file is, so the
    # message needs a default, or a test on 'exists' or 'is defined'.
    bad = []
    for mapping in mappings(load(path)):
        args = next((mapping[k] for k in ASSERTS if isinstance(mapping.get(k), dict)), {})
        for key in MESSAGES:
            message = str(args.get(key, ""))
            guarded = re.search(r"\|\s*(default|d)\s*\(|\.stat\.exists|is\s+defined", message)
            used = re.findall(r"(?:\.stat|\[['\"]stat['\"]\])(?:\.(\w+)|\[['\"](\w+)['\"]\])", message)
            if not guarded and any((a or b) != "exists" for a, b in used):
                bad.append("%s: %s" % (where(mapping), message.strip()[:80]))
    assert bad == []
