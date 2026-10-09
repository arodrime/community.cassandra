from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The plays of the nodes with something to do (group_by groups): a list of
# hosts, empty when no node has anything to do (a host pattern naming a group
# that does not exist warns "Could not match supplied host pattern").

import os

import pytest
import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


@pytest.mark.parametrize("playbook, group", [("apply_config.yml", "cassandra_apply_config_True"),
                                             ("update_java.yml", "cassandra_update_java_True")])
def test_the_step_plays_take_a_list_of_hosts(playbook, group):
    with open(os.path.join(TOP, "playbooks", playbook), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    steps = [p for p in plays if group in str(p.get("hosts"))]
    assert len(steps) == 2
    for play in steps:
        assert ":&" not in play["hosts"]
        assert "groups['%s'] | default([])" % group in play["hosts"]
