from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# start_rack's check: the rack is checked from one of its nodes and must be UN;
# a node outside the rack not UN (down, joining, leaving) is said in a warning,
# not waited for, and does not stop the run. The expressions are read from the
# playbook, rendered by Ansible, with the findings cassandra_health_findings
# gives.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import (
    cassandra_health_findings, cassandra_health_sentences)
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

load_collection_filters()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "start_rack.yml"), encoding="utf-8") as f:
    PLAY = next(p for p in yaml.safe_load(f) if p.get("name") == "Check the cluster")
TASKS = dict((t["name"], t) for t in PLAY["tasks"])
NAMES = [t["name"] for t in PLAY["tasks"]]

READ = "Read the cluster once from a node of the rack"
NOTE = "Note the nodes outside the rack not UN"
CHECK = "Check the rack's nodes and the rest of the cluster"
SAY = "Say the nodes outside the rack not UN"

# rack r2 of dc1 is started: n2 and n3; n1 in r1, n4 in r3
ADDRESSES = {"10.0.0.1": "n1", "10.0.0.2": "n2", "10.0.0.3": "n3", "10.0.0.4": "n4"}
RACKS = {"n1": "r1", "n2": "r2", "n3": "r2", "n4": "r3"}
GROUPS = {"cassandra_target_rack_nodes": ["n2", "n3"]}
OK = dict(gossip={"is_up": True}, binary={"is_up": True}, netstats={"streaming": False}, schema={"agreed": True})


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def ring(**states):
    """the ring as cassandra_status reads it, every node UN unless states says (n4="DN")"""
    nodes = [{"address": a, "rack": RACKS[n], "status": states.get(n, "UN")[0], "state": states.get(n, "UN")[1]}
             for a, n in sorted(ADDRESSES.items())]
    return {"cluster_status": {"dc1": {"nodes": nodes}}}


def findings(read, **ok):
    """one read from n2, the second view from n1"""
    views = [{"from": "n2", "result": read}, {"from": "n1", "result": read}]
    return cassandra_health_findings(views, 4, "n2", names=ADDRESSES, **dict(OK, **ok))


def check(**states):
    """the first read, the nodes outside the rack noted, the check again with them expected as they are:
    (the problems that stop the run, the warning said or None)"""
    read = ring(**states)
    first = findings(read)
    variables = dict(groups=GROUPS, cassandra_health_findings=first, cassandra_target_rack="r2", cassandra_target_dc="dc1")
    others = render(TASKS[NOTE]["ansible.builtin.set_fact"]["_cassandra_start_rack_others"], **variables)
    variables["_cassandra_start_rack_others"] = others
    if not cassandra_health_sentences(first):
        return [], None
    task_vars = dict(TASKS[CHECK]["vars"])
    variables["_others"] = others
    ok = dict((k, render(task_vars[k], **variables)) for k in (
        "cassandra_service_health_down_ok", "cassandra_service_health_joining_ok", "cassandra_service_health_leaving_ok"))
    variables["not_peers"] = render(task_vars["_cassandra_health_not_peers"], **variables)
    problems = cassandra_health_sentences(findings(
        read, down_ok=ok["cassandra_service_health_down_ok"], joining_ok=ok["cassandra_service_health_joining_ok"],
        leaving_ok=ok["cassandra_service_health_leaving_ok"]))
    if problems or not others:
        return problems, None
    say = TASKS[SAY]
    variables.update(say["vars"])
    variables["_rows"] = render(say["vars"]["_rows"], **variables)
    return problems, render(say["ansible.builtin.debug"]["msg"], **variables)


def test_a_node_down_outside_the_rack_is_a_warning_not_a_failure():
    problems, said = check(n4="DN")
    assert problems == []
    assert said.split("\n") == [
        "WARNING  every node of rack r2 (dc1) is UN",
        "WARNING  not UN outside the rack, not waited for:",
        "  n4  DN",
        "The health_check playbook checks the cluster."]
    assert all(len(line) <= 79 for line in said.split("\n"))


def test_nodes_joining_leaving_or_down_outside_the_rack_one_row_each():
    problems, said = check(n1="UL", n4="DJ")
    assert problems == []
    assert said.split("\n")[2:4] == ["  n1  UL", "  n4  DJ"]


def test_a_node_of_the_rack_not_un_stops_the_run():
    # n4 is not waited for; n3 of the rack still stops the run, as before
    problems, said = check(n3="DN", n4="DN")
    assert problems == ["n3 (10.0.0.3, r2) is DN, seen from n1 and n2"]
    assert said is None
    problems, dummy = check(n2="UJ")
    assert problems and all(p.startswith("n2 ") for p in problems)


def test_a_healthy_cluster_reads_once_and_says_nothing():
    assert check() == ([], None)


def test_the_nodes_outside_the_rack_not_read_from():
    read = ring(n1="DN", n4="UL")
    variables = dict(groups=GROUPS, cassandra_health_findings=findings(read))
    others = render(TASKS[NOTE]["ansible.builtin.set_fact"]["_cassandra_start_rack_others"], **variables)
    assert render(TASKS[CHECK]["vars"]["_cassandra_health_not_peers"], _others=others) == "n1:n4"


def test_checked_from_a_node_of_the_rack_and_not_under_check():
    checker = PLAY["vars"]["_cassandra_start_rack_checker"]
    assert render(checker, ansible_play_hosts=["n1", "n2", "n3", "n4"], groups=GROUPS) == "n2"
    # no node of the rack in the play (--limit): the first of the play, as before
    assert render(checker, ansible_play_hosts=["n1", "n4"], groups=GROUPS) == "n1"
    assert NAMES.index(READ) < NAMES.index(NOTE) < NAMES.index(CHECK) < NAMES.index(SAY)
    for name in (READ, NOTE, CHECK, SAY):
        assert "not ansible_check_mode" in TASKS[name]["when"]
    assert TASKS[READ]["vars"]["cassandra_service_health_report_only"] is True
    assert "cassandra_service_health_report_only" not in TASKS[CHECK]["vars"]
