from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# decommission_node, add_node, add_datacenter: the nodes before this one are
# done, except under --check (none was removed or added: the health checks of
# the next node expect the ring as it is). decommission_node reads the replication
# under --check too, and a cqlsh refusal shows its error.

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


def gone(check_mode, host, states=("normal", "normal")):
    with open(PLAYBOOK, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    task = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Remove this node")
    variables = {"ansible_check_mode": check_mode, "ansible_play_hosts_all": ["n2", "n5"], "inventory_hostname": host,
                 "hostvars": {"n2": {"cassandra_leaving_node": {"state": states[0]}},
                              "n5": {"cassandra_leaving_node": {"state": states[1]}}}}
    return int(Templar(loader=DataLoader(), variables=variables).template(trust_as_template(task["vars"]["_gone"])))


def test_the_nodes_before_are_gone():
    assert (gone(False, "n2"), gone(False, "n5")) == (0, 1)


def test_none_is_gone_under_check():
    assert (gone(True, "n2"), gone(True, "n5")) == (0, 0)


def test_under_check_the_ones_an_earlier_run_removed_are_gone():
    # --check -e cassandra_rolling_resume=true after a run that removed n2
    assert gone(True, "n5", states=("decommissioned", "normal")) == 1
    assert gone(True, "n5", states=("leaving", "normal")) == 0


def add_node_pending(check_mode, host):
    with open(os.path.join(os.path.dirname(PLAYBOOK), "add_node.yml"), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    task = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Add this node")
    variables = {"ansible_check_mode": check_mode, "ansible_play_hosts_all": ["n2", "n5"], "inventory_hostname": host,
                 "hostvars": {"n2": {"cassandra_new_node_state": "new"}, "n5": {"cassandra_new_node_state": "new"}}}
    return int(Templar(loader=DataLoader(), variables=variables).template(trust_as_template(task["vars"]["_pending"])))


def test_add_node_pending_nodes():
    # the nodes before this one joined, except under --check (none was added)
    assert (add_node_pending(False, "n2"), add_node_pending(False, "n5")) == (2, 1)
    assert (add_node_pending(True, "n2"), add_node_pending(True, "n5")) == (2, 2)


def test_add_datacenter_done_nodes():
    with open(os.path.join(os.path.dirname(PLAYBOOK), "add_datacenter.yml"), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    task = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Add this node")
    for check_mode, expected in ((False, 1), (True, 0)):
        variables = {"ansible_check_mode": check_mode, "ansible_play_hosts_all": ["n2", "n5"], "inventory_hostname": "n5"}
        assert int(Templar(loader=DataLoader(), variables=variables).template(trust_as_template(task["vars"]["_done"]))) == expected


def test_decommission_reads_the_replication_under_check():
    # read-only: --check refuses a datacenter left with too few nodes (and needs the CQL credentials) too
    with open(PLAYBOOK, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    tasks = [t for p in plays for b in p.get("tasks", []) for t in [b] + b.get("block", [])]
    read = next(t for t in tasks if t.get("name") == "Read the keyspaces' replication from a node that stays")
    assert read["check_mode"] is False and read["changed_when"] is False


def refusal(result):
    with open(PLAYBOOK, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    record = next(t for p in plays for b in p.get("tasks", []) for t in b.get("rescue", [])
                  if t.get("name") == "Record the refusal")
    variables = dict(ansible_failed_result=result)
    variables["_err"] = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(record["vars"]["_err"]))
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(record["ansible.builtin.set_fact"]["cassandra_op_result"]))


def test_decommission_refusal_names_cqlsh_error_and_credentials():
    err = ("Using ssl: False\nConnection error: ('Unable to connect to any servers', {'10.0.0.1:9042': "
           "AuthenticationFailed('Remote end requires authentication',)})")
    out = refusal({"msg": "module execution failed", "err": err})
    assert out.startswith("decommission refused: module execution failed: Connection error:")
    assert "check cassandra_cql_username and cassandra_cql_password" in out
    assert refusal({"msg": ["a5-n1 is in cassandra_seeds"]}) == "decommission refused: a5-n1 is in cassandra_seeds"
