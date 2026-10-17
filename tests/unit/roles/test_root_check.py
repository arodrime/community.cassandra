from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A run without root on the nodes stops before any node is touched: the real
# preflight playbook (which asks for root itself), on a local host whose
# inventory turns become off, run as this (non-root) user, fails at the root
# check.

import os
import subprocess
import sys

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.mark.skipif(os.geteuid() == 0, reason="the run is root")
def test_preflight_without_become_stops_at_the_root_check(tmp_path):
    inventory = tmp_path / "hosts.ini"
    inventory.write_text("[cassandra]\nnode1 cassandra_dc=dc1 cassandra_rack=rack1 ansible_become=false\n"
                         "[cassandra_dc1]\nnode1\n")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_BECOME="False")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(inventory), "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable,
            "community.cassandra.preflight"]
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=300, check=False)
    out = result.stdout.decode(errors="replace")
    assert result.returncode != 0, out
    assert ("These operations need root on the nodes (node1 without it): the playbook asks for it (become), check "
            "the inventory or ansible.cfg does not turn it off there (ansible_become: false, another ansible_become_user). "
            "Nothing was changed.") in out


def test_the_root_check_comes_before_any_node_action():
    nodes = load("playbooks", "preflight.yml")[1]["tasks"]
    names = [t["name"] for t in nodes]
    assert names.index("Check the run has root on the nodes") == names.index("Every node answers") + 1
    assert nodes[names.index("Check the run has root on the nodes")]["ansible.builtin.include_role"]["tasks_from"] == \
        "root_check.yml"
    # every playbook that changes nodes goes through preflight
    readonly = {"health_check.yml", "preflight.yml", "status.yml"}
    for name in sorted(n for n in os.listdir(os.path.join(TOP, "playbooks")) if n.endswith(".yml")):
        if name.endswith(".yml") and name not in readonly:
            plays = load("playbooks", name)
            assert any(p.get("ansible.builtin.import_playbook") == "community.cassandra.preflight" for p in plays), name


def test_every_node_without_root_is_named():
    check = [t for t in load("roles", "cassandra_operation", "tasks", "root_check.yml") if "ansible.builtin.assert" in t][0]
    hostvars = {"n1": {"cassandra_root_check": {"stdout": "0"}}, "n2": {"cassandra_root_check": {"stdout": "1000"}},
                "n3": {"cassandra_root_check": {"stdout": "0"}}, "n4": {"cassandra_root_check": {"stdout": "1001"}}}
    templar = Templar(loader=DataLoader(), variables={"hostvars": hostvars, "ansible_play_hosts": ["n1", "n2", "n3", "n4"]})
    assert templar.template(trust_as_template(check["vars"]["_not_root"])) == ["n2", "n4"]
    templar = Templar(loader=DataLoader(), variables={"hostvars": hostvars, "ansible_play_hosts": ["n1", "n3"]})
    assert templar.template(trust_as_template(check["vars"]["_not_root"])) == []
