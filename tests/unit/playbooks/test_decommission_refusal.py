from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# decommission_node refuses before any change (a seed, too few nodes for the
# replicas, a CQL login refused...): one REFUSED block, "nothing was changed"
# under --check or not, each problem on its line, cqlsh's error by its cause
# (none of its text). Rendered by Ansible.

import os
import warnings

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
    init_plugin_loader()  # the cassandra_hosts lookup, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
with open(os.path.join(TOP, "playbooks", "decommission_node.yml"), encoding="utf-8") as f:
    PLAY = next(p for p in yaml.safe_load(f) if p.get("name") == "Check the nodes to remove")
CHECK = next(t for t in PLAY["tasks"] if t.get("name") == "Check this node")
RECORD, STOP = CHECK["rescue"]
FORGET = CHECK["block"][0]
REPLICAS = next(t for t in CHECK["block"] if t.get("name") == "Every datacenter keeps enough nodes for its replicas")
GROUPS = {"all": ["n1", "n2", "n3"], "prod": ["n1", "n2", "n3"]}


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def refusal(result, host="n3", task=None):
    variables = dict(RECORD["vars"], ansible_failed_result=result, inventory_hostname=host, _staying=["n1", "n2"],
                     groups=GROUPS, cassandra_hosts="prod", ansible_failed_task=task or {"name": "x"})
    return render(RECORD["ansible.builtin.set_fact"]["_cassandra_decommission_refusal"], **variables)


def block(refusals, cluster="Prod"):
    hostvars = dict((h, {"_cassandra_decommission_refusal": r}) for h, r in refusals.items())
    variables = dict(STOP["vars"], ansible_play_hosts_all=list(refusals) + ["n9"], hostvars=hostvars,
                     cassandra_cluster_name=cluster)
    return render(STOP["ansible.builtin.fail"]["msg"], **variables).split("\n")


def test_a_refusal_is_one_refused_block():
    assert STOP["vars"]["cassandra_output"] is True and STOP["run_once"] is True
    assert CHECK["vars"]["cassandra_output_rescued"] is True  # no "(the playbook handles it)" line before it
    assert [t["name"] for t in CHECK["rescue"]] == ["Record the refusal", "Stop here"]  # no FAILED recap
    # a run_once check refuses every node the same: said once
    same = ["Removing n3 leaves too few nodes:", "  app needs 3 replicas in dc1, which would keep 2 node(s)", "Lower..."]
    assert block({"n3": same, "n4": same}) == [
        "REFUSED  decommission_node  Prod  nothing was changed",
        "  Removing n3 leaves too few nodes:",
        "    app needs 3 replicas in dc1, which would keep 2 node(s)",
        "  Lower..."]
    assert block({"n3": ["n3 is in cassandra_seeds: ..."], "n4": ["n4 is in cassandra_seeds: ..."]})[1:] == [
        "  n3 is in cassandra_seeds: ...", "  n4 is in cassandra_seeds: ..."]


def test_the_replica_problems_one_per_line():
    problems = ["system_auth needs 3 replicas in dc1, which would keep 2 node(s)",
                "system_distributed (SimpleStrategy, RF 3) needs 3 nodes, the cluster would keep 2"]
    msg = render(REPLICAS["ansible.builtin.assert"]["fail_msg"], _leaving=["n3"], _problems=problems)
    assert msg == ["Removing n3 leaves too few nodes:", "  " + problems[0], "  " + problems[1],
                   "Lower the replication first (ALTER KEYSPACE, then repair), or set cassandra_decommission_force: true:"
                   " those replicas are then lost, and QUORUM can fail."]
    assert refusal({"msg": msg}) == msg  # it names the node already


def test_a_cql_login_refused_by_its_cause_no_driver_dump():
    err = ("Using ssl: False\nConnection error: ('Unable to connect to any servers', {'10.0.0.1:9042': "
           "AuthenticationFailed('Failed to authenticate to 10.0.0.1:9042: Error from server: code=0100 [Bad credentials] "
           "message=\"Provided username opsadmin and/or password are incorrect\"')})")
    said = refusal({"msg": "module execution failed", "err": err})
    assert said == ["n1 refused the CQL login (CQL authentication is on): check cassandra_cql_username and"
                    " cassandra_cql_password (e.g. group_vars/prod/secrets.yml, vaulted)."]
    assert refusal({"msg": "module execution failed", "err": "AuthenticationFailed('Remote end requires authentication')"}) == said
    # another cqlsh failure: its cause, then what to check
    said = refusal({"msg": "module execution failed", "err": "Connection error: ('Unable to connect to any servers', "
                                                             "{'10.0.0.1:9042': ConnectionRefusedError(111)})"})
    assert said == ["The keyspaces' replication could not be read with cqlsh on n1 (no connection): check that Cassandra"
                    " runs there and takes CQL connections, then run this again."]


def test_the_node_named_when_the_message_does_not():
    assert refusal({"msg": ["n3 is in cassandra_seeds: take it out"]}) == ["n3 is in cassandra_seeds: take it out"]
    assert refusal({"msg": "dc2 would keep no seed"}) == ["n3: dc2 would keep no seed"]
    assert refusal({"msg": "n30 is a seed"}) == ["n3: n30 is a seed"]  # n30 is not n3
    # a check of the whole cluster (run once): about the cluster, no node named
    assert refusal({"msg": "dc2 would keep no seed"}, task={"name": "Every datacenter keeps a seed", "run_once": True}) == [
        "dc2 would keep no seed"]


def test_only_the_nodes_that_failed_say_why():
    # any_errors_fatal brings the nodes that passed into the rescue too (no failure of their own): nothing for
    # them, and none left from an earlier play (topology runs add_node's first)
    assert FORGET["name"] == "Forget an earlier failure"
    assert FORGET["ansible.builtin.set_fact"] == {"ansible_failed_result": {}, "ansible_failed_task": {}}
    assert render("{{ %s }}" % RECORD["when"], ansible_failed_result={}) is False
    assert render("{{ %s }}" % RECORD["when"], ansible_failed_result={"msg": "n3 is in cassandra_seeds"}) is True
