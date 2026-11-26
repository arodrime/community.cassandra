from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A boolean setting given as text ("false" from an INI hosts file, -e x=false) is a non-empty text: read bare in a
# condition (when, {% if %}, a ternary), it is true. Every read of a variable whose role default is a boolean goes
# through Ansible's bool filter; a module's own parameter apart (the module reads it as a boolean).

import glob
import os
import re

import yaml

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_layout import roles_defaults

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
BOOLS = sorted(k for k, v in roles_defaults().items() if isinstance(v, bool))
# a variable in a condition: after if, not, and, or, or first in a when; not tested otherwise (is, ==, in, a filter
# but bool, an attribute)
_IN_CONDITION = re.compile(r"(?:^|\bif\s+|\bnot\s+|\band\s+|\bor\s+|\(\s*)(%s)\b" % "|".join(map(re.escape, BOOLS)))
_READ_RIGHT = re.compile(r"\s*(?:\|\s*default\([^)]*\)\s*)?\|\s*bool\b|\s*(?:is|==|!=|in)\b|\s*[.\[]")
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def _bare(expression):
    """The boolean settings expression reads bare in a condition ([names]); its quoted texts left out (a message
    naming a setting)."""
    expression = re.sub(r"'[^']*'|\"[^\"]*\"", "''", expression)
    found = []
    for m in _IN_CONDITION.finditer(expression):
        rest = expression[m.end():]
        if re.search(r"\w\(\s*$", expression[:m.start(1)]):
            continue  # (an argument: default(x), a filter's: read by what it is given to)
        if not _READ_RIGHT.match(rest) and re.match(r"\s*(?:$|else\b|and\b|or\b|%\}|\)|-?%\}|\}\})", rest):
            found.append(m.group(1))
    return found


def _strings(data, key=None):
    """(key, text) of every string of a YAML document."""
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _strings(v, k)
    elif isinstance(data, list):
        for v in data:
            yield from _strings(v, key)
    elif isinstance(data, str):
        yield key, data


def bare_reads():
    found = []
    for path in sorted(glob.glob(os.path.join(TOP, "roles", "*", "templates", "**", "*.j2"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        found += ["%s: %s" % (os.path.relpath(path, TOP), name) for seg in _JINJA.findall(text) for name in _bare(seg)]
    yml = glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml")) + glob.glob(
        os.path.join(TOP, "roles", "*", "handlers", "*.yml")) + glob.glob(os.path.join(TOP, "playbooks", "*.yml"))
    for path in sorted(yml):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for key, text in _strings(data):
            segments = [text] if key in ("when", "changed_when", "failed_when", "until", "that") \
                else _JINJA.findall(text)
            found += ["%s: %s" % (os.path.relpath(path, TOP), name) for seg in segments for name in _bare(seg)]
    return sorted(set(found))


def test_the_scan_finds_a_bare_read():
    assert _bare("cassandra_log_console") == ["cassandra_log_console"]
    assert _bare("{{ '' if cassandra_log_console else '<!-- ' }}") == ["cassandra_log_console"]
    assert _bare("not cassandra_service_allow_new_seed") == ["cassandra_service_allow_new_seed"]
    assert _bare("{{ 'a' if x or (cassandra_service_restart_on_change and y) else 'b' }}") == [
        "cassandra_service_restart_on_change"]
    assert _bare("cassandra_log_console | bool") == [] and _bare("cassandra_offline | default(false) | bool") == []
    assert _bare("cassandra_auto_bootstrap is not none") == []


def test_every_boolean_setting_is_read_with_bool():
    # (audit rc3 r4 B2, r5 A2: logback.xml, cassandra-env.sh, rackdc, the service's unit and tasks)
    assert bare_reads() == []
