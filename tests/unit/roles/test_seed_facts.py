from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# seed_facts.yml: a node is a seed when one of its names or addresses is an
# entry of cassandra_seeds, the port left out; an IPv6 entry is not cut at its
# last colon ("2001:db8::4", "[2001:db8::4]:7000"). A new seed looks for the
# other seeds (main.yml) at each entry's host and port. The expressions are
# read from the task files, rendered by Ansible.

import os

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

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks")


def load(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def flat(tasks):
    out = []
    for t in tasks:
        out.append(t)
        for key in ("block", "rescue", "always"):
            out.extend(flat(t.get(key) or []))
    return out


def task(tasks, name):
    return next(t for t in flat(tasks) if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


FACTS = {"fqdn": "node4.example.net", "hostname": "node4", "all_ipv4_addresses": ["10.0.0.4"],
         "all_ipv6_addresses": ["2001:db8::4"]}
SEED_FACTS = task(load("seed_facts.yml"), "Work out whether this node is a seed")
LOOK = task(load("main.yml"), "Look for other seeds already up")
EXISTING = task(load("main.yml"), "Refuse to join an existing cluster as a seed")


def seed_facts(seeds):
    variables = dict(SEED_FACTS["vars"], cassandra_seeds=seeds, inventory_hostname="node4", ansible_host="10.0.0.4",
                     ansible_facts=FACTS)
    facts = SEED_FACTS["ansible.builtin.set_fact"]
    return dict((k, render(v, **variables)) for k, v in facts.items())


def test_a_seed_is_known_by_any_of_its_names_the_port_left_out():
    facts = seed_facts(["10.0.0.1:7000", "node4:7000"])
    assert facts["_cassandra_service_is_seed"] is True and facts["_cassandra_service_seed_entry"] == "node4"
    assert facts["_cassandra_service_seeds"] == ["10.0.0.1", "node4"]
    assert facts["_cassandra_service_names"] == ["node4", "10.0.0.4", "node4.example.net", "node4", "10.0.0.4", "2001:db8::4"]
    assert seed_facts("10.0.0.1, 10.0.0.4")["_cassandra_service_is_seed"] is True  # the string form
    assert seed_facts(["10.0.0.1", "10.0.0.2:7000"])["_cassandra_service_is_seed"] is False


def test_an_ipv6_seed_is_known_with_or_without_its_port_and_brackets():
    for entry in ("2001:db8::4", "[2001:db8::4]:7000", "[2001:db8::4]"):
        facts = seed_facts(["10.0.0.1", entry])
        assert facts["_cassandra_service_is_seed"] is True, entry
        assert facts["_cassandra_service_seed_entry"] == "2001:db8::4", entry
        assert facts["_cassandra_service_seeds"] == ["10.0.0.1", "2001:db8::4"], entry
    facts = seed_facts("2001:db8::5, [2001:db8::6]:7000")
    assert facts["_cassandra_service_is_seed"] is False and facts["_cassandra_service_seeds"] == ["2001:db8::5", "2001:db8::6"]


def test_the_other_seeds_are_probed_at_their_host_and_port():
    variables = dict(LOOK["vars"], inventory_hostname="node4", ansible_host="10.0.0.4", ansible_facts=FACTS,
                     cassandra_seeds=["10.0.0.1:7001", "[2001:db8::4]:7000", "2001:db8::2", "10.0.0.4", "node4:7000"])
    assert render(LOOK["vars"]["_others"], **variables) == ["10.0.0.1:7001", "2001:db8::2"]  # itself left out, any port
    probe = LOOK["ansible.builtin.wait_for"]
    for item, host, port in (("10.0.0.1:7001", "10.0.0.1", "7001"), ("[2001:db8::2]:7001", "2001:db8::2", "7001"),
                             ("2001:db8::2", "2001:db8::2", "7000"), ("node1", "node1", "7000")):
        assert render(probe["host"], item=item) == host, item
        assert str(render(probe["port"], item=item, cassandra_storage_port=7000)) == port, item


def test_the_seeds_answering_are_named_without_their_port():
    results = [{"item": "[2001:db8::1]:7000"}, {"item": "2001:db8::2", "msg": "Timeout when waiting for 2001:db8::2:7000"},
               {"item": "10.0.0.3:7000"}, {"item": "10.0.0.5", "skipped": True}]
    variables = dict(EXISTING["vars"], cassandra_service_seeds_up={"results": results}, ansible_play_hosts_all=[], hostvars={})
    assert render(EXISTING["vars"]["_existing"], **variables) == ["2001:db8::1", "10.0.0.3"]
