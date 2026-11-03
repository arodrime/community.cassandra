# -*- coding: utf-8 -*-
# The health checks only read: none of their tasks reports a change (a job started with async reports changed
# unless told otherwise), so a health_check run shows changed=0.
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import yaml

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_operation", "tasks",
                     "cluster_health_read.yml")


def _tasks(items):
    for task in items or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _tasks(task.get(key))


def test_every_async_read_reports_no_change():
    with open(TASKS) as f:
        tasks = list(_tasks(yaml.safe_load(f)))
    started = [t for t in tasks if "async" in t]
    assert len(started) >= 5
    for task in started:
        assert task.get("changed_when") is False, task.get("name")
