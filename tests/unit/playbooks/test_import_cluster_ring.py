from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster reading the ring: nodetool must read the JMX password file
# (cassandra's, 0400), and a failure must say why.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name):
    return next(t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == name)


def test_nodetool_runs_with_become():
    # every play that runs nodetool, the ring read included
    for play in PLAYS:
        if "nodetool" in yaml.safe_dump(play) or "cassandra_status" in yaml.safe_dump(play):
            assert play.get("become") is True, play["name"]


def test_inventory_written_without_become():
    # run with -b for the nodes: sudo on the controller would fail (password) or write root's files
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    assert write.get("become") is False


def test_failure_shows_each_node_error():
    stop = task("Stop if no node answered")
    hostvars = {
        "n1": {"import_cluster_ring": {"failed": True, "msg": "Unable to determine Cassandra version: ",
                                       "stderr": "error: ******** (Permission denied)\n-- StackTrace --\n..."}},
        "n2": {"import_cluster_ring": {"unreachable": True}},
    }
    variables = {"import_cluster_given": ["n1", "n2"], "hostvars": hostvars}
    templar = Templar(loader=DataLoader(), variables=variables)
    errors = templar.template(trust_as_template(stop["vars"]["_errors"]))
    assert errors == ["n1: Unable to determine Cassandra version:  error: ******** (Permission denied)",
                      "n2: unreachable"]
