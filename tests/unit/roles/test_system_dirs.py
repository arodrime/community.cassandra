from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A node that started once has a system keyspace in one of its data dirs
# (cassandra_data_file_directories, cassandra_data_dir) or, 4.1+, in
# local_system_data_file_directory: add_node, replace_node and the start of a
# new seed look in all of them, not in cassandra_data_dir alone.

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
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


DIRS = load("roles", "cassandra_service", "defaults", "main.yml")["_cassandra_service_system_dirs"]


@pytest.mark.parametrize("variables, dirs", [
    ({}, ["/var/lib/cassandra/data"]),
    ({"cassandra_data_dir": "/data/cassandra"}, ["/data/cassandra"]),
    ({"cassandra_data_file_directories": ["/d1/data/", "/d2/data"], "cassandra_data_dir": "/d1/data"},
     ["/d1/data", "/d2/data"]),
    ({"cassandra_data_file_directories": "/d1/data", "cassandra_data_dir": "/var/lib/cassandra/data"},
     ["/d1/data", "/var/lib/cassandra/data"]),
    ({"cassandra_data_file_directories": ["/d1/data"], "cassandra_data_dir": "/d1/data",
      "cassandra_extra_settings": {"local_system_data_file_directory": "/ssd/system"}},
     ["/d1/data", "/ssd/system"]),
    ({"cassandra_extra_settings": {"local_system_data_file_directory": None}}, ["/var/lib/cassandra/data"]),
])
def test_the_dirs_looked_at(variables, dirs):
    assert render(DIRS, **variables) == dirs


def stat_results(found):
    return {"results": [{"item": d, "stat": {"exists": d in found}} for d in ["/d1/data", "/d2/data", "/ssd/system"]]}


@pytest.mark.parametrize("found, has_data", [([], False), (["/d2/data"], True), (["/ssd/system"], True)])
def test_add_node_finds_data_in_any_dir(found, has_data):
    block = load("roles", "cassandra_service", "tasks", "new_node_state.yml")[0]
    look = next(t for t in block["block"] if t["name"] == "Look for this node's system keyspace")
    assert look["loop"] == "{{ _cassandra_service_system_dirs }}"
    assert look["ansible.builtin.stat"]["path"] == "{{ item }}/system"
    v = dict(cassandra_new_node_system=stat_results(found))
    v["_data"] = render(block["vars"]["_data"], **v)
    assert v["_data"] == [d + "/system" for d in found]
    assert render(block["vars"]["_has_data"], **v) is has_data


@pytest.mark.parametrize("found", [[], ["/ssd/system"]])
def test_replace_node_finds_data_in_any_dir(found):
    play = next(p for p in load("playbooks", "replace_node.yml") if p.get("name") == "Check the replacement")
    tasks = dict((t["name"], t) for t in walk(play["tasks"]))
    assert tasks["Look for data on the new host"]["loop"] == "{{ _cassandra_service_system_dirs }}"
    blank = tasks["The new host is blank"]
    v = dict(cassandra_replace_system=stat_results(found))
    v["_found"] = render(blank["vars"]["_found"], **v)
    assert v["_found"] == [d + "/system" for d in found]
    assert render("{{ %s }}" % blank["ansible.builtin.assert"]["that"], **v) is (not found)


@pytest.mark.parametrize("found, initialized", [([], False), (["/d2/data"], True)])
def test_a_seed_started_once_is_known_by_any_dir(found, initialized):
    tasks = dict((t["name"], t) for t in walk(load("roles", "cassandra_service", "tasks", "main.yml")))
    assert tasks["Check whether this node was already initialized"]["loop"] == "{{ _cassandra_service_system_dirs }}"
    remember = tasks["Remember whether it was"]["ansible.builtin.set_fact"]["cassandra_service_was_initialized"]
    assert render(remember, cassandra_service_initialized=stat_results(found)) is initialized
    assert "not cassandra_service_was_initialized | bool" in tasks["Look for other seeds already up"]["when"]
