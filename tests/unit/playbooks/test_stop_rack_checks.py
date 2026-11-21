from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# stop_rack checks the rack by the ring's names and addresses: a datacenter or a
# rack the ring does not have under the inventory's name is refused (else no
# check would find anything), and the rack's own nodes are told apart in the
# ring by the address they have there (the broadcast address when set; IPv6
# however it is written). The expressions are read from the playbook, rendered
# by Ansible.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

load_collection_filters()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "stop_rack.yml"), encoding="utf-8") as f:
    PLAY = next(p for p in yaml.safe_load(f) if p.get("name") == "Check that the rack can go down")
TASKS = dict((t["name"], t) for t in PLAY["tasks"])
NAMES = [t["name"] for t in PLAY["tasks"]]


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def node(address, rack, status="U"):
    return {"address": address, "rack": rack, "status": status, "state": "N"}


RING = {"dc1": {"nodes": [node("192.0.2.1", "r1"), node("192.0.2.2", "r2"), node("192.0.2.3", "r3")]}}


def hostvars(**racks):
    """{host: (rack, ring address)}, all in dc1; the private address differs from the ring's."""
    return dict((h, {"inventory_hostname": h, "_cassandra_preflight": {
        "cassandra_dc": "dc1", "cassandra_rack": rack, "address": "10.0.0.%s" % ring[-1], "ring_address": ring}})
        for h, (rack, ring) in racks.items())


HOSTS = hostvars(n1=("r1", "192.0.2.1"), n2=("r2", "192.0.2.2"), n3=("r3", "192.0.2.3"))


def checked(ring=None, hosts=None, dc="dc1", rack="r3", **extra):
    ring, hosts = ring or RING, hosts or HOSTS
    task = TASKS["The ring has the rack as the inventory says it"]
    variables = dict(PLAY["vars"], ansible_play_hosts_all=sorted(hosts), hostvars=hosts, cassandra_target_dc=dc,
                     cassandra_target_rack=rack, cassandra_rack_ring={"cluster_status": ring}, **extra)
    variables.update(task["vars"])
    variables["_rack_nodes"] = render(PLAY["vars"]["_rack_nodes"], **variables)
    variables["_rack_node_addresses"] = render(PLAY["vars"]["_rack_node_addresses"], **variables)
    variables["_ring_rack"] = render(PLAY["vars"]["_ring_rack"], **variables)
    variables["_checker"] = render(PLAY["vars"]["_checker"], **variables)
    ok = render("{{ %s }}" % task["ansible.builtin.assert"]["that"], **variables)
    return ok, " ".join(render(task["ansible.builtin.assert"]["fail_msg"], **variables).split())


def test_the_ring_check_comes_before_any_risk_is_worked_out():
    assert NAMES.index("Read the ring from {{ _checker }}") < NAMES.index("The ring has the rack as the inventory says it") \
        < NAMES.index("Work out the risks")
    task = TASKS["The ring has the rack as the inventory says it"]
    # cassandra_rack_force does not skip it
    assert "ignore_errors" not in task and "when" not in task and "force" not in task["ansible.builtin.assert"]["that"]


def test_cassandra_rack_force_does_not_get_past_the_ring_check():
    assert checked(ring={"dc1-suffix": RING["dc1"]}, cassandra_rack_force=True)[0] is False


def test_the_rack_as_the_ring_has_it_passes():
    assert checked()[0] is True


def test_a_datacenter_the_ring_does_not_have_is_refused():
    ok, msg = checked(ring={"dc1-suffix": RING["dc1"]})
    assert ok is False
    assert msg.startswith("The ring (nodetool status on n1) has no datacenter dc1 (it has dc1-suffix).")
    assert msg.endswith("Nothing was changed.")


def test_a_node_in_another_rack_of_the_ring_is_refused():
    # n2 joined in r2, its inventory now says r3: stop_rack would stop two real racks
    hosts = hostvars(n1=("r1", "192.0.2.1"), n2=("r3", "192.0.2.2"), n3=("r3", "192.0.2.3"))
    ok, msg = checked(hosts=hosts)
    assert ok is False and "Not in dc1/r3 in the ring (nodetool status on n1): n2 (192.0.2.2)." in msg


def test_a_node_of_the_rack_not_in_the_ring_at_all_is_said_so():
    hosts = hostvars(n1=("r1", "192.0.2.1"), n2=("r2", "192.0.2.2"), n3=("r3", "192.0.2.9"))
    ok, msg = checked(hosts=hosts)
    assert ok is False and "n3 (192.0.2.9, not in the ring at all)." in msg


def test_ipv6_as_nodetool_prints_it_matches_the_inventory_spelling():
    # nodetool status prints IPv6 in full, the inventory and the facts compressed
    ring = {"dc1": {"nodes": [node("2001:db8:0:0:0:0:0:1", "r1"), node("2001:db8:0:0:0:0:0:2", "r2"),
                              node("2001:db8:0:0:0:0:0:3", "r3", "D")]}}
    hosts = hostvars(n1=("r1", "2001:db8::1"), n2=("r2", "2001:db8::2"), n3=("r3", "2001:db8::3"))
    assert checked(ring=ring, hosts=hosts)[0] is True
    task = TASKS["Work out the risks"]
    variables = dict(PLAY["vars"], ansible_play_hosts_all=sorted(hosts), hostvars=hosts, cassandra_target_dc="dc1",
                     cassandra_target_rack="r3", cassandra_rack_ring={"cluster_status": ring})
    variables.update(task["vars"])
    variables["_rack_nodes"] = render(PLAY["vars"]["_rack_nodes"], **variables)
    variables["_rack_node_addresses"] = render(PLAY["vars"]["_rack_node_addresses"], **variables)
    variables["_ring_rack"] = render(PLAY["vars"]["_ring_rack"], **variables)
    variables["_rack_addresses"] = render(task["vars"]["_rack_addresses"], **variables)
    # n3 down is the rack's own node (a stop_rack run again), not a node already down elsewhere
    assert render(task["ansible.builtin.set_fact"]["_cassandra_rack_down_here"], **variables) == []


def test_inventory_ipv6_written_in_full_matches_too():
    ring = {"dc1": {"nodes": [node("2001:db8:0:0:0:0:0:1", "r1"), node("2001:db8:0:0:0:0:0:2", "r2"),
                              node("2001:db8:0:0:0:0:0:3", "r3")]}}
    hosts = hostvars(n1=("r1", "2001:db8::1"), n2=("r2", "2001:db8::2"), n3=("r3", "2001:db8:0::3"))
    assert checked(ring=ring, hosts=hosts)[0] is True


def test_a_ring_address_that_is_a_name_matches_by_the_host_addresses():
    # cassandra_listen_address set to a name: nodetool lists the node by its IP
    hosts = hostvars(n1=("r1", "192.0.2.1"), n2=("r2", "192.0.2.2"), n3=("r3", "n3.example"))
    hosts["n3"]["ansible_facts"] = {"all_ipv4_addresses": ["198.51.100.7", "192.0.2.3"]}
    assert checked(hosts=hosts)[0] is True
    # the host's other addresses count only for a name: an IP ring_address is matched as it is
    hosts = hostvars(n1=("r1", "192.0.2.1"), n2=("r2", "192.0.2.2"), n3=("r3", "192.0.2.9"))
    hosts["n3"]["ansible_facts"] = {"all_ipv4_addresses": ["192.0.2.3"]}
    assert checked(hosts=hosts)[0] is False


def test_a_host_address_of_a_rack_node_never_hides_another_node_down():
    # n3's ring_address is a name; its host also has n2's address (an alias): n2 down still counts
    hosts = hostvars(n1=("r1", "192.0.2.1"), n2=("r2", "192.0.2.2"), n3=("r3", "n3.example"))
    hosts["n3"]["ansible_facts"] = {"all_ipv4_addresses": ["192.0.2.3", "192.0.2.2"]}
    ring = {"dc1": {"nodes": [node("192.0.2.1", "r1"), node("192.0.2.2", "r2", "D"), node("192.0.2.3", "r3", "D")]}}
    task = TASKS["Work out the risks"]
    variables = dict(PLAY["vars"], ansible_play_hosts_all=sorted(hosts), hostvars=hosts, cassandra_target_dc="dc1",
                     cassandra_target_rack="r3", cassandra_rack_ring={"cluster_status": ring})
    variables.update(task["vars"])
    variables["_rack_nodes"] = render(PLAY["vars"]["_rack_nodes"], **variables)
    variables["_rack_node_addresses"] = render(PLAY["vars"]["_rack_node_addresses"], **variables)
    variables["_ring_rack"] = render(PLAY["vars"]["_ring_rack"], **variables)
    variables["_rack_addresses"] = render(task["vars"]["_rack_addresses"], **variables)
    assert render(task["ansible.builtin.set_fact"]["_cassandra_rack_down_here"], **variables) == ["192.0.2.2"]


def test_a_failed_ring_read_stops_the_play():
    assert TASKS["Read the ring from {{ _checker }}"]["failed_when"] == "cassandra_rack_ring.cluster_status is not mapping"


@pytest.mark.parametrize("down, expected", [("192.0.2.3", []), ("192.0.2.2", ["192.0.2.2"])])
def test_the_rack_nodes_are_told_apart_by_their_ring_address(down, expected):
    task = TASKS["Work out the risks"]
    ring = {"dc1": {"nodes": [node(n["address"], n["rack"], "D" if n["address"] == down else "U") for n in RING["dc1"]["nodes"]]}}
    variables = dict(PLAY["vars"], ansible_play_hosts_all=sorted(HOSTS), hostvars=HOSTS, cassandra_target_dc="dc1",
                     cassandra_target_rack="r3", cassandra_rack_ring={"cluster_status": ring})
    variables.update(task["vars"])
    variables["_rack_nodes"] = render(PLAY["vars"]["_rack_nodes"], **variables)
    variables["_rack_node_addresses"] = render(PLAY["vars"]["_rack_node_addresses"], **variables)
    variables["_ring_rack"] = render(PLAY["vars"]["_ring_rack"], **variables)
    variables["_rack_addresses"] = render(task["vars"]["_rack_addresses"], **variables)
    # a down node of the rack itself (a stop_rack run again) is not "already down" elsewhere in the datacenter
    assert render(task["ansible.builtin.set_fact"]["_cassandra_rack_down_here"], **variables) == expected
    # the racks counted are the ring's
    assert render(task["vars"]["_racks"], **variables) == ["r1", "r2", "r3"]


COMMAND_ARGS = {"inventory": ["/inv/hosts.yml"], "default_inventory": "/etc/ansible/hosts", "hosts": "prod", "cwd": "/inv"}


def test_the_command_args_are_the_run_s():
    # the run's inventory and cluster, the same in the two plays that say a command
    with open(os.path.join(TOP, "playbooks", "stop_rack.yml"), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    args = [p["vars"]["_cassandra_rack_command_args"] for p in plays if "_cassandra_rack_command_args" in p.get("vars", {})]
    assert len(args) == 2 and args[0] == args[1]
    assert args[0]["hosts"] == "{{ cassandra_hosts | default('') }}"  # (as start_rack's)


def test_the_replica_refusal_is_a_refused_block_of_79_columns():
    """A rack whose stop would take more than one replica of some data down: a REFUSED block the ops callback
    prints as is (cassandra_output), each line within 79 columns, the way out in it; it was the assert's dump
    (TASK ..., fatal: ..., assertion:, msg: as YAML, lines over 79 columns). Its way out was prose ("Fix that
    first, or set cassandra_rack_force: true: ..."), its nodes inline: now a WAY OUT with the command to paste,
    the nodes as rows."""
    task = TASKS["Refuse when a keyspace would lose more than one replica"]
    assert task["vars"]["cassandra_output"] is True
    problems = ["system_distributed uses SimpleStrategy with RF 3, which ignores racks: a rack down can hold several"
                " of its replicas"]
    variables = dict(task["vars"], cassandra_target_dc="dc1", cassandra_target_rack="r2", _rack_nodes=["n2", "n3"],
                     _problems=problems, _warnings=["app has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails"],
                     hostvars={"localhost": {"_cassandra_cluster_shown": "prod"}}, _cassandra_rack_command_args=COMMAND_ARGS)
    lines = render(task["ansible.builtin.assert"]["fail_msg"], **variables).split("\n")
    assert lines == [
        "REFUSED  stop_rack  prod  nothing was changed",
        "  Stopping dc1/r2 would take down more than one replica of some data:",
        "    system_distributed uses SimpleStrategy with RF 3, which ignores racks: a",
        "      rack down can hold several of its replicas",
        "  its nodes:",
        "    n2",
        "    n3",
        "  WARNING  app has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails",
        "  Forced, reads and writes needing those replicas fail, and data only on them",
        "    is unavailable until the rack is back.",
        "  WAY OUT  fix that first, or force the stop:",
        "    ansible-playbook -i hosts.yml community.cassandra.stop_rack \\",
        "      -e cassandra_hosts=prod -e cassandra_target_dc=dc1 \\",
        "      -e cassandra_target_rack=r2 -e cassandra_rack_force=true"]


def test_how_to_start_it_again_is_the_exact_command_within_79_columns():
    # S26: one line of 190 columns, "-i <inventory>" for the run's inventory; now the command as start_rack takes
    # it (the run's inventory, its cluster), on a line of its own, cut between its arguments
    with open(os.path.join(TOP, "playbooks", "stop_rack.yml"), encoding="utf-8") as f:
        say = next(t for p in yaml.safe_load(f) for t in p.get("tasks", []) if t.get("name") == "Say how to start it again")
    variables = dict(cassandra_target_dc="dc1", cassandra_target_rack="r2", ansible_play_hosts_all=["h-n4", "h-n5"],
                     ansible_inventory_sources=["/inv/hosts.yml"], _cassandra_rack_command_args=COMMAND_ARGS)
    lines = render(say["ansible.builtin.debug"]["msg"], _start=render(say["vars"]["_start"], **variables), **variables)
    assert lines == ["dc1/r2 is down: h-n4, h-n5", "start it again with:",
                     "  ansible-playbook -i hosts.yml community.cassandra.start_rack \\",
                     "    -e cassandra_hosts=prod -e cassandra_target_dc=dc1 \\",
                     "    -e cassandra_target_rack=r2"]
    assert say["vars"]["cassandra_output"] is True
