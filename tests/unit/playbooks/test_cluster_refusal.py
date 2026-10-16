from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# An inventory of two clusters and none named (or a wrong name): the
# operation playbooks stop in their first play, on localhost, with the
# lookup's refusal as a verdict of the ops callback, before any play on the
# nodes (whose hosts would fail on it with a raw Ansible error). Run by
# ansible-playbook on the collection's playbooks: no node is reached.

import os
import re
import subprocess
import sys

import pytest

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
INVENTORY = """
[orders]
o1
[orders_dc1]
o1
[billing]
b1
[billing_dc1]
b1
"""
# the verdict line alone (red under the ops callback), then what to do: never a cluster to paste
WAY_OUT = [
    "Name the one to run on:",
    "  export CASSANDRA_CLUSTER=<cluster>",
    "or add -e cassandra_hosts=<cluster> to the command.",
    "Nothing was changed.",
]
GAP = ["", "----", ""]
NONE_NAMED = ["====", "", "REFUSED  no cluster named: this inventory has 2 clusters: billing, orders"] + GAP + WAY_OUT


def run(tmp_path, playbook, *args, **env_vars):
    (tmp_path / "inventory").write_text(INVENTORY)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_FORCE_COLOR="0", PWD=str(tmp_path), COLUMNS="80")
    env.update(env_vars)
    for name in ("ANSIBLE_CONFIG", "ANSIBLE_INVENTORY") + (() if "CASSANDRA_CLUSTER" in env_vars else ("CASSANDRA_CLUSTER",)):
        env.pop(name, None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", "inventory", "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable, "community.cassandra." + playbook] + list(args)
    proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          check=False, cwd=str(tmp_path), timeout=300)
    out = proc.stdout.decode("utf-8", "replace")
    assert "Traceback" not in out and "unhandled" not in out and "exception" not in out, out
    # each rule as 4 of its characters (its width is the terminal's)
    return proc.returncode, [re.sub(r"^([-=])\1{3,}$", lambda m: m.group(1) * 4, line) for line in out.splitlines()]


@pytest.mark.parametrize("playbook,args", [
    ("status", []),
    ("apply_config", []),
    ("add_node", ["-e", "cassandra_target_nodes=o1"]),
    ("status", ["--tags", "nothing"]),  # the check runs under --tags too
])
def test_no_cluster_named(tmp_path, playbook, args):
    rc, lines = run(tmp_path, playbook, *args)
    assert rc != 0
    assert lines == NONE_NAMED


def test_empty_cassandra_hosts(tmp_path):
    rc, lines = run(tmp_path, "status", "-e", "cassandra_hosts=")
    assert rc != 0
    assert lines == ["====", "", "REFUSED  no cluster named (-e cassandra_hosts is empty): this inventory has 2 clusters:"
                     " billing, orders"] + GAP + WAY_OUT


def test_typo_in_cassandra_hosts(tmp_path):
    rc, lines = run(tmp_path, "status", "-e", "cassandra_hosts=ordrs")
    assert rc != 0
    assert lines == ["====", "", "REFUSED  cassandra_hosts=ordrs: no group of that name in the inventory"] + GAP + [
        "Its groups: billing, billing_dc1, orders, orders_dc1",
        "Name the cluster's group: -e cassandra_hosts=<group>",
        "Nothing was changed."]


def test_unknown_cassandra_cluster(tmp_path):
    rc, lines = run(tmp_path, "status", CASSANDRA_CLUSTER="ordrs")
    assert rc != 0
    assert lines == ["====", "", "REFUSED  CASSANDRA_CLUSTER=ordrs: no group of that name with hosts, and no host with"
                     " that cassandra_cluster_name"] + GAP + ["This inventory has 2 clusters: billing, orders."] + WAY_OUT


def test_only_the_verdict_line_is_red(tmp_path):
    rc, lines = run(tmp_path, "status", ANSIBLE_FORCE_COLOR="1", ANSIBLE_NOCOLOR="0")
    assert rc != 0
    red = [line for line in lines if "\x1b[0;31m" in line]
    assert red == ["\x1b[0;31mREFUSED  no cluster named: this inventory has 2 clusters: billing, orders\x1b[0m"]
    assert any("export CASSANDRA_CLUSTER=<cluster>" in line for line in lines)  # shown, not red


def test_under_limit_the_hosts_fail_on_it(tmp_path):
    """--limit skips the localhost play: the first play's hosts stop the run on the same refusal (Ansible's
    error around it), never a run of no play that ends ok."""
    rc, lines = run(tmp_path, "status", "--limit", "o1")
    assert rc != 0
    assert "REFUSED  no cluster named: this inventory has 2 clusters: billing, orders" in "\n".join(lines)
