# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""A difference of import_cluster's self-check as an operator reads it: the file, the setting in that file's own
words, the node's value and the one Ansible would write.

read(text): one difference as the self-check says it (cassandra_import_check's, its values masked there) ->
    [Finding]: what is in the file named as that file names it (a YAML setting by its dotted key, a JVM option or a
    variable of cassandra-env.sh by its name, a .properties key, an XML element by the names of the elements down to
    it, an element named by its name=: AUDIT / rollingPolicy / fileNamePattern), each side's value as such, absent
    when it has none, present for an element or a flag that has no value, (empty) for an empty one. A difference
    that is no setting's (a file that could not be read, an error) is a sentence.
xml_where(path): the XPath of an XML difference -> how the stop names it.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import ast
import json
import re
from collections import namedtuple


# the heap's differences are named so (the self-check compares MAX_HEAP_SIZE and HEAP_NEWSIZE wherever they are set)
HEAP_FILE = "heap (cassandra-env.sh, else the unit's Environment)"
ABSENT, PRESENT, EMPTY = "absent", "present", "(empty)"
# a file the roles would rewrite with the same settings (cassandra_config does not keep it as it is)
LAYOUT = "the same settings in another layout"
# what the self-check says of the parts of a node outside the config files, by their role
PARTS = {"cassandra_linux": "OS (cassandra_linux)", "cassandra_firewall": "firewall (cassandra_firewall)",
         "cassandra_repository": "repositories (cassandra_repository)", "cassandra_install": "cqlsh (cassandra_install)"}
# where: the setting (None for a sentence); node, ansible: the values (None for a sentence); sentence: the text
Finding = namedtuple("Finding", "file where node ansible sentence")

_SETTING = re.compile(r"^(%s|[^:\s]+): (.+?): node has (.*), (?:import would write|the roles would set) (.*)$"
                      % re.escape(HEAP_FILE), re.S)
_LINES = re.compile(r"^([^:\s]+): node has (.*), import would write (.*)$", re.S)
_CREATED = re.compile(r"^([^:\s]+): not on the node, the roles would create it$")
_SAME = re.compile(r"^([^:\s]+): the same settings, written in a way cassandra_config does not keep")
_ELEMENT = re.compile(r'^<([\w.:-]+)((?: [\w.:-]+="[^"]*")*)>(?:(.*)</\1>)?$', re.S)
_ASSIGN = re.compile(r"^(?:export\s+)?(\w+)=(.*)$", re.S)


def value(text):
    """A value as the self-check gives it ('64', "a'b", true, nothing) -> as the stop shows it (64, a'b, true,
    absent, (empty))."""
    text = str(text)
    if text == "nothing":
        return ABSENT
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        try:
            found = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(found, str):
            return found or EMPTY
    if text.startswith("["):  # (a list: its items, a blank between them)
        try:
            found = json.loads(text)
        except ValueError:
            return text
        if isinstance(found, list) and all(not isinstance(v, (dict, list)) for v in found):
            return " ".join(str(v) for v in found) or EMPTY
    return text or EMPTY


def _steps(path):
    """An XPath's steps, a / inside a quoted name kept."""
    steps, step, quote = [], "", False
    for c in path.lstrip("/"):
        if c == '"':
            quote = not quote
        if c == "/" and not quote:
            steps.append(step)
            step = ""
        else:
            step += c
    return steps + [step]


def xml_where(path):
    """/configuration/appender[@name="AUDIT"]/rollingPolicy/fileNamePattern -> AUDIT / rollingPolicy /
    fileNamePattern: an element by its name= (an appender, a logger), an appender-ref by its ref=, one of several
    siblings of a tag by its place (pattern (2)), an attribute as @level; the root left out but alone."""
    shown = []
    for step in _steps(path):
        place = re.search(r"\[(\d+)\]$", step)
        step = step[:place.start()] if place else step
        m = re.match(r'^([^\[]+)\[@(name|ref)="(.*)"\]$', step)
        if m:
            step = m.group(3) if m.group(2) == "name" else "%s %s" % (m.group(1), m.group(3))
        shown.append(step + (" (%s)" % place.group(1) if place else ""))
    return " / ".join(shown[1:] if len(shown) > 1 else shown)


def _element(text):
    """An element one side has alone, as the self-check gives it (<maxHistory>14</maxHistory>, <logger
    level="DEBUG" name="x">) -> its text when it has nothing else, else present (its attributes but the one that
    names it: present (level DEBUG))."""
    m = _ELEMENT.match(text)
    if not m:
        return text
    attrs = [(k, v) for k, v in re.findall(r'([\w.:-]+)="([^"]*)"', m.group(2)) if k not in ("name", "ref")]
    if m.group(3) and not attrs:
        return m.group(3)
    return PRESENT + (" (%s)" % ", ".join("%s %s" % kv for kv in attrs) if attrs else "")


def _option(option):
    """A JVM option -> (its name, its value): -Xss512k (-Xss, 512k), -Dx=1 (-Dx, 1), -XX:Foo=3 (-XX:Foo, 3); a flag
    (-XX:+UseZGC) or another word (-javaagent:...) as itself, present."""
    for pattern in (r"^(-X(?:mx|ms|mn|ss))(.+)$", r"^(-D[^=]+)=(.*)$", r"^(-XX:\w+)=(.*)$"):
        m = re.match(pattern, option)
        if m:
            return m.group(1), m.group(2) or EMPTY
    return option, PRESENT


def _jvm(key, node, ansible):
    """A JVM options file's difference (its setting as jvm_options_settings names it, each side's options) -> (where,
    node, ansible): by the option's name, each side's value; the options themselves when written another way
    (-XX:ThreadStackSize=256 and -Xss512k, -XX:+UseFoo and -XX:-UseFoo)."""
    a, b = value(node), value(ansible)
    if key.startswith("("):  # (options without a name, the ones set before an unlock...)
        return key.strip("()"), a, b
    sides = [v for v in (a, b) if v != ABSENT]
    if not all(v.startswith("-") and " " not in v for v in sides):  # (masked, a list)
        return key if key.startswith("-") else "-XX:" + key, a, b
    named = [_option(v) for v in sides]
    if len(named) == 1:
        name, shown = named[0]
        return (name, shown, ABSENT) if a != ABSENT else (name, ABSENT, shown)
    if named[0][0] == named[1][0]:
        return named[0][0], named[0][1], named[1][1]
    return key if key.startswith("-") else "-XX:" + key, a, b


def _env_line(line):
    """A line of cassandra-env.sh -> (what it sets, its value): a variable (MAX_HEAP_SIZE, 8G), the option a JVM_OPTS
    line adds (-Dx, 1); (None, the line) for another line."""
    m = _ASSIGN.match(line)
    if not m:
        return None, line
    name, text = m.groups()
    added = re.match(r'^"\$JVM_OPTS (\S+)"$', text) if name == "JVM_OPTS" else None
    if added:
        return _option(added.group(1))
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return name, text or EMPTY


def _env(node, ansible):
    """A line of cassandra-env.sh that differs (a line each side, or nothing) -> (where, node, ansible)."""
    a, b = [(None, None) if v == "nothing" else _env_line(v) for v in (node, ansible)]
    names = set(n for n, v in (a, b) if v is not None)
    if len(names) == 1 and None not in names:
        name = names.pop()
        return name, ABSENT if a[1] is None else a[1], ABSENT if b[1] is None else b[1]
    return "a line", ABSENT if node == "nothing" else node, ABSENT if ansible == "nothing" else ansible


def _owner(name, node, ansible):
    """owner:group mode on each side (root:cassandra 0644) -> a finding for each that differs."""
    a, b = [re.match(r"^(\S+):(\S+) (\S+)$", v) for v in (node, ansible)]
    if not (a and b):
        return [Finding(name, "owner:group mode", node, ansible, None)]
    return [Finding(name, what, x, y, None) for what, x, y in zip(("owner", "group", "mode"), a.groups(), b.groups())
            if x != y]


def read(text):
    """A self-check difference -> [Finding] (see the module)."""
    text = str(text)
    same = _SAME.match(text)
    if same:
        return [Finding(same.group(1), None, None, None, LAYOUT)]
    m = None if _LINES.match(text) else _SETTING.match(text)  # (a line: "<file>: node has <line>, ...")
    if m:
        name, where, node, ansible = m.groups()
        if name in PARTS:
            return [Finding(PARTS[name], where, node, ansible, None)]
        if where == "owner:group mode":
            return _owner(name, node, ansible)
        if name.endswith(".xml") and where.startswith("/"):
            return [Finding(name, xml_where(where), *[_element(v) if v.startswith("<") else value(v)
                                                      for v in (node, ansible)], sentence=None)]
        if name.endswith(".options"):
            return [Finding(name, *_jvm(where, node, ansible), sentence=None)]
        return [Finding(name, where, value(node), value(ansible), None)]
    m = _LINES.match(text)
    if m:
        name, node, ansible = m.groups()
        if name.endswith(".xml"):  # (the same elements, their order aside)
            return [Finding(name, None, None, None, "the same elements, in another order")]
        return [Finding(name, *_env(node, ansible), sentence=None)]
    m = _CREATED.match(text)
    if m:
        return [Finding(m.group(1), "the file", ABSENT, PRESENT, None)]
    head, sep, rest = text.partition(": ")
    if head in PARTS:
        return [Finding(PARTS[head], None, None, None, rest)]
    return [Finding(head, None, None, None, rest) if sep else Finding("", None, None, None, text)]
