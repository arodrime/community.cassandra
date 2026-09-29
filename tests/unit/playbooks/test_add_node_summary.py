from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node: the screen shown before the confirmation says whether the new
# nodes get Medusa, and with which fqdn (their folder in the backups). The
# expressions are read from the playbook and rendered by Ansible.

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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "add_node.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
TASK = [t for p in PLAYS for t in p.get("tasks", []) if t.get("name") == "Show what the add will do"][0]


def summary(**inventory):
    new = {"_cassandra_preflight": {"address": "10.0.0.7", "cassandra_dc": "dc1", "cassandra_rack": "r1",
                                    "cassandra_cluster_name": "Orders", "cassandra_version": "50x"},
           "ansible_facts": {"hostname": "node7"}}
    new.update(inventory.pop("node7", {}))
    variables = {
        "hostvars": {"node7": new, "node1": {"cassandra_preflight_describe": {"stdout": ""}}},
        "_new": ["node7"], "_joining": [], "_existing": ["node1"], "ansible_play_hosts_all": ["node7"],
        "cassandra_add_node_plan": {"estimate": [], "cleanup": {}, "scope": {}, "warnings": []},
        "cassandra_stream_check_interval": 300, "cassandra_stream_stall_checks": 3, "_cassandra_session_warning": "",
    }
    variables.update(inventory)
    variables.update((k, trust_as_template(v)) for k, v in TASK["vars"].items())
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(TASK["vars"]["_summary"]))


def test_medusa_off_by_default():
    assert ", Medusa off\n" in summary()
    assert "Medusa fqdn" not in summary()


@pytest.mark.parametrize("inventory, line, fqdn", [
    ({"cassandra_medusa_version": "0.30.1", "cassandra_medusa_venv": "/srv/tools/venv", "cassandra_medusa_fqdn_domain": "db.example.internal"},
     "Medusa on (0.30.1 in /srv/tools/venv)", "node7.db.example.internal"),
    ({}, "Medusa on (in /opt/cassandra-medusa)", "(Medusa works it out)"),
    ({"cassandra_medusa_venv": "", "node7": {"cassandra_medusa_fqdn": "n7.example.org"}},
     "Medusa on (in the system Python)", "n7.example.org"),
])
def test_medusa_on(inventory, line, fqdn):
    text = summary(cassandra_medusa_enabled=True, **inventory)
    assert line in text
    assert "node7 (10.0.0.7): datacenter dc1, rack r1, Medusa fqdn %s\n" % fqdn in text
