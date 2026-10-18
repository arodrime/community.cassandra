from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The wait for a node to be NORMAL after its (re)start (wait_normal.yml):
# progress lines like the other waits, read after read, every 10 s, then
# every 30 s after 5 minutes.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_normal_wait_line

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks")


def load(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def flat(tasks):
    out = []
    for t in tasks:
        out.append(t)
        for key in ("block", "rescue", "always"):
            out.extend(flat(t.get(key) or []))
    return out


def task(tasks, name):
    return next(t for t in flat(tasks) if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_the_progress_line():
    assert cassandra_normal_wait_line("JOINING", waited=70, timeout=600, node="node2", operation="restart") == \
        u"node2 restart  waiting for NORMAL (UJ→UN)  1m10s/10m"
    # a rolling operation: [i/n], as its other progress lines
    assert cassandra_normal_wait_line("STARTING", waited=5, timeout=600, index=2, steps=5, node="node2",
                                      operation="restart") == \
        "[2/5] node2 restart  waiting for NORMAL (mode STARTING)  5s/10m"
    # JMX not answering yet
    assert cassandra_normal_wait_line("", waited=10, timeout=600, node="node2", operation="start") == \
        "node2 start  waiting for NORMAL (no JMX answer yet)  10s/10m"
    assert cassandra_normal_wait_line("NORMAL", done=True, waited=80, node="node2", operation="restart") == \
        "node2 restart  NORMAL after 1m20s"


def test_reads_through_the_wait_loop_with_a_line_and_a_wait_each():
    tasks = load("wait_normal.yml")
    # read once, the loop only when it is not NORMAL yet
    assert task(tasks, "Read its mode")["ansible.builtin.include_tasks"] == "wait_normal_check.yml"
    wait = task(tasks, "Wait for the node to be joined (Mode NORMAL)")
    assert wait["ansible.builtin.include_tasks"] == "wait_loop.yml" and wait["when"] == "_cassandra_normal_going | bool"
    assert wait["vars"]["_cassandra_wait_check"] == "wait_normal_check.yml"
    check = load("wait_normal_check.yml")
    said = task(check, "While it is not NORMAL")
    assert said["when"] == "_cassandra_normal_going | bool"
    line = task(said["block"], "Say what is waited for")
    assert line["vars"]["cassandra_output"] is True
    # its progress lines a block of their own: a gap before the first one
    assert task(said["block"], "Start the progress lines")["vars"]["cassandra_output_gap"] is True
    # every 10 s, then every 30 s after 5 minutes (the health wait's intervals)
    interval = task(said["block"], "Wait before the next read")["vars"]["_interval"]
    for waited, seconds in ((0, 10), (290, 10), (300, 30), (900, 30)):
        assert int(render(interval, now=lambda: _Now(1000 + waited), _cassandra_normal_start=1000,
                          cassandra_stream_early_check_interval=10, cassandra_stream_early_time=300,
                          cassandra_stream_check_interval=30)) == seconds


def test_the_head_of_its_lines():
    head = task(load("wait_normal.yml"), "Note what is waited for")
    facts, variables = head["ansible.builtin.set_fact"]["_cassandra_normal_head"], head["vars"]
    hosts = ["node1", "node2", "node3"]

    def shown(**given):
        values = dict(variables, ansible_play_hosts_all=hosts, inventory_hostname="node2", **given)
        values["_rolling"] = render(variables["_rolling"], **values)
        return dict((k, str(render(v, **values)).strip()) for k, v in facts.items())

    assert shown(cassandra_service_node_action="restart") == {
        "index": "2", "steps": "3", "node": "node2", "operation": "restart"}
    # not a node operation (the role on its own): what the role does
    assert shown(cassandra_service_state="restarted")["operation"] == "restart"
    assert shown(cassandra_service_state="started") == {"index": "0", "steps": "0", "node": "node2", "operation": "start"}
    assert shown(cassandra_service_state="started", cassandra_service_restart_on_change=True,
                 cassandra_service_unit={"changed": True})["operation"] == "restart"  # its unit changed: restarted


class _Now(object):
    def __init__(self, epoch):
        self.epoch = epoch

    def timestamp(self):
        return self.epoch
