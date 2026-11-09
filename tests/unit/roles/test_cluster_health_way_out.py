from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cluster_health.yml's stop: its way out ends "then resume", "then check again" under --check (no progress file
# was written to resume), as the stop block of the run says it.

import os

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_operation", "tasks", "cluster_health.yml")


def said(check, problems):
    with open(PATH, encoding="utf-8") as f:
        stop = next(t for t in yaml.safe_load(f) if t.get("name") == "Stop on an unhealthy cluster")
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in stop["vars"].items() if k != "_absent")
    variables.update(_absent=[], ansible_check_mode=check, cassandra_health_problems=problems)
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(stop["ansible.builtin.fail"]["msg"]))


def test_the_way_out_under_check():
    down = ["n4 (10.0.0.4, r1) is DN, seen from n1"]
    assert said(False, down)[1] == "WAY OUT  start n4 (the server, then Cassandra), then resume"
    assert said(True, down)[1] == "WAY OUT  start n4 (the server, then Cassandra), then check again"
    assert said(True, ["the schema differs"])[1] == "WAY OUT  fix it (the lines above say what), then check again"
    assert said(False, ["the schema differs"])[1] == "WAY OUT  fix it (the lines above say what), then resume"
