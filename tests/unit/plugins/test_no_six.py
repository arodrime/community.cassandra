from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# ansible.module_utils.six is deprecated from ansible-core 2.22 (a warning on every
# run that loads the plugin) and removed in 2.24: the collection's Python code
# uses the standard library instead.

import os
import re

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def test_no_plugin_imports_six():
    found = []
    for where in ("plugins", "roles", "playbooks"):
        for root, dirs, files in os.walk(os.path.join(TOP, where)):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        if re.search(r"^\s*(from|import)\s+(ansible\.module_utils\.)?six\b", f.read(), re.M):
                            found.append(os.path.join(root, name))
    assert found == []
