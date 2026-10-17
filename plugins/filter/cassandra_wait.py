# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_stream_failure: why a streaming wait stopped (stream_wait.yml), from its status;
cassandra_cleanup_failure: why the wait for a batch of cleanups stopped (cleanup_wait.yml). One line each,
ending with the last progress line."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

_GENERIC = "check nodetool netstats and system.log on the nodes."


def _flat(value):
    """value, a list flattened (as the flatten filter does: no null items)."""
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _flat(v)]
    return [] if value in (None, "None", "null") else [value]


def _text(value):
    """value as a template prints it: nothing for a null."""
    return "" if value is None else value


def _login(refused):
    return ("JMX login refused on %s: check cassandra_jmx_username/password (or cassandra_jmx_password_file) for %s"
            " (nodetool: %s). Nothing was stopped:"
            % (", ".join(str(r["host"]) for r in refused), "it" if len(refused) == 1 else "them", refused[0]["error"]))


def cassandra_stream_failure(status, host, what, job=None, advice=None, force=False, refused=None, idle=0,
                             stall_time=0, max_time=0, last=""):
    """status: _cassandra_stream_status (not done nor going); host: the node; what: what was waited for (e.g.
    "bootstrap of node7"); job: the async job's result (job_failed, job_lost); advice: what to check
    (_cassandra_stream_advice, None when not set); force: cassandra_decommission_force; refused: [{host, error}]
    (jmx_refused); idle: the seconds without progress (stalled); stall_time, max_time: the limits;
    last: the last progress line. -> the message."""
    job = job or {}
    tip = " " + advice if advice else ""
    if status == "job_failed":
        why = "failed: %s %s" % (" ".join(str(m) for m in _flat([job.get("msg", "")])), _text(job.get("stderr", "")))
    elif status == "job_lost":
        why = ("the job running it can't be read (%s): check the node (nodetool netstats, system.log)."
               % (_text(job.get("msg", "")),))
    elif status == "join_failed":
        why = "the bootstrap failed (Mode JOINING_FAILED): see why in its system.log." + tip
    elif status == "leave_failed":
        why = ("the decommission failed (Mode DECOMMISSION_FAILED): see why in its system.log. Once fixed, nodetool"
               " decommission%s on it resumes it (the ranges already streamed are skipped), or restart Cassandra on"
               " it to cancel it (it comes back UN)." % (" --force" if force else ""))
    elif status == "stopped":
        why = "Cassandra stopped during the %s: see why in its system.log.%s" % (what, tip)
    elif status == "jmx_refused":
        return ("%s, %s: %s the %s goes on by itself; once fixed, run the playbook again to follow it."
                % (host, what, _login(refused), what))
    elif status == "stalled":
        why = ("no streaming progress for %s (cassandra_stream_stall_time %ss). Nothing was stopped: %s"
               % (out.duration(idle, short=True), stall_time, _GENERIC if advice is None else advice))
    else:
        why = ("still going after %ss (cassandra_stream_max_time). Nothing was stopped: %s"
               % (max_time, _GENERIC if advice is None else advice))
    return "%s, %s: %s Last progress: %s" % (host, what, why, last)


def cassandra_cleanup_failure(status, jobs, refused=None, idle=0, stall_time=0, last=""):
    """status: _cassandra_cleanup_status (failed, jmx_refused or stalled); jobs: the cleanup jobs' async results
    (item.item: the node); refused: [{host, error}] (jmx_refused); idle: the seconds without progress; stall_time:
    the limit; last: the last progress line. -> the message. failed: the nodes whose job failed, or ended without
    a result."""
    if status == "failed":
        failed = [j for j in jobs if j.get("failed")]
        hosts = []
        for j in failed + [j for j in jobs if "unreachable" not in j and "finished" not in j]:
            if j["item"]["item"] not in hosts:
                hosts.append(j["item"]["item"])
        why = ("Cleanup failed or can't be followed on %s: %s (see system.log there)."
               % (", ".join(str(h) for h in hosts), "; ".join(str(j.get("msg", "")) for j in failed)))
    elif status == "jmx_refused":
        why = _login(refused) + " the cleanups go on by themselves."
    else:
        why = ("no cleanup progress for %s (cassandra_stream_stall_time %ss; nodetool compactionstats). Nothing was"
               " stopped: check nodetool compactionstats and system.log on the nodes."
               % (out.duration(idle, short=True), stall_time))
    return "%s Last progress: %s" % (why, last)


class FilterModule(object):
    def filters(self):
        return {"cassandra_stream_failure": cassandra_stream_failure,
                "cassandra_cleanup_failure": cassandra_cleanup_failure}
