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
    variables.update(_absent=[], ansible_check_mode=check, cassandra_health_problems=problems,
                     _cassandra_run_command_args={"inventory": ["/inv/hosts.yml"], "default_inventory": "/etc/ansible/hosts",
                                                  "hosts": "prod", "cwd": "/inv"})
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(stop["ansible.builtin.fail"]["msg"]))


def test_the_way_out_under_check():
    down = ["n4 (10.0.0.4, r1) is DN, seen from n1"]
    assert said(False, down)[1] == "WAY OUT  bring n4 back to UN (its host and/or Cassandra), then resume"
    assert said(True, down)[1] == "WAY OUT  bring n4 back to UN (its host and/or Cassandra), then check again"
    assert said(True, ["the schema differs"])[1] == "WAY OUT  fix it (the lines above say what), then check again"
    assert said(False, ["the schema differs"])[1] == "WAY OUT  fix it (the lines above say what), then resume"


def test_a_node_lost_for_good_gets_full_commands():
    # each way out a command to paste, not a playbook's name
    lines = said(False, ["n4 (10.0.0.4, r1) is DN, seen from n1"])
    # (review 3) a block of its own, apart from the resume, with the conditions its playbooks state (replace_node.yml:
    # a blank host, the dead one out of the inventory, a dead seed out of cassandra_seeds first; remove_dead_node.yml:
    # the last resort, its data rebuilt from the other replicas, the dead host out of the inventory first)
    assert lines[2:] == [
        "IF n4 IS LOST FOR GOOD",
        "replace it: NEW_NODE (a blank host) in prod, n4 out of the inventory",
        "(a seed: out of cassandra_seeds first, with change_seeds)",
        "  ansible-playbook -i hosts.yml community.cassandra.replace_node -e cassandra_hosts=prod"
        " -e cassandra_target_nodes=NEW_NODE -e cassandra_replace_address=10.0.0.4",
        "last resort, no replacement: n4 out of the inventory first; the other replicas rebuild its data (removenode)",
        "  ansible-playbook -i hosts.yml community.cassandra.remove_dead_node -e cassandra_hosts=prod"
        " -e cassandra_target_nodes=10.0.0.4"]
    # two nodes down: "their"
    assert said(False, ["n4 (10.0.0.4, r1) is DN, seen from n1", "n5 (10.0.0.5, r1) is DN, seen from n1"])[2] == (
        "WAY OUT  bring n4, n5 back to UN (their host and/or Cassandra), then resume")


def test_each_problem_and_the_wait_on_a_line_of_their_own():
    # the stop block says the cause one fact per line under its Why: (80 columns)
    with open(PATH, encoding="utf-8") as f:
        stop = next(t for t in yaml.safe_load(f) if t.get("name") == "Stop on an unhealthy cluster")
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in stop["vars"].items() if k != "_absent")
    variables.update(_absent=[], ansible_check_mode=False, cassandra_service_health_timeout=600,
                     cassandra_health_problems=["n4 (10.0.0.4, r1) is DN, seen from n1", "the schema differs"],
                     _cassandra_health_waited=True, _cassandra_health_wait_start=0,
                     _cassandra_run_command_args={"hosts": "prod"})
    lines = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(stop["ansible.builtin.fail"]["msg"]))
    assert lines[:2] == ["n4 (10.0.0.4, r1) is DN, seen from n1", "the schema differs"]
    assert lines[2].startswith("waited ") and lines[2].endswith(" (cassandra_service_health_timeout: 600s)")
    assert lines[3] == "WAY OUT  bring n4 back to UN (its host and/or Cassandra), then resume"
