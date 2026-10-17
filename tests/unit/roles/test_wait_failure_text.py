from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The message a wait stops with (stream_wait.yml, cleanup_wait.yml): one line of sentences, each status its own
# text, without a leading space, a kept line break or a double space.

import os

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks")


def stop(name, file):
    with open(os.path.join(TASKS, file), encoding="utf-8") as f:
        return next(t for t in yaml.safe_load(f) if t.get("name") == name)


def render(task, **variables):
    variables = dict(task.get("vars") or {}, **variables)
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(task["ansible.builtin.fail"]["msg"]))


STREAM = dict(inventory_hostname="n1", _cassandra_stream_what="bootstrap of n1", _cassandra_stream_report=["r1 50%"],
              cassandra_stream_stall_time=600, cassandra_stream_max_time=3600, _cassandra_stream_state={"idle": 700},
              _cassandra_stream_job_result={"msg": ["boom", "x"], "stderr": "err"},
              _cassandra_stream_refused=[{"host": "n2", "error": "auth"}, {"host": "n3", "error": "auth2"}])
HEAD = "n1, bootstrap of n1: "
LAST = " Last progress: r1 50%"
GENERIC = "check nodetool netstats and system.log on the nodes."


@pytest.mark.parametrize("status, advice, force, text", [
    ("job_failed", None, False, "failed: boom x err" + LAST),
    ("job_lost", None, False, "the job running it can't be read (['boom', 'x']): check the node (nodetool netstats,"
                              " system.log)." + LAST),
    ("join_failed", None, False, "the bootstrap failed (Mode JOINING_FAILED): see why in its system.log." + LAST),
    ("join_failed", "check X.", False, "the bootstrap failed (Mode JOINING_FAILED): see why in its system.log."
                                       " check X." + LAST),
    ("leave_failed", None, True, "the decommission failed (Mode DECOMMISSION_FAILED): see why in its system.log. Once"
                                 " fixed, nodetool decommission --force on it resumes it (the ranges already streamed"
                                 " are skipped), or restart Cassandra on it to cancel it (it comes back UN)." + LAST),
    ("leave_failed", None, False, "the decommission failed (Mode DECOMMISSION_FAILED): see why in its system.log. Once"
                                  " fixed, nodetool decommission on it resumes it (the ranges already streamed"
                                  " are skipped), or restart Cassandra on it to cancel it (it comes back UN)." + LAST),
    ("stopped", "", False, "Cassandra stopped during the bootstrap of n1: see why in its system.log." + LAST),
    ("jmx_refused", None, False, "JMX login refused on n2, n3: check cassandra_jmx_username/password (or"
                                 " cassandra_jmx_password_file) for them (nodetool: auth). Nothing was stopped: the"
                                 " bootstrap of n1 goes on by itself; once fixed, run the playbook again to follow it."),
    ("stalled", None, False, "no streaming progress for 11m (cassandra_stream_stall_time 600s). Nothing was stopped: "
                             + GENERIC + LAST),
    ("max_time", "check X.", False, "still going after 3600s (cassandra_stream_max_time). Nothing was stopped:"
                                    " check X." + LAST),
])
def test_stream_stop(status, advice, force, text):
    variables = dict(STREAM, _cassandra_stream_status=status, cassandra_decommission_force=force)
    if advice is not None:
        variables["_cassandra_stream_advice"] = advice
    assert render(stop("Stop when it failed or stopped moving", "stream_wait.yml"), **variables) == HEAD + text


CLEANUP = dict(_cassandra_cleanup_jobs=[{"item": {"item": "n1"}, "failed": True, "msg": "bad"},
                                        {"item": {"item": "n2"}, "finished": 1}, {"item": {"item": "n3"}}],
               _cassandra_cleanup_refused=[{"host": "n2", "error": "auth"}], _cassandra_stream_state={"idle": 70},
               cassandra_stream_stall_time=600)


@pytest.mark.parametrize("status, text", [
    ("failed", "Cleanup failed or can't be followed on n1, n3: bad (see system.log there)."
               " Last progress: cleanup  FAILED  total unknown"),
    ("jmx_refused", "JMX login refused on n2: check cassandra_jmx_username/password (or cassandra_jmx_password_file)"
                    " for it (nodetool: auth). Nothing was stopped: the cleanups go on by themselves."
                    " Last progress: cleanup  JMX LOGIN REFUSED  total unknown"),
    ("stalled", "no cleanup progress for 1m (cassandra_stream_stall_time 600s; nodetool compactionstats). Nothing was"
                " stopped: check nodetool compactionstats and system.log on the nodes."
                " Last progress: cleanup  STALLED  total unknown"),
])
def test_cleanup_stop(status, text):
    task = stop("Stop when a cleanup failed or stopped moving", "cleanup_wait.yml")
    assert render(task, _cassandra_cleanup_status=status, **CLEANUP) == text
