from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The status and health_check playbooks run for real by ansible-playbook with
# the ops stdout callback, on 3 local hosts whose nodetool is a script that
# prints a ring (one node down): the whole output compared.

import os
import re
import socket
import subprocess
import sys

import pytest

from ansible_collections.community.cassandra.tests.unit.helpers import fake_nodetool, path_with, run_playbook

pytestmark = pytest.mark.playbook

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "plugins", "modules", "fixtures")

NODETOOL = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    version|status|statusgossip|statusbinary|netstats|describecluster)
      [ -f "%s/$a.txt" ] && { cat "%s/$a.txt"; exit 0; }
      echo "nodetool: Failed to connect to '127.0.0.1:7199'" >&2; exit 1;;
  esac
done
exit 2
"""

STATUS = """Datacenter: dc1
===============
Status=Up/Down
|/ State=Normal/Leaving/Joining/Moving
--  Address        Load       Tokens  Owns (effective)  Host ID                               Rack
UN  10.100.100.5   71.84 MiB  16      76.0%%             ca0e3575-b45a-487b-a323-2c021fbc16a3  rack_c
UN  10.100.100.3   94.39 MiB  16      64.7%%             76108dea-c527-4c5d-afff-1055bfa15b3f  rack_a
%sN  10.100.100.4   94.41 MiB  16      59.3%%             4fbaaa06-f1fc-4bb1-98e5-3fbe3fdb8e51  rack_b
"""

DESCRIBE = """Cluster Information:
\tName: my_cluster
\tSchema versions:
\t\t8b0d9a9c-4e6f-3c1b-9e1f-6f7b2a1c0d11: [10.100.100.3, 10.100.100.4, 10.100.100.5]
"""

INVENTORY = """[my_cluster]
node1 ansible_host=10.100.100.3 cassandra_dc=dc1 cassandra_rack=rack_a
node2 ansible_host=10.100.100.4 cassandra_dc=dc1 cassandra_rack=rack_b
node3 ansible_host=10.100.100.5 cassandra_dc=dc1 cassandra_rack=rack_c

[my_cluster_dc1:children]
my_cluster

[my_cluster:vars]
ansible_connection=local
cassandra_listen_address=127.0.0.1
cassandra_rpc_address=127.0.0.1
"""


@pytest.fixture
def port():
    """A port that answers, for the storage and CQL port checks."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    yield server.getsockname()[1]
    server.close()


def run(tmp_path, playbook, down=True, *args, **answers_given):
    answers = tmp_path / "nodetool"
    answers.mkdir(exist_ok=True)
    (answers / "version.txt").write_text("ReleaseVersion: 4.1.5\n")
    (answers / "statusgossip.txt").write_text("running\n")
    (answers / "statusbinary.txt").write_text("running\n")
    with open(os.path.join(FIXTURES, "nodetool_netstats_normal.txt")) as f:
        (answers / "netstats.txt").write_text(f.read())
    (answers / "describecluster.txt").write_text(answers_given.get("describecluster", DESCRIBE))
    (answers / "status.txt").write_text(STATUS % ("D" if down else "U"))
    bin_dir = tmp_path / "bin"
    fake_nodetool(bin_dir, NODETOOL % (answers, answers))
    (tmp_path / "hosts.ini").write_text(INVENTORY)
    env = dict(ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops", ANSIBLE_DEPRECATION_WARNINGS="0", PATH=path_with(bin_dir))
    args = ["-i", str(tmp_path / "hosts.ini"), "community.cassandra.%s" % playbook,
            "-e", "ansible_python_interpreter=" + sys.executable] + list(args)
    return run_playbook(args, env, ["ANSIBLE_CONFIG", "CASSANDRA_CLUSTER"], cwd=tmp_path, timeout=600, stdin=subprocess.DEVNULL)


# (not a terminal: 79 columns, the Host IDs cut to fit, a line says where they are whole)
RING = [
    "Datacenter: dc1",
    "",
    " Node   --  Address       Load       Tokens  Owns   Host ID      Rack",
    " node3  UN  10.100.100.5  71.84 MiB  16      76.0%  ca0e3575...  rack_c",
    " node1  UN  10.100.100.3  94.39 MiB  16      64.7%  76108dea...  rack_a",
]
CUT = "Host IDs shortened; full: -e cassandra_status_raw=true"
# when the ring was read (the controller's clock, its date and zone): "at 2026-11-16 14:02 CET"
SEEN = "  my_cluster  seen from node1 at YYYY-MM-DD HH:MM"
# the run's first line (no version: these playbooks read none); not repeated at the end
CLUSTER = "my_cluster  3 nodes  1 DC (dc1)"


def test_status_with_a_node_down(tmp_path):
    rc, output = run(tmp_path, "status")
    assert rc == 0, output
    # the verdict first, alone, between heavy rules, the cluster under it; the nodes by their name first
    assert ruled(output) == [
        CLUSTER,
        "",
        "====",
        "",
        "1 NODE DOWN: node2 (dc1/rack_b)",
        SEEN,
        "  ring and inventory: the same 3 nodes",
        "",
        "====",  # then the tables (a blank line each side of a rule)
        ""] + RING + [
        u" node2  DN  10.100.100.4  94.41 MiB  16      59.3%  4fbaaa06...  rack_b  \u2190 down",
        "",
        "  rack_a   1 node    1 up           load  94.4 MiB",
        "  rack_b   1 node    0 up, 1 down   load  94.4 MiB",
        "  rack_c   1 node    1 up           load  71.8 MiB",
        "  dc1      3 nodes   2 up, 1 down   load 260.6 MiB",
        "",
        CUT,
        "",
        "====",
    ]
    assert all(len(line) <= 80 for line in output.splitlines()), output


def test_status_all_up(tmp_path):
    rc, output = run(tmp_path, "status", False)
    assert rc == 0, output
    lines = ruled(output)
    assert lines[:7] == [CLUSTER, "", "====", "", "ALL 3 NODES UP", SEEN, "  ring and inventory: the same 3 nodes"]
    assert lines[-5:] == ["  dc1      3 nodes   3 up   load 260.6 MiB", "", CUT, "", "===="]


def ruled(output):
    """The lines (the blank ones at the end left out), each rule (as wide as the terminal) as 4 of its characters,
    the time the ring was seen as YYYY-MM-DD HH:MM (its zone checked: a name or an offset)."""
    return [re.sub(r" at \d{4}-\d\d-\d\d \d\d:\d\d (?:[A-Z]{2,5}|[+-]\d\d:\d\d)$", " at YYYY-MM-DD HH:MM",
                   re.sub(r"^([-=])\1{3,}$", lambda m: m.group(1) * 4, line)) for line in output.rstrip("\n").splitlines()]


# (the clocks left out: this host's time sync is not the test's; test_health_check_clocks shows the CLOCKS block)
def test_health_check_a_node_down(tmp_path, port):
    rc, output = run(tmp_path, "health_check", True, "-e", "cassandra_storage_port=%d" % port,
                     "-e", "cassandra_native_transport_port=%d" % port, "-e", "cassandra_clock_check=false")
    assert rc != 0, output  # for scheduling
    assert ruled(output) == [
        CLUSTER,
        "",
        "====",  # a recap: heavy rules around it, a blank line each side of a rule
        "",
        "NOT HEALTHY  my_cluster  3 nodes checked, 1 problem",
        "  ring:  node2 10.100.100.4 (rack_b) DN   seen from node1..node3",
        "TO DO",
        "  1. start Cassandra on node2 (its server first if it is down); a node that can't be recovered: replace_node",
        "",
        "====",
    ]


def test_health_check_healthy(tmp_path, port):
    rc, output = run(tmp_path, "health_check", False, "-e", "cassandra_storage_port=%d" % port,
                     "-e", "cassandra_native_transport_port=%d" % port, "-e", "cassandra_clock_check=false")
    assert rc == 0, output
    assert ruled(output) == [CLUSTER, "", "====", "", "HEALTHY  my_cluster  3 nodes  3 UN, schema agreed, no streams, ports open",
                             "", "===="]


def test_health_check_a_schema_disagreement(tmp_path, port):
    # describecluster of a real 4.1 cluster whose node2 missed a schema change: two versions, a problem
    with open(os.path.join(FIXTURES, "nodetool_describecluster_41_disagreement.txt")) as f:
        disagreement = f.read()
    rc, output = run(tmp_path, "health_check", False, "-e", "cassandra_storage_port=%d" % port,
                     "-e", "cassandra_native_transport_port=%d" % port, "-e", "cassandra_clock_check=false",
                     "-e", "_cassandra_health_schema_interval=0", describecluster=disagreement)
    assert rc != 0, output
    assert ruled(output) == [
        CLUSTER,
        "",
        "====",
        "",
        "NOT HEALTHY  my_cluster  3 nodes checked, 1 problem",
        "  schema:  disagreement: The cluster has not reached consensus on the schema: 2 versions,"
        " 4d51f0a3-0298-3f7b-8fe6-b50ea5158af7 on 172.19.0.2; 54e17321-3f2e-37ca-9b08-d91ba7bdd369 on 172.19.0.3",
        "",
        "====",
    ]


def test_health_check_with_a_node_unreachable_for_describecluster(tmp_path, port):
    # describecluster of a real 4.1 cluster listing a node UNREACHABLE: not a schema version; the check goes on
    # (a result key 'unreachable' would have made Ansible take the host itself for unreachable)
    with open(os.path.join(FIXTURES, "nodetool_describecluster_41_unreachable.txt")) as f:
        unreachable = f.read()
    rc, output = run(tmp_path, "health_check", False, "-e", "cassandra_storage_port=%d" % port,
                     "-e", "cassandra_native_transport_port=%d" % port, "-e", "cassandra_clock_check=false",
                     "-e", "_cassandra_health_schema_interval=0", describecluster=unreachable)
    assert rc == 0, output
    assert "HEALTHY  my_cluster  3 nodes  3 UN, schema agreed, no streams, ports open" in ruled(output), output


def test_health_check_a_disagreement_among_the_nodes_reached(tmp_path, port):
    # a real 5.0 cluster of 3: node3 UNREACHABLE (not counted), node1 and node2 on two schema versions: a problem
    with open(os.path.join(FIXTURES, "nodetool_describecluster_50_unreachable_disagreement.txt")) as f:
        described = f.read()
    rc, output = run(tmp_path, "health_check", False, "-e", "cassandra_storage_port=%d" % port,
                     "-e", "cassandra_native_transport_port=%d" % port, "-e", "cassandra_clock_check=false",
                     "-e", "_cassandra_health_schema_interval=0", describecluster=described)
    assert rc != 0, output
    assert ruled(output)[4:6] == [
        "NOT HEALTHY  my_cluster  3 nodes checked, 1 problem",
        "  schema:  disagreement: The cluster has not reached consensus on the schema: 2 versions,"
        " 25631fff-7ab7-3b7d-8360-c4340f95ea89 on 172.18.0.2; d03783d7-b468-3c1a-82f1-8e30b2edde8b on 172.18.0.3"
        " (unreachable, not counted: 172.18.0.4)"], output
