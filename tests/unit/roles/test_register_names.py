from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import glob
import os

import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import TOP


def _registers(node):
    if isinstance(node, dict):
        if isinstance(node.get("register"), str):
            yield node["register"]
        for value in node.values():
            yield from _registers(value)
    elif isinstance(node, list):
        for value in node:
            yield from _registers(value)


def test_no_register_shadows_a_role_variable():
    # a registered result beats the inventory: a task registering a variable's name would replace its value for the
    # rest of the play (the AxonOps agent key became a skipped task's result). Every role's variables against every
    # task of the roles and the playbooks: cassandra_operation and the playbooks run the other roles
    variables = set()
    for path in glob.glob(os.path.join(TOP, "roles", "*", "defaults", "main.yml")):
        with open(path) as f:
            variables.update(yaml.safe_load(f) or {})
    shadowing = set()
    for path in glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml")) + glob.glob(
            os.path.join(TOP, "playbooks", "*.yml")) + glob.glob(os.path.join(TOP, "playbooks", "tasks", "*.yml")):
        with open(path) as f:
            shadowing.update("%s: %s" % (os.path.relpath(path, TOP), n) for n in _registers(yaml.safe_load(f))
                             if n in variables)
    assert not sorted(shadowing)
