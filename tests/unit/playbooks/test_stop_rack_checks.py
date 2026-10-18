from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# stop_rack checks the rack by the ring's names and addresses: a datacenter or a
# rack the ring does not have under the inventory's name is refused (else no
# check would find anything), and the rack's own nodes are told apart in the
# ring by the address they have there (the broadcast address when set). The
# expressions are read from the playbook, rendered by Ansible.

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


def checked(ring=None, hosts=None, dc="dc1", rack="r3"):
    ring, hosts = ring or RING, hosts or HOSTS
    task = TASKS["The ring has the rack as the inventory says it"]
    variables = dict(PLAY["vars"], ansible_play_hosts_all=sorted(hosts), hostvars=hosts, cassandra_target_dc=dc,
                     cassandra_target_rack=rack, cassandra_rack_ring={"cluster_status": ring})
    variables.update(task["vars"])
    variables["_rack_nodes"] = render(PLAY["vars"]["_rack_nodes"], **variables)
    variables["_checker"] = render(PLAY["vars"]["_checker"], **variables)
    ok = render("{{ %s }}" % task["ansible.builtin.assert"]["that"], **variables)
    return ok, " ".join(render(task["ansible.builtin.assert"]["fail_msg"], **variables).split())


def test_the_ring_check_comes_before_any_risk_is_worked_out():
    assert NAMES.index("Read the ring from {{ _checker }}") < NAMES.index("The ring has the rack as the inventory says it") \
        < NAMES.index("Work out the risks")
    assert "ignore_errors" not in TASKS["The ring has the rack as the inventory says it"]  # cassandra_rack_force does not skip it


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


@pytest.mark.parametrize("down, expected", [("192.0.2.3", []), ("192.0.2.2", ["192.0.2.2"])])
def test_the_rack_nodes_are_told_apart_by_their_ring_address(down, expected):
    task = TASKS["Work out the risks"]
    ring = {"dc1": {"nodes": [node(n["address"], n["rack"], "D" if n["address"] == down else "U") for n in RING["dc1"]["nodes"]]}}
    variables = dict(PLAY["vars"], ansible_play_hosts_all=sorted(HOSTS), hostvars=HOSTS, cassandra_target_dc="dc1",
                     cassandra_target_rack="r3", cassandra_rack_ring={"cluster_status": ring})
    variables.update(task["vars"])
    variables["_rack_nodes"] = render(PLAY["vars"]["_rack_nodes"], **variables)
    variables["_rack_addresses"] = render(task["vars"]["_rack_addresses"], **variables)
    # a down node of the rack itself (a stop_rack run again) is not "already down" elsewhere in the datacenter
    assert render(task["ansible.builtin.set_fact"]["_cassandra_rack_down_here"], **variables) == expected
    # the racks counted are the ring's
    assert render(task["vars"]["_racks"], **variables) == ["r1", "r2", "r3"]
