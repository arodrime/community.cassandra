from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# start_rack's check: the rack is checked from one of its nodes and must be UN;
# a node outside the rack not UN (down, joining, leaving, moving...) or not
# reached over ssh is said in a warning, not waited for, and does not stop the
# run. The expressions are read from the playbook, rendered by Ansible, with the
# findings cassandra_health_findings gives; the ssh probe is run by
# ansible-playbook on local hosts and one not reached.

import os
import subprocess
import sys

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
SAY = "Say the nodes outside the rack not UN or not reached"
PROBE = ["Reach each node over ssh", "Stop when a node of the rack is not reached over ssh",
         "Leave out a node outside the rack not reached over ssh", "Gather the network facts"]

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


def check(unreached=(), **states):
    """the first read, the nodes outside the rack noted, the check again with them expected as they are (when the
    first read found a problem): (the problems that stop the run, the warning said or None)"""
    read = ring(**states)
    first = findings(read)
    variables = dict(groups=GROUPS, cassandra_health_findings=first, cassandra_target_rack="r2", cassandra_target_dc="dc1",
                     _cassandra_start_rack_unreached=list(unreached))
    others = render(TASKS[NOTE]["ansible.builtin.set_fact"]["_cassandra_start_rack_others"], **variables)
    variables["_cassandra_start_rack_others"] = others
    problems = []
    if cassandra_health_sentences(first):
        task_vars = dict(TASKS[CHECK]["vars"])
        variables["_others"] = others
        down_ok = render(task_vars["cassandra_service_health_down_ok"], **variables)
        up_ok = render(task_vars["cassandra_service_health_up_ok"], **variables)
        problems = cassandra_health_sentences(findings(read, down_ok=down_ok, up_ok=up_ok))
    if problems or not render("{{ %s }}" % TASKS[SAY]["when"][2], **variables):
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
        "WARNING  outside the rack, not waited for:",
        "  n4  DN",
        "The health_check playbook checks the cluster."]
    assert all(len(line) <= 79 for line in said.split("\n"))


def test_any_state_outside_the_rack_one_row_each():
    # moving (UM) too, and any other up state: not judged outside the rack
    problems, said = check(n1="UL", n4="UM")
    assert problems == []
    assert said.split("\n")[2:4] == ["  n1  UL", "  n4  UM"]
    problems, said = check(n1="UJ", n4="DJ")
    assert problems == []
    assert said.split("\n")[2:4] == ["  n1  UJ", "  n4  DJ"]


def test_a_node_outside_the_rack_not_reached_over_ssh():
    # its ring state when the read has one for it, else the probe's fact alone
    problems, said = check(unreached=["n4"])
    assert problems == []
    assert said.split("\n")[1:3] == ["WARNING  outside the rack, not waited for:", "  n4  not reached over ssh"]
    problems, said = check(unreached=["n4"], n4="DN", n1="UL")
    assert problems == []
    assert said.split("\n")[2:4] == ["  n1  UL", "  n4  DN  not reached over ssh"]


def test_a_node_of_the_rack_not_un_stops_the_run():
    # n4 is not waited for; n3 of the rack still stops the run, as before
    problems, said = check(n3="DN", n4="DN")
    assert problems == ["n3 (10.0.0.3, r2) is DN, seen from n1 and n2"]
    assert said is None
    for state in ("UJ", "UM", "UL"):
        problems, dummy = check(n2=state)
        assert problems and all(p.startswith("n2 ") for p in problems)


def test_a_healthy_cluster_reads_once_and_says_nothing():
    assert check() == ([], None)


def test_the_nodes_outside_the_rack_not_read_from():
    read = ring(n1="DN", n4="UL")
    variables = dict(groups=GROUPS, cassandra_health_findings=findings(read))
    others = render(TASKS[NOTE]["ansible.builtin.set_fact"]["_cassandra_start_rack_others"], **variables)
    assert render(TASKS[CHECK]["vars"]["_cassandra_health_not_peers"], _others=others,
                  _cassandra_start_rack_unreached=["n5", "n4"]) == "n1:n4:n5"
    assert render(TASKS[READ]["vars"]["_cassandra_health_not_peers"], _cassandra_start_rack_unreached=["n5"]) == "n5"


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
    # the facts gathered after the probe, before any read
    assert PLAY["gather_facts"] is False
    assert [NAMES.index(n) for n in PROBE] == sorted(NAMES.index(n) for n in PROBE) and NAMES.index(PROBE[-1]) < NAMES.index(READ)


# the probe run by ansible-playbook: node1 (the rack) and node2 local, node3 an ssh port nothing listens on
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
INVENTORY = """
all:
  hosts:
    node1: {ansible_connection: local}
    node2: {ansible_connection: local}
    node3: {ansible_connection: ssh, ansible_host: 127.0.0.1, ansible_port: 1, ansible_user: nobody}
  children:
    cassandra_target_rack_nodes:
      hosts:
        %s:
"""


def probe(tmp_path, rack):
    tasks = [t for t in PLAY["tasks"] if t["name"] in PROBE] + [
        {"name": "Say", "ansible.builtin.debug": {"msg": "unreached={{ _cassandra_start_rack_unreached }}"
                                                         " checker={{ _cassandra_start_rack_checker }}"},
         "run_once": True}]
    play = dict(PLAY, hosts="all", become=False, tasks=tasks)
    (tmp_path / "play.yml").write_text(yaml.safe_dump([play]))
    (tmp_path / "hosts.yml").write_text(INVENTORY % rack)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0",
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
               ANSIBLE_SSH_ARGS="-o ConnectTimeout=5 -o BatchMode=yes")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.yml"),
            "-e", "ansible_python_interpreter=" + sys.executable, "-e", "cassandra_target_rack=r2", str(tmp_path / "play.yml")]
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)


def test_a_host_outside_the_rack_not_reached_leaves_the_run_a_success(tmp_path):
    run = probe(tmp_path, "node1")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "unreached=['node3'] checker=node1" in run.stdout
    assert "unreachable=0" in run.stdout.replace(" ", "")


def test_a_host_of_the_rack_not_reached_stops_the_run(tmp_path):
    run = probe(tmp_path, "node3")
    assert run.returncode != 0
    assert "node3 (rack r2) is not reached over ssh" in run.stdout
    assert "WAY OUT  reach it, then run start_rack again" in run.stdout
    assert "unreached=" not in run.stdout  # any_errors_fatal: nothing after it
