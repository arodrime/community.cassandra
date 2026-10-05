from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# upgrade: with cassandra_install_method packages, the target files are checked
# before any node stops; skipped checks (the repository method) are no failure.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "upgrade.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)

ASSERT = next(t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == "The target package files are there")


def render(name, hostvars):
    variables = dict(ASSERT["vars"], hostvars=hostvars, ansible_play_hosts_all=list(hostvars))
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(ASSERT["vars"][name]))


def results(*items):
    return {"cassandra_upgrade_files": {"results": list(items)}}


SKIPPED = {"skipped": True, "item": "cassandra"}


def test_skipped_checks_are_no_failure():
    hostvars = {"n1": dict(results(SKIPPED, SKIPPED), inventory_hostname="n1"),
                "n2": dict(results({"status": 206}, {"status": 200}), inventory_hostname="n2")}
    assert render("_missing", hostvars) == []


def test_missing_files_and_unreachable_mirror():
    hostvars = {"n1": dict(results({"status": 206}, {"status": 404}), inventory_hostname="n1"),
                "n2": dict(results({"status": -1}, {"status": -1}), inventory_hostname="n2"),
                "n3": {"inventory_hostname": "n3"}}
    assert render("_missing", hostvars) == ["n1", "n2"]
    assert sorted(render("_codes", hostvars)) == [-1, 404]
