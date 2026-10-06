from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A run without root on the nodes (no -b) stops before any node is touched:
# the real rolling_restart playbook, run by ansible-playbook on a local host
# as this (non-root) user, fails at the root check, before any drain. And a
# restart writes the systemd unit before it drains the node.

import os
import subprocess
import sys

import pytest
import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.mark.skipif(os.geteuid() == 0, reason="the run is root")
def test_rolling_restart_without_become_stops_before_any_drain(tmp_path):
    inventory = tmp_path / "hosts.ini"
    inventory.write_text("[cassandra]\nnode1 cassandra_dc=dc1 cassandra_rack=rack1\n")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_BECOME="False")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(inventory), "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable, "-e", "cassandra_operation_confirm=false",
            "community.cassandra.rolling_restart"]
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=300, check=False)
    out = result.stdout.decode(errors="replace")
    assert result.returncode != 0, out
    assert ("These operations need root on the nodes (node1 without it): add -b (or become = true in ansible.cfg). "
            "Nothing was changed.") in out
    assert "Drain the node" not in out


def test_the_root_check_comes_before_any_node_action():
    nodes = load("playbooks", "preflight.yml")[1]["tasks"]
    names = [t["name"] for t in nodes]
    assert names.index("Check the run has root on the nodes") == names.index("Every node answers") + 1
    assert nodes[names.index("Check the run has root on the nodes")]["ansible.builtin.include_role"]["tasks_from"] == \
        "root_check.yml"
    start = [p for p in load("playbooks", "start_rack.yml") if p["name"] == "Start the rack"][0]["tasks"]
    assert start[0]["ansible.builtin.include_role"]["tasks_from"] == "root_check.yml"
    # every playbook that changes nodes goes through preflight (or is start_rack)
    readonly = {"health_check.yml", "import_cluster.yml", "preflight.yml", "start_rack.yml", "status.yml"}
    for name in sorted(os.listdir(os.path.join(TOP, "playbooks"))):
        if name.endswith(".yml") and name not in readonly:
            plays = load("playbooks", name)
            assert any(p.get("ansible.builtin.import_playbook") == "community.cassandra.preflight" for p in plays), name


def test_a_restart_writes_the_unit_before_draining():
    tasks = load("roles", "cassandra_service", "tasks", "action_restart.yml")
    assert [t["name"] for t in tasks] == ["Install the cassandra systemd unit", "Drain the node",
                                          "Restart it and wait until it has joined"]
    assert tasks[0]["ansible.builtin.include_tasks"] == "unit.yml"
    assert tasks[2]["vars"]["_cassandra_service_unit_written"] is True
    main = load("roles", "cassandra_service", "tasks", "main.yml")
    unit = [t for t in main if t["name"] == "Install the cassandra systemd unit"][0]
    assert unit["ansible.builtin.include_tasks"] == "unit.yml"
    assert unit["when"] == "not _cassandra_service_unit_written | default(false) | bool"
