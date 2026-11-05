from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Every cqlsh the collection runs on a node takes cassandra_cqlsh_command: a cqlsh outside root's PATH (a
# virtualenv's bin/cqlsh) reaches every one of them, none left on the bare "cqlsh".

import glob
import os

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
MODULE = "community.cassandra.cassandra_cqlsh"


def _calls(node):
    """Each cassandra_cqlsh call in a loaded YAML file, blocks and plays included."""
    if isinstance(node, dict):
        if MODULE in node:
            yield node[MODULE]
        for value in node.values():
            yield from _calls(value)
    elif isinstance(node, list):
        for item in node:
            yield from _calls(item)


def test_every_cqlsh_call_takes_the_command():
    files = glob.glob(os.path.join(ROOT, "playbooks", "*.yml")) + glob.glob(
        os.path.join(ROOT, "roles", "*", "tasks", "*.yml"))
    found = []
    for path in files:
        with open(path) as f:
            for call in _calls(yaml.safe_load(f)):
                found.append(os.path.relpath(path, ROOT))
                assert call.get("cqlsh_cmd") == "{{ cassandra_cqlsh_command | default(omit, true) }}", path
    assert len(found) >= 8, found  # (the checks over CQL: keyspaces, the login, the playbooks that read the schema)


def test_the_default_and_its_spec():
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "defaults", "main.yml")) as f:
        assert yaml.safe_load(f)["cassandra_cqlsh_command"] == "cqlsh"
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "meta", "argument_specs.yml")) as f:
        spec = yaml.safe_load(f)["argument_specs"]["main"]["options"]["cassandra_cqlsh_command"]
    assert spec["default"] == "cqlsh" and spec["type"] == "str"


def test_no_cqlsh_names_the_variable():
    # the import's TO DO when its login check finds no cqlsh: where to say where it is
    with open(os.path.join(ROOT, "roles", "cassandra_operation", "tasks", "cql_login_try.yml")) as f:
        outcome = next(t for t in yaml.safe_load(f) if t.get("name") == "Keep the outcome of the CQL login")
    why = outcome["ansible.builtin.set_fact"]["cassandra_cql_login"]["why"]
    assert "set' ~ ' cassandra_cqlsh_command to its bin/cqlsh'" in " ".join(why.split())
