from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# start_rack's check: the rack is checked from one of its nodes and must be UN;
# a node outside the rack not UN (down, joining, leaving, moving...) or not
# reached over ssh is said in a warning, not waited for, and does not stop the
# run. A node that may be of the rack (the inventory, the ring, or no host known
# by its address) is judged as one. The expressions are read from the playbook,
# rendered by Ansible, with the findings cassandra_health_findings gives; the
# ssh probe is run by ansible-playbook on local hosts and one not reached.

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

MEMBERS = "Find the rack's nodes in the inventory"
READ = "Read the cluster once from a node of the rack"
NOTE = "Note the nodes outside the rack not UN"
CHECK = "Check the rack's nodes and the rest of the cluster"
SAY = "Say the nodes outside the rack not UN or not reached"
PROBE = ["Reach each node over ssh", "Stop when a node of the rack is not reached over ssh",
         "Leave out a node outside the rack not reached over ssh", "Gather the network facts"]

# rack r2 of dc1 is started: n2 and n3; n1 in r1, n4 in r3
ADDRESSES = {"10.0.0.1": "n1", "10.0.0.2": "n2", "10.0.0.3": "n3", "10.0.0.4": "n4"}
RACKS = {"n1": "r1", "n2": "r2", "n3": "r2", "n4": "r3"}
HOSTVARS = dict((n, {"cassandra_dc": "dc1", "cassandra_rack": r}) for n, r in RACKS.items())
GROUPS = {"cassandra_target_rack_nodes": ["n2", "n3"]}
OK = dict(gossip={"is_up": True}, binary={"is_up": True}, netstats={"streaming": False}, schema={"agreed": True})
COMMAND_ARGS = {"inventory": ["/inv/hosts.yml"], "default_inventory": "/etc/ansible/hosts", "hosts": "", "cwd": "/inv"}


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def ring(racks=None, **states):
    """the ring as cassandra_status reads it, every node UN unless states says (n4="DN"); racks: the ring's rack of
    a node when it is not the inventory's"""
    racks = dict(RACKS, **(racks or {}))
    nodes = [{"address": a, "rack": racks[n], "status": states.get(n, "UN")[0], "state": states.get(n, "UN")[1]}
             for a, n in sorted(ADDRESSES.items())]
    return {"cluster_status": {"dc1": {"nodes": nodes}}}


def findings(read, peer=None, names=None, **ok):
    """one read from n2 (the rack), the second view from n1 (outside the rack: peer, else the same read)"""
    views = [{"from": "n2", "result": read}, {"from": "n1", "result": peer or read}]
    return cassandra_health_findings(views, 4, "n2", names=ADDRESSES if names is None else names, **dict(OK, **ok))


def members(groups=None, hostvars=None):
    """the rack's nodes as the playbook finds them: every node of the cluster by its variables, and the group"""
    template = TASKS[MEMBERS]["ansible.builtin.set_fact"]["_cassandra_start_rack_members"]
    template = template.replace("query('community.cassandra.cassandra_nodes')", "_nodes")
    return render(template, _nodes=sorted(RACKS), hostvars=HOSTVARS if hostvars is None else hostvars,
                  groups=GROUPS if groups is None else groups, cassandra_target_dc="dc1", cassandra_target_rack="r2")


def check(unreached=(), peer=None, then=None, names=None, groups=None, racks=None, **states):
    """the first read (peer: n1's ring, when it differs), the nodes outside the rack noted, the check again with
    them not judged (when the first read found a problem; then: the ring read then, else the same) -> (the
    problems that stop the run, the lines said or None)"""
    read = ring(racks, **states)
    first = findings(read, peer, names)
    variables = dict(groups=GROUPS if groups is None else groups, cassandra_health_findings=first,
                     cassandra_target_rack="r2", cassandra_target_dc="dc1", inventory_hostname="n2",
                     _cassandra_health_peer="n1", _cassandra_start_rack_unreached=list(unreached),
                     _cassandra_start_rack_members=members(groups),
                     _cassandra_start_rack_command_args=COMMAND_ARGS)
    outside = render(TASKS[NOTE]["ansible.builtin.set_fact"]["_cassandra_start_rack_outside"], **variables)
    variables["_cassandra_start_rack_outside"] = outside
    problems = []
    if cassandra_health_sentences(first):
        any_ok = render(TASKS[CHECK]["vars"]["cassandra_service_health_any_ok"], **variables)
        problems = cassandra_health_sentences(findings(then or read, then or peer, names, any_ok=any_ok))
    say = TASKS[SAY]
    if problems or not all(render("{{ %s }}" % w, ansible_check_mode=False, **variables) for w in say["when"][1:]):
        return problems, None
    return problems, render(say["ansible.builtin.debug"]["msg"], **variables)


def test_a_node_down_outside_the_rack_is_a_warning_not_a_failure():
    problems, said = check(n4="DN")
    assert problems == []
    assert said == [
        "Rack r2 (dc1): every node is UN.",
        "WARNING  outside the rack, not waited for:",
        "  n4  DN",
        "Check the cluster:",
        "  ansible-playbook -i hosts.yml community.cassandra.health_check"]
    assert all(len(line) <= 79 for line in said)


def test_any_state_outside_the_rack_one_row_each():
    # moving (UM) too, and any other up state: not judged outside the rack
    problems, said = check(n1="UL", n4="UM")
    assert problems == []
    assert said[2:4] == ["  n1  UL", "  n4  UM"]
    problems, said = check(n1="UJ", n4="DJ")
    assert problems == []
    assert said[2:4] == ["  n1  UJ", "  n4  DJ"]


def test_a_node_outside_the_rack_seen_in_two_states_is_not_judged_in_either():
    # DN from the node of the rack, UL from the other one: one row with both, and the check again passes
    problems, said = check(peer=ring(n4="UL"), n4="DN")
    assert problems == []
    assert said[2] == "  n4  DN/UL"


def test_a_node_outside_the_rack_seen_up_by_one_view_is_waited_for():
    # just started, n2 may still see a healthy peer DN for a few seconds; n1 (up for long) sees it UN: not said,
    # and waited for (a problem until it is seen UN)
    problems, said = check(peer=ring(), n4="DN")
    assert said is None and problems == ["n4 (10.0.0.4, r3) is DN, seen from n2"]
    problems, said = check(peer=ring(), then=ring(), n4="DN")
    assert (problems, said) == ([], None)


def test_a_node_outside_the_rack_not_reached_over_ssh():
    # its ring state when the read has one for it, else the probe's fact alone
    problems, said = check(unreached=["n4"])
    assert problems == []
    assert said[1:3] == ["WARNING  outside the rack, not waited for:", "  n4  not reached over ssh"]
    problems, said = check(unreached=["n4"], n4="DN", n1="UL")
    assert problems == []
    assert said[2:4] == ["  n1  UL", "  n4  DN  not reached over ssh"]


def test_a_node_of_the_rack_not_un_stops_the_run():
    # n4 is not waited for; n3 of the rack still stops the run, as before
    problems, said = check(n3="DN", n4="DN")
    assert problems == ["n3 (10.0.0.3, r2) is DN, seen from n1 and n2"]
    assert said is None
    for state in ("UJ", "UM", "UL"):
        problems, dummy = check(n2=state)
        assert problems and all(p.startswith("n2 ") for p in problems)


def test_a_node_no_host_is_known_by_is_one_of_the_rack():
    # n3 of the rack listens on an address no host has (another NIC): its finding has no name; it is still
    # judged as a node of the rack, here in another rack as the ring says, and stops the run
    names = dict((a, n) for a, n in ADDRESSES.items() if n != "n3")
    for racks in ({}, {"n3": "r9"}):
        problems, said = check(names=names, racks=racks, n3="DN")
        assert problems == ["10.0.0.3 (%s) is DN, seen from n1 and n2" % racks.get("n3", "r2")]
        assert said is None


def test_a_node_the_ring_puts_in_the_rack_is_one_of_the_rack():
    # n4 is in r3 for the inventory, in r2 for the ring: judged as a node of the rack
    problems, said = check(racks={"n4": "r2"}, n4="DN")
    assert problems == ["n4 (10.0.0.4, r2) is DN, seen from n1 and n2"]
    assert said is None


def test_under_limit_a_node_of_the_rack_left_out_is_still_one_of_the_rack():
    # --limit n2: the group has n2 alone; n3, of the rack in the inventory, still stopped, stops the run
    assert members(groups={"cassandra_target_rack_nodes": ["n2"]}) == ["n2", "n3"]
    problems, said = check(groups={"cassandra_target_rack_nodes": ["n2"]}, n3="DN")
    assert problems == ["n3 (10.0.0.3, r2) is DN, seen from n1 and n2"]
    assert said is None
    # the cassandra_config defaults for a node that does not set them
    assert members(hostvars={"n1": {}, "n2": {"cassandra_rack": "r2"}}, groups={}) == ["n2"]
    assert render(TASKS[MEMBERS]["ansible.builtin.set_fact"]["_cassandra_start_rack_members"].replace(
        "query('community.cassandra.cassandra_nodes')", "_nodes"), _nodes=["n1", "n2"], hostvars={"n1": {}, "n2": {}},
        groups={}, cassandra_target_dc="dc1", cassandra_target_rack="rack1") == ["n1", "n2"]
    assert TASKS[MEMBERS]["run_once"] is True and NAMES.index(MEMBERS) < NAMES.index(PROBE[0])


def test_a_healthy_cluster_reads_once_and_says_nothing():
    assert check() == ([], None)


def test_the_nodes_read_from():
    # the first read's second view: a node outside the rack (up for long), not one not reached
    assert render(TASKS[READ]["vars"]["_cassandra_health_not_peers"], _cassandra_start_rack_members=["n2", "n3"],
                  _cassandra_start_rack_unreached=["n5"]) == "n2:n3:n5"
    # the check again: not from a node outside the rack noted, nor one not reached, nor one whose nodetool failed
    read = ring(n1="DN", n4="UL")
    first = findings(read) + [{"kind": "nodetool", "on": "n6", "error": "x", "text": "x"}]
    outside = render(TASKS[NOTE]["ansible.builtin.set_fact"]["_cassandra_start_rack_outside"],
                     cassandra_health_findings=first, _cassandra_start_rack_members=["n2", "n3"],
                     cassandra_target_dc="dc1", cassandra_target_rack="r2", inventory_hostname="n2",
                     _cassandra_health_peer="n1")
    assert render(TASKS[CHECK]["vars"]["_cassandra_health_not_peers"], _cassandra_start_rack_outside=outside,
                  cassandra_health_findings=first, _cassandra_start_rack_unreached=["n5", "n4"]) == "n1:n4:n5:n6"


def test_checked_from_a_node_of_the_rack_and_not_under_check():
    checker = PLAY["vars"]["_cassandra_start_rack_checker"]
    assert render(checker, ansible_play_hosts=["n1", "n2", "n3", "n4"], _cassandra_start_rack_members=["n2", "n3"]) == "n2"
    # no node of the rack in the play (--limit): the first of the play, as before
    assert render(checker, ansible_play_hosts=["n1", "n4"], _cassandra_start_rack_members=["n2", "n3"]) == "n1"
    assert NAMES.index(READ) < NAMES.index(NOTE) < NAMES.index(CHECK) < NAMES.index(SAY)
    for name in (READ, NOTE, CHECK, SAY):
        assert "not ansible_check_mode" in TASKS[name]["when"]
    assert TASKS[READ]["vars"]["cassandra_service_health_report_only"] is True
    assert "cassandra_service_health_report_only" not in TASKS[CHECK]["vars"]
    # the facts gathered after the probe, before any read; the probe under --check too, without root
    assert PLAY["gather_facts"] is False
    assert [NAMES.index(n) for n in PROBE] == sorted(NAMES.index(n) for n in PROBE) and NAMES.index(PROBE[-1]) < NAMES.index(READ)
    for name in PROBE:
        assert "ansible_check_mode" not in str(TASKS[name].get("when", "")), name
    assert TASKS[PROBE[0]]["become"] is False and TASKS[PROBE[-1]]["become"] is False


# the probe run by ansible-playbook: node1 (the rack) and node2 local, node3 an ssh port nothing listens on
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
INVENTORY = """
all:
  children:
    cluster1:
      hosts:
        node1: {ansible_connection: local}
        node2: {ansible_connection: local}
        node3: {ansible_connection: ssh, ansible_host: 127.0.0.1, ansible_port: 1, ansible_user: nobody}
    cassandra_target_rack_nodes:
      hosts:
        %s:
"""


def probe(tmp_path, rack, check=False):
    tasks = [t for t in PLAY["tasks"] if t["name"] in [MEMBERS] + PROBE] + [
        {"name": "Say", "ansible.builtin.debug": {"msg": "unreached={{ _cassandra_start_rack_unreached }}"
                                                         " checker={{ _cassandra_start_rack_checker }}"},
         "run_once": True}]
    play = dict(PLAY, hosts="all", become=False, tasks=tasks)
    (tmp_path / "play.yml").write_text(yaml.safe_dump([play]))
    (tmp_path / "hosts.yml").write_text(INVENTORY % rack)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0", PWD=str(tmp_path),
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
               ANSIBLE_SSH_ARGS="-o ConnectTimeout=5 -o BatchMode=yes")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.yml"),
            "-e", "ansible_python_interpreter=" + sys.executable, "-e", "cassandra_hosts=cluster1", "-e", "cassandra_target_dc=dc1",
            "-e", "cassandra_target_rack=r2", str(tmp_path / "play.yml")] + (["--check"] if check else [])
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False,
                          cwd=str(tmp_path))


def test_a_host_outside_the_rack_not_reached_leaves_the_run_a_success(tmp_path):
    for check in (False, True):  # (--check: the same probe, the same outcome)
        run = probe(tmp_path, "node1", check)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "unreached=['node3'] checker=node1" in run.stdout
        assert "unreachable=0" in run.stdout.replace(" ", "")
        assert "ignored=1" in run.stdout.replace(" ", "")


def test_a_host_of_the_rack_not_reached_stops_the_run(tmp_path):
    for check in (False, True):
        run = probe(tmp_path, "node3", check)
        assert run.returncode != 0
        assert "node3 (rack r2) is not reached over ssh" in run.stdout
        assert "ansible -i hosts.yml node3 -m ping -vvv" in run.stdout  # (the run's inventory)
        assert "WAY OUT  reach it, then run start_rack again" in run.stdout
        assert "unreached=" not in run.stdout  # any_errors_fatal: nothing after it
