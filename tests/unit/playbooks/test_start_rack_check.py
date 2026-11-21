from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# start_rack's check: the rack is checked from one of its nodes and must be UN;
# a node outside the rack not UN (down, joining, leaving, moving...) or not
# reached over ssh is said in a warning, not waited for, and does not stop the
# run. A node that may be of the rack (the inventory, the ring, or no host known
# by its address, unless every node of the rack is in the ring) is judged as one.
# The expressions are read from the playbook, rendered by Ansible, with the
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
from ansible_collections.community.cassandra.plugins.filter.cassandra_start_rack import cassandra_rack_verdict
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

load_collection_filters()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "start_rack.yml"), encoding="utf-8") as f:
    PLAY = next(p for p in yaml.safe_load(f) if p.get("name") == "Check the cluster")
TASKS = dict((t["name"], t) for t in PLAY["tasks"])
NAMES = [t["name"] for t in PLAY["tasks"]]

MEMBERS = "Find the rack's nodes in the inventory"
READ = "Read the cluster once from a node of the rack"
NOTE = "Judge the first read"
CHECK = "Check the rack's nodes and the rest of the cluster"
SAY = "Say the rack's nodes are UN, and the nodes outside the run not UN or not reached"
AFTER = "Judge the last read"
STOP = "Stop on the rack's nodes not UN"
PROBE = ["Reach each node over ssh", "Stop when a node of the rack is not reached over ssh",
         "Leave out a node outside the rack not reached over ssh", "Gather the network facts"]

# rack r2 of dc1 is started: n2 and n3; n1 in r1, n4 in r3
ADDRESSES = {"10.0.0.1": "n1", "10.0.0.2": "n2", "10.0.0.3": "n3", "10.0.0.4": "n4"}
RACKS = {"n1": "r1", "n2": "r2", "n3": "r2", "n4": "r3"}
HOSTVARS = dict((n, {"cassandra_dc": "dc1", "cassandra_rack": r}) for n, r in RACKS.items())
GROUPS = {"cassandra_target_rack_nodes": ["n2", "n3"]}
OK = dict(gossip={"is_up": True}, binary={"is_up": True}, netstats={"streaming": False}, schema={"agreed": True})
COMMAND_ARGS = {"inventory": ["/inv/hosts.yml"], "default_inventory": "/etc/ansible/hosts", "hosts": "", "cwd": "/inv"}


def load_tasks(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def with_addresses(names):
    """the hostvars of the nodes, each with the address names gives it (its ansible_host)"""
    hostvars = dict((n, dict(v)) for n, v in HOSTVARS.items())
    for address, name in names.items():
        hostvars[name].setdefault("ansible_host", address)
    return hostvars


def noted(read, peer=None, names=None, unreached=(), groups=None, hostvars=None, schema=None, play=None):
    """the variables after the first read (peer: n1's result, when it differs) and its judgment; play: the hosts of
    the play (--limit), every node by default"""
    names = ADDRESSES if names is None else names
    variables = dict(groups=GROUPS if groups is None else groups, cassandra_health_findings=findings(read, peer, names),
                     cassandra_target_rack="r2", cassandra_target_dc="dc1", inventory_hostname="n2",
                     _cassandra_health_peer_host="n1", _cassandra_start_rack_checker="n2",
                     _cassandra_start_rack_unreached=list(unreached),
                     _cassandra_start_rack_members=members(groups), _cassandra_start_rack_not_probed=[],
                     _cassandra_start_rack_command_args=COMMAND_ARGS,
                     hostvars=with_addresses(names) if hostvars is None else hostvars,
                     cassandra_health_self=read, cassandra_health_peer=peer or read, cassandra_health_schema=schema or {},
                     ansible_play_hosts_all=sorted(RACKS) if play is None else play)
    variables["_cassandra_start_rack_left_out"] = render(PLAY["vars"]["_cassandra_start_rack_left_out"], **variables)
    set_facts(NOTE, variables)
    return variables


def set_facts(name, variables):
    """the facts a set_fact task of the play sets, the play's filter arguments rendered with the last read"""
    variables["_cassandra_start_rack_filter_args"] = dict(
        (k, render(v.replace("query('community.cassandra.cassandra_nodes')", "_nodes"), _nodes=sorted(RACKS), **variables))
        for k, v in PLAY["vars"]["_cassandra_start_rack_filter_args"].items())
    for fact, template in TASKS[name]["ansible.builtin.set_fact"].items():
        variables[fact] = render(template, **variables)


def check_var(name, variables):
    """a variable of the check again"""
    return render(TASKS[CHECK]["vars"][name], **variables)


def check(unreached=(), peer=None, then=None, then_peer=None, names=None, groups=None, racks=None, hostvars=None,
          schema=None, play=None, **states):
    """the first read (peer: n1's result, when it differs), judged, the check again with the nodes outside the rack
    noted not judged (when the first read found a problem; then: the ring read then, else the same; then_peer:
    n1's then, else the same), the last read judged -> (the problems that stop the run, then the lines that say
    why (the stop's), the lines said or None)"""
    read = ring(racks, **states)
    variables = noted(read, peer, names, unreached, groups, hostvars, schema, play)
    if cassandra_health_sentences(variables["cassandra_health_findings"]):
        # the check again (its findings once the wait ends)
        any_ok = check_var("cassandra_service_health_any_ok", variables)
        second, second_peer = then or read, then_peer or then or peer or read
        variables.update(cassandra_health_findings=findings(second, second_peer, names, any_ok=any_ok),
                         cassandra_health_self=second, cassandra_health_peer=second_peer)
    assert all(render("{{ %s }}" % w, ansible_check_mode=False, **variables) for w in TASKS[AFTER]["when"])
    set_facts(AFTER, variables)
    verdict = variables["_cassandra_start_rack_last_verdict"]
    stop = TASKS[STOP]
    if all(render("{{ %s }}" % w, ansible_check_mode=False, **variables) for w in stop["when"]):
        return render(stop["vars"]["_cassandra_health_stop_on"], **variables) + render(
            stop["vars"]["_cassandra_health_why"], **variables), None
    assert verdict["problems"] == []
    say = TASKS[SAY]
    variables.update((k, render(v, **variables)) for k, v in say["vars"].items() if k.startswith("_"))
    if not all(render("{{ %s }}" % w, ansible_check_mode=False, **variables) for w in say["when"]):
        return [], None
    return [], render(say["ansible.builtin.debug"]["msg"], **variables)


UP = ["Rack r2 (dc1): the nodes of this run are UN (n2)."]
LEFT_OUT = ["WARNING  rack r2 (dc1), not UN, left out by --limit (not in this run):"]
START = ["start n3: run start_rack on it alone",
         "  ansible-playbook -i hosts.yml community.cassandra.start_rack \\",
         "    -e cassandra_target_dc=dc1 -e cassandra_target_rack=r2 --limit n3"]


def test_a_node_of_the_rack_left_out_by_limit_is_a_warning_not_waited_for():
    # S26 d: start_rack --limit n2 while n3 (same rack) is down: n3 was waited for 10 minutes (this run never
    # starts it), named by its address, and the way out said "run start_rack again" (the same --limit). Then a
    # stop on it, exit 2, although n2, the node of the run, was UN. Now (owner rule: the run succeeds when its
    # own nodes are UN): not waited for, the run's nodes said UN, n3 a WARNING by its name, with the command
    # that starts it (start_rack without --limit)
    variables = noted(ring(n3="DN"), play=["n1", "n2", "n4"])
    first = variables["_cassandra_start_rack_first_verdict"]
    assert [n["name"] for n in first["left_out"]] == ["n3"] and first["problems"] == []
    assert "10.0.0.3" in check_var("cassandra_service_health_any_ok", variables)
    problems, said = check(play=["n1", "n2", "n4"], n3="DN")
    assert problems == [] and said == UP + LEFT_OUT + ["  n3  DN"] + START
    assert all(len(line) <= 79 for line in said)
    assert NAMES.index(STOP) < NAMES.index(SAY) and "Stop on the rack's nodes left out by --limit not UN" not in NAMES
    # n3 UN: nothing to warn of, the nodes of the run said UN
    assert check(play=["n1", "n2", "n4"]) == ([], UP)
    # a node of the rack in the run not UN: its stop, as without --limit (n3 left out of it, not waited for)
    problems, said = check(play=["n1", "n2", "n4"], n2="UJ", n3="DN")
    assert problems and all(p.startswith("n2 ") for p in problems) and said is None
    # no --limit: n3 is waited for and stops the run as before
    assert check(n3="DN")[0] == ["n3 (10.0.0.3, r2) is DN, seen from n1 and n2"]


def test_a_node_left_out_by_limit_is_said_in_its_state_of_the_last_read():
    # DN at the first read, UN after the wait (a colleague started it): nothing said of it
    play = ["n1", "n2", "n4"]
    assert check(play=play, then=ring(n2="UN"), n2="DN", n3="DN") == ([], UP)
    # DN at first, UJ after the wait: said UJ, no "start it"
    problems, said = check(play=play, then=ring(n3="UJ"), n2="DN", n3="DN")
    assert said == UP + LEFT_OUT + ["  n3  UJ", "n3 is joining (UJ): wait for it to finish"]


def test_a_node_left_out_by_limit_running_is_not_told_to_start():
    # UJ, UL, UM: the node runs, a start does nothing; its state said, wait for it to finish
    for state, doing in (("UJ", "joining"), ("UL", "leaving"), ("UM", "moving")):
        problems, said = check(play=["n1", "n2", "n4"], n3=state)
        assert problems == [] and said == UP + LEFT_OUT + ["  n3  " + state, "n3 is %s (%s): wait for it to finish" % (
            doing, state)]


def test_the_whole_rack_left_out_by_limit_is_not_said_started():
    # --limit n1,n4: no node of r2 in the run, nothing started; the line said so, not "every node is UN"
    problems, said = check(play=["n1", "n4"], groups={"cassandra_target_rack_nodes": []})
    assert problems == [] and said == ["Rack r2 (dc1): nothing started, --limit leaves out all of its nodes."]
    problems, said = check(play=["n1", "n4"], groups={"cassandra_target_rack_nodes": []}, n3="DN")
    assert said[0] == "Rack r2 (dc1): nothing started, --limit leaves out all of its nodes."
    assert said[1:] == LEFT_OUT + ["  n3  DN"] + START


def test_under_force_the_nodes_left_out_are_said_too():
    # cassandra_service_health_force: the rack's problems go on as a warning (cluster_health.yml); the nodes left
    # out by --limit said as without it (never a stop of their own)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_start_rack import cassandra_rack_outside_lines
    verdict = {"outside": [], "failed": [], "warnings": [], "problems": ["n2 (10.0.0.2, r2) is DN, seen from n1"],
               "left_out": [{"address": "10.0.0.3", "name": "n3", "states": ["DN"]}]}
    assert cassandra_rack_outside_lines(verdict, [], "dc1", "r2", "x", run=["n2"], left_out=["n3"],
                                        start_command="y") == LEFT_OUT + ["  n3  DN", START[0], "  y --limit n3"]


def test_a_node_left_out_by_limit_known_by_no_address_is_named_as_the_controller_resolves_it(monkeypatch):
    # out of the play, n3 has no facts and the inventory no address for it (ansible_host not set): its name
    # resolved on the controller (cassandra_ring's hosts_by_address)
    from ansible_collections.community.cassandra.plugins.filter import cassandra_ring
    names = dict((a, n) for a, n in ADDRESSES.items() if n != "n3")
    monkeypatch.setattr(cassandra_ring, "_resolved", lambda name: ["10.0.0.3"] if name == "n3" else [])
    problems, said = check(names=names, play=["n1", "n2", "n4"], n3="DN")
    assert said == UP + LEFT_OUT + ["  n3  DN"] + START
    # not resolved: its address, the nodes left out named under it; no "no inventory host has this address" lines
    monkeypatch.setattr(cassandra_ring, "_resolved", lambda name: [])
    problems, said = check(names=names, play=["n1", "n2", "n4"], n3="DN")
    assert said == UP + LEFT_OUT + ["  10.0.0.3  DN", "  (no host known by that address; left out by --limit: n3)",
                                    "start it: run start_rack on the nodes left out named above"] + START[1:]
    variables = noted(ring(n3="DN"), names=names, play=["n1", "n2", "n4"])
    assert variables["_cassandra_start_rack_first_verdict"]["unknown_lines"] == []
    # a node no host is known by that the ring puts in another rack: not taken for the node left out
    names = dict((a, n) for a, n in ADDRESSES.items() if n not in ("n3", "n4"))
    variables = noted(ring(n3="DN", n4="DN"), names=names, play=["n1", "n2"])
    assert [n["address"] for n in variables["_cassandra_start_rack_first_verdict"]["left_out"]] == ["10.0.0.3"]


def test_a_ghost_entry_of_the_rack_is_not_taken_for_the_node_left_out():
    # a stale DN entry in r2 no host owns (10.0.0.9: a failed replace), n3 left out, UN, known by its address:
    # the ghost was said "left out by --limit" (start them), the ring size stop gone. Now judged as without
    # --limit: a node of the rack DN, and the ring's size a stop
    read = ring()
    read["cluster_status"]["dc1"]["nodes"].append({"address": "10.0.0.9", "rack": "r2", "status": "D", "state": "N"})
    variables = noted(read, play=["n1", "n2", "n4"])
    verdict = variables["_cassandra_start_rack_first_verdict"]
    assert verdict["left_out"] == []
    assert verdict["problems"][0] == "10.0.0.9 (r2) is DN, seen from n1 and n2"
    assert verdict["problems"][1].startswith("the ring has 5 nodes, the inventory 4 (seen from n2)")


def test_a_node_down_outside_the_rack_is_a_warning_not_a_failure():
    problems, said = check(n4="DN")
    assert problems == []
    assert said == [
        "Rack r2 (dc1): every node is UN (n2, n3).",
        "WARNING  the rest of the cluster:",
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
    # just started, n2 may still see a healthy peer DN for a few seconds; n1 (up for long) sees it UN: not noted,
    # waited for (judged in the check again); seen UN then: nothing said
    assert noted(ring(n4="DN"), peer=ring())["_cassandra_start_rack_first_verdict"]["outside"] == []
    problems, said = check(peer=ring(), then=ring(), n4="DN")
    assert (problems, said) == ([], ["Rack r2 (dc1): every node is UN (n2, n3)."])


def test_a_node_outside_the_rack_not_reached_over_ssh():
    # its ring state when the read has one for it, else the probe's fact alone
    problems, said = check(unreached=["n4"])
    assert problems == []
    assert said[1:3] == ["WARNING  the rest of the cluster:", "  n4  not reached over ssh"]
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


WHY = ["10.0.0.%s (dc1, %s): no inventory host has this address,", "  so it is judged as a node of rack r2",
       "if it is outside rack r2: give its inventory host that address",
       "  (broadcast_address in cassandra_extra_settings, else",
       "  cassandra_listen_address or ansible_host), then run start_rack again"]


def test_a_node_no_host_is_known_by_is_one_of_the_rack():
    # n3 of the rack listens on an address no host has (another NIC): its finding has no name; it is still
    # judged as a node of the rack, here in another rack as the ring says, and stops the run, saying why
    names = dict((a, n) for a, n in ADDRESSES.items() if n != "n3")
    problems, said = check(names=names, n3="DN")
    assert (problems, said) == (["10.0.0.3 (r2) is DN, seen from n1 and n2"], None)
    problems, said = check(names=names, racks={"n3": "r9"}, n3="DN")
    assert problems == ["10.0.0.3 (r9) is DN, seen from n1 and n2", WHY[0] % (3, "r9")] + WHY[1:]
    assert said is None and all(len(line) <= 79 for line in problems)
    # said before the wait too
    variables = noted(ring({"n3": "r9"}, n3="DN"), names=names)
    say = TASKS["Say the nodes judged as the rack's for want of a host known by their address"]
    assert render(say["ansible.builtin.debug"]["msg"], **variables) == [WHY[0] % (3, "r9")] + WHY[1:]
    assert all(render("{{ %s }}" % w, ansible_check_mode=False, **variables) for w in say["when"][1:])
    assert NAMES.index(NOTE) < NAMES.index(say["name"]) < NAMES.index(CHECK)
    assert say["vars"]["cassandra_output"] is True
    # n3 now gives its address: n3 found, the rest as before
    assert check(racks={"n3": "r9"}, n3="DN")[0] == ["n3 (10.0.0.3, r9) is DN, seen from n1 and n2"]


def test_a_node_outside_the_rack_no_host_is_known_by_is_a_warning_when_every_rack_node_is_in_the_ring():
    # n4 of r3 is down and not reached over ssh, its address from its facts (none): its finding has no name; the
    # ring puts it in r3 and n2, n3 (the rack) are in the ring at their addresses: surely outside, a warning
    names = dict((a, n) for a, n in ADDRESSES.items() if n != "n4")
    problems, said = check(names=names, unreached=["n4"], n4="DN")
    assert problems == []
    assert said[1:4] == ["WARNING  the rest of the cluster:", "  10.0.0.4  DN",
                         "  n4        not reached over ssh"]
    # a node of the rack not in the ring at an address it gives (n3: none known): 10.0.0.4 may be n3, judged
    # as one of the rack, why said
    names = dict((a, n) for a, n in ADDRESSES.items() if n not in ("n3", "n4"))
    problems, said = check(names=names, unreached=["n4"], n4="DN")
    assert problems == ["10.0.0.4 (r3) is DN, seen from n1 and n2", WHY[0] % (4, "r3")] + WHY[1:]
    assert said is None


def test_a_node_at_an_address_a_node_of_the_rack_gives_is_one_of_the_rack():
    # n3 (the rack) is in r9 for the ring and DN; n4 (outside) gives n3's address too (a stale ansible_host),
    # first: the finding is named n4; n3 gives that address: judged as a node of the rack, it stops the run
    names = dict(ADDRESSES, **{"10.0.0.3": "n4"})
    hostvars = with_addresses(ADDRESSES)
    problems, said = check(names=names, racks={"n3": "r9"}, hostvars=hostvars, n3="DN")
    assert problems == ["n4 (10.0.0.3, r9) is DN, seen from n1 and n2"]
    assert said is None


def test_a_node_outside_the_rack_the_settled_view_sees_not_un_is_a_warning():
    # n1 (outside the rack, up for long) sees n4 DN, n2 (just started) sees it UN: a partition between n1 and
    # n4, outside the rack: a warning, not waited for
    problems, said = check(peer=ring(n4="DN"))
    assert problems == []
    assert said[1:3] == ["WARNING  the rest of the cluster:", "  n4  DN"]


def test_an_outside_node_still_not_un_after_the_wait_is_a_warning():
    # n1 sees n4 UN, n2 (just started) DN: waited for; after the wait both see it DN (it went down meanwhile):
    # a warning row, not a stop
    problems, said = check(peer=ring(), then=ring(n4="DN"), n4="DN")
    assert problems == []
    assert said[1:3] == ["WARNING  the rest of the cluster:", "  n4  DN"]
    # n1 joins outside the rack during the wait: its row too, with n4's
    problems, said = check(peer=ring(), then=ring(n1="UJ", n4="DN"), n4="DN")
    assert problems == [] and said[2:4] == ["  n1  UJ", "  n4  DN"]
    # a node of the rack still not UN after the wait: the stop, its rows only
    problems, said = check(peer=ring(), then=ring(n3="DN", n4="DN"), n4="DN")
    assert (problems, said) == (["n3 (10.0.0.3, r2) is DN, seen from n1 and n2"], None)


def test_after_the_wait_a_nodetool_failing_outside_the_rack_is_a_warning_unless_no_ring_was_read():
    failing = [{"kind": "nodetool", "on": "n1", "error": "x", "text": "nodetool status failed on n1: x"}]
    args = dict(members=["n2", "n3"], dc="dc1", rack="r2", checker="n2", peer="n1",
                addresses=dict((a, n) for a, n in ADDRESSES.items()), first_verdict={})
    verdict = cassandra_rack_verdict(failing, reads=[ring()], **args)
    assert (verdict["problems"], verdict["failed"]) == ([], ["n1"])
    # no ring read at all: nothing judged, the failure stops the run
    verdict = cassandra_rack_verdict(failing, reads=[{"msg": "x"}], **args)
    assert (verdict["problems"], verdict["failed"]) == (["nodetool status failed on n1: x"], [])
    # a node of the rack whose nodetool fails: a stop
    on_n3 = [dict(failing[0], on="n3", text="nodetool status failed on n3: x")]
    assert cassandra_rack_verdict(on_n3, reads=[ring()], **args)["problems"] == ["nodetool status failed on n3: x"]


def test_only_after_the_wait_the_peer_failing_outside_the_rack_is_said():
    # n2 alone sees n4 DN: waited for; then n2 sees it UN, n1's nodetool fails: no stop, n1's row alone
    failed = {"msg": "nodetool gets no answer from JMX on 127.0.0.1 (connection refused): x"}
    problems, said = check(peer=ring(), then=ring(), then_peer=failed, n4="DN")
    assert problems == []
    assert said[1:3] == ["WARNING  the rest of the cluster:", "  n1  nodetool status failed"]


def test_after_the_wait_a_node_outside_the_rack_seen_not_un_by_one_view_is_said():
    # n2 alone sees n4 DN before and after the wait (n1 sees it UN): not a stop, and not lost: its row
    problems, said = check(peer=ring(), then=ring(n4="DN"), then_peer=ring(), n4="DN")
    assert problems == []
    assert said[1:3] == ["WARNING  the rest of the cluster:", "  n4  DN"]


def test_a_node_noted_before_the_wait_keeps_its_row_after_it():
    # n1 sees n4 DN (noted, not judged again); n2 alone sees n1 DN (waited for), then both do: both rows, n4 first
    problems, said = check(peer=ring(n4="DN"), then=ring(n1="DN"), n1="DN", n4="DN")
    assert problems == []
    assert said[2:4] == ["  n4  DN", "  n1  DN"]


def test_a_reader_whose_nodetool_failed_does_not_count():
    # n1's nodetool status fails: n2's view alone; n4 seen DN by it is outside the rack: a warning, n1's failure
    # said in it; the check again does not read from n1
    failed = {"msg": "nodetool gets no answer from JMX on 127.0.0.1 (connection refused): x"}
    problems, said = check(peer=failed, then=ring(n4="DN"), n4="DN")
    assert problems == []
    assert said[1:4] == ["WARNING  the rest of the cluster:", "  n4  DN",
                         "  n1  nodetool status failed"]
    variables = noted(ring(n4="DN"), peer=failed)
    assert check_var("_cassandra_health_not_peers", variables) == "n4,n1"
    # n4 seen DN by n2 alone counts, n1's read failed: noted (not waited for)
    assert [n["name"] for n in variables["_cassandra_start_rack_first_verdict"]["outside"]] == ["n4"]


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


def test_a_healthy_cluster_reads_once_and_says_the_rack_is_up():
    # its success said (it was not: only each node's start line), the nodes of the rack as the ring has them UN;
    # nothing outside the rack: no warning, no command
    assert check() == ([], ["Rack r2 (dc1): every node is UN (n2, n3)."])
    # a node of the rack by its address when no host is known by it
    names = dict((a, n) for a, n in ADDRESSES.items() if n != "n3")
    assert check(names=names) == ([], ["Rack r2 (dc1): every node is UN (10.0.0.3, n2)."])


def test_the_nodes_read_from():
    # the first read's second view: a node outside the rack (up for long), not one not reached, nor one out of
    # the play (--limit: not probed)
    assert render(TASKS[READ]["vars"]["_cassandra_health_not_peers"], _cassandra_start_rack_members=["n2", "n3"],
                  _cassandra_start_rack_unreached=["n5"], _cassandra_start_rack_not_probed=["n6"]) == "n2,n3,n5,n6"
    not_probed = PLAY["vars"]["_cassandra_start_rack_not_probed"].replace(
        "query('community.cassandra.cassandra_nodes')", "_nodes")
    assert render(not_probed, _nodes=["n1", "n2", "n3"], ansible_play_hosts_all=["n2", "n3"]) == ["n1"]
    # the check again: from another node of the rack first; not from a node outside the rack noted, nor one seen
    # down, not reached, not probed, or whose nodetool failed
    variables = noted(ring(n1="DN", n3="DN", n4="UL"))
    variables["cassandra_health_findings"].append({"kind": "nodetool", "on": "n6", "error": "x", "text": "x"})
    set_facts(NOTE, variables)
    variables.update(_cassandra_start_rack_unreached=["n5", "n4"], _cassandra_start_rack_not_probed=["n7"])
    assert check_var("_cassandra_health_not_peers", variables) == "n1,n4,n3,n6,n5,n7"
    assert render(TASKS[CHECK]["vars"]["_cassandra_health_prefer_peers"], **variables) == ["n2", "n3"]


def test_host_names_that_are_ipv6_addresses_are_left_out_as_peers():
    # the patterns joined with "," (":" would split an IPv6 address)
    from ansible.inventory.manager import InventoryManager
    inventory = InventoryManager(loader=DataLoader(), sources="2001:db8::5,2001:db8::6,2001:db8::7,")
    pattern = render(TASKS[READ]["vars"]["_cassandra_health_not_peers"], _cassandra_start_rack_members=["2001:db8::5"],
                     _cassandra_start_rack_unreached=["2001:db8::6"], _cassandra_start_rack_not_probed=[])
    assert sorted(h.name for h in inventory.get_hosts(pattern)) == ["2001:db8::5", "2001:db8::6"]


def test_a_node_outside_the_rack_accepted_is_not_counted_down():
    # cassandra_status fails past its count of nodes allowed down: the nodes accepted in any state count in it
    read = load_tasks("roles", "cassandra_operation", "tasks", "cluster_health_read.yml")[0]["block"]
    for name in ("Read the ring from this node", "Read the ring from {{ _cassandra_health_peer_host }}"):
        task = next(t for t in read if t["name"] == name)
        down = task["community.cassandra.cassandra_status"]["down"]
        # (an int; text before ansible-core 2.19)
        assert int(render(down, _down_ok=["10.0.0.9"], cassandra_service_health_any_ok=["10.0.0.4", "10.0.0.5"])) == 3
        assert int(render(down, _down_ok=[])) == 0


def test_the_defaults_are_the_cassandra_config_role_s():
    from ansible_collections.community.cassandra.plugins.filter import cassandra_start_rack
    defaults = load_tasks("roles", "cassandra_config", "defaults", "main.yml")
    assert (cassandra_start_rack.DEFAULT_DC, cassandra_start_rack.DEFAULT_RACK) == (
        defaults["cassandra_dc"], defaults["cassandra_rack"])


def test_checked_from_a_node_of_the_rack_and_not_under_check():
    checker = PLAY["vars"]["_cassandra_start_rack_checker"]
    assert render(checker, ansible_play_hosts=["n1", "n2", "n3", "n4"], _cassandra_start_rack_members=["n2", "n3"]) == "n2"
    # no node of the rack in the play (--limit): the first of the play, as before
    assert render(checker, ansible_play_hosts=["n1", "n4"], _cassandra_start_rack_members=["n2", "n3"]) == "n1"
    assert NAMES.index(READ) < NAMES.index(NOTE) < NAMES.index(CHECK) < NAMES.index(SAY)
    for name in (READ, NOTE, CHECK, AFTER, STOP, SAY):
        assert "not ansible_check_mode" in TASKS[name]["when"]
    assert TASKS[READ]["vars"]["cassandra_service_health_report_only"] is True
    # the check again waits, reports; the stop after it, on the rack's own problems (the role's stop alone)
    assert TASKS[CHECK]["vars"]["cassandra_service_health_report_only"] is True
    assert NAMES.index(CHECK) < NAMES.index(AFTER) < NAMES.index(STOP) < NAMES.index(SAY)
    assert TASKS[STOP]["vars"]["_cassandra_health_stop_on"] == "{{ _cassandra_start_rack_last_verdict.problems }}"
    assert TASKS[STOP]["vars"]["_cassandra_health_again"].replace(u"\xa0", " ") == "run start_rack again"
    assert TASKS[STOP]["ansible.builtin.include_role"]["tasks_from"] == "cluster_health.yml"
    assert "cassandra_service_health_report_only" not in TASKS[STOP]["vars"]
    # the facts gathered after the probe, before any read; the probe under --check too, without root
    assert PLAY["gather_facts"] is False
    assert [NAMES.index(n) for n in PROBE] == sorted(NAMES.index(n) for n in PROBE) and NAMES.index(PROBE[-1]) < NAMES.index(READ)
    for name in PROBE:
        assert "ansible_check_mode" not in str(TASKS[name].get("when", "")), name
    # (an inventory ansible_become beats the keyword: the variable too)
    for name in (PROBE[0], PROBE[-1]):
        assert TASKS[name]["become"] is False and TASKS[name]["vars"]["ansible_become"] is False


# the probe run by ansible-playbook: node1 (the rack) and node2 local, node3 an ssh port nothing listens on
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
INVENTORY = """
all:
  children:
    cluster1:
      hosts:
        node1: {ansible_connection: local}
        node2: {ansible_connection: local, ansible_become: true, ansible_become_exe: /bin/false}
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


def test_a_rack_node_given_by_an_ipv6_address_written_otherwise_is_found_in_the_ring():
    # nodetool prints IPv6 in full, the inventory compressed: n2, n3 are in the ring, so an unnamed node DN in r3
    # is surely outside (a warning), not judged of the rack for want of a host known by its address
    full = dict(("2001:db8:0:0:0:0:0:%s" % a[-1], n) for a, n in ADDRESSES.items())
    nodes = [{"address": a, "rack": RACKS[n], "status": "D" if n == "n4" else "U", "state": "N"}
             for a, n in sorted(full.items())]
    read = {"cluster_status": {"dc1": {"nodes": nodes}}}
    found = cassandra_health_findings([{"from": "n2", "result": read}, {"from": "n1", "result": read}], 4, "n2",
                                      names={}, **OK)
    verdict = cassandra_rack_verdict(found, ["n2", "n3"], "dc1", "r2", "n2", "n1", reads=[read], first_verdict={},
                                     addresses={"2001:db8::2": "n2", "2001:db8::3": "n3"})
    assert verdict["problems"] == [] and verdict["unknown_lines"] == []
    assert [n["address"] for n in verdict["outside"]] == ["2001:db8:0:0:0:0:0:4"]


def test_the_ring_size_is_a_warning_when_the_rack_is_in_the_ring_and_up():
    # n4 (r3) left the ring, still in the inventory: the rack is in the ring and UN, a warning row
    read = ring()
    read["cluster_status"]["dc1"]["nodes"] = [n for n in read["cluster_status"]["dc1"]["nodes"] if n["address"] != "10.0.0.4"]
    variables = noted(read)
    assert variables["_cassandra_start_rack_first_verdict"]["problems"] == []
    assert variables["_cassandra_start_rack_first_verdict"]["ring_size"] == [[3, 4]]
    problems, said = check_read(read)
    assert problems == []
    assert said[1:3] == ["WARNING  the ring has 3 nodes, the inventory 4", "Check the cluster:"]
    # with a node outside the rack down too: each under its own heading
    problems, said = check_read(ring(n1="DN"), count=3)
    assert said[1:4] == ["WARNING  the rest of the cluster:", "  n1  DN", "WARNING  the ring has 4 nodes, the inventory 3"]
    # a node of the rack (n3) is the one missing: a stop
    read = ring()
    read["cluster_status"]["dc1"]["nodes"] = [n for n in read["cluster_status"]["dc1"]["nodes"] if n["address"] != "10.0.0.3"]
    problems, said = check_read(read)
    assert problems == ["the ring has 3 nodes, the inventory 4 (seen from n2): a node is missing from the ring, or the"
                        " inventory is incomplete", "the ring has 3 nodes, the inventory 4 (seen from n1): a node is"
                        " missing from the ring, or the inventory is incomplete"]
    # a node of the rack not UN too: the ring's size is not judged the rest of the cluster's either
    read = ring(n3="DN")
    read["cluster_status"]["dc1"]["nodes"] = [n for n in read["cluster_status"]["dc1"]["nodes"] if n["address"] != "10.0.0.4"]
    problems, dummy = check_read(read)
    assert problems[0] == "n3 (10.0.0.3, r2) is DN, seen from n1 and n2" and len(problems) == 3


SCHEMA = {"agreed": False, "msg": "The cluster has not reached consensus on the schema: 2 versions, ..."}


def test_a_schema_disagreement_of_nodes_outside_the_rack_is_a_warning():
    # 4.0+ prints each address with its port; n4 alone on another version: its row, no stop
    schema = dict(SCHEMA, schema_status={"aaaa": ["10.0.0.1:7000", "10.0.0.2:7000", "10.0.0.3:7000"],
                                         "bbbb": ["10.0.0.4:7000"]})
    problems, said = check_read(ring(), schema=schema, ok=dict(schema=schema))
    assert problems == []
    assert said[1:3] == ["WARNING  the rest of the cluster:", "  n4  on another schema version than the rack"]
    # n4 down too: one row; an address no host has: the address
    schema = dict(SCHEMA, schema_status={"aaaa": ["10.0.0.1", "10.0.0.2", "10.0.0.3"], "bbbb": ["10.0.0.4", "10.0.0.9"]})
    problems, said = check_read(ring(n4="DN"), schema=schema, ok=dict(schema=schema))
    assert said[1:4] == ["WARNING  the rest of the cluster:", "  n4        DN  on another schema version than the rack",
                         "  10.0.0.9  on another schema version than the rack"]
    # a node of the rack not UN: the schema is not judged the rest of the cluster's either
    problems, dummy = check_read(ring(n3="DN"), schema=schema, ok=dict(schema=schema))
    assert problems == ["n3 (10.0.0.3, r2) is DN, seen from n1 and n2", "schema disagreement: " + SCHEMA["msg"]]
    # a node of the rack on another version than the other: a stop
    schema = dict(SCHEMA, schema_status={"aaaa": ["10.0.0.1", "10.0.0.2", "10.0.0.4"], "bbbb": ["10.0.0.3"]})
    problems, dummy = check_read(ring(), schema=schema, ok=dict(schema=schema))
    assert problems == ["schema disagreement: " + SCHEMA["msg"]]
    # the rack alone on its version (the others' majority on another one, or the cluster split even): the rack
    # may be the one behind, a stop
    # the rack's version the majority by the rack's own nodes alone (n1 not listed: unreachable): a stop too
    for status in ({"aaaa": ["10.0.0.2", "10.0.0.3"], "bbbb": ["10.0.0.1", "10.0.0.4", "10.0.0.9"]},
                   {"aaaa": ["10.0.0.2", "10.0.0.3"], "bbbb": ["10.0.0.1", "10.0.0.4"]},
                   {"aaaa": ["10.0.0.2", "10.0.0.3"], "bbbb": ["10.0.0.4"]},
                   {"aaaa": ["10.0.0.1", "10.0.0.2", "10.0.0.3"], "bbbb": ["10.0.0.4", "10.0.0.8", "10.0.0.9"]},
                   {"aaaa": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                    "bbbb": ["10.0.0.4", "10.0.0.7", "10.0.0.8", "10.0.0.9"]}):
        schema = dict(SCHEMA, schema_status=status)
        assert check_read(ring(), schema=schema, ok=dict(schema=schema))[0] == ["schema disagreement: " + SCHEMA["msg"]]
    # a node of the rack not listed (UNREACHABLE for describecluster), none listed, or the last read failed: a stop
    for schema in (dict(SCHEMA, schema_status={"aaaa": ["10.0.0.1", "10.0.0.2"], "bbbb": ["10.0.0.4"]}),
                   dict(SCHEMA, schema_status={"aaaa": ["10.0.0.1"], "bbbb": ["10.0.0.4"]}),
                   dict(SCHEMA, rc=1, schema_status={"aaaa": ["10.0.0.1", "10.0.0.2", "10.0.0.3"], "bbbb": ["10.0.0.4"]})):
        assert check_read(ring(), schema=schema, ok=dict(schema=schema))[0] == ["schema disagreement: " + SCHEMA["msg"]]


def test_a_schema_disagreement_while_the_versions_are_mixed_is_a_warning_row():
    # cassandra_allow_mixed_versions: the disagreement is a warning finding, said as a row, not dropped
    schema = dict(SCHEMA, schema_status={"aaaa": ["10.0.0.2", "10.0.0.3"], "bbbb": ["10.0.0.1", "10.0.0.4"]})
    problems, said = check_read(ring(), schema=schema, ok=dict(schema=schema, mixed_versions=True, target="5.0.6"))
    assert problems == []
    assert said[1:3] == ["WARNING  the rest of the cluster:",
                         "  schema  versions differ: expected while the versions are mixed, agreed again"]
    assert said[3].startswith("  ") and "5.0.6" in " ".join(said[3:5])


def test_ccm_like_hosts_at_one_ansible_host_are_judged_by_their_own_address():
    # every ansible_host 127.0.0.1, each node listening on 127.0.0.<n>: n1 (r1, down) is outside the rack, not
    # taken for a node of the rack by n2's or n3's ansible_host
    ccm = dict((n, dict(v, ansible_host="127.0.0.1", cassandra_listen_address="127.0.0.%s" % n[1:]))
               for n, v in HOSTVARS.items())
    names = dict(("127.0.0.%s" % n[1:], n) for n in RACKS)
    nodes = [{"address": a, "rack": RACKS[n], "status": "D" if n == "n1" else "U", "state": "N"}
             for a, n in sorted(names.items())]
    verdict = noted({"cluster_status": {"dc1": {"nodes": nodes}}}, names=names, hostvars=ccm)[
        "_cassandra_start_rack_first_verdict"]
    assert verdict["problems"] == [] and [n["name"] for n in verdict["outside"]] == ["n1"]


def test_why_lines_are_from_the_last_read():
    # 10.0.0.4 (no host known by it, n3 not at a known address) is DN at first, UN after the wait; n3's own
    # problem (DN) stops the run: the stop says no why for 10.0.0.4
    names = dict((a, n) for a, n in ADDRESSES.items() if n not in ("n3", "n4"))
    problems, said = check(names=names, then=ring(n3="DN"), unreached=["n4"], n4="DN", n3="DN")
    assert problems == ["10.0.0.3 (r2) is DN, seen from n1 and n2"]
    # the other way round: UN at first, DN after the wait: said
    problems, said = check(names=names, then=ring(n4="DN"), unreached=["n4"], n3="DN")
    assert problems == ["10.0.0.4 (r3) is DN, seen from n1 and n2", WHY[0] % (4, "r3")] + WHY[1:]


def test_under_force_the_rack_is_not_said_up():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_start_rack import cassandra_rack_outside_lines
    verdict = {"outside": [{"address": "10.0.0.4", "name": "n4", "states": ["DN"]}], "failed": [], "warnings": [],
               "problems": ["n3 (10.0.0.3, r2) is DN, seen from n1"]}
    assert cassandra_rack_outside_lines(verdict, [], "dc1", "r2", "x")[:2] == [
        "WARNING  the rest of the cluster:", "  n4  DN"]


def check_read(read, schema=None, ok=None, count=4):
    """check() with a first read given (the same after the wait), a schema result, the inventory's node count"""
    variables = noted(read, schema=schema)
    variables["cassandra_health_findings"] = cassandra_health_findings(
        [{"from": "n2", "result": read}, {"from": "n1", "result": read}], count, "n2", names=ADDRESSES,
        **dict(OK, **(ok or {})))
    set_facts(NOTE, variables)
    set_facts(AFTER, variables)
    verdict = variables["_cassandra_start_rack_last_verdict"]
    if verdict["problems"]:
        return verdict["problems"], None
    say = TASKS[SAY]
    variables.update((k, render(v, **variables)) for k, v in say["vars"].items() if k.startswith("_"))
    if not all(render("{{ %s }}" % w, ansible_check_mode=False, **variables) for w in say["when"]):
        return [], None
    return [], render(say["ansible.builtin.debug"]["msg"], **variables)


def start_failed(failed, task):
    """the rescue of start_rack's start: its lines for the role's failed result, the name of its task that failed"""
    with open(os.path.join(TOP, "playbooks", "start_rack.yml"), encoding="utf-8") as f:
        start_play = next(p for p in yaml.safe_load(f) if p.get("name") == "Start the rack")
    start = next(t for t in start_play["tasks"] if t["name"] == "Start Cassandra and wait until the node has joined")
    assert start["vars"]["cassandra_output_rescued"] is True and start_play["any_errors_fatal"] is True
    assert start["block"][0]["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_service"
    stop = start["rescue"][0]
    assert stop["vars"]["cassandra_output"] is True
    variables = dict(inventory_hostname="n2", cassandra_target_dc="dc1", cassandra_target_rack="r2", ansible_limit="n2",
                     ansible_failed_result=failed, ansible_failed_task={"name": task}, _args=COMMAND_ARGS)
    variables["_again"] = render(stop["vars"]["_again"], **variables)
    return render(stop["ansible.builtin.fail"]["msg"], **variables)


AGAIN = ["  ansible-playbook -i hosts.yml community.cassandra.start_rack --limit n2 \\",
         "    -e cassandra_target_dc=dc1 -e cassandra_target_rack=r2"]


def role_task(path, name):
    """a task of the cassandra_service role, by its name (the rescue tells the failures apart by it)"""
    tasks = load_tasks("roles", "cassandra_service", "tasks", path)
    found = [t for t in tasks if t.get("name") == name]
    for block in (t.get("block", []) for t in tasks):
        found += [t for t in block if t.get("name") == name]
        for inner in (t.get("block", []) for t in block):
            found += [t for t in inner if t.get("name") == name]
    assert found, name
    return found[0]


def test_a_node_of_the_rack_that_does_not_start_is_said_with_a_way_out():
    # its unit failing (ExecStartPre=/bin/false): the run printed the role's task failure, "fatal: [n2]: FAILED!" and
    # systemd's message, no way out
    role_task("main.yml", "Start or restart cassandra")
    failed = {"msg": "Unable to start service cassandra: Job for cassandra.service failed because the control process"
                     " exited with error code.\nSee \"systemctl status cassandra.service\" for details."}
    lines = start_failed(failed, "Start or restart cassandra")
    assert lines[0] == "n2 (rack r2) did not start:"
    assert " ".join(x.strip() for x in lines[1:-6]) == (
        "Unable to start service cassandra: Job for cassandra.service failed because the control process exited"
        " with error code. See \"systemctl status cassandra.service\" for details.")
    # a command per line
    assert lines[-6:] == ["see why on it:", "  systemctl status cassandra", "  journalctl -u cassandra",
                          "WAY OUT  fix it, then run start_rack again:"] + AGAIN
    assert max(len(x) for x in lines) <= 79


def test_a_node_that_stops_while_it_joins_is_said_with_its_journal():
    # wait_normal_check.yml's stop: a list, its first line then the journal's last 40 lines. The rescue kept the
    # first line only, ending on "journalctl -u cassandra:" with nothing under it: the cause (here a rack change)
    # was gone from the screen
    task = role_task("wait_normal_check.yml", "Cassandra is not running")
    assert "stdout_lines" in task["ansible.builtin.fail"]["msg"]
    journal = ["Nov 21 10:00:01 h-n2 cassandra[811]: ERROR [main] 2026-11-21 10:00:01,042 CassandraDaemon.java:911"
               " - Cannot start node if snitch's rack (r9) differs from previous rack (r2).",
               "Nov 21 10:00:01 h-n2 systemd[1]: cassandra.service: Main process exited, code=exited, status=3/NOTIMPL"]
    failed = {"msg": ["cassandra on cassandra-prod-node-017 is failed (failed, exit-code): it stopped instead of"
                      " joining. journalctl -u cassandra:"] + journal}
    lines = start_failed(failed, "Cassandra is not running")
    assert lines[0] == "n2 (rack r2) did not start:"
    text = " ".join(x.strip() for x in lines)
    assert all(" ".join(line.split()) in text for line in journal)
    assert "cassandra-prod-node-017" in text and "exit-code" in text  # (never cut at a hyphen)
    assert all(x.startswith("    ") for x in lines[3:-6]) and lines[-3] == "WAY OUT  fix it, then run start_rack again:"
    assert max(len(x) for x in lines) <= 79


def test_a_node_not_normal_in_time_is_said_as_seen_not_did_not_start():
    # wait_normal.yml's timeout: the node runs (mode JOINING); "did not start" was false
    role_task("wait_normal.yml", "Give up after cassandra_service_wait_timeout")
    failed = {"msg": "n2 is not NORMAL after 600s (cassandra_service_wait_timeout): mode JOINING"}
    lines = start_failed(failed, "Give up after cassandra_service_wait_timeout")
    assert lines[:2] == ["n2 (rack r2) is not NORMAL after 600s (cassandra_service_wait_timeout): mode",
                         "  JOINING"]
    assert "did not start" not in " ".join(lines)
    assert lines[2:] == ["see on it:", "  nodetool netstats", "  journalctl -u cassandra",
                         "WAY OUT  once it is NORMAL (or fixed), run start_rack again:"] + AGAIN


def test_a_refusal_of_the_role_before_the_start_is_said_as_the_role_says_it():
    # an account missing, a new node listed in cassandra_seeds, the drain timeout check: each became "n2 (rack r2)
    # did not start:" its first line, "see why on it: systemctl status cassandra, journalctl -u cassandra" (nothing
    # there: Cassandra was never started) and "fix it": the role's own way out was lost. Now its message as it is
    # (within 79 columns)
    seed = role_task("main.yml", "Refuse to join an existing cluster as a seed")
    assert "cassandra_service_allow_new_seed: true" in seed["ansible.builtin.fail"]["msg"]
    refusals = [
        ("Refuse to join an existing cluster as a seed",
         "n2 is a new node listed in cassandra_seeds, but the cluster already exists (n1 answering). Seeds don't"
         " bootstrap, so it would join without its data: remove it from cassandra_seeds, start it, and make it a"
         " seed later if needed (cassandra_service_allow_new_seed: true overrides this check)."),
        ("Refuse an account that could not read the config", ["the user cassandra does not exist on n2", "  x"]),
        ("Leave time for the drain within the stop timeout",
         "cassandra_service_drain_timeout (120) + 10 must be below cassandra_service_timeout_stop (100), or systemd"
         " kills the JVM mid-drain.")]
    for task, msg in refusals:
        lines = start_failed({"msg": msg}, task)
        # every stop says a way out: its own when it has one, else fix what it says and run the same command again
        assert lines[-3:] == ["WAY OUT  fix what the message above says, then run start_rack again:"] + AGAIN
        assert " ".join(x.strip() for x in lines[:-3]) == " ".join(
            x.strip() for x in (msg if isinstance(msg, list) else [msg]))
        assert max(len(x) for x in lines) <= 79 and "did not start" not in " ".join(lines)
    # one with a WAY OUT of its own (a REFUSED block of the role): no second one
    own = ["REFUSED  start  n2  nothing was changed", "  the storage mode moves back", "  WAY OUT  set it back:",
           "    ansible-playbook x"]
    assert start_failed({"msg": own}, "Refuse a start into another storage_compatibility_mode") == own


def test_the_verdict_s_keys_are_the_ones_its_docstring_gives():
    # its "rack_up" key (the nodes seen UN) shared its name with a boolean of the same function; its example lacked
    # two keys
    verdict = noted(ring())["_cassandra_start_rack_first_verdict"]
    assert sorted(verdict) == ["failed", "left_out", "not_readers", "outside", "problems", "rack_seen_un", "ring_size",
                               "unknown_lines", "warnings"]
    example = cassandra_rack_verdict.__doc__.split("e.g.")[1]
    assert all(key + ":" in example for key in verdict) and verdict["rack_seen_un"] == ["n2", "n3"]


def test_each_node_left_out_down_gets_the_command_that_starts_it_alone():
    # "start them: run start_rack without --limit" started every node left out, one left out on purpose too (a
    # host waiting for replace_node); now a command per node down, --limit that node
    from ansible_collections.community.cassandra.plugins.filter.cassandra_start_rack import cassandra_rack_outside_lines
    verdict = {"outside": [], "failed": [], "warnings": [], "problems": [], "left_out": [
        {"address": "10.0.0.3", "name": "n3", "states": ["DN"]}, {"address": "10.0.0.5", "name": "n5", "states": ["UL"]},
        {"address": "10.0.0.6", "name": "n6", "states": ["DN"]}]}
    command = "ansible-playbook -i hosts.yml community.cassandra.start_rack -e cassandra_target_rack=r2"
    lines = cassandra_rack_outside_lines(verdict, [], "dc1", "r2", "x", run=["n2"], left_out=["n3", "n5", "n6", "n7"],
                                         start_command=command)
    assert lines[5:] == ["start n3: run start_rack on it alone",
                         "  ansible-playbook -i hosts.yml community.cassandra.start_rack \\",
                         "    -e cassandra_target_rack=r2 --limit n3",
                         "start n6: run start_rack on it alone",
                         "  ansible-playbook -i hosts.yml community.cassandra.start_rack \\",
                         "    -e cassandra_target_rack=r2 --limit n6",
                         "n5 is leaving (UL): wait for it to finish"]
    assert "without --limit" not in " ".join(lines) and max(len(x) for x in lines) <= 79


def test_under_limit_a_node_of_the_run_not_seen_un_in_the_ring_stops_the_run(monkeypatch):
    # n2 (in the run) and n3 (left out) known by no address in the ring; n2 DN there: its ring entry has no name,
    # so it was taken for n3 left out, a WARNING with the command starting n3, and the run said "the nodes of this
    # run are UN (n2)", exit 0. Now an unnamed entry is taken for a node left out only while every node of the run
    # is in the ring at an address it gives: n2's entry DN stops the run, as without --limit
    from ansible_collections.community.cassandra.plugins.filter import cassandra_ring
    monkeypatch.setattr(cassandra_ring, "_resolved", lambda name: [])
    names = dict((a, n) for a, n in ADDRESSES.items() if n not in ("n2", "n3"))
    problems, said = check(names=names, play=["n1", "n2", "n4"], n2="DN")
    assert said is None
    assert problems[:2] == ["10.0.0.2 (r2) is DN, seen from n1 and n2",
                            "n2 (r2) is not in the ring at an address it gives: not seen UN"]
    assert problems[2:] == ["give n2 the address the ring has it at (broadcast_address in",
                            "  cassandra_extra_settings, else cassandra_listen_address or ansible_host)"]
    # all UN, n2 still not at an address it gives: not seen UN, a stop (the success line says only what the
    # ring shows)
    problems, said = check(names=names, play=["n1", "n2", "n4"])
    assert said is None and "n2 (r2) is not in the ring at an address it gives: not seen UN" in problems
    # n2 at its address: the success line names it, as seen UN
    names = dict((a, n) for a, n in ADDRESSES.items() if n != "n3")
    assert check(names=names, play=["n1", "n2", "n4"]) == ([], UP)
