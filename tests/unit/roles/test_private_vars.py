from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The roles' private variables (a leading underscore: computed from the public
# settings, or fixed) are role vars (vars/main.yml), not defaults: an inventory
# or play variable of the same name does not override them by accident.

import glob
import os

import yaml

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")


def test_no_private_variable_in_the_defaults():
    found = []
    for path in sorted(glob.glob(os.path.join(ROLES, "*", "defaults", "*.yml"))):
        with open(path, encoding="utf-8") as f:
            found += ["%s: %s" % (os.path.relpath(path, ROLES), k) for k in (yaml.safe_load(f) or {}) if k.startswith("_")]
    assert found == []
