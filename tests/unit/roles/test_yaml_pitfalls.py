from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Mistakes that pass the syntax check and fail (or silently misbehave) at run
# time. Each was found in this collection once.

import glob
import os
import re

import jinja2
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


@pytest.mark.parametrize("path", FILES, ids=ids)
def test_no_escaped_quote_in_templates(path):
    # ansible-core before 2.19 doubles the backslashes of a template before
    # Jinja reads it: 'can\'t' becomes 'can\\'t', the string ends there and the
    # task fails with a syntax error (seen on 2.16). Put the text between the
    # other quotes instead: "can't".
    with open(path, encoding="utf-8") as f:
        pairs = [pair for doc in yaml.compose_all(f, Loader=Loader) if doc is not None for pair in scalars(doc)]
    bad = ["line %d: %s" % (node.start_mark.line + 1, m.group(0).strip()[:80])
           for key, node in pairs if isinstance(node.value, str)
           for m in re.finditer(r"{{.*?}}|{%.*?%}", node.value, re.S)
           if re.search(r"\\['\"]", m.group(0))]
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


@pytest.mark.parametrize("path", FILES, ids=ids)
def test_conditions_are_strings(path):
    # A condition written with ": " unquoted (e.g. default({'a': 1})) is read
    # as a mapping: "Conditional expressions must be strings" at run time.
    bad = []
    for mapping in mappings(load(path)):
        for key in CONDITIONS[:-1]:  # not "var" (debug)
            values = mapping.get(key)
            for value in values if isinstance(values, list) else [values]:
                if value is not None and not isinstance(value, (str, bool)):
                    bad.append("%s: %s %r" % (where(mapping), key, value))
    assert bad == []


TASK_FILES = sorted(
    f for pattern in ("playbooks/*.yml", "roles/*/tasks/*.yml", "roles/*/handlers/*.yml")
    for f in glob.glob(os.path.join(ROOT, pattern))
)
# Task keywords, everything else in a task is its module
KEYWORDS = {
    "name", "when", "block", "rescue", "always", "vars", "register", "loop", "loop_control", "tags",
    "become", "become_user", "become_method", "changed_when", "failed_when", "check_mode", "until",
    "retries", "delay", "args", "notify", "run_once", "delegate_to", "delegate_facts", "no_log", "diff",
    "throttle", "environment", "ignore_errors", "ignore_unreachable", "module_defaults", "async", "poll",
    "any_errors_fatal", "listen", "timeout", "with_items", "local_action",
}


def task_lists(path):
    docs = load(path)
    if ids(path).startswith("playbooks/"):
        for play in (d for doc in docs for d in (doc if isinstance(doc, list) else [doc])):
            if isinstance(play, dict):
                for key in ("tasks", "pre_tasks", "post_tasks", "handlers"):
                    yield play.get(key) or []
    else:
        for doc in docs:
            yield doc if isinstance(doc, list) else []


def short_names(tasks):
    for task in tasks:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            yield from short_names(task.get(key) or [])
        modules = [key for key in task if key not in KEYWORDS]
        if "block" not in task and modules and not any("." in key for key in modules):
            yield "%s: %s" % (where(task), ", ".join(modules))


@pytest.mark.parametrize("path", TASK_FILES, ids=ids)
def test_modules_have_fully_qualified_names(path):
    # The collection names every module in full (ansible.builtin.copy, not copy)
    bad = [name for tasks in task_lists(path) for name in short_names(tasks)]
    assert bad == []


JINJA = jinja2.Environment(extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"])


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # \s in regexes: Ansible escapes them itself
@pytest.mark.parametrize("path", FILES, ids=ids)
def test_templates_parse(path):
    # A syntax error (a stray parenthesis...) only shows when the task runs.
    with open(path, encoding="utf-8") as f:
        pairs = [pair for doc in yaml.compose_all(f, Loader=Loader) if doc is not None for pair in scalars(doc)]
    bad = []
    for key, node in pairs:
        if not isinstance(node.value, str):
            continue
        sources = [node.value] if "{{" in node.value or "{%" in node.value else []
        if key in CONDITIONS and node.value and not sources:
            sources = ["{{ %s }}" % node.value]
        for source in sources:
            try:
                JINJA.parse(source)
            except jinja2.TemplateSyntaxError as e:
                bad.append("line %d: %s" % (node.start_mark.line + 1, e.message))
    assert bad == []


TEMPLATES = sorted(glob.glob(os.path.join(ROOT, "roles/*/templates/**/*.j2"), recursive=True))


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_template_backreferences_are_escaped(path):
    # In .j2 files Jinja unescapes string literals itself: '\1' there is
    # chr(1), and regex_search/regex_replace get no backreference.
    with open(path, encoding="utf-8") as f:
        text = f.read()
    bad = ["line %d" % (text.count("\n", 0, m.start()) + 1)
           for m in re.finditer(r"(?<!\\)'\\\d'|(?<!\\)\"\\\d\"", text)]
    assert bad == []
