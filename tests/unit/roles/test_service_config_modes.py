from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service's check that its account can read the config files: the modes cassandra_config gives them,
# quoted or not (an unquoted 0640 is the number 416; before ansible-core 2.19 a template gives it as text).
# The role's variables, templated for real by ansible-playbook.

import json
import os
import subprocess

import pytest
import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import LOCAL, run_playbook

pytestmark = pytest.mark.playbook

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

with open(os.path.join(TOP, "roles", "cassandra_service", "vars", "main.yml"), encoding="utf-8") as _f:
    PERMS = yaml.safe_load(_f)["_cassandra_service_config_perms"]


def modes(tmp_path, variables):
    """The modes the check expects on cassandra.yaml and on logback.xml."""
    play = {"hosts": "all", "gather_facts": False,
            "vars": dict({"_perms": PERMS, "cassandra_group": "cassandra", "cassandra_service_user": "cassandra",
                          "cassandra_service_group": "cassandra"}, **variables),
            "tasks": [{"ansible.builtin.debug": {"msg": "MODES {{ ['cassandra.yaml', 'logback.xml'] | map("
                                                        "'community.cassandra.cassandra_file_permissions', _perms)"
                                                        " | map(attribute='mode') | join(' ') }}"}}]}
    (tmp_path / "play.yml").write_text(json.dumps([play]))
    (tmp_path / "hosts.ini").write_text("node1\n")
    rc, out = run_playbook(["-i", str(tmp_path / "hosts.ini")] + LOCAL + [str(tmp_path / "play.yml")],
                           dict(ANSIBLE_STDOUT_CALLBACK="default"), ["ANSIBLE_CONFIG"], cwd=tmp_path, stdin=subprocess.DEVNULL)
    assert rc == 0, out
    return out.split("MODES ", 1)[1].split('"', 1)[0]


@pytest.mark.parametrize("variables, expected", [
    ({}, "0640 0644"),
    ({"cassandra_config_mode": "0600", "cassandra_config_public_mode": "0640"}, "0600 0640"),
    ({"cassandra_config_mode": 0o600, "cassandra_config_public_mode": 0o640}, "0600 0640"),  # unquoted
])
def test_modes(tmp_path, variables, expected):
    assert modes(tmp_path, variables) == expected
