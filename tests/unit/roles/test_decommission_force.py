from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_decommission_force: the decommission runs with --force (4.0+
# refuses to go below the replication factor without it), and the advice to
# resume a failed one says --force too.

import os

import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import render_with

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_operation", "tasks")


def load(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    value = render_with(template, variables)
    return " ".join(value.split()) if isinstance(value, str) else value


def test_the_module_forced_and_the_advice_says_so():
    tasks = dict((t["name"], t) for t in load("action_decommission.yml"))
    force = tasks["Decommission the node"]["community.cassandra.cassandra_decommission"]["force"]
    advice = tasks["Follow the decommission"]["vars"]["_cassandra_stream_advice"]
    stop = next(t for t in load("stream_wait.yml") if t["name"] == "Stop when it failed or stopped moving")
    for given, word in ((True, "nodetool decommission --force on it resumes it"),
                        (False, "nodetool decommission on it resumes it")):
        assert render(force, cassandra_decommission_force=given) is given
        assert word in render(advice, cassandra_decommission_force=given)
        msg = render(stop["ansible.builtin.fail"]["msg"], inventory_hostname="node3", cassandra_stream_stall_time=1800,
                     cassandra_stream_max_time=0, _cassandra_stream_what="decommission",
                     _cassandra_stream_status="leave_failed", cassandra_decommission_force=given,
                     _cassandra_stream_report=["x"])
        assert word in msg
