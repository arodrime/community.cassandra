# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_confirm_answer: the operator's answer to the confirmation
(the result of ansible.builtin.pause), as go, stop, again or no_terminal.
Not yes and no: set_fact turns those strings into booleans before 2.19.
cassandra_confirm_stop: the stop after a no (or no yes or no), its way out."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import datetime

from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_command
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

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
    around spaces; an empty answer returned at once means no terminal.
    -> "go" or "stop": {"user_input": " Y "} -> "go"."""
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


# the width of a stop: 79 columns (an 80-column terminal)
WIDTH = 79


def cassandra_confirm_stop(why, operation="", cluster="", unchanged="nothing changed", inventory=None,
                           default_inventory=None, hosts="", cwd=None, limit="", again="", targets=None):
    """why: what stopped the question ("at your request", "no yes or no after 3 answers"); operation: the playbook
    asked about ("" when not one: a role's question); unchanged: what was left as it was; limit: the run's --limit;
    again: the run's own command when its playbook builds it (stop_rack's -e cassandra_target_dc...), else the
    playbook's with the run's inventory, cluster, --limit and targets (its target variables set: {name: value}). -> the stop's lines: the verdict naming the operation
    and the cluster, then WAY OUT: the command to run again (the other -e options of this run not known here: said
    so).
    Example: "at your request", "apply_config", "c1", inventory=["inv.yml"] -> ["STOPPED  apply_config  c1  at your
    request, nothing changed", "WAY OUT  run it again (add the -e options you gave, if any), answer yes:",
    "  ansible-playbook -i inv.yml community.cassandra.apply_config"]."""
    head = "  ".join(x for x in ("STOPPED", str(operation or ""), str(cluster or ""), "%s, %s" % (why, unchanged)) if x)
    if not operation:
        return out.fit([head], WIDTH)
    extra = dict((k, str(v)) for k, v in sorted((targets or {}).items()) if str(v))
    command = str(again or "") or cassandra_command(str(operation), inventory=inventory, hosts=hosts, cwd=cwd,
                                                    limit=limit or None, extra=extra or None,
                                                    default_inventory=default_inventory)
    note = "with the other -e options you gave" if " -e " in command else "add the -e options you gave, if any"
    return out.fit([head, "WAY OUT  run it again (%s), answer yes:" % note, "  " + command], WIDTH)


class FilterModule(object):
    def filters(self):
        return {"cassandra_confirm_answer": cassandra_confirm_answer, "cassandra_confirm_stop": cassandra_confirm_stop}
