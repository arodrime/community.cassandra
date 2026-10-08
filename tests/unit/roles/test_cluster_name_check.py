from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service cluster_name.yml, run by ansible-playbook on local hosts
# with a stand-in nodetool: the running cluster's name must be the inventory's
# cassandra_cluster_name before a playbook changes anything. A run whose
# inventory was not loaded (the role default 'Test Cluster') is told so first;
# the package's stock instance is only suggested when the node runs 'Test
# Cluster'. The name is read on the first candidate that answers (a node left
# out by --limit, a node of another rack for start_rack).

import os
import re
import stat
import subprocess
import sys

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

NODETOOL = """#!/bin/sh
echo "$FAKE_NODE $*" >> "$FAKE_LOG"
case "$FAKE_DOWN" in
  yes) echo "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'." >&2; exit 1;;
esac
printf 'Cluster Information:\\n\\tName: %s\\n\\tSnitch: org.apache.cassandra.locator.GossipingPropertyFileSnitch\\n' "$FAKE_NAME"
"""


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(tmp_path, inventory_name, node_name, candidates=("gone", "n1", "n2", "n3"), down=("n1",), describe=None, extra=None):
    """Runs cluster_name.yml on n0 and n9 over candidates (gone is unreachable, those in down refuse nodetool),
    then another play: (rc, output, the hosts asked, the hosts that reached the next play)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "nodetool").write_text(NODETOOL)
    lines = ["gone ansible_host=192.0.2.1 ansible_connection=ssh ansible_ssh_common_args='-o ConnectTimeout=2'"]
    for host in ("n0", "n1", "n2", "n3", "n9"):
        # each host's own python, which tells the stand-in nodetool which host runs it
        python = bin_dir / ("python-" + host)
        python.write_text('#!/bin/sh\nFAKE_NODE=%s FAKE_DOWN=%s exec %s "$@"\n' % (host, "yes" if host in down else "no", sys.executable))
        lines.append("%s ansible_connection=local ansible_python_interpreter=%s" % (host, python))
    for f in bin_dir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "hosts.ini").write_text("[cassandra]\n" + "\n".join(lines) + "\n")
    play = [{"hosts": "n0,n9", "gather_facts": False, "vars": dict({"cassandra_cluster_name": inventory_name}, **(extra or {})),
             "tasks": [{"ansible.builtin.include_role": {"name": "community.cassandra.cassandra_service",
                                                         "tasks_from": "cluster_name.yml"},
                        "vars": dict({"cassandra_service_cluster_candidates": list(candidates)},
                                     **({"cassandra_service_cluster_describe": describe} if describe else {}))}]},
            {"hosts": "n0,n9", "gather_facts": False,
             "tasks": [{"ansible.builtin.debug": {"msg": "NEXT PLAY ON {{ inventory_hostname }}"}}]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    log = tmp_path / "asked.log"
    log.write_text("")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_HOST_KEY_CHECKING="False", ANSIBLE_TIMEOUT="3",
               PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "")), FAKE_LOG=str(log), FAKE_NAME=node_name)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.ini"),
            str(tmp_path / "play.yml")]
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=300, check=False)
    asked = [line.split()[0] for line in log.read_text().splitlines()]
    out = " ".join(result.stdout.decode(errors="replace").split())
    return result.returncode, out, asked, sorted(set(re.findall(r"NEXT PLAY ON (n\d)", out)))


def test_an_inventory_not_loaded_is_named_first(tmp_path):
    rc, out, asked, went_on = run(tmp_path, "Test Cluster", "my_cluster")
    assert rc != 0, out
    assert ("n2 runs Cassandra for cluster 'my_cluster', not 'Test Cluster' (cassandra_cluster_name). Nothing was changed. "
            "cassandra_cluster_name is the role default 'Test Cluster': are the inventory's group_vars loaded? Check with "
            "ansible-inventory -i <inventory> --host n2 (group_vars are read next to the inventory file or directory given, "
            "not in its subdirectories).") in out
    assert "reset_node" not in out and "empty" not in out
    # the first that answers: gone unreachable, n1 refused, n3 never asked
    assert asked == ["n1", "n2"]
    # every host of the play stops there
    assert went_on == []


def test_a_stock_instance_is_only_suggested_when_the_node_runs_the_stock_name(tmp_path):
    rc, out, asked, went_on = run(tmp_path, "my_cluster", "Test Cluster", down=())
    assert rc != 0, out
    assert ("n1 runs Cassandra for cluster 'Test Cluster', not 'my_cluster' (cassandra_cluster_name). Nothing was changed. "
            "'Test Cluster' is the package's stock name: is n1 a new host the package started with its stock config? "
            "Check it is the right host; if it never joined 'my_cluster', the reset_node playbook makes it ready to join.") in out
    assert "group_vars" not in out
    assert asked == ["n1"]


def test_another_name(tmp_path):
    rc, out, asked, went_on = run(tmp_path, "my_cluster", "other_cluster")
    assert rc != 0, out
    assert ("n2 runs Cassandra for cluster 'other_cluster', not 'my_cluster' (cassandra_cluster_name). Nothing was changed. "
            "Check the inventory (-i) and cassandra_cluster_name.") in out
    assert went_on == []


def test_the_same_name_passes(tmp_path):
    rc, out, asked, went_on = run(tmp_path, "my_cluster", "my_cluster")
    assert rc == 0, out
    assert asked == ["n1", "n2"]
    assert "WARNING" not in out
    # an unreachable candidate is passed over, the play goes on
    assert went_on == ["n0", "n9"]


def test_no_node_answering_is_said(tmp_path):
    rc, out, asked, went_on = run(tmp_path, "my_cluster", "my_cluster", down=("n1", "n2", "n3"))
    assert rc == 0, out
    assert "WARNING no node of the cluster answered nodetool (gone: " in out  # spaces collapsed
    assert ("; n3: nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'.): the cluster "
            "name is not checked against cassandra_cluster_name (my_cluster).") in out
    assert asked == ["n1", "n2", "n3"] and went_on == ["n0", "n9"]


def test_no_node_answering_under_the_default_name_is_refused(tmp_path):
    # an inventory not loaded has no JMX login either: no node answers
    rc, out, asked, went_on = run(tmp_path, "Test Cluster", "my_cluster", down=("n1", "n2", "n3"))
    assert rc != 0, out
    assert ("The cluster name could not be read (no node answered nodetool) and cassandra_cluster_name is the role default "
            "'Test Cluster': are the inventory's group_vars loaded? Check with ansible-inventory -i <inventory> --host n0") in out
    assert went_on == []
    rc, out, asked, went_on = run(tmp_path / "accept", "Test Cluster", "my_cluster", down=("n1", "n2", "n3"),
                                  extra={"cassandra_accept_default_identity": True})
    assert rc == 0 and went_on == ["n0", "n9"], out


def test_no_node_to_read_from_is_refused_when_required(tmp_path):
    # start_rack on a rack that is the whole cluster (e.g. every node at the defaults dc1/rack1)
    rc, out, asked, went_on = run(tmp_path, "Test Cluster", "my_cluster", candidates=(),
                                  extra={"cassandra_service_cluster_required": True})
    assert rc != 0 and "The cluster name could not be read (no other node to read it from) and cassandra_cluster_name" in out, out
    assert went_on == []
    rc, out, asked, went_on = run(tmp_path / "optional", "Test Cluster", "my_cluster", candidates=())
    assert rc == 0 and went_on == ["n0", "n9"], out


def test_a_description_that_failed_is_refused(tmp_path):
    describe = {"from": "n3", "rc": 1, "stdout": "",
                "stderr": "error: Authentication failed! Credentials required\n-- StackTrace --\njava.lang.SecurityException: x"}
    rc, out, asked, went_on = run(tmp_path, "Test Cluster", "my_cluster", describe=describe)
    assert rc != 0, out
    assert ("nodetool describecluster failed on n3 (java.lang.SecurityException: x): the cluster name is not checked "
            "against 'Test Cluster' (cassandra_cluster_name). Nothing was changed. cassandra_cluster_name is the role "
            "default 'Test Cluster': are the inventory's group_vars loaded?") in out
    assert asked == [] and went_on == []


def test_a_description_read_already_is_used(tmp_path):
    describe = {"from": "n3", "stdout": "Cluster Information:\n\tName: other_cluster\n"}
    rc, out, asked, went_on = run(tmp_path, "my_cluster", "my_cluster", describe=describe)
    assert rc != 0 and "n3 runs Cassandra for cluster 'other_cluster', not 'my_cluster'" in out, out
    assert asked == [] and went_on == []


def test_the_playbooks_check_it_before_any_change():
    # preflight, first thing after reading the nodes: from a running node of the run, else from a node --limit left out
    play = load("playbooks", "preflight.yml")[1]
    names = [t.get("name") for t in play["tasks"]]
    assert names.index("Read this node's settings") + 1 == names.index("Read the cluster from a running node") \
        == names.index("Check the running node belongs to this cluster") - 1 \
        < names.index("Check the account Cassandra runs as can read the config") < names.index("Check the running cluster")
    first = play["tasks"][names.index("Read the cluster from a running node")]
    assert first["run_once"] is True
    assert [t["name"] for t in first["block"]] == ["Find a running node to read the cluster from",
                                                   "Read the cluster description from a running node"]
    # waits for a node still starting only: a JMX login refused is told at once, by cluster_name.yml
    read = first["block"][1]
    assert read["failed_when"] is False
    until = "{{ %s }}" % read["until"]
    for result, again in (({"rc": 0, "stdout": "Name: x", "stderr": ""}, False),
                          ({"rc": 1, "stdout": "", "stderr": "nodetool: Failed to connect - ConnectException: refused"}, True),
                          ({"rc": 1, "stdout": "", "stderr": "error: Authentication failed! Credentials required"}, False)):
        assert render(until, {"cassandra_preflight_describe": result}) is (not again)
    # not under run_once: an unreachable candidate would end the play
    check = play["tasks"][names.index("Check the running node belongs to this cluster")]
    assert "run_once" not in check
    assert check["ansible.builtin.include_role"]["tasks_from"] == "cluster_name.yml"
    for running, describe, candidates in ((["n1"], {"from": "n1", "stdout": "Name: x", "stderr": "", "rc": 0}, []),
                                          ([], {}, ["n4", "n5"])):
        variables = {"_cassandra_preflight_running_nodes": running, "_cassandra_preflight_from": "n1",
                     "cassandra_preflight_describe": {"stdout": "Name: x", "stderr": "", "rc": 0} if running else {"skipped": True},
                     "ansible_play_hosts_all": ["n1", "n2"]}
        assert render(check["vars"]["cassandra_service_cluster_describe"], variables) == describe
        variables["_cassandra_preflight_peers"] = ["n4", "n5"]
        assert render(check["vars"]["cassandra_service_cluster_candidates"], variables) == candidates
    # the peers: the cluster's nodes (lookup) the run left out, not a node being added or reset
    peers = first["block"][0]["ansible.builtin.set_fact"]["_cassandra_preflight_peers"]
    assert "query('community.cassandra.cassandra_nodes') | reject('in', ansible_play_hosts_all + _aside) | list" in peers
    # start_rack, which writes the unit of the rack's nodes, checks it on the other nodes first
    plays = load("playbooks", "start_rack.yml")
    find = [p for p in plays if p["name"] == "Find the rack's nodes"][0]
    check = [t for t in find["tasks"] if t["name"] == "Check the running nodes belong to this cluster"][0]
    assert check["ansible.builtin.include_role"]["tasks_from"] == "cluster_name.yml" and "run_once" not in check
    assert check["vars"]["cassandra_service_cluster_required"] is True
    assert "reject('in', groups['cassandra_target_rack_nodes'])" in check["vars"]["cassandra_service_cluster_candidates"]
    assert [p["name"] for p in plays].index("Find the rack's nodes") < [p["name"] for p in plays].index("Start the rack")
