from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# ansible-core 2.21 gives every host the facts of one of them once a run_once
# task ran in the play (ansible/ansible#87148): the playbooks stop there,
# before anything, unless cassandra_ansible_core_check is false.

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


@pytest.mark.parametrize("version, check, ok", [
    ("2.17.14", None, True), ("2.20.10", None, True), ("2.21.0", None, False), ("2.21.4", None, False),
    ("2.21.4", False, True), ("2.22.0", None, True)])
def test_stops_on_2_21(version, check, ok):
    task = load("roles", "cassandra_service", "tasks", "core_check.yml")[0]
    assert task["vars"]["cassandra_output"] is True and "87148" in task["ansible.builtin.assert"]["fail_msg"]
    variables = {"ansible_version": {"full": version}}
    if check is not None:
        variables["cassandra_ansible_core_check"] = check
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ %s }}" % task["ansible.builtin.assert"]["that"])) is ok


def test_checked_first():
    assert load("roles", "cassandra_service", "tasks", "hosts_check.yml")[0]["ansible.builtin.include_tasks"] == \
        "core_check.yml"
    first = load("playbooks", "import_cluster.yml")[0]["tasks"][0]
    assert first["ansible.builtin.include_role"]["tasks_from"] == "core_check.yml"
    assert load("roles", "cassandra_service", "defaults", "main.yml")["cassandra_ansible_core_check"] is True
