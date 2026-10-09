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
    M(ansible.builtin.assert) or M(ansible.builtin.fail) written as a verdict), its C(msg) is printed the same way.
    A marked M(ansible.builtin.assert) that passes prints its C(success_msg), nothing without one.
  - A failed task that is not ignored (C(ignore_errors)) and an unreachable host are printed as the default
    callback prints them, task name included; a host unreachable where the play goes on without it
    (C(ignore_unreachable)) on one line. A failure the playbook handles (a task of a block with a C(rescue)) on
    one line too, its host, task and the first line of its message. Diffs (C(--diff)) too.
  - The warnings of the tasks are printed too.
  - The operator messages are coloured by the start of their lines (Ansible's colours, so C(ANSIBLE_NOCOLOR),
    C(ANSIBLE_FORCE_COLOR) and a non-terminal output apply). C(WARNING) as a change (yellow); C(DONE), C(HEALTHY),
    C(NOTHING TO DO), C(READY) and C(CHECK) as ok; C(REFUSED), C(FAILED), C(NOT HEALTHY) and a progress line that
    stalled or failed as an error; C(NOTE), the progress lines (C([1/2] node5 ...)) and the phase lines (ending
    with C(...)) as verbose output. The lines indented under a coloured one keep its colour. The messages
    themselves stay plain text.
  - Rules mark the blocks out. A heavy one (C(=====)) above a plan or a screen (its first line C(PLAN  ...),
    C(REFUSED  ...), C(IMPORT <cluster> ...), or an operation's screen header) and around a recap (C(DONE  ...), C(FAILED  ...),
    C(CHECK  ...), C(HEALTHY  ...), C(NOT HEALTHY  ...)). A light one (C(-----)) for each blank line inside
    them, under a plan's first line and above a question, and one naming the node above the first progress
    line of each node (C(---- [1/2] node5 bootstrap ----)). As wide as the terminal, 100 columns at most.
  - The output goes by blocks, one blank line between two, never two in a row. A blank line comes before the
    next operator message after a task with the task variable C(cassandra_output_gap) set to C(true) (a literal,
    like C(cassandra_output); on an operator message itself, before its own lines; on a question, a
    M(ansible.builtin.pause), above it), and after the answer to a question (the answer stays shown).
  - With C(-v) or more, everything is printed as the default callback does.
  - Set it in C(ansible.cfg) (C([defaults]) C(stdout_callback = community.cassandra.ops)) or with
    C(ANSIBLE_STDOUT_CALLBACK=community.cassandra.ops).
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

MARKER = "cassandra_output"
GAP = "cassandra_output_gap"  # a blank line before the next operator message
_PAUSES = ("pause", "ansible.builtin.pause", "ansible.legacy.pause")
# the actions whose failure is the message itself (a verdict)
_ASSERTS = ("assert", "ansible.builtin.assert", "ansible.legacy.assert")
_VERDICTS = _ASSERTS + ("fail", "ansible.builtin.fail", "ansible.legacy.fail")


def _task(result):
    """The task of a result (task since ansible-core 2.19, _task before)."""
    return getattr(result, "task", None) or result._task


def _result(result):
    """The result dict (result since ansible-core 2.19, _result before)."""
    value = getattr(result, "result", None)
    return value if isinstance(value, dict) or hasattr(value, "get") else result._result


# the colour of a message line by its start, the lines indented under it the same
_COLOURS = (
    (re.compile(r"(REFUSED|FAILED|NOT HEALTHY|UNREACHABLE)\b"), "COLOR_ERROR"),
    (re.compile(r"\[\d+/\d+\] .*\b(STALLED|FAILED|TOO LONG|STOPPED)\b"), "COLOR_ERROR"),
    (re.compile(r"WARNING\b"), "COLOR_CHANGED"),  # yellow: COLOR_WARN is purple
    (re.compile(r"(DONE|HEALTHY|NOTHING TO DO|READY|CHECK)\b"), "COLOR_OK"),
    (re.compile(r"NOTE\b|\[\d+/\d+\] |\S.*[^,]\.\.\.$"), "COLOR_VERBOSE"),
)


# a plan or a screen (its first line), a recap, a progress line
_HEADED = re.compile(r"(PLAN|REFUSED|READY|NOTHING TO DO)  |IMPORT \S+ |(add_node|replace_node|decommission_node|remove_dead_node|"
                     r"reset_node|move_node|stop_rack|start_rack|change_seeds|apply_config|update_java|add_datacenter|"
                     r"remove_datacenter|create_cluster|upgrade|cleanup|topology|rolling_restart)( on cluster |: |$)")
_RECAP = re.compile(r"(DONE|FAILED|CHECK|HEALTHY|NOT HEALTHY)  ")
_STEP = re.compile(r"(\[\d+/\d+\] \S+ \S+)")


def colour(line, above=None):
    """The display colour of a message line (None: the default), above:
    the colour of the line before, kept by an indented line."""
    if line[:1] in (" ", "\t") and line.strip():
        return above
    for pattern, name in _COLOURS:
        if pattern.match(line):
            return getattr(C, name, None)
    return None


def _flag(task, name):
    value = (getattr(task, "vars", None) or {}).get(name)
    return value is True or str(value).strip().lower() in ("true", "yes")


def marked(task):
    """True when the task is an operator message: vars cassandra_output: true."""
    return _flag(task, MARKER)


def _uuid(item):
    return getattr(item, "_uuid", None)


def rescued(task):
    """True when the task is in the block part of a block with a rescue (at
    any level, through includes): the playbook handles its failure."""
    child, parent = task, getattr(task, "_parent", None)
    while parent is not None:
        if (getattr(parent, "rescue", None) and _uuid(child) is not None
                and _uuid(child) in [_uuid(t) for t in getattr(parent, "block", None) or []]):
            return True
        child, parent = parent, getattr(parent, "_parent", None)
    return False


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
        self._ruled = ""  # the rule just printed, if any

    def _verbose(self):
        return self._display.verbosity > 0

    def _line(self, line, color=None):
        if not line.strip():
            if self._blank:
                return  # never two blank lines in a row (a rule counts as one)
            line = ""
        self._display.display(line, color=color)
        self._blank, self._ruled = not line, ""

    def _rule(self, heavy=False, text=""):
        """A rule as wide as the terminal (100 columns at most), never two in a row."""
        width = min(100, max(40, (getattr(self._display, "columns", 0) or 79) - 1))
        line = (("==== " if heavy else "---- ") + text + " ") if text else ""
        line += ("=" if heavy else "-") * max(4, width - len(line))
        if not self._ruled or (heavy and line != self._ruled):  # a heavy one wins over a light one
            self._display.display(line)
        self._ruled, self._blank = line, True

    def _print(self, msg, color=None):
        texts = lines(msg)
        first = next((t for t in texts if t.strip()), "")
        headed, recap, step = _HEADED.match(first), _RECAP.match(first), _STEP.match(first)
        if headed or recap:
            self._rule(heavy=True)
        elif self._gap and step:
            self._rule(text=step.group(1))
        elif self._gap:
            self._line("")
        self._gap = False
        above = None
        for i, line in enumerate(texts):
            if (headed or recap) and not line.strip():
                self._rule()  # the blocks inside
                above = None
                continue
            above = color or colour(line, above)
            self._line(line, color=above)
            if headed and first.startswith("PLAN") and i == texts.index(first) and i + 1 < len(texts) and texts[i + 1].strip():
                self._rule()  # under the plan's first line
        if recap:
            self._rule(heavy=True)

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
            self._print(_result(result).get("msg"))

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
            self._print(_result(result).get("msg"))

    def _own_failure(self, result):
        """A marked assert or fail with something to say: its msg is the
        verdict, printed as is (else the default callback's failure)."""
        task, res = _task(result), _result(result)
        said = [line for line in lines(res.get("msg")) if line.strip()]
        # an empty msg: nothing (2.16), or core's own "Task failed: ..." (2.19+): the default output then
        return marked(task) and task.action in _VERDICTS and bool(said) and not said[0].startswith("Task failed: ")

    def _verdict(self, result):
        """The verdict lines; the host first when the task runs on each host."""
        msg = lines(_result(result)["msg"])
        if not _task(result).run_once:
            host = getattr(result, "host", None) or result._host
            first = next(i for i, line in enumerate(msg) if line.strip())
            msg[first] = "%s: %s" % (host.get_name(), msg[first])
        self._print(msg, color=C.COLOR_ERROR)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_failed(result, ignore_errors)
        if ignore_errors:
            return None  # the playbook expects it (ignore_errors) and handles it
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
    v2_playbook_on_play_start = _quiet("v2_playbook_on_play_start")
    v2_playbook_on_handler_task_start = _quiet("v2_playbook_on_handler_task_start")
    v2_playbook_on_include = _quiet("v2_playbook_on_include")
    v2_playbook_on_notify = _quiet("v2_playbook_on_notify")
    v2_playbook_on_no_hosts_remaining = _quiet("v2_playbook_on_no_hosts_remaining")  # after the failures shown
    v2_playbook_on_no_hosts_matched = _quiet("v2_playbook_on_no_hosts_matched")  # a step with nothing to do
    del _quiet

    def v2_playbook_on_stats(self, stats):
        drop_typeahead()  # the run is over: the shell reads the terminal next
        if self._verbose():
            return super(CallbackModule, self).v2_playbook_on_stats(stats)
        return None
