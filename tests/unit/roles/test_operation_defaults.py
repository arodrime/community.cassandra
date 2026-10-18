# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_operation's task files read some of cassandra_service's defaults: repeated in its own defaults
# (role defaults are only seen inside the role's include), they must keep the same values; the conf dir and
# the record of the config a node started with are defined once, in cassandra_service's, which the task files
# that read them load (include_role: tasks_from: defaults.yml). The record is written by cassandra_config and
# cassandra_service: one path, one file list.

import glob
import os
import re

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")
# defined once, in cassandra_service's defaults, loaded by the task files that read them
LOADED = ("_cassandra_service_conf_dir", "_cassandra_service_config_snapshot")


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
    assert [k for k in read if k not in OPERATION and k not in LOADED] == []
    assert not [k for k in LOADED if k in OPERATION]


def test_the_task_files_load_the_conf_dir_and_the_record():
    # each task file reading them loads cassandra_service's defaults first (a play's public include does not
    # reach another play)
    loads = re.compile(r"name: community\.cassandra\.cassandra_service\n\s+tasks_from: defaults\.yml\n\s+public: true"
                       r"|loop: \[cassandra_config, cassandra_service\]")
    for path in sorted(glob.glob(os.path.join(ROLES, "cassandra_operation", "tasks", "*.yml"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        uses = [re.search(r"(?<!\w)%s(?!\w)" % k, text) for k in LOADED]
        if any(uses):
            load = loads.search(text)
            assert load and load.start() < min(m.start() for m in uses if m), os.path.basename(path)


def test_the_conf_dir_has_no_trailing_slash():
    # the record keys its files by their normalised path: /etc/cassandra/ would never match it
    template = trust_as_template(SERVICE["_cassandra_service_conf_dir"])
    facts = {"ansible_facts": {"os_family": "Debian"}}
    for conf, expected in (("/etc/cassandra/", "/etc/cassandra"), ("/etc/cassandra", "/etc/cassandra"),
                           (" /etc/cassandra/conf// ", "/etc/cassandra/conf")):
        assert Templar(loader=DataLoader(), variables=dict(facts, cassandra_conf_dir=conf)).template(template) == expected
    for family, expected in (("RedHat", "/etc/cassandra/conf"), ("Debian", "/etc/cassandra")):
        variables = {"ansible_facts": {"os_family": family}}
        assert Templar(loader=DataLoader(), variables=variables).template(template) == expected


def test_one_record_of_the_config_at_start():
    config = load("cassandra_config", "vars")
    assert config["_cassandra_config_snapshot"] == SERVICE["_cassandra_service_config_snapshot"]
    assert config["_cassandra_config_snapshot_files"] == OPERATION["_cassandra_service_config_files"]
