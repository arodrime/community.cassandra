# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install run twice for a host in one playbook run: the second report
# lists only what the second run changed, not the first run's items again.

import os
import re

import yaml

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_install", "tasks")


def test_report_facts_reset_first():
    with open(os.path.join(TASKS, "install.yml"), encoding="utf-8") as f:
        tasks = yaml.safe_load(f)
    # right after the interpreter pin, which comes first (test_module_python.py)
    reset = tasks[1]["ansible.builtin.set_fact"]
    assert reset == {"_cassandra_install_items": [], "_cassandra_install_dsbulk_change": {}}
    # every fact of the report the role adds to, or reads with a default, is in the reset
    read = set()
    for name in os.listdir(TASKS):
        with open(os.path.join(TASKS, name), encoding="utf-8") as f:
            read.update(re.findall(r"(_cassandra_install_(?:items|dsbulk_change)) \| default", f.read()))
    assert read == set(reset)
