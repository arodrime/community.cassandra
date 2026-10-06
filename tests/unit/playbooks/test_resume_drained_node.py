from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A rolling run that stopped after draining a node (DN for the others):
# resumed, that node only (the first one not done) is checked from another
# node and may be down, its drain may fail (drained or stopped already), and
# its action brings it back. Any other node down still stops the run.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import cassandra_health_problems

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


OPERATION = load("roles", "cassandra_service", "tasks", "node_operation.yml")[1]
BEFORE = [t for t in OPERATION["block"] if t["name"] == "Check the cluster before touching this node"][0]
HOSTS = ["n1", "n2", "n3"]


@pytest.mark.parametrize("resume, action, host, done, resumed", [
    (True, "restart", "n2", ["n1"], True),          # the node the run stopped at
    (True, "restart", "n3", ["n1"], False),         # a later node: checked as usual
    (False, "restart", "n2", ["n1"], False),        # not a resumed run
    (True, "upgrade", "n1", [], True),
    (True, "decommission", "n2", ["n1"], False),    # an operation that does not bring the node back
])
def test_only_the_node_the_run_stopped_at(resume, action, host, done, resumed):
    variables = dict(cassandra_rolling_resume=resume, cassandra_service_node_action=action, inventory_hostname=host,
                     ansible_play_hosts_all=HOSTS, cassandra_progress_done=done)
    assert render(OPERATION["vars"]["_cassandra_resumed_node"], **variables) is resumed
    checks = dict(variables, _cassandra_resumed_node=resumed, _cassandra_preflight={"ring_address": "10.0.0.2"})
    assert render(BEFORE["vars"]["cassandra_service_health_node_checks"], **checks) is (not resumed)
    assert render(BEFORE["vars"]["cassandra_service_health_resumed_down"], **checks) == (["10.0.0.2"] if resumed else [])


def test_the_health_check_lets_that_node_only_be_down():
    block = [t for t in load("roles", "cassandra_service", "tasks", "cluster_health.yml") if "block" in t][0]
    down_ok = render(block["vars"]["_down_ok"], cassandra_service_health_resumed_down=["10.0.0.2"])
    assert down_ok == ["10.0.0.2"]
    ring = {"dc1": {"nodes": [{"address": "10.0.0.%d" % i, "rack": "r1", "status": "U", "state": "N"} for i in (1, 2, 3)]}}
    ring["dc1"]["nodes"][1]["status"] = "D"
    view = [{"from": "n1", "result": {"cluster_status": ring}}]
    assert cassandra_health_problems(view, 3, "n2", down_ok=down_ok) == []
    ring["dc1"]["nodes"][2]["status"] = "D"
    assert cassandra_health_problems(view, 3, "n2", down_ok=down_ok) == ["10.0.0.3 (r1) is DN, seen from n1"]


@pytest.mark.parametrize("action", ["restart", "reboot", "upgrade"])
def test_its_drain_may_fail(action):
    drain = [t for t in load("roles", "cassandra_service", "tasks", "action_%s.yml" % action) if t["name"] == "Drain the node"][0]
    failed = {"failed": True, "msg": "Connection refused"}
    condition = "{{ %s }}" % drain["failed_when"]
    assert render(condition, cassandra_service_drain=failed, _cassandra_resumed_node=True) is False
    assert render(condition, cassandra_service_drain=failed, _cassandra_resumed_node=False) is True
    assert render(condition, cassandra_service_drain={"changed": True}) is False


def test_preflight_lets_a_resumed_run_pass_a_member_seed_down():
    play = load("playbooks", "preflight.yml")[1]
    check = [t for t in play["tasks"] if t.get("name") == "Check the running cluster"][0]
    seeds = [t for t in check["block"] if t["name"] == "Every seed is up"][0]
    condition = "{{ %s }}" % seeds["ansible.builtin.assert"]["that"].strip()
    variables = {"_addr": "10.0.0.2", "_up": ["10.0.0.1"], "_all": ["10.0.0.1", "10.0.0.2"], "_host": ["10.0.0.2"]}
    assert render(condition, cassandra_rolling_resume=True, **variables) is True
    assert render(condition, cassandra_rolling_resume=False, **variables) is False
    # not a member of the ring: refused, resumed or not
    assert render(condition, cassandra_rolling_resume=True, **dict(variables, _all=["10.0.0.1"])) is False
