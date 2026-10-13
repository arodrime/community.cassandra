# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
name: ops
type: stdout
short_description: Only the operator messages of the community.cassandra playbooks
version_added: 2.1.0
description:
  - Prints the plans, progress lines, recaps and TO DO lists of the collection's playbooks, the confirmation
    questions, and every failure in full. Nothing for the tasks that go well otherwise (no task headers, no C(ok),
    C(changed) or C(skipping) lines, no retries, no play recap).
  - An operator message is a task with the task variable C(cassandra_output) set to C(true) (a literal, in the
    task's own C(vars)). Its C(msg) is printed as plain text, a line per list item. When such a task fails (an
    M(ansible.builtin.assert) or M(ansible.builtin.fail) written as a verdict), its C(msg) is printed the same way,
    once for a C(run_once) task that runs again with the same C(msg) (a rescue some hosts reach later); in red,
    or coloured by its lines when it is a block of its own (C(REFUSED  ...), C(FAILED  ...), C(SELF-CHECK FAILED  ...)).
    A marked task that fails on its own template (the error core gives instead of its C(msg)) is printed as the
    default callback does.
    A marked M(ansible.builtin.assert) that passes prints its C(success_msg), nothing without one.
  - A failed task that is not ignored (C(ignore_errors)) and an unreachable host are printed as the default
    callback prints them, task name included; a host unreachable where the play goes on without it
    (C(ignore_unreachable)) on one line. A failure the playbook handles (a task of a block with a C(rescue)) on
    one line too, its host, task and the first line of its message; nothing when that block sets the variable
    C(cassandra_output_rescued) to C(true) (its rescue says what failed). Diffs (C(--diff)) too.
  - The warnings of the tasks are printed too.
  - The operator messages are coloured by the start of their lines (Ansible's colours, so C(ANSIBLE_NOCOLOR),
    C(ANSIBLE_FORCE_COLOR) and a non-terminal output apply). C(WARNING) in O(warning_color), indented or not; C(DONE), C(HEALTHY),
    C(NOTHING TO DO), C(READY), C(IMPORT ... PASSED) and a C(CHECK) with nothing left to do as ok; a C(CHECK) that
    says what a real run C(would) do as a change; C(REFUSED), C(FAILED), C(NOT HEALTHY), C(IMPORT ... FAILED),
    C(SELF-CHECK FAILED), a
    C(CHECK) with a node that C(failed) and a progress line that stalled or failed as an error; C(NOTE), the
    progress lines (C([1/2] node5 ...), and the waits for nodes down or a node to be NORMAL, C(... waiting for ...  1m30s/10m)),
    the phase lines (ending with C(...)) and the first line of a plan or a screen (C(PLAN  ...), C(add_node on cluster ...))
    in O(progress_color). The lines of an informative summary
    (a task that also sets the task variable C(cassandra_output_info) to C(true), a literal like C(cassandra_output))
    in O(progress_color) too, but for its C(WARNING) lines and its lines with a mark of their own. The lines
    indented under a coloured one keep its colour, but for an indented line with a mark of its own, which colours that line only (a node down
    (C(← down)), a recap's failed node (C(node5  FAILED)) and a line of the other ends of a stream
    with a C(STALLED) one (C(from node3   4.2/17.0 GiB   24% STALLED)) as an error, a setting that differs
    (C(← differs)), a reset (C(will be reset)) and what it
    deletes (C(DELETED)) as a change, wherever they are; what a node would get or got as a change too, on a line
    of its own, not indented (apply_config's C(node1  would apply, then restart), C(node1  will apply), C(node1  applied))). The rows of
    a C(FAILED) recap do not keep its colour (the nodes done stay plain). The C(status)
    playbook's verdict, C(ALL 5 NODES UP), as ok, followed by nodes joining, leaving or moving (C(1 JOINING))
    as a change, C(1 NODE DOWN), C(1 NODE NOT REACHED) as an error; a ring that differs from the inventory, a
    C(WARNING - ...) line under the verdict, as a warning; the rows of its table (C( DN  10.0.0.5 ...)) by the
    node's state, down as an error, joining, leaving or moving as a change. The messages themselves stay plain text.
  - Rules mark the blocks out. A heavy one (C(=====)) above a plan or a screen (its first line C(PLAN  ...),
    C(REFUSED  ...), C(READY  ...), C(IMPORT <cluster> ...), or an operation's screen header) and around a recap (C(DONE  ...),
    C(FAILED  ...), C(CHECK  ...), C(NOTHING TO DO  ...), C(HEALTHY  ...), C(NOT HEALTHY  ...)), above the C(status) playbook's report and around its
    verdict, its last line (and the warning under it). A light one (C(-----)) for each empty line inside them (the blank lines of the status
    report stay blank), under a plan's first line and above a question, and one naming the node above the first
    progress line of each node (C(---- [1/2] node5 bootstrap ----)). As wide as the terminal, 100 columns at most, a
    blank line above and under each one. A line with only a space inside them separates two paragraphs of a block,
    printed blank (not as a rule). Two warnings in a row (a C(WARNING - ...) line right under another one or its
    indented lines) are a blank line apart, but for the rows of a table (columns two spaces apart).
  - The text of the C(help) playbook (a message with the task variable C(cassandra_output_help) set to C(true) too)
    is laid out at the terminal's width (100 columns at most), its sections between rules, each operation's
    name as ok, its commands (C($ ansible-playbook ...)) and the options of its commands in O(progress_color), what
    they do plain.
  - The output goes by blocks, one blank line between two, never two in a row. A phase line (one line ending with
    C(...)) is a block of its own. A blank line comes before the
    next operator message after a task with the task variable C(cassandra_output_gap) set to C(true) (a literal,
    like C(cassandra_output); on an operator message itself, before its own lines; on a question, a
    M(ansible.builtin.pause), above it), and after the answer to a question (the answer stays shown).
  - With C(-v) or more, everything is printed as the default callback does.
  - Set it in C(ansible.cfg) (C([defaults]) C(stdout_callback = community.cassandra.ops)) or with
    C(ANSIBLE_STDOUT_CALLBACK=community.cassandra.ops).
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
    version_added: 2.1.0
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
    version_added: 2.1.0
extends_documentation_fragment:
  - default_callback
  - result_format_callback
requirements:
  - set as stdout in configuration
"""

import os
import re
import sys

try:
    import termios
except ImportError:  # not a POSIX controller
    termios = None

from ansible.plugins.callback.default import CallbackModule as DefaultCallback

from ansible import constants as C
from ansible.utils.color import parsecolor, stringc

from ansible_collections.community.cassandra.plugins.module_utils.cassandra_output import BULLET, FLOW, PARAGRAPH, laid_out

MARKER = "cassandra_output"
GAP = "cassandra_output_gap"  # a blank line before the next operator message
RESCUED = "cassandra_output_rescued"  # on a block: its rescue says what failed
HELP = "cassandra_output_help"  # the help playbook's text: laid out at the terminal's width, its keys coloured
INFO = "cassandra_output_info"  # an informative summary: in the progress colour, except its WARNING lines
_PAUSES = ("pause", "ansible.builtin.pause", "ansible.legacy.pause")
# the actions whose failure is the message itself (a verdict)
_ASSERTS = ("assert", "ansible.builtin.assert", "ansible.legacy.assert")
_VERDICTS = _ASSERTS + ("fail", "ansible.builtin.fail", "ansible.legacy.fail")
# a marked task that failed on its own template: core's message, not the verdict ("Task failed: ..." since
# ansible-core 2.19, the others before)
_CORE_ERRORS = ("Task failed: ", "The conditional check ", "The task includes an option with an undefined variable",
                "Unexpected templating type error occurred on ", "An unhandled exception occurred while ",
                "the field 'args' has an invalid value", "Unexpected failure during module execution")


def _task(result):
    """The task of a result (task since ansible-core 2.19, _task before)."""
    return getattr(result, "task", None) or result._task


def _result(result):
    """The result dict (result since ansible-core 2.19, _result before)."""
    value = getattr(result, "result", None)
    return value if isinstance(value, dict) or hasattr(value, "get") else result._result


# a phase line: what the run does now, a block of its own
_PHASE = re.compile(r"\S.*[^,]\.\.\.$")  # "Checking the cluster (3 nodes)..."
# the first line of a plan or a screen ("PLAN  ...", "add_node on cluster ..."): as the progress lines
PLAN_COLOUR = "PROGRESS"
_WARNING = re.compile(r"WARNING\b")  # wherever it is indented
# the colour of a message line by its start, the lines indented under it the same
_COLOURS = (
    (re.compile(r"(REFUSED|FAILED|NOT HEALTHY|UNREACHABLE|SELF-CHECK FAILED)\b"), "COLOR_ERROR"),
    (re.compile(r"\[\d+/\d+\] .*\b(STALLED|FAILED|TOO LONG|STOPPED|REFUSED)\b"), "COLOR_ERROR"),
    (re.compile(r"\d+ NODES? (DOWN|NOT REACHED): "), "COLOR_ERROR"),  # the status verdict
    (re.compile(r"IMPORT \S+ .*\bFAILED\b|CHECK\b.*\bfailed\b"), "COLOR_ERROR"),
    (_WARNING, "WARNING"),
    (re.compile(r"ALL \d+ NODES? UP \u2014 "), "COLOR_CHANGED"),
    (re.compile(r"ALL \d+ NODES? UP$"), "COLOR_OK"),
    (re.compile(r"CHECK\b.*\bwould\b"), "COLOR_CHANGED"),  # something left to do
    (re.compile(r"(DONE|HEALTHY|NOTHING TO DO|READY|CHECK)\b|IMPORT \S+ .*\bPASSED\b"), "COLOR_OK"),
    (re.compile(r"NOTE\b|\[\d+/\d+\] |%s" % _PHASE.pattern), "PROGRESS"),
    # the waits for nodes down, for a node to be NORMAL, outside a rolling operation (no [i/n])
    (re.compile(r"\S.*  (waiting for .*  \d+[smhd]\S*/\d+[smhd]\S*|(all back|NORMAL) after \d\S*)$"), "PROGRESS"),
)
# a mark anywhere in a line, its own colour over the one of the line it is indented under (that line only):
# a node down, a stalled peer, a setting that differs, what a node would get or got, what a reset deletes
_MARKS = (
    (re.compile(r"\u2190 down\b|^\s*\S.*?\S  (\w+ )?FAILED\b"), "COLOR_ERROR"),  # (a recap's failed node)
    # a line of the other ends under a progress line (cassandra_output.peer_lines, indented 6) with a stalled one
    (re.compile(r"^ {6}(?:(?:to|from|with) +\S+ +[\d.]+(?: \w+)?/|\d+ peers? ok\b).*\bSTALLED\b"), "COLOR_ERROR"),
    (re.compile(r"\u2190 differs\b|\bwill be reset\b|\u2014 will be$|\bDELETED\b"
                r"|^\S.*?\S  (would apply|will apply|applied)\b"), "COLOR_CHANGED"),  # (apply_config's outcomes)
)
PROGRESS_COLOR = "cyan"  # the progress, phase and NOTE lines (option progress_color)


# a plan or a screen (its first line), a recap, a progress line
_HEADED = re.compile(r"(PLAN|REFUSED|READY|NOTHING TO DO)  |IMPORT \S+ |(add_node|replace_node|decommission_node|remove_dead_node|"
                     r"reset_node|move_node|stop_rack|start_rack|change_seeds|apply_config|update_java|sync_clocks|add_datacenter|"
                     r"remove_datacenter|create_cluster|upgrade|cleanup|topology|rolling_restart|rolling_reboot)( on cluster |: |$)")
_RECAP = re.compile(r"(DONE|FAILED|CHECK|HEALTHY|NOT HEALTHY|NOTHING TO DO)  ")
# a verdict block without rules, coloured by its lines (its blank lines blank): import_cluster's failed self-check
_BLOCK = re.compile(r"SELF-CHECK FAILED  ")
_STEP = re.compile(r"(\[\d+/\d+\] \S+ \S+)")
# the status playbook's verdict, its last line or the one above a ring that differs: heavy rules around them
_RING_VERDICT = re.compile(r"ALL \d+ NODES? UP\b|\d+ NODES? (DOWN|NOT REACHED): ")
_RING_DIFFER = re.compile(r"WARNING - ring and inventory differ$")
# a row of the status playbook's nodetool table: its colour by the node's state
_RING_ROW = re.compile(r" ([UD])([NJLM])  \S")
# the help playbook's text: a rule naming a section, an operation (its key), a command
_HELP_RULE = re.compile(r"(====|----) (\S.*?) [=-]{4,}$")
_HELP_COMMAND = re.compile(r"\s*\$ ")


def _ring_verdict(texts):
    """(the lines above, the verdict lines) of the status playbook's report, its verdict last (its warning
    under it), the blank lines around them left out; (texts, []) for any other message."""
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


def _indent(line):
    return len(line) - len(line.lstrip(" \t")) if line.strip() else 0


def _under(shades, line):
    """shades: (indent, colour) of the lines printed above; those the line
    is not indented under left out (an indented line takes the colour of the
    last line less indented than it)."""
    indent = _indent(line)
    return [s for s in shades if s[0] < indent] if line[:1] in (" ", "\t") and line.strip() else []


_OUR_WARNING = re.compile(r"\s*WARNING - ")
_ROW = re.compile(r"\s*WARNING - \S.*\S  +\S")  # a table's row (its columns two spaces apart): "WARNING - Seeds  dc2  ..."


def apart(texts):
    """The lines of a message, two warnings in a row a blank line (PARAGRAPH)
    apart: a WARNING - line right under another one, or under its indented
    lines (its continuation); not two rows of a table."""
    shown, warned, row = [], None, False  # the indent of the warning the line above is part of; that line a row
    for line in texts:
        indent = _indent(line)
        if _OUR_WARNING.match(line):
            is_row = bool(_ROW.match(line))
            if warned is not None and not (row and is_row):
                shown.append(PARAGRAPH)
            warned, row = indent, is_row
        elif not line.strip() or warned is None or indent <= warned:
            warned, row = None, False
        else:
            row = False  # a continuation: the warning above is not a row
        shown.append(line)
    return shown


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
    if termios is None:
        return
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd) and os.getpgrp() == os.tcgetpgrp(fd):
            termios.tcflush(fd, termios.TCIFLUSH)
    except (AttributeError, OSError, ValueError, termios.error):
        pass  # no terminal (or not ours): nothing typed to drop


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


class CallbackModule(DefaultCallback):

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "community.cassandra.ops"

    def __init__(self, *args, **kwargs):
        super(CallbackModule, self).__init__(*args, **kwargs)
        self._blank = True  # the last line printed was blank (or none yet): no blank line next
        self._gap = False  # a blank line before the next message
        self._looping = False  # the items of a loop with a gap: one blank line, before the first
        self._verdicts = set()  # the run_once verdicts printed: (task, lines)
        self._ruled = ""  # the rule just printed, if any
        self._progress = PROGRESS_COLOR
        self._warning = C.COLOR_WARN

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
        self._blank, self._ruled = not line, ""

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
        self._ruled, self._blank = line, True

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
                self._line(help_line(line, key if i == 0 else "", self._progress, text.startswith(FLOW)), plain=line)

    def _print(self, msg, color=None, info=False):
        texts = lines(msg)
        first = next((t for t in texts if t.strip()), "")
        headed, recap, step = _HEADED.match(first), _RECAP.match(first), _STEP.match(first)
        phase = sum(1 for t in texts if t.strip()) == 1 and _PHASE.match(first)  # a blank line above and under it
        texts, ring = _ring_verdict(apart(texts))  # the status report: a screen, its verdict last, printed between rules
        if ring:
            headed = recap = step = phase = None  # its first line is the cluster's name, whatever that name is
        if headed or recap or ring:
            self._rule(heavy=True)
        elif self._gap and step:
            self._rule(text=step.group(1))
        elif self._gap or phase:
            self._line("")
        self._gap = False
        shades = []  # (indent, colour) of the lines above, each less indented than the next
        for i, line in enumerate(texts):
            if (headed or recap) and not line:  # a blank line between paragraphs (not "") stays blank
                self._rule()  # the blocks inside
                shades = []
                continue
            shades = _under(shades, line)
            above = shades[-1][1] if shades else None
            hue = color or colour(line, above, self._progress, self._warning)
            if hue is None and info:  # an informative summary: its plain lines in the progress colour
                hue = self._progress
            if hue is None and headed and not recap and i == texts.index(first):  # a plan's or a screen's first line
                hue = shade(PLAN_COLOUR, self._progress, self._warning)
            # an indented line's own mark: that line only; a FAILED recap: its rows by their own marks
            failed = recap and i == texts.index(first) and first.startswith("FAILED")
            shades.append((_indent(line), None if failed else (above if _indent(line) and not color else hue)))
            self._line(line, color=hue)
            if headed and first.startswith("PLAN") and i == texts.index(first) and i + 1 < len(texts) and texts[i + 1].strip():
                self._rule()  # under the plan's first line
                shades = []
        if ring:
            self._rule(heavy=True)
            for line in ring:
                self._line(line, color=color or colour(line, None, self._progress, self._warning))
        if recap or ring:
            self._rule(heavy=True)
        elif phase:
            self._gap = True

    def _shown(self):
        """Something else printed a line: a blank line may follow."""
        self._blank, self._ruled = False, ""

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
        display = self._display

        def shown(msg, *args, **kwargs):
            return type(display).display(display, msg[1:] if msg.startswith("\n") else msg, *args, **kwargs)
        display.display = shown
        try:
            return super(CallbackModule, self)._print_task_banner(task)
        finally:
            del display.display

    # --- results ---

    def v2_runner_on_ok(self, result):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_ok(result)
        task = _task(result)
        self._warned(_result(result))
        if task.action in _PAUSES and "user_input" in _result(result):
            return self._answered(_result(result))
        looped = task.loop and "results" in _result(result)
        self._gap = self._gap or (_flag(task, GAP) and not looped)  # a loop's: before its first item
        self._looping = False
        if marked(task) and not looped and self._says(task):
            if _flag(task, HELP):
                return self._print_help(_result(result).get("msg"))
            self._print(_result(result).get("msg"), info=_flag(task, INFO))

    def _answered(self, res):
        """pause clears the answer's line once it is read: shown again (not
        a hidden one), then a blank line."""
        self._blank = False  # the question
        if res.get("echo", True) and str(res.get("user_input") or "").strip():
            self._display.display(str(res["user_input"]))
        self._line("")

    def v2_playbook_on_task_start(self, task, is_conditional):
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
        """The verdict lines; the host first when the task runs on each host. A run_once
        verdict the same task gave already is not printed again (ansible-core 2.15 and 2.16
        run a run_once task of a rescue again for the hosts any_errors_fatal sends there later)."""
        msg = lines(_result(result)["msg"])
        if _task(result).run_once:
            key = (_task(result).get_path() or _task(result).get_name(), tuple(msg))
            if key in self._verdicts:
                return None
            self._verdicts.add(key)
        else:
            host = getattr(result, "host", None) or result._host
            first = next(i for i, line in enumerate(msg) if line.strip())
            msg[first] = "%s: %s" % (host.get_name(), msg[first])
        # a block of its own (REFUSED  add_node ..., FAILED  ...): coloured by its lines, its verdict line red
        head = next((line for line in msg if line.strip()), "")
        self._print(msg, color=None if _HEADED.match(head) or _RECAP.match(head) or _BLOCK.match(head) else C.COLOR_ERROR)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_failed(result, ignore_errors)
        if ignore_errors:
            return None  # the playbook expects it (ignore_errors) and handles it
        if reported(_task(result)):  # its rescue says it (a verdict too: said once, there)
            return None
        if self._own_failure(result) and not (_task(result).loop and "results" in _result(result)):
            return self._verdict(result)
        if _task(result).loop and "results" in _result(result):
            return None  # each failed item was printed already
        if rescued(_task(result)):  # its rescue handles it: one line, never silent
            return self._handled(result)
        return self._default("v2_runner_on_failed", result, ignore_errors)

    def _handled(self, result):
        res = _result(result)
        # a command's own error rather than "non-zero return code" (its last line: JVM warnings come first)
        msg = [line.strip() for line in lines(res.get("msg") or "") if line.strip()]
        err = [line.strip() for line in lines(res.get("stderr") or "") if line.strip()]
        # ("non-zero return code" before ansible-core 2.19, "The command exited with a non-zero return code." since)
        msg = ["non-zero return code" if line.rstrip(".").endswith("non-zero return code") else line for line in msg]
        said = err[-1:] if err and (not msg or msg[0] == "non-zero return code") else msg
        host = getattr(result, "host", None) or result._host
        first = said[0] if said else "failed"
        # the playbook says it in full itself (a summary, a refusal): a short line here
        first = first if len(first) <= 160 else first[:157].rstrip() + "..."
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
        if self._verbose() or not _task(result).ignore_unreachable:
            return self._default("v2_runner_on_unreachable", result)
        # the playbook goes on without the host (ignore_unreachable): one line, never silent
        msg = lines(_result(result).get("msg") or "")
        host = getattr(result, "host", None) or result._host
        self._display.display("UNREACHABLE  %s  %s" % (host.get_name(), msg[0] if msg else ""), color=C.COLOR_UNREACHABLE)
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

    def _quiet(name):  # pylint: disable=no-self-argument
        def method(self, *args, **kwargs):
            if self._verbose():
                return getattr(super(CallbackModule, self), name)(*args, **kwargs)
            return None
        return method

    v2_runner_on_skipped = _quiet("v2_runner_on_skipped")
    v2_runner_item_on_skipped = _quiet("v2_runner_item_on_skipped")
    v2_runner_retry = _quiet("v2_runner_retry")
    v2_runner_on_start = _quiet("v2_runner_on_start")
    v2_runner_on_async_poll = _quiet("v2_runner_on_async_poll")
    v2_runner_on_async_ok = _quiet("v2_runner_on_async_ok")
    v2_playbook_on_start = _quiet("v2_playbook_on_start")
    v2_playbook_on_handler_task_start = _quiet("v2_playbook_on_handler_task_start")
    v2_playbook_on_include = _quiet("v2_playbook_on_include")
    v2_playbook_on_notify = _quiet("v2_playbook_on_notify")
    v2_playbook_on_no_hosts_remaining = _quiet("v2_playbook_on_no_hosts_remaining")  # after the failures shown
    v2_playbook_on_no_hosts_matched = _quiet("v2_playbook_on_no_hosts_matched")  # a step with nothing to do
    del _quiet

    def v2_playbook_on_play_start(self, play):
        self._verdicts = set()  # a verdict said again by another play is its own
        if self._verbose():
            return super(CallbackModule, self).v2_playbook_on_play_start(play)
        return None

    def v2_playbook_on_stats(self, stats):
        drop_typeahead()  # the run is over: the shell reads the terminal next
        if self._verbose():
            return super(CallbackModule, self).v2_playbook_on_stats(stats)
        return None
