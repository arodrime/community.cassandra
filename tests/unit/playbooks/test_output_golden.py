from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The status and health_check playbooks run for real by ansible-playbook with
# the ops stdout callback, on 3 local hosts whose nodetool is a script that
# prints a ring (one node down): the whole output compared.

import os
import socket
import subprocess
import sys

import pytest

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))
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


def run(tmp_path, playbook, down=True, *args):
    answers = tmp_path / "nodetool"
    answers.mkdir(exist_ok=True)
    (answers / "version.txt").write_text("ReleaseVersion: 4.1.5\n")
    (answers / "statusgossip.txt").write_text("running\n")
    (answers / "statusbinary.txt").write_text("running\n")
    with open(os.path.join(FIXTURES, "nodetool_netstats_normal.txt")) as f:
        (answers / "netstats.txt").write_text(f.read())
    (answers / "describecluster.txt").write_text(DESCRIBE)
    (answers / "status.txt").write_text(STATUS % ("D" if down else "U"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "nodetool").write_text(NODETOOL % (answers, answers))
    (bin_dir / "nodetool").chmod(0o755)
    (tmp_path / "hosts.ini").write_text(INVENTORY)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
               ANSIBLE_DEPRECATION_WARNINGS="0", PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "")))
    env.pop("ANSIBLE_CONFIG", None)
    env.pop("CASSANDRA_CLUSTER", None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.ini"),
            "community.cassandra.%s" % playbook, "-e", "ansible_python_interpreter=" + sys.executable] + list(args)
    proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          check=False, cwd=str(tmp_path), timeout=600)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


RING = [
    "Datacenter: dc1",
    "  --  Address       Load       Tokens  Owns   Host ID                               Rack    Inventory",
    "  UN  10.100.100.5  71.84 MiB  16      76.0%  ca0e3575-b45a-487b-a323-2c021fbc16a3  rack_c  node3",
    "  UN  10.100.100.3  94.39 MiB  16      64.7%  76108dea-c527-4c5d-afff-1055bfa15b3f  rack_a  node1",
]


def test_status_with_a_node_down(tmp_path):
    rc, output = run(tmp_path, "status")
    assert rc == 0, output
    assert output.splitlines() == [
        "my_cluster  1 DOWN  (seen from node1, ring = inventory, 3 nodes)"] + RING + [
        u"  DN  10.100.100.4  94.41 MiB  16      59.3%  4fbaaa06-f1fc-4bb1-98e5-3fbe3fdb8e51  rack_b  node2      ← down",
        "  rack_a: 1 node, 1 up, 0 down; load 94.4 MiB",
        "  rack_b: 1 node, 0 up, 1 down; load 94.4 MiB",
        "  rack_c: 1 node, 1 up, 0 down; load 71.8 MiB",
        "  dc1: 3 nodes, 2 up, 1 down; load 260.6 MiB",
    ]


def test_status_all_up(tmp_path):
    rc, output = run(tmp_path, "status", False)
    assert rc == 0, output
    lines = output.splitlines()
    assert lines[0] == "my_cluster  OK  (seen from node1, ring = inventory, 3 nodes)"
    assert lines[-1] == "  dc1: 3 nodes, 3 up, 0 down; load 260.6 MiB"


def test_health_check_a_node_down(tmp_path, port):
    rc, output = run(tmp_path, "health_check", True, "-e", "cassandra_storage_port=%d" % port,
                     "-e", "cassandra_native_transport_port=%d" % port)
    assert rc != 0, output  # for scheduling
    assert output.splitlines() == [
        "NOT HEALTHY  my_cluster  3 nodes checked, 1 problem",
        "  ring:  node2 10.100.100.4 (rack_b) DN   seen from node1..node3",
        "TO DO",
        "  1. start Cassandra on node2 (its server first if it is down); a node that can't be recovered: replace_node",
    ]


def test_health_check_healthy(tmp_path, port):
    rc, output = run(tmp_path, "health_check", False, "-e", "cassandra_storage_port=%d" % port,
                     "-e", "cassandra_native_transport_port=%d" % port)
    assert rc == 0, output
    assert output.splitlines() == ["HEALTHY  my_cluster  3 nodes  3 UN, schema agreed, no streams, ports open"]
