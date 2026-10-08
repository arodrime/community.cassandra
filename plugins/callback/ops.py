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
    (C(ignore_unreachable)) on one line. Diffs (C(--diff)) too.
  - The warnings of the tasks are printed too.
  - With C(-v) or more, everything is printed as the default callback does.
  - Set it in C(ansible.cfg) (C([defaults]) C(stdout_callback = community.cassandra.ops)) or with
    C(ANSIBLE_STDOUT_CALLBACK=community.cassandra.ops).
extends_documentation_fragment:
  - default_callback
  - result_format_callback
requirements:
  - set as stdout in configuration
"""

from ansible.plugins.callback.default import CallbackModule as DefaultCallback

from ansible import constants as C

MARKER = "cassandra_output"
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


def marked(task):
    """True when the task is an operator message: vars cassandra_output: true."""
    value = (getattr(task, "vars", None) or {}).get(MARKER)
    return value is True or str(value).strip().lower() in ("true", "yes")


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

    def _verbose(self):
        return self._display.verbosity > 0

    def _print(self, msg, color=None):
        for line in lines(msg):
            self._display.display(line, color=color)

    # --- results ---

    def v2_runner_on_ok(self, result):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_ok(result)
        task = _task(result)
        self._handle_warnings(_result(result))  # a warning is meant to be read
        if marked(task) and not (task.loop and "results" in _result(result)) and self._says(task):
            self._print(_result(result).get("msg"))

    @staticmethod
    def _says(task):
        """A passed assert says something only with a success_msg (else
        "All assertions passed")."""
        return task.action not in _ASSERTS or "success_msg" in (task.args or {})

    def v2_runner_item_on_ok(self, result):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_item_on_ok(result)
        self._handle_warnings(_result(result))
        if marked(_task(result)) and self._says(_task(result)):
            self._print(_result(result).get("msg"))

    def _own_failure(self, result):
        """A marked assert or fail: its msg is the verdict, printed as is."""
        task, res = _task(result), _result(result)
        return marked(task) and task.action in _VERDICTS and "msg" in res

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if self._verbose():
            return super(CallbackModule, self).v2_runner_on_failed(result, ignore_errors)
        if ignore_errors:
            return None  # the playbook expects it (ignore_errors) and handles it
        if self._own_failure(result) and not (_task(result).loop and "results" in _result(result)):
            return self._print(_result(result)["msg"], color=C.COLOR_ERROR)
        if _task(result).loop and "results" in _result(result):
            return None  # each failed item was printed already
        return super(CallbackModule, self).v2_runner_on_failed(result, ignore_errors)

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
            return self._print(res["msg"], color=C.COLOR_ERROR)
        return super(CallbackModule, self).v2_runner_item_on_failed(result)

    def v2_runner_on_unreachable(self, result):
        if self._verbose() or not _task(result).ignore_unreachable:
            return super(CallbackModule, self).v2_runner_on_unreachable(result)
        # the playbook goes on without the host (ignore_unreachable): one line, never silent
        msg = lines(_result(result).get("msg") or "")
        host = getattr(result, "host", None) or result._host
        self._display.display("UNREACHABLE  %s  %s" % (host.get_name(), msg[0] if msg else ""), color=C.COLOR_UNREACHABLE)
        return None

    def v2_runner_on_async_failed(self, result):
        return super(CallbackModule, self).v2_runner_on_async_failed(result)

    def v2_on_file_diff(self, result):
        return super(CallbackModule, self).v2_on_file_diff(result)

    # --- what is quiet without -v ---

    def _quiet(name):  # pylint: disable=no-self-argument
        def method(self, *args, **kwargs):
            if self._verbose():
                return getattr(super(CallbackModule, self), name)(*args, **kwargs)
            return None
        method.__name__ = name
        return method

    v2_runner_on_skipped = _quiet("v2_runner_on_skipped")
    v2_runner_item_on_skipped = _quiet("v2_runner_item_on_skipped")
    v2_runner_retry = _quiet("v2_runner_retry")
    v2_runner_on_start = _quiet("v2_runner_on_start")
    v2_runner_on_async_poll = _quiet("v2_runner_on_async_poll")
    v2_runner_on_async_ok = _quiet("v2_runner_on_async_ok")
    v2_playbook_on_start = _quiet("v2_playbook_on_start")
    v2_playbook_on_play_start = _quiet("v2_playbook_on_play_start")
    v2_playbook_on_task_start = _quiet("v2_playbook_on_task_start")
    v2_playbook_on_handler_task_start = _quiet("v2_playbook_on_handler_task_start")
    v2_playbook_on_include = _quiet("v2_playbook_on_include")
    v2_playbook_on_notify = _quiet("v2_playbook_on_notify")
    v2_playbook_on_stats = _quiet("v2_playbook_on_stats")
    v2_playbook_on_no_hosts_remaining = _quiet("v2_playbook_on_no_hosts_remaining")  # after the failures shown
    del _quiet
