# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: ops
type: stdout
short_description: Only the operator messages of the community.cassandra playbooks
version_added: 3.0.0
description:
  - Prints the plans, progress lines, recaps and TODO lists of the collection's playbooks, the confirmation
    questions, and every failure in full. Nothing for the tasks that go well otherwise (no task headers, no C(ok),
    C(changed) or C(skipping) lines, no retries, no play recap).
  - An operator message is a task with the task variable C(cassandra_output) set to C(true) (a literal, in the
    task's own C(vars)); its C(msg) is printed as plain text, a line per list item, coloured by the start of its
    lines (Ansible's colours, so C(ANSIBLE_NOCOLOR), C(ANSIBLE_FORCE_COLOR) and a non-terminal output apply), with
    rules around the plans, screens and recaps.
  - With C(-v) or more, everything is printed as the default callback does.
  - Set it in C(ansible.cfg) (C([defaults]) C(stdout_callback = community.cassandra.ops)) or with
    C(ANSIBLE_STDOUT_CALLBACK=community.cassandra.ops).
  - What it prints, the task variables it reads, its colours, rules and progress lines are in the collection's
    I(Operator output) guide (C(docs/docsite/rst/guide_output.rst)).
options:
  progress_color:
    description:
      - The colour of the progress lines, the phase lines, the C(NOTE) lines, the first line of a plan or a screen,
        the informative summaries (C(cassandra_output_info)) and the help's commands and options, an Ansible colour
        name (C(cyan), C(bright cyan), C(blue), C(normal)...).
      - Not set, C(cyan), readable on a dark and on a light background; or Ansible's C(COLOR_VERBOSE) when the
        configuration sets that one (C(blue) by default, hard to read on a dark background). A name Ansible does
        not know is left for that default.
    type: str
    ini:
      - section: callback_cassandra_ops
        key: progress_color
    env:
      - name: ANSIBLE_CALLBACK_CASSANDRA_OPS_PROGRESS_COLOR
    version_added: 3.0.0
  warning_color:
    description:
      - The colour of the C(WARNING) lines, an Ansible colour name (C(bright purple), C(bright yellow)...).
      - Not set, Ansible's own warning colour (C(COLOR_WARN), set by C(warn) in the C([colors]) section of
        C(ansible.cfg), C(bright purple) by default), as Ansible's own C([WARNING]) lines; apart from the yellow of
        the changes. A name Ansible does not know is left for that default.
    type: str
    ini:
      - section: callback_cassandra_ops
        key: warning_color
    env:
      - name: ANSIBLE_CALLBACK_CASSANDRA_OPS_WARNING_COLOR
    version_added: 3.0.0
extends_documentation_fragment:
  - default_callback
  - result_format_callback
requirements:
  - set as stdout in configuration
"""

import atexit
import os
import re
import sys
import textwrap
import threading
import time
from collections import namedtuple

try:
    import termios
except ImportError:  # not a POSIX controller
    termios = None

from ansible.plugins.callback.default import CallbackModule as DefaultCallback

from ansible import constants as C
from ansible.utils.color import parsecolor, stringc

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import (
    BULLET, FLOW, duration, is_command, laid_out)

MARKER = "cassandra_output"
GAP = "cassandra_output_gap"  # a blank line before the next operator message
RESCUED = "cassandra_output_rescued"  # on a block: its rescue says what failed
SAID = "cassandra_output_said"  # on a task: its failure is the stop of a verdict printed already (nothing printed)
HELP = "cassandra_output_help"  # the help playbook's text: laid out at the terminal's width, its keys coloured
INFO = "cassandra_output_info"  # an informative summary: in the progress colour, except its WARNING lines
STEP = "cassandra_output_step"  # on a phase line: a step of the run, its tasks counted and its time said
_PAUSES = ("pause", "ansible.builtin.pause", "ansible.legacy.pause")
# the actions whose failure is the message itself (a verdict)
_ASSERTS = ("assert", "ansible.builtin.assert", "ansible.legacy.assert")
_VERDICTS = _ASSERTS + ("fail", "ansible.builtin.fail", "ansible.legacy.fail")
# a marked task that failed on its own template: core's message, not the verdict ("Task failed: ..." since
# ansible-core 2.19, the others before)
_CORE_ERRORS = ("Task failed: ", "The conditional check ", "The task includes an option with an undefined variable",
                "Unexpected templating type error occurred on ", "An unhandled exception occurred while ",
                "the field 'args' has an invalid value", "Unexpected failure during module execution")


# a role's argument check that refused a value (its spec printed whole by the default callback: thousands of lines):
# "argument 'x' is of type <given> and we were unable to convert to <needed>: <reason>", <given> "<class '...'>"
# before ansible-core 2.19, its name since
_ARG_TYPE = re.compile(r"argument '([^']+)' is of type (?:<class '(?:[\w.]+\.)?(\w+)'>|(\w+)) and we were unable to"
                       r" convert to (\w+)(?:: (.*))?$")
# a reason that only echoes a type: "<class 'str'> cannot be converted to an int"; since 2.19, the value for an int
# ("\"'3w'\" cannot be converted to an int")
_ARG_ECHO = re.compile(r"(?:<class '[\w.]+'>|\"(.*)\") cannot be converted to ")
_ARG_TYPE_NAMES = {
    "AnsibleUnicode": "a text", "str": "a text", "_AnsibleTaggedStr": "a text", "AnsibleSequence": "a list",
    "list": "a list", "AnsibleMapping": "a dict", "dict": "a dict", "int": "a whole number", "float": "a number",
    "bool": "a boolean"}
ARGUMENTS_WAY_OUT = "set them, or fix them where they are set (inventory or -e), then run again."


def argument_errors(result):
    """A failed role argument check (ansible-core's validate_argument_spec) -> (its role, its rows: each variable
    refused and why, short), None for any other failure."""
    errors = result.get("argument_errors") if "argument_spec_data" in result else None
    if not errors:
        return None
    role = (result.get("validate_args_context") or {}).get("name") or "the role"
    said = []
    for error in errors:
        found = _ARG_TYPE.match(str(error))
        if not found:  # (missing required arguments: x...)
            said.append(re.sub(r"<class '(?:[\w.]+\.)?(\w+)'>", r"\1", str(error)))
            continue
        name, given, needed = found.group(1), found.group(2) or found.group(3), found.group(4)
        needed = _ARG_TYPE_NAMES.get(needed, "a " + needed)
        reason = " ".join((found.group(5) or "").split())
        echo = _ARG_ECHO.match(reason)
        if reason and not echo:  # its own words (a bool: the value and the valid ones; a dict: why)
            said.append("%s: not %s: %s" % (name, needed, reason))
        else:
            value = echo.group(1) + " is " if echo and echo.group(1) else ""
            said.append("%s: %s%s, not %s" % (name, value, _ARG_TYPE_NAMES.get(given, given), needed))
    return role, tuple(said)


def argument_lines(role, rows, hosts):
    """The block of a refused role argument check: what was refused (rows, said once for the hosts that share
    them), the hosts (in natural order: node2 before node10), the way out; 79 columns."""
    out = ["%s refused these variables (it changed nothing):" % role]
    for row in rows:
        out += textwrap.wrap(row, 79, initial_indent="  ", subsequent_indent="    ", break_on_hyphens=False)
    hosts = sorted(set(hosts), key=lambda h: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(h))])
    out += textwrap.wrap(", ".join(hosts), 79, initial_indent="node%s: " % ("s" if len(hosts) > 1 else ""),
                         subsequent_indent="  ", break_on_hyphens=False)
    return out + [ARGUMENTS_WAY_OUT]


HANDLED_LINE_MAX = 160  # the most of a handled failure's line


def _refused_with_names(role, rows, whole):
    """A refused role argument check in one line: its first whole rows as they are, the others by their name."""
    names = [row.split(":")[0] for row in rows[whole:]]
    return "%s refused %d: %s; -v shows all" % (role, len(rows), "; ".join(list(rows[:whole]) + [", ".join(names)]))


def refused_line(role, rows):
    """A refused role argument check in one line (a failure the playbook handles): its rows, else the ones that fit
    whole then the names of the others, never a name cut: "the role refused 3: a: a text, not a whole number; b, c;
    -v shows all"."""
    line = "%s refused %s" % (role, "; ".join(rows))
    if len(line) <= HANDLED_LINE_MAX:
        return line
    for whole in range(len(rows) - 1, 0, -1):
        line = _refused_with_names(role, rows, whole)
        if len(line) <= HANDLED_LINE_MAX:
            return line
    return _refused_with_names(role, rows, 0)


def _task(result):
    """The task of a result (task since ansible-core 2.19, _task before)."""
    return getattr(result, "task", None) or result._task


def _result(result):
    """The result dict (result since ansible-core 2.19, _result before)."""
    value = getattr(result, "result", None)
    return value if isinstance(value, dict) or hasattr(value, "get") else result._result


# a phase line: what the run does now, a block of its own
_PHASE = re.compile(r"\S.*[^,]\.\.\.$")  # "Checking the cluster (3 nodes)..."
# the run's first line, the cluster it is on: "prod  5 nodes  1 DC (dc1)  Cassandra 5.0.9" (the version once known)
_BANNER = re.compile(r".+?  \d+ nodes?  \d+ DCs?( \(.*\))?(  Cassandra .+)?$")
# a wait outside a rolling operation (no [i/n]): for nodes down, for a node to be NORMAL
_WAIT = re.compile(r"\S+  \S.*  (waiting  .*  \d+[smhd]\S*/\d+[smhd]\S*|(all back|NORMAL) after \d\S*)$")
# the first line of a plan or a screen ("ADD NODE  ...", "REFUSED  ..."): as the progress lines
PLAN_COLOUR = "PROGRESS"
_WARNING = re.compile(r"WARNING\b")  # wherever it is indented
# the colour of a message line by its start, the lines indented under it the same (import_cluster's IMPORT line: read
# with the verdict under it, _import_head)
_COLOURS = (
    (re.compile(r"(REFUSED|FAILED|STOPPED|NOT HEALTHY|UNREACHABLE|SELF-CHECK FAILED)\b"), "COLOR_ERROR"),
    (re.compile(r"\[\d+/\d+\] .*\b(STALLED|FAILED|TOO LONG|STOPPED|REFUSED)\b"), "COLOR_ERROR"),
    (re.compile(r"\d+ NODES? (DOWN|NOT REACHED): "), "COLOR_ERROR"),  # the status verdict
    (re.compile(r"IMPORT \S+ .*\bFAILED\b|CHECK\b.*\bfailed\b"), "COLOR_ERROR"),
    (_WARNING, "WARNING"),
    (re.compile(r"ALL \d+ NODES? UP \u2014 "), "COLOR_CHANGED"),
    (re.compile(r"ALL \d+ NODES? UP$"), "COLOR_OK"),
    (re.compile(r"CHECK\b.*\bwould\b"), "COLOR_CHANGED"),  # something left to do
    (re.compile(r"(NOT IN SYNC|NOT ALL COMPARED|NONE COMPARED)  "), "COLOR_CHANGED"),  # (drift's: not all in sync)
    (re.compile(r"(DONE|HEALTHY|NOTHING TO DO|READY|CHECK)\b|IMPORT \S+ .*\b(PASSED|nothing to act on)\b|IN SYNC$"),
     "COLOR_OK"),
    (re.compile(r"NOTE\b|\[\d+/\d+\] |%s|%s" % (_PHASE.pattern, _BANNER.pattern)), "PROGRESS"),
    (_WAIT, "PROGRESS"),
)
# a mark anywhere in a line, its own colour over the one of the line it is indented under (the lines indented under
# it take it in turn): a node down, a stalled peer, a setting that differs, what a node would get or got, what a reset
# deletes, whether the import is in sync with Ansible
# - import_cluster's head: in sync with Ansible or not; the rows indented under it take its colour (the other marks
#   colour their own line only)
_SYNC_MARKS = (
    (re.compile(r"^  in sync with Ansible: yes\b"), "COLOR_OK"),
    (re.compile(r"^  in sync with Ansible: not all verified\b"), "COLOR_CHANGED"),
    (re.compile(r"^  in sync with Ansible: no\b"), "COLOR_ERROR"),
)
_MARKS = _SYNC_MARKS + (
    (re.compile(r"\u2190 down\b|^\s*\S.*?\S  (\w+ )?FAILED\b"), "COLOR_ERROR"),  # (a recap's failed node)
    # a line of the other ends under a progress line (cassandra_output.peer_lines, indented 6) with a stalled one
    (re.compile(r"^ {6}(?:(?:to|from|with) +\S+ +[\d.]+(?: \w+)?/|\d+ peers?: |\+\d+ more: ).*\bSTALLED\b"),
     "COLOR_ERROR"),
    (re.compile(r"\u2190 differs\b|\bwill be reset\b|\u2014 will be$|\bDELETED\b"
                r"|^\S.*?\S  (would apply|will apply|applied)\b"), "COLOR_CHANGED"),  # (apply_config's outcomes)
)
PROGRESS_COLOR = "cyan"  # the progress, phase and NOTE lines (option progress_color)


# The first words of the blocks the playbooks print, each followed by two spaces; the one table _HEADED and _RECAP
# are built from (the test of the callback checks the operations against the playbooks):
# - the operations with a plan: its title is the operation in capitals ("ADD NODE  node4 into dc1 of my_cluster");
_OPERATIONS = ("add_node", "replace_node", "decommission_node", "remove_dead_node", "reset_node", "move_node", "stop_rack",
               "start_rack", "change_seeds", "apply_config", "update_java", "sync_clocks", "add_datacenter",
               "remove_datacenter", "create_cluster", "upgrade", "cleanup", "topology", "rolling_restart", "rolling_reboot")
# - the other first words of a plan or a screen ("REFUSED  topology ..."; import_cluster's "IMPORT prod ..." apart;
#   drift's "DRIFT  prod  24 nodes  in sync");
_SCREEN_HEADS = ("PLAN", "REFUSED", "READY", "NOTHING TO DO", "DRIFT")
# - the first words of a recap, a stop block's too ("STOPPED  rolling_restart  prod  at node2", "WAY OUT  ...").
_RECAP_HEADS = ("DONE", "FAILED", "STOPPED", "CHECK", "HEALTHY", "NOT HEALTHY", "NOTHING TO DO", "WAY OUT")
# a plan or a screen (its first line): a heavy rule above it
_HEADED = re.compile(r"(%s)  |IMPORT \S+ " % "|".join(
    _SCREEN_HEADS + tuple(operation.replace("_", " ").upper() for operation in _OPERATIONS)))
# a recap: heavy rules around it ("STOPPED at node2: ..." too)
_RECAP = re.compile(r"(%s)  |(STOPPED|FAILED) at " % "|".join(_RECAP_HEADS))
_HEAVY = re.compile(r"(?:==== (\S.*?) )?={4,}$")  # a rule of = inside a screen, a title in it or not: its heavy rule
# a verdict block without rules, coloured by its lines (its blank lines blank): import_cluster's failed self-check
_BLOCK = re.compile(u"SELF-CHECK FAILED \u2014 ")
# a progress line: a step of a rolling operation ("[2/5] node2  restart  ..."), or a wait outside one (_WAIT); the
# other lines of its message, indented, are the other ends of its streams
_STEP = re.compile(r"\[\d+/\d+\] \S+  ")
# a progress line still going, rewritten in place on a terminal: a bar, a wait, a pause; any other is frozen (the
# step over, STALLED, FAILED...)
_GOING = re.compile(r"  (\[[#-]+\]|waiting\b|no progress\b|all sent, finishing|STALLED \d|draining$)")
# the figures of a progress line (a bar, a duration, a size, a speed, a percent, a time): without them, its state
_FIGURES = re.compile(r"\[[#-]+\]|(?:\d+[smhd])+\b|[\d.:,]+(?:\s*(?:[KMGT]iB(?:/s)?|%))?")
LOG_EVERY = 300  # seconds between two lines of a step going on, in a log or a pipe (a change of state: at once)
# the status playbook's verdict, its last line or the one above a ring that differs: heavy rules around them
_RING_VERDICT = re.compile(r"ALL \d+ NODES? UP\b|\d+ NODES? (DOWN|NOT REACHED): ")
_RING_DIFFER = re.compile(r"WARNING  ring and inventory differ$")
# a row of the status playbook's nodetool table: its colour by the node's state
_RING_ROW = re.compile(r" (?:\S+ +)?([UD])([NJLM])  \S")  # (its node name first, from 3.0.0 on)
# the help playbook's text: a rule naming a section, an operation (its key), a command
_HELP_RULE = re.compile(r"(====|----) (\S.*?) [=-]{4,}$")
_HELP_COMMAND = re.compile(r"\s*\$ ")


def _ring_verdict(texts):
    """(the lines above, the verdict lines) of the status playbook's report, its verdict last (its warning
    under it), the blank lines around them left out; (texts, []) for any other message. Its verdict first (the
    status report from 3.0.0 on: the verdict, the cluster's line, a warning): (["", the rest], the verdict
    lines), the rest printed after them (_print)."""
    first = [t for t in texts if t.strip()][:1]
    if first and _RING_VERDICT.match(first[0]) and texts[0] == first[0]:
        size = 1
        while size < len(texts) and texts[size].strip() and (texts[size].startswith("  ") or _RING_DIFFER.match(texts[size])):
            size += 1
        rest = list(texts[size:])
        while rest and not rest[0].strip():
            rest.pop(0)
        return [None] + rest, list(texts[:size])
    shown = list(texts)
    while shown and not shown[-1].strip():
        shown.pop()
    size = 2 if len(shown) > 1 and _RING_DIFFER.match(shown[-1]) else 1
    if not shown or not _RING_VERDICT.match(shown[-size]):
        return texts, []
    above = shown[:-size]
    while above and not above[-1].strip():
        above.pop()
    return above, shown[-size:]


# How a message is laid out (_layout), then printed (CallbackModule._print):
# - texts: its lines (the status report's tables only, its verdict apart in ring); first: its first line with text;
# - headed: a plan or a screen (a heavy rule above it); recap: a recap (heavy rules around it);
# - phase: a phase line alone (a blank line above and under it);
# - ring: the status report's verdict lines (between heavy rules); verdict_first: above its tables (the report
#   from 2.1.0 on), else under them.
Layout = namedtuple("Layout", "texts first headed recap phase ring verdict_first")


def _layout(texts):
    """The Layout of a message's lines (not a progress message: _progress_block prints those)."""
    first = next((t for t in texts if t.strip()), "")
    tables, ring = _ring_verdict(texts)
    if ring:  # the status report: its first line is the cluster's name, whatever that name is
        verdict_first = tables[:1] == [None]
        return Layout(tables[1:] if verdict_first else tables, first, False, False, False, ring, verdict_first)
    phase = sum(1 for t in texts if t.strip()) == 1 and bool(_PHASE.match(first))
    return Layout(texts, first, bool(_HEADED.match(first)), bool(_RECAP.match(first)), phase, [], False)


def shade(name, progress=PROGRESS_COLOR, warning=None):
    """A colour of _COLOURS, _MARKS or PLAN_COLOUR as Ansible's colour name
    (warning: the WARNING lines', Ansible's COLOR_WARN when None)."""
    if name == "PROGRESS":
        return progress
    if name == "WARNING":
        return warning or C.COLOR_WARN
    return getattr(C, name, None) if name else None


def colour(line, above=None, progress=PROGRESS_COLOR, warning=None):
    """The display colour of a message line (None: the default), above:
    the colour of the line it is indented under, kept by an indented line
    without a mark of its own; progress: the colour of the progress, phase
    and NOTE lines; warning: the colour of the WARNING lines."""
    row = _RING_ROW.match(line)
    if row:  # a node down as an error, joining, leaving or moving as a change, up and normal plain
        if row.group(1) == "D":
            return C.COLOR_ERROR
        return None if row.group(2) == "N" else C.COLOR_CHANGED
    mark = next((name for pattern, name in _MARKS if pattern.search(line)), None)
    if line[:1] in (" ", "\t") and line.strip():  # its own mark, a WARNING, else the colour it is under
        return shade(mark or ("WARNING" if _WARNING.match(line.lstrip()) else None), progress, warning) or above
    for pattern, name in _COLOURS:
        if pattern.match(line):
            return shade(name, progress, warning)
    return shade(mark, progress, warning)


def _import_head(texts, top):
    """import_cluster's head, its first paragraph as one line, to colour its IMPORT line (texts[top]) by the
    verdict under it: "IMPORT c1  3 nodes", "  nothing to act on" -> "IMPORT c1  3 nodes  nothing to act on"."""
    end = next((i for i in range(top + 1, len(texts)) if not texts[i].strip()), len(texts))
    return "  ".join([texts[top]] + [line.strip() for line in texts[top + 1:end]])


def _indent(line):
    return len(line) - len(line.lstrip(" \t")) if line.strip() else 0


def _under(shades, line):
    """shades: (indent, colour) of the lines printed above; those the line
    is not indented under left out (an indented line takes the colour of the
    last line less indented than it)."""
    indent = _indent(line)
    return [s for s in shades if s[0] < indent] if line[:1] in (" ", "\t") and line.strip() else []


def _known(given):
    """given when it is a colour Ansible knows (a name, colorN, rgbRGB, grayN), else None."""
    if given:
        try:
            parsecolor(str(given))
            return str(given)
        except (KeyError, ValueError, AttributeError, TypeError):
            pass  # not a colour
    return None


def warning_color(given=None):
    """The colour of the WARNING lines: given (option warning_color) when it
    is a colour Ansible knows, else Ansible's own warning colour (COLOR_WARN,
    [colors] warn in ansible.cfg)."""
    return _known(given) or C.COLOR_WARN


def help_line(line, key, progress=PROGRESS_COLOR, laid=False):
    """A line of the help text as printed: key (an operation's name, an
    option), the start of the line, coloured: an operation as ok, an option
    in progress; a command ($ ...) in progress, unless the line is text laid
    out (laid: a value of the inventory starting with "$ "); the rest plain."""
    if key and line.lstrip().startswith(key):
        at = line.index(key)
        tint = C.COLOR_OK if key.startswith(BULLET) else progress
        return line[:at] + stringc(key, tint) + line[at + len(key):]
    if not laid and _HELP_COMMAND.match(line):
        at = line.index("$")
        return line[:at] + stringc(line[at:], progress)
    return line


def help_command(line, progress=PROGRESS_COLOR):
    """A line of a command of the help text (its first, "$ ...", or a continuation) coloured as progress."""
    at = len(line) - len(line.lstrip())
    return line[:at] + stringc(line[at:], progress)


def rule_width(columns):
    """The width of the rules and the help text for Ansible's columns (one
    less than the terminal's; 79 when unknown): one less, 40 to 100."""
    return min(100, max(40, (columns if columns and columns > 0 else 79) - 1))


def progress_color(given=None):
    """The colour of the progress lines: given (option progress_color) when it
    is a colour Ansible knows, else COLOR_VERBOSE when the configuration sets
    it, else PROGRESS_COLOR."""
    if _known(given):
        return str(given)
    try:
        value, origin = C.config.get_config_value_and_origin("COLOR_VERBOSE")
    except Exception:  # pylint: disable=broad-except  # a config API this ansible-core does not have
        return PROGRESS_COLOR
    return value if value and origin != "default" else PROGRESS_COLOR


def _flag(task, name):
    value = (getattr(task, "vars", None) or {}).get(name)
    return value is True or str(value).strip().lower() in ("true", "yes")


def marked(task):
    """True when the task is an operator message: vars cassandra_output: true."""
    return _flag(task, MARKER)


def _uuid(item):
    return getattr(item, "_uuid", None)


def _rescuer(task):
    """The block whose rescue handles the task's failure (the task is in its
    block part, at any level, through includes), None when there is none."""
    child, parent = task, getattr(task, "_parent", None)
    while parent is not None:
        if (getattr(parent, "rescue", None) and _uuid(child) is not None
                and _uuid(child) in [_uuid(t) for t in getattr(parent, "block", None) or []]):
            return parent
        child, parent = parent, getattr(parent, "_parent", None)
    return None


def rescued(task):
    """True when the task is in the block part of a block with a rescue (at
    any level, through includes): the playbook handles its failure."""
    return _rescuer(task) is not None


def reported(task):
    """True when the rescue that handles the task's failure says what failed
    itself (its block sets cassandra_output_rescued: true)."""
    block = _rescuer(task)
    return block is not None and _flag(block, RESCUED)


def drop_typeahead():
    """Drops the keys typed during the run that nothing read (an answer typed
    again while the run was quiet): the shell would run them once the run is
    over, "yes" then printing y lines forever. Only on a terminal the run is
    in the foreground of."""
    # Called once, when the run is over (v2_playbook_on_stats). A pause reads its answer from the terminal; keys
    # typed while no pause is waiting stay in the terminal's input queue, and the shell reads them as a command
    # line after the run. tcflush(TCIFLUSH) empties that queue (what was typed but not read; nothing is written).
    # Left alone: an input that is not a terminal (redirected), and a terminal the run is in the background of
    # (another process group in the foreground reads what is typed). Without termios (not POSIX): nothing dropped.
    if termios is None:
        return
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd) and os.getpgrp() == os.tcgetpgrp(fd):
            termios.tcflush(fd, termios.TCIFLUSH)
    except (AttributeError, OSError, ValueError, termios.error):
        pass  # no terminal (or not ours): nothing typed to drop


def on_terminal():
    """True when the run's output is a terminal (a line can be rewritten in place), not a file or a pipe."""
    try:
        return os.isatty(sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        return False


def rows(texts, columns):
    """The rows of the terminal (columns wide) the lines take, a long one wrapped by the terminal."""
    return sum(max(1, (len(text) + columns - 1) // columns) for text in texts)


def progress_state(line):
    """A progress line without its figures (bars, sizes, speeds, durations, percents and times as "#"): what is
    left changes when its state does (the streams start, STALLED, the step over)."""
    return " ".join(_FIGURES.sub("#", line).split())


def is_progress(texts):
    """True when the lines are a progress message: its first a progress line, the others blank or indented under
    it (the other ends of the streams)."""
    said = [t for t in texts if t.strip()]
    return bool(said) and bool(_STEP.match(said[0]) or _WAIT.match(said[0])) and all(t[:1] == " " for t in said[1:])


def lines(msg):
    """A msg as the lines to print: a list one item per line, a string split
    on its newlines, anything else as text."""
    if msg is None:
        return []
    if isinstance(msg, (list, tuple)):
        out = []
        for item in msg:
            out.extend(lines(item) if isinstance(item, (list, tuple)) else str(item).split("\n"))
        return out
    return str(msg).split("\n")


# A step: a phase line marked cassandra_output_step, until the next phase line, plan, screen, recap or play (done
# then, its time said from 1 s on), or until an error, a progress line or the end of a run that failed (its time not
# said then); a question (pause) hides its line until the next task
Step = namedtuple("Step", "phase tint start")
STEP_WIDTH = 79  # the most of a step's line
STEP_DRAW_EVERY = 0.2  # seconds between two rewrites of a step's line on a terminal
_ANSIBLE_ERRORS = ("[ERROR]", "ERROR!")  # an error Ansible prints itself (Display.error: "User interrupted execution")


def step_line(phase, tasks, seconds, width=STEP_WIDTH):
    """The line of a step going on: its phase line, the tasks it has done, the time since it started ("Comparing the
    config with the inventory (24 nodes)...  task 37  1m12s"); its phase line cut when they do not fit in width."""
    figures = "task %d  %s" % (tasks, duration(seconds))
    if len(phase) + 2 + len(figures) <= width:
        return "%s  %s" % (phase, figures)
    room = width - 2 - len(figures) - 3
    return "%s...  %s" % (phase[:room].rstrip(), figures) if room >= 10 else figures[:width]


def step_done(phase, tasks, seconds, width=STEP_WIDTH):
    """The lines of a step done: its phase line, the tasks it has done and its time ("...  312 tasks  done in
    4m50s"), the count and the time on a line of their own when they do not fit in width."""
    said = "%d task%s  done in %s" % (tasks, "" if tasks == 1 else "s", duration(seconds))
    if len(phase) + 2 + len(said) <= width:
        return ["%s  %s" % (phase, said)]
    return [phase, "  " + said]


class CallbackModule(DefaultCallback):

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "community.cassandra.ops"

    _step = None  # the step going on (Step), if any
    _footer = None  # on a terminal, the step's line drawn under the output: True when a blank row was added above it
    _hold = False  # a line left open by Ansible (no newline): the step's line drawn again after the next one
    _paused = False  # a question (pause) may be asked: the step's line not drawn until the next task
    _step_said = False  # in a log or a pipe: the step's own line is the last printed
    _drawn = _logged = 0.0  # when the step's line was last drawn on the terminal, last given to the log or the pipe
    _pid = None  # the controller's process (a worker forked during a step prints as Ansible does)

    def __init__(self, *args, **kwargs):
        super(CallbackModule, self).__init__(*args, **kwargs)
        self._blank = True  # the last line printed was blank (or none yet): no blank line next
        self._gap = False  # a blank line before the next message
        self._looping = False  # the items of a loop with a gap: one blank line, before the first
        self._verdicts = set()  # the run_once verdicts printed: (task, lines)
        self._ruled = ""  # the rule just printed, if any
        self._tty = on_terminal()  # a progress line is rewritten in place
        self._clock = time.time
        self._live = None  # (the node of the progress block printed last, its rows): rewritten by its next one
        self._said = (None, None, 0)  # (node, state, when) of the progress line last given to the log or the pipe
        self._progress = PROGRESS_COLOR
        self._warning = C.COLOR_WARN
        self._refused = {}  # the refused role arguments of the task running: {(role, rows): [hosts]}, said after it
        self._tasks, self._task_said = 0, False  # the tasks the step has done; the task running gave a result
        self._lock = threading.RLock()  # the step's line and Ansible's output (results and warnings: two threads)
        atexit.register(self._footer_at_exit)  # (run after the one below: atexit runs the last registered first)
        atexit.register(self._say_refused_at_exit)  # (a run that ends before any next task or its stats)

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super(CallbackModule, self).set_options(task_keys=task_keys, var_options=var_options, direct=direct)
        given = self.get_option("progress_color")
        self._progress = progress_color(given)
        if given and self._progress != str(given):
            self._display.warning("progress_color: %s is not a colour Ansible knows, %s instead" % (given, self._progress))
        given = self.get_option("warning_color")
        self._warning = warning_color(given)
        if given and self._warning != str(given):
            self._display.warning("warning_color: %s is not a colour Ansible knows, %s instead" % (given, self._warning))

    def _verbose(self):
        return self._display.verbosity > 0

    def _line(self, line, color=None, plain=None):
        """plain: the line without the colours it has in it (for the log)."""
        if not line.strip():
            if self._blank:
                return  # never two blank lines in a row (a rule counts as one)
            line = ""
        if plain is not None and plain != line:  # coloured here: the log (log_path) gets it plain
            self._display.display(line, screen_only=True)
            self._display.display(plain, log_only=True)
        else:
            self._display.display(line, color=color)
        self._blank, self._ruled, self._live, self._step_said = not line, "", None, False

    def _width(self):
        """The width of the rules and the help text: rule_width of the
        terminal's columns as Ansible counts them, without Ansible's floor of
        79 (a narrower terminal too); Ansible's when the size is unknown (a
        file, a pipe, a terminal that says 0)."""
        try:
            columns = os.get_terminal_size(1).columns - 1  # the descriptor Ansible reads its columns from
        except (AttributeError, OSError, ValueError):  # not a terminal
            columns = 0
        return rule_width(columns if columns > 0 else getattr(self._display, "columns", 0))

    def _rule(self, heavy=False, text=""):
        """A rule as wide as the terminal (100 columns at most), never two in a row."""
        line = (("==== " if heavy else "---- ") + text + " ") if text else ""
        line += ("=" if heavy else "-") * max(4, self._width() - len(line))
        if not self._ruled or (heavy and line != self._ruled):  # a heavy one wins over a light one
            if not self._blank:
                self._display.display("")  # text never touches a rule: a blank line above it, one under it
            self._display.display(line)
            self._display.display("")
        self._ruled, self._blank, self._live, self._step_said = line, True, None, False

    def _print_help(self, msg):
        """The help playbook's text: its rules drawn as the other rules, its
        lines laid out at the terminal's width, coloured by help_line."""
        if self._gap:
            self._line("")
        self._gap = False
        room = self._width()
        for text in lines(msg):
            rule = _HELP_RULE.match(text)
            if rule:
                self._rule(heavy=rule.group(1) == "====", text=rule.group(2))
                continue
            laid, key = laid_out(text, room)
            for i, line in enumerate(laid):
                if is_command(text):  # each of its lines, continuations too
                    self._line(help_command(line, self._progress), plain=line)
                    continue
                self._line(help_line(line, key if i == 0 else "", self._progress, text.startswith(FLOW)), plain=line)

    def _print(self, msg, color=None, info=False, step=False):
        """An operator message: a progress message (_progress_block), the status report (_print_ring), else its
        lines between the rules and blank lines of its _layout. color: the colour of all its lines (a verdict's
        red), else each line's own; info: an informative summary (its plain lines in the progress colour); step: a
        phase line that starts a step (_step_start)."""
        texts = lines(msg)
        progress = color is None and is_progress(texts)
        layout = None if progress else _layout(texts)
        self._step_ends(texts, layout, color)
        if progress:
            return self._progress_block(texts)
        if layout.ring:
            return self._print_ring(layout, color, info)
        if step and layout.phase and self._tty:
            return self._step_start(layout.first)  # its line under the output until the step is over
        if layout.headed or layout.recap:
            self._rule(heavy=True)
        elif self._gap or layout.phase:
            self._line("")
        self._gap = False
        self._print_lines(layout.texts, layout.first, layout.headed, layout.recap, color, info)
        if layout.recap:
            self._rule(heavy=True)
        elif layout.phase:
            self._gap = True
            if step:
                self._step_start(layout.first)
        return None

    # --- steps ---

    def _step_start(self, phase):
        """A step starts (its phase line printed already in a log or a pipe): on a terminal, its line drawn under
        the output, the tasks it has done and its time rewritten in place."""
        with self._lock:
            self._step = Step(phase, colour(phase, None, self._progress, self._warning), self._clock())
            self._tasks, self._task_said = 0, False
            self._drawn = self._logged = self._step.start
            if not self._tty:
                self._step_said = True
                return
            self._live = None  # a progress block above: left as it is
            self._display.display(phase, log_only=True)  # (the log file: the step's start, as in a pipe)
            self._hook()
            self._draw(force=True)

    def _step_ends(self, texts, layout, color):
        """The step going on, at an operator message: over at a phase line, a plan, a screen or a recap (done), at
        an error or a progress line (its time not said); any other message is printed above its line."""
        if self._step is None:
            return
        first = next((t for t in texts if t.strip()), "")
        if layout is None or (color or colour(first, None, self._progress, self._warning)) == C.COLOR_ERROR:
            self._step_end(done=False)
        elif layout.phase or layout.headed or layout.recap or layout.ring:
            self._step_end(done=True)

    def _step_end(self, done):
        """The step going on is over: done, its phase line with its count and time (step_done); else, or done in less
        than a second, its phase line alone on a terminal (as without a step), nothing more in a log or a pipe."""
        with self._lock:
            step, self._step = self._step, None
            if step is None:
                return
            seconds = self._clock() - step.start
            done = done and seconds >= 1
            if self._tty:
                self._unhook()
                self._clear()
            elif not done:
                self._step_said = False
                return
            if not done:  # on a terminal: its phase line alone, a blank line above (the log file has its start)
                if not self._blank:
                    self._display.display("", screen_only=True)
                self._display.display(step.phase, color=step.tint, screen_only=True)
                self._blank, self._ruled, self._live, self._gap, self._step_said = False, "", None, True, False
                return
            if self._tty or not self._step_said:
                self._line("")
            for line in step_done(step.phase, self._tasks, seconds):
                self._line(line, color=step.tint)
            self._gap, self._step_said = True, False

    def _count(self, result):
        """A result of the task running: the step's line drawn again (in a log or a pipe, once LOG_EVERY seconds
        have gone by since the last line)."""
        with self._lock:  # (the results and the lines printed: two threads on ansible-core 2.19+)
            self._task_said = True
            if self._step is None:
                return
            now = self._clock()
            if self._tty:
                self._draw()
            elif now - self._logged >= LOG_EVERY:
                self._logged = now
                if not self._step_said:
                    self._line("")
                self._line(step_line(self._step.phase, self._tasks, now - self._step.start), color=self._step.tint)
                self._gap, self._step_said = True, True

    def _draw(self, force=False):
        """On a terminal, the step's line drawn (a blank row above it) or rewritten in place, every STEP_DRAW_EVERY
        seconds at most unless forced; never two rows (cut to the terminal's width)."""
        with self._lock:
            now = self._clock()
            if self._step is None or not self._tty or self._hold or self._paused:
                return
            if not force and now - self._drawn < STEP_DRAW_EVERY:
                return
            width = min(STEP_WIDTH, self._columns() - 1)
            text = step_line(self._step.phase, self._tasks, now - self._step.start, width=width)
            text = stringc(text, self._step.tint) if self._step.tint else text
            if self._footer is None:
                self._footer = not self._blank
                sys.stdout.write(("\n" if self._footer else "") + text)
            else:
                sys.stdout.write("\r\x1b[K" + text)
            sys.stdout.flush()
            self._drawn = now

    def _clear(self):
        """The step's line cleared, the cursor back where the output goes on (the blank row added above it too)."""
        with self._lock:
            if self._footer is not None:
                sys.stdout.write("\r\x1b[K" + ("\x1b[1A" if self._footer else ""))
                sys.stdout.flush()
                self._footer = None

    def _hook(self):
        """While a step goes on, on a terminal: what Ansible prints (the callback's lines, Ansible's own warnings
        from any thread) goes above the step's line, cleared first and drawn again under it."""
        # Ansible prints through its Display (one per process, the callback's _display): its display method,
        # wrapped on that instance until the step is over (_unhook), as _print_task_banner does for one call.
        # A line printed without its newline (newline=False: a prompt) holds the step's line until the next one.
        # A worker forked meanwhile has this wrapper too (and the lock as the fork found it): it prints as Ansible does.
        display = self._display
        shown = display.display
        self._pid = os.getpid()

        def over_the_step(msg="", *args, **kwargs):
            if os.getpid() != self._pid or kwargs.get("log_only", len(args) > 3 and args[3]):
                return shown(msg, *args, **kwargs)
            with self._lock:
                self._clear()
                try:
                    return shown(msg, *args, **kwargs)
                finally:
                    text = str(msg)
                    self._blank, self._ruled, self._live = not text.rstrip("\n").rsplit("\n", 1)[-1].strip(), "", None
                    self._hold = not text.endswith("\n") and not kwargs.get("newline", len(args) <= 4 or args[4])
                    if kwargs.get("stderr", len(args) > 1 and args[1]) and text.lstrip().startswith(_ANSIBLE_ERRORS):
                        self._step = None  # Ansible's own error (a Ctrl-C): the step's line never drawn again
                        self._unhook()
                    else:
                        self._draw(force=True)
        over_the_step.own = display.__dict__.get("display")  # (an instance's own display: put back after)
        display.display = over_the_step

    def _unhook(self):
        hooked = self._display.__dict__.get("display")
        if hooked is None or not hasattr(hooked, "own"):
            return
        if hooked.own is None:
            del self._display.display
        else:
            self._display.display = hooked.own

    def _footer_at_exit(self):
        """At the exit of the process (an interrupted run, a crash), a step's line still drawn cleared: the last
        line is Ansible's own (its error), never a step that looks still going on."""
        try:  # (no lock: a thread that held it is gone at exit)
            if self._footer is not None:
                sys.stdout.write("\r\x1b[K")  # (that row only: something else may have been written under it)
                sys.stdout.flush()
                self._footer = None
        except (IOError, OSError, ValueError):
            pass

    def _print_ring(self, layout, color, info):
        """The status report: its verdict lines and its tables, in the order of its layout, a heavy rule above,
        between and under them."""
        def verdict():
            for line in layout.ring:
                self._line(line, color=color or colour(line, None, self._progress, self._warning))

        def tables():
            self._print_lines(layout.texts, layout.first, False, False, color, info)

        self._rule(heavy=True)
        self._gap = False
        for part in (verdict, tables) if layout.verdict_first else (tables, verdict):
            part()
            self._rule(heavy=True)
        return None

    def _progress_block(self, texts):
        """A node's progress line, the other ends of its streams under it. On a terminal, rewritten in place
        while the step goes on, STALLED included (the previous block of the same node cleared first); a frozen
        line (the step over, FAILED: not _GOING) replaces the going block and stays; the block of another node starts under
        what is there. In a log or a pipe: the first line of a node, then one every LOG_EVERY seconds and every
        change of state (a terminal's log file gets those too)."""
        texts = [t if t.strip() else "" for t in texts]
        while texts and not texts[-1]:
            texts.pop()
        node, state, now = texts[0].split("  ", 1)[0], progress_state(texts[0]), self._clock()
        logged = (node, state) != self._said[:2] or now - self._said[2] >= LOG_EVERY
        if logged:
            self._said = (node, state, now)
        hue = colour(texts[0], None, self._progress, self._warning)
        shaded = [(t, colour(t, hue, self._progress, self._warning) if t[:1] == " " else hue) for t in texts]
        if not self._tty:
            if logged:
                if self._gap:
                    self._line("")
                for text, tint in shaded:
                    self._line(text, color=tint)
            self._gap = False
            return None
        if self._live and self._live[0] == node:
            sys.stdout.write("\x1b[%dA\r\x1b[J" % self._live[1])  # up to the block's first row, cleared to the end
        elif self._gap and not self._blank:
            sys.stdout.write("\n")
        for text, tint in shaded:
            sys.stdout.write((stringc(text, tint) if tint and text else text) + "\n")
        sys.stdout.flush()
        if logged:
            for text, dummy in shaded:
                self._display.display(text, log_only=True)
        self._blank, self._ruled, self._gap = False, "", False
        self._live = (node, rows(texts, self._columns())) if _GOING.search(texts[0]) else None
        return None

    def _columns(self):
        """The terminal's columns (80 when it does not say), to count the rows a progress block takes."""
        try:
            return os.get_terminal_size(1).columns or 80
        except (AttributeError, OSError, ValueError):
            return 80

    def _print_lines(self, texts, first, headed, recap, color, info):
        """A message's lines, each in its colour (its own mark, else the one of the less indented line above it);
        in a screen (headed) or a recap, a rule between its blocks, and under a plan's first line."""
        ruled = headed or recap  # a screen or a recap: rules inside it
        top = texts.index(first) if ruled else None  # its first line
        shades = []  # (indent, colour) of the lines above, each less indented than the next
        head = headed and first.startswith("IMPORT ")  # import_cluster's head: its paragraphs in its colours
        for i, line in enumerate(texts):
            if ruled and not line:  # a blank line between paragraphs (not "") stays blank
                self._rule()  # the blocks inside
                shades = []
                continue
            heavy = _HEAVY.match(line) if ruled else None
            if heavy:  # a section of the screen: drawn as the heavy rule
                self._rule(heavy=True, text=heavy.group(1) or "")
                shades, head = [], False
                continue
            if head and not line.strip():  # a blank line between the head's paragraphs: the colours above go on
                self._line(line)
                continue
            shades = _under(shades, line)
            above = shades[-1][1] if shades else None
            # the IMPORT line: in the colour of its verdict, the line under it (_import_head)
            hue = self._hue(_import_head(texts, top) if head and i == top else line, above, color, info,
                            headed and not recap and i == top)
            # an indented line's own mark: that line only (the import's in sync line: its rows too); a FAILED or
            # STOPPED recap: its rows by their own marks
            failed = recap and i == top and first.startswith(("FAILED", "STOPPED"))
            carried = any(pattern.search(line) for pattern, dummy in _SYNC_MARKS)
            shades.append((_indent(line), None if failed else (
                above if _indent(line) and not color and not carried else hue)))
            self._line(line, color=hue)
            if headed and i == top and first.startswith("PLAN") and i + 1 < len(texts) and texts[i + 1].strip():
                self._rule()  # under the plan's first line
                shades = []

    def _hue(self, line, above, color, info, title):
        """The colour of a message line: color when given, else its own (colour, above: the colour of the line it
        is indented under); a plain line of an informative summary (info) in the progress colour, the first line
        of a plan or a screen (title) as PLAN_COLOUR."""
        hue = color or colour(line, above, self._progress, self._warning)
        if hue is None and info:
            hue = self._progress
        if hue is None and title:
            hue = shade(PLAN_COLOUR, self._progress, self._warning)
        return hue

    def _shown(self):
        """Something else printed a line: a blank line may follow, a progress block above is left as it is."""
        self._blank, self._ruled, self._live, self._step_said = False, "", None, False

    def _default(self, name, *args):
        """The default callback's output (its task header without a second blank line above)."""
        getattr(super(CallbackModule, self), name)(*args)
        self._shown()

    def _warned(self, res):
        warned = bool(res.get("warnings") or res.get("deprecations"))  # _handle_warnings takes them out
        self._handle_warnings(res)  # a warning is meant to be read
        if warned:
            self._shown()

    def _print_task_banner(self, task):
        """The default callback's task header, its blank line above left out
        when one is printed already."""
        if self._verbose() or not self._blank:
            return super(CallbackModule, self)._print_task_banner(task)
        # This overrides a private method of the default callback, an ansible-core internal. Without -v, the only
        # task headers printed are those of the failures shown as the default callback shows them (_default) and
        # those of the diffs (--diff). _print_task_banner prints them with Display.banner, which puts a blank line
        # of its own above the header ("\n" + the header), and no public API leaves that line out. So this one
        # display call is wrapped, on this instance and for this call only, to drop the leading newline; the rest
        # of the default callback's banner is kept as it is (its _last_task_banner, which the results read).
        # Should a later ansible-core rename the method, this override is no longer called: the header then has
        # one more blank line above it, nothing else changes.
        # A step's own wrapper (_hook), if any, is called by this one and put back after.
        display = self._display
        own = display.__dict__.get("display")

        def shown(msg, *args, **kwargs):
            msg = msg[1:] if msg.startswith("\n") else msg
            return own(msg, *args, **kwargs) if own is not None else type(display).display(display, msg, *args, **kwargs)
        display.display = shown
        try:
            return super(CallbackModule, self)._print_task_banner(task)
        finally:
            if own is None:
                del display.display
            else:
                display.display = own

    # --- results ---

    def v2_runner_on_ok(self, result):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_ok(result)
        task = _task(result)
        self._count(result)
        self._warned(_result(result))
        if task.action in _PAUSES and "user_input" in _result(result):
            return self._answered(_result(result))
        looped = task.loop and "results" in _result(result)
        self._gap = self._gap or (_flag(task, GAP) and not looped)  # a loop's: before its first item
        self._looping = False
        if marked(task) and not looped and self._says(task):
            if _flag(task, HELP):
                return self._print_help(_result(result).get("msg"))
            self._print(_result(result).get("msg"), info=_flag(task, INFO), step=_flag(task, STEP))

    def _answered(self, res):
        """pause clears the answer's line once it is read: shown again (not
        a hidden one), then a blank line."""
        self._blank = False  # the question
        if res.get("echo", True) and str(res.get("user_input") or "").strip():
            self._display.display(str(res["user_input"]))
        self._line("")

    def _say_refused(self):
        """The refused role arguments of the task that just ran, a block for the hosts that share them."""
        refused, self._refused = self._refused, {}
        for (role, rows), hosts in refused.items():
            self._print(argument_lines(role, rows, hosts), color=C.COLOR_ERROR)

    def _say_refused_at_exit(self):
        """_say_refused at the exit of the process: its output closed already (a pipe gone) says nothing, rather
        than Python's "Exception ignored in atexit callback" under Ansible's last line."""
        try:
            self._say_refused()
        except (IOError, OSError, ValueError):
            pass

    def v2_runner_on_skipped(self, result):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_skipped(result)
        self._count(result)
        return None

    def _task_starts(self):
        """A task starts: the one before it done (one that gave no result, a meta task, not counted)."""
        with self._lock:
            counted, self._task_said = self._task_said, False
            self._tasks += 1 if counted else 0
            if self._tty:
                self._draw(force=counted)  # (the new count shown at once: the task may run long with no result)

    def v2_playbook_on_handler_task_start(self, task):
        self._say_refused()
        self._task_starts()
        if self._verbose():
            return super(CallbackModule, self).v2_playbook_on_handler_task_start(task)
        return None

    def v2_playbook_on_cleanup_task_start(self, task):
        self._say_refused()
        self._task_starts()
        return super(CallbackModule, self).v2_playbook_on_cleanup_task_start(task)

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._say_refused()
        self._paused = task.action in _PAUSES  # a question: no step's line under it
        if self._paused:
            self._clear()
        self._task_starts()
        if self._verbose():
            return super(CallbackModule, self).v2_playbook_on_task_start(task, is_conditional)
        if task.action in _PAUSES and _flag(task, GAP):  # a rule above its question (skipped too: one at most)
            self._rule()
        return None

    @staticmethod
    def _says(task):
        """A passed assert says something only with a success_msg (else
        "All assertions passed")."""
        return task.action not in _ASSERTS or "success_msg" in (task.args or {})

    def v2_runner_item_on_ok(self, result):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_item_on_ok(result)
        self._warned(_result(result))
        if _flag(_task(result), GAP) and not self._looping:
            self._gap = self._looping = True
        if marked(_task(result)) and self._says(_task(result)):
            self._print(_result(result).get("msg"), info=_flag(_task(result), INFO))

    def _own_failure(self, result):
        """A marked assert or fail with something to say: its msg is the
        verdict, printed as is (else the default callback's failure)."""
        task, res = _task(result), _result(result)
        said = [line for line in lines(res.get("msg")) if line.strip()]
        # an empty msg: nothing (2.16); the task's own template that failed (core's error, not the verdict): the
        # default output then, task name included
        return marked(task) and task.action in _VERDICTS and bool(said) and not said[0].startswith(_CORE_ERRORS)

    def _verdict(self, result):
        """The verdict lines; the host first when the task runs on each host, unless the verdict names
        it already ("STOPPED  rolling_restart  prod  at node2"). A run_once verdict the same task gave already is not printed
        again (ansible-core 2.15 and 2.16 run a run_once task of a rescue again for the hosts
        any_errors_fatal sends there later)."""
        msg = lines(_result(result)["msg"])
        head = next((line for line in msg if line.strip()), "")
        if _task(result).run_once:
            key = (_task(result).get_path() or _task(result).get_name(), tuple(msg))
            if key in self._verdicts:
                return None
            self._verdicts.add(key)
        else:
            host = (getattr(result, "host", None) or result._host).get_name()
            if not re.search(r"(?<![\w.-])%s(?![\w.-])" % re.escape(host), head):
                first = msg.index(head)
                msg[first] = head = "%s: %s" % (host, head)
        # a block of its own (REFUSED  add_node ..., FAILED  ...): coloured by its lines, its verdict line red
        self._print(msg, color=None if _HEADED.match(head) or _RECAP.match(head) or _BLOCK.match(head) else C.COLOR_ERROR)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_failed(result, ignore_errors)
        self._count(result)
        if ignore_errors:
            return None  # the playbook expects it (ignore_errors) and handles it
        if reported(_task(result)):  # its rescue says it (a verdict too: said once, there)
            return None
        if _flag(_task(result), SAID):  # the stop of a verdict printed already (a summary): said once, there
            return None
        if self._own_failure(result) and not (_task(result).loop and "results" in _result(result)):
            return self._verdict(result)
        if _task(result).loop and "results" in _result(result):
            return None  # each failed item was printed already
        if rescued(_task(result)):  # its rescue handles it: one line, never silent
            return self._handled(result)
        refused = argument_errors(_result(result))
        if refused:  # (-v: the default callback's, the whole spec); said once the task is over, once for its hosts
            self._refused.setdefault(refused, []).append((getattr(result, "host", None) or result._host).get_name())
            return None
        return self._default("v2_runner_on_failed", result, ignore_errors)

    def _handled(self, result):
        res = _result(result)
        # a command's own error rather than "non-zero return code" (its last line: JVM warnings come first)
        msg = [line.strip() for line in lines(res.get("msg") or "") if line.strip()]
        err = [line.strip() for line in lines(res.get("stderr") or "") if line.strip()]
        # ("non-zero return code" before ansible-core 2.19, "The command exited with a non-zero return code." since)
        msg = ["non-zero return code" if line.rstrip(".").endswith("non-zero return code") else line for line in msg]
        said = err[-1:] if err and (not msg or msg[0] == "non-zero return code") else msg
        refused = argument_errors(res)
        if refused:  # (a role's argument check: its rows, not its message cut at a colon)
            said = [refused_line(*refused)]
        host = getattr(result, "host", None) or result._host
        first = said[0] if said else "failed"
        # the playbook says it in full itself (a summary, a refusal): a short line here
        first = first if len(first) <= HANDLED_LINE_MAX or refused else first[:HANDLED_LINE_MAX - 3].rstrip() + "..."
        self._display.display("%s: %s: %s (the playbook handles it)" % (host.get_name(), _task(result).get_name(), first))
        self._shown()
        return None

    def v2_runner_item_on_failed(self, result):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_item_on_failed(result)
        res = _result(result)
        ignored = res.get("_ansible_ignore_errors")
        if ignored is None:
            ignored = _task(result).ignore_errors
        if ignored is True or str(ignored).strip().lower() in ("true", "yes"):
            return None
        if reported(_task(result)):
            return None
        if self._own_failure(result):
            return self._verdict(result)
        if rescued(_task(result)):
            return self._handled(result)
        return self._default("v2_runner_item_on_failed", result)

    def v2_runner_on_unreachable(self, result):
        if self._verbose():
            return self._default("v2_runner_on_unreachable", result)
        self._count(result)
        # one form, whether the play goes on without the host or not: the first sentence of Ansible's error, then
        # the command that shows all of it (never Ansible's raw dump, never silent)
        msg = lines(_result(result).get("msg") or "")
        host = getattr(result, "host", None) or result._host
        name = host.get_name()
        first = re.split(r"(?<=[^.\d])\.(?:\s|$)", msg[0], maxsplit=1)[0].strip() if msg else ""
        first = re.sub(r"^Task failed: ", "", first)  # (ansible-core 2.19+'s own words before the error)
        self._display.display("UNREACHABLE  %s  %s" % (name, first), color=C.COLOR_UNREACHABLE)
        self._display.display("%s  all of the error:  ansible %s -m ping -vvv" % (" " * len("UNREACHABLE"), name),
                              color=C.COLOR_UNREACHABLE)
        self._shown()
        return None

    def v2_runner_on_async_failed(self, result):
        return self._default("v2_runner_on_async_failed", result)

    def v2_on_file_diff(self, result):
        if getattr(_task(result), "diff", True) is False:
            return  # the task's own diff: false (ansible-core 2.16-2.18 call this under --diff all the same)
        res = _result(result)
        super(CallbackModule, self).v2_on_file_diff(result)
        items = res["results"] if _task(result).loop and "results" in res else [res]
        if any(r.get("diff") and r.get("changed") and self._get_diff(r["diff"]) for r in items):
            self._shown()  # what the default callback prints

    # --- what is quiet without -v ---

    def _quiet(name):  # pylint: disable=no-self-argument  # a factory run in the class body, not a method
        def method(self, *args, **kwargs):
            if self._verbose():
                return getattr(super(CallbackModule, self), name)(*args, **kwargs)
            return None
        return method

    v2_runner_item_on_skipped = _quiet("v2_runner_item_on_skipped")
    v2_runner_retry = _quiet("v2_runner_retry")
    v2_runner_on_start = _quiet("v2_runner_on_start")
    v2_runner_on_async_poll = _quiet("v2_runner_on_async_poll")
    v2_runner_on_async_ok = _quiet("v2_runner_on_async_ok")
    v2_playbook_on_start = _quiet("v2_playbook_on_start")
    v2_playbook_on_include = _quiet("v2_playbook_on_include")
    v2_playbook_on_notify = _quiet("v2_playbook_on_notify")
    v2_playbook_on_no_hosts_remaining = _quiet("v2_playbook_on_no_hosts_remaining")  # after the failures shown
    v2_playbook_on_no_hosts_matched = _quiet("v2_playbook_on_no_hosts_matched")  # a step with nothing to do
    del _quiet

    def v2_playbook_on_play_start(self, play):
        self._say_refused()
        self._step_end(done=True)  # (its play is over)
        self._verdicts = set()  # a verdict said again by another play is its own
        if self._verbose():
            return super(CallbackModule, self).v2_playbook_on_play_start(play)
        return None

    def v2_playbook_on_stats(self, stats):
        self._say_refused()
        self._step_end(done=not (getattr(stats, "failures", None) or getattr(stats, "dark", None)))
        drop_typeahead()  # the run is over: the shell reads the terminal next
        if self._verbose():
            return super(CallbackModule, self).v2_playbook_on_stats(stats)
        return None
