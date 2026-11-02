from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster refuses a DataStax Enterprise or ScyllaDB node before anything
# is written: read on the given nodes before the ring, and on every node found
# in the ring; one REFUSED block, under --check too. Rendered by Ansible.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

load_collection_filters()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
with open(os.path.join(TOP, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
FIND = next(p for p in PLAYS if p["name"] == "Find the cluster from the given nodes")
READ = next(p for p in PLAYS if p["name"] == "Read every node")
GIVEN_READ = next(t for t in FIND["tasks"] if t["name"] == "Read what runs on the given nodes")
GIVEN_STOP = next(t for t in FIND["tasks"] if t["name"] == "Refuse a DataStax Enterprise or ScyllaDB cluster")
NODE_READ = next(t for t in READ["tasks"] if t["name"] == "Read the running Cassandra (conf dir, Cassandra and Java versions)")
NODE_STOP = next(t for t in READ["tasks"] if t["name"] == "Refuse a DataStax Enterprise or ScyllaDB node")
SNIPPET = "lookup('ansible.builtin.file', playbook_dir ~ '/files/import_distribution.sh')"

DSE = "distribution=dse\ndistribution_version=6.8.42"
SCYLLA = "distribution=scylla\ndistribution_version=5.4.3-0.20240305.c0c3ad4ac5a1"
CASSANDRA = "distribution=cassandra\ndistribution_version="
ONLY = ("  This collection drives Apache Cassandra 4.0, 4.1 and 5.0 only (DSE and ScyllaDB have their own packages,"
        " services and configuration files).")


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def given(reads):
    """The first play's refusal, reads: {given node: its read's stdout, None when unreachable}."""
    hostvars = dict((h, {"import_cluster_distribution": {"stdout": r}} if r is not None else {}) for h, r in reads.items())
    variables = dict(GIVEN_STOP["vars"], import_cluster_given=list(reads), hostvars=hostvars)
    return render(GIVEN_STOP["ansible.builtin.assert"]["fail_msg"], **variables).split("\n")


def found(reads):
    """The second play's refusal, reads: {node: the stdout of its Cassandra read}."""
    hostvars = dict((h, {"import_cluster_running": {"stdout": r, "rc": 3}}) for h, r in reads.items())
    variables = dict(NODE_STOP["vars"], ansible_play_hosts=list(reads), hostvars=hostvars)
    return render(NODE_STOP["ansible.builtin.assert"]["fail_msg"], **variables).split("\n")


def test_the_given_nodes_are_read_before_the_ring():
    names = [t["name"] for t in FIND["tasks"]]
    assert names.index("Load the cassandra_service and cassandra_operation defaults (JMX access)") < names.index(GIVEN_READ["name"])
    assert names.index(GIVEN_READ["name"]) < names.index(GIVEN_STOP["name"]) < names.index("Read the ring")
    assert GIVEN_READ["check_mode"] is False and GIVEN_READ["failed_when"] is False
    assert SNIPPET in GIVEN_READ["ansible.builtin.shell"]
    # nothing known runs: nodetool's ReleaseVersion, the four numbers of a DataStax build
    assert "_cassandra_service_nodetool }} version" in GIVEN_READ["ansible.builtin.shell"]


def test_every_node_found_in_the_ring_is_read_too():
    names = [t["name"] for t in READ["tasks"]]
    assert names.index(NODE_READ["name"]) < names.index(NODE_STOP["name"]) < names.index("Work out the series and the conf dir")
    shell = NODE_READ["ansible.builtin.shell"]
    assert SNIPPET in shell and shell.index(SNIPPET) < shell.index("exit 3")
    assert NODE_READ["check_mode"] is False


def test_the_refusals_are_verdicts_said_once():
    for task in (GIVEN_STOP, NODE_STOP):
        assert task["run_once"] is True and task["vars"]["cassandra_output"] is True
        assert task["ansible.builtin.assert"]["quiet"] is True
        assert "when" not in task  # under --check too


def test_a_dse_cluster_is_refused_before_the_ring():
    assert given({"h-n1": DSE}) == [
        "REFUSED  import_cluster  nothing was written",
        "  h-n1 runs DataStax Enterprise 6.8.42: not supported.",
        ONLY]


def test_a_scylla_node_found_in_the_ring_is_refused():
    assert found({"h-n1": "config=/etc/cassandra/cassandra.yaml\nversion=4.1.5\n" + CASSANDRA,
                  "h-n2": SCYLLA + "\nrelease="}) == [
        "REFUSED  import_cluster  nothing was written",
        "  h-n2 runs ScyllaDB 5.4.3: not supported.",
        ONLY]


def test_a_datastax_build_by_its_release_version():
    assert given({"h-n1": "distribution=\ndistribution_version=\nrelease=4.0.0.6816"})[1] == (
        "  h-n1 runs a DataStax Enterprise build (ReleaseVersion 4.0.0.6816): not supported.")


def test_apache_cassandra_nodes_are_not_refused():
    assert given({"h-n1": CASSANDRA, "h-n2": None}) == [""]  # h-n2 unreachable: nothing read, nothing refused
    assert given({"h-n1": "distribution=\ndistribution_version=\nrelease="}) == [""]  # stopped: said later
    assert found({"h-n1": "config=/etc/cassandra/cassandra.yaml\nversion=4.1.5\n" + CASSANDRA}) == [""]
