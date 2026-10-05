from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# decommission_node: the nodes before this one are gone, except under --check
# (none was removed: the health checks of the next node expect the whole ring).

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "decommission_node.yml")


def gone(check_mode, host):
    with open(PLAYBOOK, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    task = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Remove this node")
    variables = {"ansible_check_mode": check_mode, "ansible_play_hosts_all": ["n2", "n5"], "inventory_hostname": host}
    return int(Templar(loader=DataLoader(), variables=variables).template(trust_as_template(task["vars"]["_gone"])))


def test_the_nodes_before_are_gone():
    assert (gone(False, "n2"), gone(False, "n5")) == (0, 1)


def test_none_is_gone_under_check():
    assert (gone(True, "n2"), gone(True, "n5")) == (0, 0)
