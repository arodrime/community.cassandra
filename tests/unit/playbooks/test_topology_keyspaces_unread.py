from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# topology: a removal with the keyspaces' replication not read is refused, also once the none of the read is
# shared by set_fact ('' before ansible-core 2.19). The playbook's tasks, run for real by ansible-playbook.

import json
import os
import subprocess

import pytest
import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import LOCAL, run_playbook

pytestmark = pytest.mark.playbook

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

with open(os.path.join(TOP, "playbooks", "topology.yml"), encoding="utf-8") as _f:
    PLAN = next(p for p in yaml.safe_load(_f) if p.get("name") == "Work out the plan and confirm it")
TASKS = dict((t.get("name"), t) for t in PLAN["tasks"])
UNREAD = "the keyspaces' replication could not be read"


def run(tmp_path, keyspaces):
    play = {"hosts": "all", "gather_facts": False,
            "vars": {"_tp_replication": PLAN["vars"]["_tp_replication"], "_tp_cql_node": "node1", "_tp_todo": True,
                     "_tp_cluster": "c",
                     "cassandra_topology_plan": {"problems": [], "in_ring": ["node3"], "nodes_left": {"dc1": 2}}},
            "tasks": [{"name": "Read", "ansible.builtin.set_fact": {"cassandra_keyspaces": keyspaces}},
                      TASKS["Share the replication with every node"], TASKS["Refuse a plan that can't be done"],
                      {"name": "Remove", "ansible.builtin.debug": {"msg": "WENT ON"}}]}
    (tmp_path / "plan.yml").write_text(json.dumps([play]))
    (tmp_path / "hosts.ini").write_text("node1\n")
    env = dict(ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_CALLBACK_RESULT_FORMAT="yaml")
    return run_playbook(["-i", str(tmp_path / "hosts.ini")] + LOCAL + [str(tmp_path / "plan.yml")], env, ["ANSIBLE_CONFIG"],
                        cwd=tmp_path, stdin=subprocess.DEVNULL)


def test_not_read_refused(tmp_path):
    rc, out = run(tmp_path, None)
    assert rc != 0 and "WENT ON" not in out and UNREAD in out, out


@pytest.mark.parametrize("rf, goes_on", [(2, True), (3, False)])
def test_read_checked(tmp_path, rf, goes_on):
    rc, out = run(tmp_path, {"ks": {"class": "NetworkTopologyStrategy", "rf": {"dc1": rf}}})
    assert UNREAD not in out, out
    assert (rc == 0 and "WENT ON" in out) if goes_on else \
        (rc != 0 and "ks needs 3 replicas in dc1, which would keep 2 node(s)" in out), out
