from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster without -i <node>,: the cluster -e cassandra_hosts or CASSANDRA_CLUSTER names is the one read
# (not every host of the inventory), and a cluster not in the inventory stops at once, saying how to import a
# new one (-i <node>,).

import os
import subprocess
import sys

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))

INVENTORY = """all:
  vars:
    ansible_connection: local
    ansible_become: false
  children:
    old_cluster:
      children:
        old_cluster_dc1:
          hosts:
            old1: {}
            old2: {}
      vars:
        cassandra_cluster_name: Old Cluster
    other:
      children:
        other_dc1:
          hosts:
            other1: {}
"""


def run(tmp_path, *args, **env_extra):
    (tmp_path / "inventories").mkdir(exist_ok=True)
    (tmp_path / "inventories" / "hosts.yml").write_text(INVENTORY)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
               ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_INVENTORY=str(tmp_path / "inventories"),
               ANSIBLE_PYTHON_INTERPRETER=sys.executable,
               PATH=os.pathsep.join(["/usr/bin", "/bin"]))  # no nodetool: the ring read fails on every given node
    env.pop("ANSIBLE_CONFIG", None)
    env.pop("CASSANDRA_CLUSTER", None)
    env.update(env_extra)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()",
            "community.cassandra.import_cluster", "-e", "import_cluster_dir=%s" % (tmp_path / "out")] + list(args)
    proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          check=False, cwd=str(tmp_path), timeout=300)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


NEW = ("give one of its nodes: ansible-playbook -i <node>, community.cassandra.import_cluster. Nothing was written.")


def test_a_cluster_not_in_the_inventory_stops_at_once(tmp_path):
    rc, output = run(tmp_path, CASSANDRA_CLUSTER="new_prod")
    assert rc != 0
    assert output.splitlines() == ["new_prod is not in the inventory (CASSANDRA_CLUSTER): to import a new cluster, "
                                   + NEW]
    rc, output = run(tmp_path, "-e", "cassandra_hosts=new_prod")
    assert rc != 0
    assert output.splitlines() == ["new_prod is not in the inventory: to import a new cluster, " + NEW]
    assert not (tmp_path / "out").exists()


def test_only_the_cluster_named_is_read(tmp_path):
    # by its group or its cluster name: its hosts only are given (the ring read fails on each: listed)
    for env in ({"CASSANDRA_CLUSTER": "old_cluster"}, {"CASSANDRA_CLUSTER": "Old Cluster"}):
        rc, output = run(tmp_path, **env)
        assert rc != 0
        assert "nodetool status failed on every given node" in output
        assert "old1: " in output and "old2: " in output and "other1" not in output, output
    rc, output = run(tmp_path, "-e", "cassandra_hosts=other")
    assert "other1: " in output and "old1" not in output, output


def test_cassandra_cluster_left_out_with_given_nodes(tmp_path):
    # -i <node>,: the nodes of a new cluster, whatever CASSANDRA_CLUSTER says
    rc, output = run(tmp_path, "-i", "new1,", "-c", "local", "-e", "ansible_become=false", CASSANDRA_CLUSTER="new_prod")
    assert rc != 0
    assert "not in the inventory" not in output and "new1: " in output, output


def test_no_node_answered_as_plain_text(tmp_path):
    # the refusal as it is: no task header, no JSON
    rc, output = run(tmp_path, CASSANDRA_CLUSTER="old_cluster")
    assert rc != 0
    lines = output.splitlines()
    assert lines[0] == "Reading the ring from old1, old2..."  # its phase line
    assert lines[1].startswith("nodetool status failed on every given node."), output
    assert sorted(line.split(":")[0] for line in lines[2:]) == ["old1", "old2"], output
    assert "TASK [" not in output and "FAILED!" not in output and "{" not in output


def test_the_lookup_error_itself_when_the_cluster_is_there(tmp_path):
    # a cluster in the inventory whose hosts the playbooks can't take: their own error, not a guess
    global INVENTORY  # pylint: disable=global-statement
    saved = INVENTORY
    INVENTORY = saved.replace("            old1: {}", "            old1: {cassandra_node_state: gone}")
    try:
        rc, output = run(tmp_path, CASSANDRA_CLUSTER="old_cluster")
    finally:
        INVENTORY = saved
    assert rc != 0
    assert "cassandra_node_state must be present or absent: old1 (gone)" in output, output
    assert "not in the inventory" not in output and "marked absent" not in output
