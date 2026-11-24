from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import glob
import os

import pytest
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


ROLES = sorted(os.path.basename(os.path.dirname(os.path.dirname(p)))
               for p in glob.glob(os.path.join(TOP, "roles", "*", "defaults", "main.yml")))


@pytest.mark.parametrize("role", ROLES)
def test_no_register_shadows_a_role_variable(role):
    # a registered result beats the inventory: a task registering a variable's name would replace its value for the
    # rest of the play (the AxonOps agent key became a skipped task's result)
    with open(os.path.join(TOP, "roles", role, "defaults", "main.yml")) as f:
        variables = set(yaml.safe_load(f) or {})
    shadowing = set()
    for path in glob.glob(os.path.join(TOP, "roles", role, "tasks", "*.yml")):
        with open(path) as f:
            shadowing.update(n for n in _registers(yaml.safe_load(f)) if n in variables)
    assert not shadowing
