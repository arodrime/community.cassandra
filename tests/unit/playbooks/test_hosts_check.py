from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The operation playbooks check first, on localhost, that the group named by
# cassandra_hosts is in the inventory (a typo like -e cassandra_host=...
# otherwise ends in a raw template error). Rendered by Ansible.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


CHECK = next(t for t in load("roles", "cassandra_service", "tasks", "hosts_check.yml")
             if t["name"] == "The cluster's group is in the inventory")
GROUPS = {"all": ["n1", "n2"], "ungrouped": [], "prod": ["n1", "n2"], "empty": []}


def check(**variables):
    variables = dict(CHECK["vars"], groups=GROUPS, **variables)
    passed = render("{{ %s }}" % CHECK["ansible.builtin.assert"]["that"], **variables)
    return passed, render(CHECK["ansible.builtin.assert"]["fail_msg"], **variables)


def test_existing_group_passes():
    assert check(cassandra_hosts="prod")[0] is True


def test_missing_group_named_with_the_groups():
    passed, msg = check(cassandra_hosts="prd")
    assert passed is False
    assert msg == "cassandra_hosts: group 'prd' not found in the inventory (groups: empty, prod)."


def test_default_group_missing():
    # -e cassandra_host=prod (typo): cassandra_hosts is not set, the default group does not exist
    passed, msg = check()
    assert passed is False
    assert msg == ("cassandra_hosts: group 'cassandra' not found in the inventory (groups: empty, prod);"
                   " cassandra_hosts is not set, 'cassandra' is its default.")


def test_empty_group():
    passed, msg = check(cassandra_hosts="empty")
    assert passed is False and msg.startswith("cassandra_hosts: group 'empty' has no host")


@pytest.mark.parametrize("playbook", ["preflight.yml", "health_check.yml", "start_rack.yml", "status.yml"])
def test_first_play_checks_the_group(playbook):
    first = load("playbooks", playbook)[0]
    assert first["hosts"] == "localhost"
    assert first["tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml"


def test_every_playbook_on_the_group_checks_it():
    # each playbook that runs on cassandra_hosts starts with the check or imports preflight first
    for name in sorted(os.listdir(os.path.join(TOP, "playbooks"))):
        plays = load("playbooks", name)
        if "cassandra_hosts" not in str(plays) or name == "import_cluster.yml":
            continue
        first = plays[0]
        assert first.get("ansible.builtin.import_playbook") == "community.cassandra.preflight" \
            or first["tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml", name
