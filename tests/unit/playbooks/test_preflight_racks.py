from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# preflight's rack check follows the token allocator: a node joining a rack
# already in the ring is refused when the ring (down nodes too: a dead node's
# rack is still there) has more than 1 rack but fewer than the hint, each new
# node joining the ring the next one sees; a replacement allocates nothing;
# with no ring, the inventory's racks. The expressions are read from the playbook,
# rendered by Ansible.

import os
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the inventory_hostnames lookup, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "preflight.yml"), encoding="utf-8") as f:
    PLAY = yaml.safe_load(f)[0]
RACKS = next(t for t in PLAY["tasks"] if t.get("name") == "Racks per datacenter")


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def passes(inventory, ring, hint=3, **extra):
    """inventory: host -> (rack, address); ring: [(rack, address)] or None (no running node)"""
    hostvars = dict((h, {"_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": rack, "address": address,
                                                  "layout": {"name": h}}})
                    for h, (rack, address) in inventory.items())
    variables = dict(RACKS["vars"], ansible_play_hosts=list(inventory), hostvars=hostvars,
                     cassandra_allocate_tokens_for_local_replication_factor=hint, item="dc1",
                     groups=extra.pop("groups", {"all": list(inventory)}), **extra)
    if ring is not None:
        variables["cassandra_preflight_status"] = {"cluster_status": {"dc1": {"nodes": [
            {"address": a, "rack": r} for r, a in ring]}}}
    else:  # no running node: the ring read is skipped
        variables["cassandra_preflight_status"] = {"skipped": True}
    return render("{{ %s }}" % RACKS["ansible.builtin.assert"]["that"], **variables)


def test_the_check_runs_after_the_ring_is_read():
    names = [t["name"] for t in PLAY["tasks"]]
    assert names.index("Check the running cluster") < names.index("Racks per datacenter")


N1, N2, N3, N9 = ("r1", "10.0.0.1"), ("r2", "10.0.0.2"), ("r3", "10.0.0.3"), ("r3", "10.0.0.9")


@pytest.mark.parametrize("inventory, ring, ok", [
    # r3's only node is dead and out of the inventory: its rack is still in the ring
    ({"n1": N1, "n2": N2}, [N1, N2, N3], True),
    # the ring really has 2 racks, nobody joins: nothing to allocate
    ({"n1": N1, "n2": N2}, [N1, N2], True),
    # a new node in a rack of that ring: the allocator refuses it
    ({"n1": N1, "n2": N2, "n8": ("r1", "10.0.0.8")}, [N1, N2], False),
    # a new node in a rack not in the ring: the allocator takes 1 rack
    ({"n1": N1, "n2": N2, "n9": N9}, [N1, N2], True),
    # both at once: the r1 one is refused
    ({"n1": N1, "n2": N2, "n8": ("r1", "10.0.0.8"), "n9": N9}, [N1, N2], False),
    # enough racks in the ring
    ({"n1": N1, "n2": N2, "n8": ("r1", "10.0.0.8")}, [N1, N2, N3], True),
    ({"n1": ("r1", "10.0.0.1"), "n8": ("r1", "10.0.0.8")}, [N1], True),
    # a new cluster, nothing running: the inventory alone
    ({"n1": N1, "n2": N2, "n3": N3}, None, True),
    ({"n1": N1, "n2": N2}, None, False),
    ({"n1": N1, "n2": ("r1", "10.0.0.2")}, None, True),
    # one join after the other: n2 brings r2, then n3 joins r1 in a 2-rack ring
    ({"n1": N1, "n2": N2, "n3": ("r1", "10.0.0.3")}, [N1], False),
    ({"n1": N1, "n2": N2, "n3": N3}, [N1], True),
])
def test_racks(inventory, ring, ok):
    assert passes(inventory, ring) is ok


def test_racks_hint_as_a_string_and_4():
    assert passes({"n1": N1, "n2": N2, "n3": N3, "n9": N9}, [N1, N2], hint="4") is False
    assert passes({"n1": N1, "n2": N2, "n8": ("r1", "10.0.0.8")}, [N1, N2], hint="2") is True


@pytest.mark.parametrize("new_nodes, groups, ok", [
    # add_node runs cassandra_new_nodes in its order: n3 (r1) first joins a 1-rack ring, then n2 brings r2
    ("n3,n2", {}, True),
    (["n3", "n2"], {}, True),
    ("new", {"all": ["n1", "n2", "n3"], "new": ["n3", "n2"]}, True),
    ("n3:n2", {}, True),
    ("n*:!n1:!n2", {}, True),  # n3 only, then n2 in inventory order
    # n2 first: n3 then joins r1 in a 2-rack ring
    ("n2, n3", {}, False),
    ("", {}, False),  # inventory order
])
def test_racks_join_in_the_new_nodes_order(new_nodes, groups, ok):
    inventory = {"n1": N1, "n2": N2, "n3": ("r1", "10.0.0.3")}
    groups = groups or {"all": list(inventory)}
    assert passes(inventory, [N1], cassandra_new_nodes=new_nodes, groups=groups) is ok


def test_a_replacement_allocates_nothing():
    inventory = {"n1": N1, "n2": N2, "n8": ("r1", "10.0.0.8")}
    assert passes(inventory, [N1, N2], cassandra_replace_address="10.0.0.3") is True
    assert passes(inventory, [N1, N2], cassandra_replace_address="") is False


def test_the_message_names_the_refused_node():
    inventory = {"n1": N1, "n2": N2, "n8": ("r1", "10.0.0.8")}
    hostvars = dict((h, {"_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": r, "address": a,
                                                  "layout": {"name": h}}}) for h, (r, a) in inventory.items())
    variables = dict(RACKS["vars"], ansible_play_hosts=list(inventory), hostvars=hostvars, item="dc1", groups={},
                     cassandra_allocate_tokens_for_local_replication_factor=3,
                     cassandra_preflight_status={"cluster_status": {"dc1": {"nodes": [
                         {"address": a, "rack": r} for r, a in [N1, N2]]}}})
    msg = render(RACKS["ansible.builtin.assert"]["fail_msg"], **variables)
    assert "refuse, in dc1: n8 in r1 when the ring has r1, r2 (" in msg and "at least 3" in msg
    variables["cassandra_preflight_status"] = {"skipped": True}
    msg = render(RACKS["ansible.builtin.assert"]["fail_msg"], **variables)
    assert msg.startswith("dc1 has 2 racks (r1, r2): use 1 rack or at least 3")


def test_racks_per_datacenter():
    # dc2's ring has 2 racks and a new node joins one of them; dc1 is fine
    hostvars = dict((h, {"_cassandra_preflight": {"cassandra_dc": dc, "cassandra_rack": r, "address": a, "layout": {"name": h}}})
                    for h, dc, r, a in [("n1", "dc1", "r1", "10.0.0.1"), ("m1", "dc2", "r1", "10.0.1.1"),
                                        ("m2", "dc2", "r2", "10.0.1.2"), ("m8", "dc2", "r1", "10.0.1.8")])
    ring = {"dc1": {"nodes": [{"address": "10.0.0.1", "rack": "r1"}]},
            "dc2": {"nodes": [{"address": "10.0.1.1", "rack": "r1"}, {"address": "10.0.1.2", "rack": "r2"}]}}
    variables = dict(RACKS["vars"], ansible_play_hosts=list(hostvars), hostvars=hostvars, groups={"all": list(hostvars)},
                     cassandra_allocate_tokens_for_local_replication_factor=3, cassandra_preflight_status={"cluster_status": ring})
    assert render("{{ %s }}" % RACKS["ansible.builtin.assert"]["that"], item="dc1", **variables) is True
    assert render("{{ %s }}" % RACKS["ansible.builtin.assert"]["that"], item="dc2", **variables) is False
