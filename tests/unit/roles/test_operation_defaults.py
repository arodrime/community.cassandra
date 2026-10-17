# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_operation's task files read some of cassandra_service's defaults: repeated in its own defaults
# (role defaults are only seen inside the role's include), they must keep the same values. The record of the
# config a node started with is written by cassandra_config and cassandra_service: one path, one file list.

import glob
import os
import re

import yaml

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")


def load(role, kind="defaults"):
    with open(os.path.join(ROLES, role, kind, "main.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


SERVICE, OPERATION = load("cassandra_service"), load("cassandra_operation")


def test_the_shared_defaults_are_equal():
    shared = set(SERVICE) & set(OPERATION)
    assert shared  # the JMX login, the waits, the unit switches
    assert [k for k in sorted(shared) if SERVICE[k] != OPERATION[k]] == []


def test_every_service_default_the_task_files_read_is_there():
    text = ""
    for path in glob.glob(os.path.join(ROLES, "cassandra_operation", "t*", "*")):  # tasks, templates
        with open(path, encoding="utf-8") as f:
            text += f.read()
    text += yaml.safe_dump(OPERATION)
    read = [k for k in SERVICE if re.search(r"(?<!\w)%s(?!\w)" % re.escape(k), text)]
    assert [k for k in read if k not in OPERATION] == []


def test_one_record_of_the_config_at_start():
    config = load("cassandra_config", "vars")
    assert config["_cassandra_config_snapshot"] == SERVICE["_cassandra_service_config_snapshot"]
    assert config["_cassandra_config_snapshot_files"] == OPERATION["_cassandra_service_config_files"]
