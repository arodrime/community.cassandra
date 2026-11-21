from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# remove_dead_node's "Check the dead node" refusal, its expressions read from the playbook and rendered by Ansible.

import os
import warnings

import pytest
import yaml

from ansible.plugins.loader import init_plugin_loader

from ansible_collections.community.cassandra.tests.unit.helpers import TOP, render_with
from ansible_collections.community.cassandra.tests.unit.task_files import find_play, find_task

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

with open(os.path.join(TOP, "playbooks", "remove_dead_node.yml"), encoding="utf-8") as f:
    PLAY = find_play(yaml.safe_load(f), "Remove the dead node")
CHECK = find_task(PLAY, "Check the dead node")
RING = {"cluster_status": {"dc1": {"nodes": [{"address": "10.0.0.1", "status": "D", "state": "N"}]}}}


@pytest.mark.parametrize("host_vars", [
    {"ansible_host": 167772161},  # an integer IPv4 (YAML int): ssh takes it
    {"cassandra_extra_settings": {"broadcast_address": 167772161}, "ansible_host": "10.0.0.8 "},
])
def test_integer_address_not_in_ring(host_vars):
    # ansible-core 2.19+ keeps the int of a single-expression template: the refusal still says why
    variables = dict(PLAY["vars"], **CHECK["vars"])
    variables.update(cassandra_target_nodes="n8", cassandra_dead_ring=RING, groups={"all": ["n1", "n8"]},
                     hostvars={"n1": {"ansible_host": "10.0.0.1"}, "n8": host_vars})
    assert render_with(CHECK["ansible.builtin.assert"]["fail_msg"], variables) == (
        "n8 (167772161, from the inventory) is not in the ring: give its ring address or host ID in"
        " cassandra_target_nodes (nothing to do if it was removed already).")
