# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_confirm_answer: the operator's answer to the confirmation
(the result of ansible.builtin.pause), as go, stop, again or no_terminal.
Not yes and no: set_fact turns those strings into booleans before 2.19."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import datetime

# Without a terminal, pause returns an empty answer at once (a few ms, with a
# warning). Someone pressing Enter needs more: pause also drops the keys
# pressed before the prompt shows.
NO_TERMINAL_SECONDS = 0.1


def _time(text):
    # str(datetime): no fraction when the microseconds are 0
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except (TypeError, ValueError):
            pass
    return None


def _seconds(reply):
    start, stop = _time(reply.get("start")), _time(reply.get("stop"))
    return None if start is None or stop is None else (stop - start).total_seconds()


def cassandra_confirm_answer(reply):
    """reply: the registered result of pause. yes/y and no/n in any case,
    around spaces; an empty answer returned at once means no terminal."""
    reply = reply or {}
    answer = str(reply.get("user_input") or "").strip().lower()
    if answer in ("yes", "y"):
        return "go"
    if answer in ("no", "n"):
        return "stop"
    if not answer:
        seconds = _seconds(reply)
        if seconds is not None and seconds < NO_TERMINAL_SECONDS:
            return "no_terminal"
    return "again"


class FilterModule(object):
    def filters(self):
        return {"cassandra_confirm_answer": cassandra_confirm_answer}
