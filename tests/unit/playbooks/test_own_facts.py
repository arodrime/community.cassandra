from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# ansible-core 2.21 (ansible/ansible#87148, ansible/ansible#87415) gives a
# run_once task's facts to the play's hosts as one object: a host with no facts
# yet keeps that object as its own, and the facts gathered next on any of them
# (setup, package_facts, service_facts) land on all. import_cluster read one
# node twice and another not at all. Every play on the nodes first gives each
# node its own facts, before any run_once task. Real runs check it: hosts on
# the local connection, each with its own interpreter (a wrapper that sets
# OWN_FACTS_NODE), and a callback that records hosts sharing one facts object.

import glob
import json
import os
import subprocess
import sys

import pytest
import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
# its own name in its facts, under --tags too (not --skip-tags always, nor --start-at-task)
OWN = {"name": "Give each node its own facts", "tags": ["always"],
       "ansible.builtin.set_fact": {"_cassandra_own_facts": "{{ inventory_hostname }}", "cacheable": True}}
# the first node play of each playbook run on its own checks it: a play before it in the same run (one of the
# operator's) can have made the nodes share their facts already, which the task above does not undo
FIRST_PLAYS = [("health_check.yml", "Cluster health"), ("import_cluster.yml", "Find the cluster from the given nodes"),
               ("preflight.yml", "Cassandra preflight checks"), ("start_rack.yml", "Find the rack's nodes"),
               ("status.yml", "Cluster status")]

CALLBACK = '''
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import os

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
name: own_facts
type: aggregate
short_description: test only
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "own_facts"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super(CallbackModule, self).__init__()
        self._vm = None
        self._task = None
        self.shared = []
        self.tasks = []
        self.seen = {}

    def v2_playbook_on_play_start(self, play):
        self._vm = play.get_variable_manager()

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._task = task.get_name()
        self.tasks.append(self._task)

    def _check(self, *args, **kwargs):
        if self._vm is None:
            return
        objects = {}
        for host in self._vm._inventory.hosts:
            try:
                facts = self._vm._fact_cache.get(host)
            except KeyError:
                facts = None
            if not facts:
                continue
            objects.setdefault(id(facts), []).append(host)
            node = (facts.get("ansible_env") or facts.get("env") or {}).get("OWN_FACTS_NODE")
            if node:
                self.seen.setdefault(host, set()).add(node)
        for hosts in objects.values():
            if len(hosts) > 1 and {"task": self._task, "hosts": sorted(hosts)} not in self.shared:
                self.shared.append({"task": self._task, "hosts": sorted(hosts)})

    v2_runner_on_ok = v2_runner_on_failed = v2_runner_on_skipped = v2_runner_on_unreachable = _check

    def v2_playbook_on_stats(self, stats):
        self._check()
        with open(os.environ["OWN_FACTS_OUT"], "w") as f:
            json.dump({"shared": self.shared, "tasks": self.tasks,
                       "seen": dict((h, sorted(n)) for h, n in self.seen.items())}, f)
'''


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def node_plays():
    for path in sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml"))):
        for play in load(path):
            if "hosts" in play and str(play["hosts"]).strip() != "localhost":
                yield os.path.basename(path), play


def test_every_node_play_gives_its_nodes_their_own_facts_first():
    plays = list(node_plays())
    assert len(plays) > 50
    for playbook, play in plays:
        # pre_tasks, then roles, then tasks: the first task of the play
        first = (play.get("pre_tasks") or ([] if play.get("roles") else play.get("tasks")) or [None])[0]
        assert first == OWN, (playbook, play["name"])


def test_the_first_plays_check_the_nodes_own_their_facts():
    # every playbook that does not start with the preflight's node play is one of these
    checked = {playbook for playbook, play in FIRST_PLAYS}
    for name in sorted(n for n in os.listdir(os.path.join(TOP, "playbooks")) if n.endswith(".yml")):
        plays = load("playbooks", name)
        if name not in checked and any("hosts" in p and str(p["hosts"]).strip() != "localhost" for p in plays):
            assert plays[0].get("ansible.builtin.import_playbook") == "community.cassandra.preflight", name
    for playbook, name in FIRST_PLAYS:
        check = next(p for p in load("playbooks", playbook) if p.get("name") == name)["tasks"][1]
        assert check["name"] == "Check each node has its own facts", playbook
        assert check["run_once"] is True and check["tags"] == ["always"] and check["vars"]["cassandra_output"] is True


def test_nothing_clears_the_facts():
    # meta: clear_facts would take a node back to no facts of its own
    paths = glob.glob(os.path.join(TOP, "playbooks", "*.yml"))
    paths += glob.glob(os.path.join(TOP, "roles", "*", "*", "**", "*.yml"), recursive=True)
    assert len(paths) > 100
    for path in paths:
        with open(path, encoding="utf-8") as f:
            assert "clear_facts" not in f.read(), path


def run(tmp_path, playbook, hosts, *args):
    """ansible-playbook on local hosts, each with its own interpreter; the callback's record."""
    plugins = tmp_path / "callback_plugins"
    plugins.mkdir(exist_ok=True)
    (plugins / "own_facts.py").write_text(CALLBACK)
    lines = ["[cassandra]"]
    for host in hosts:
        python = tmp_path / ("python-" + host)
        python.write_text('#!/bin/sh\nOWN_FACTS_NODE=%s exec %s "$@"\n' % (host, sys.executable))
        python.chmod(0o755)
        lines.append("%s ansible_python_interpreter=%s cassandra_dc=dc1 cassandra_rack=rack1" % (host, python))
    lines += ["[cassandra_dc1:children]", "cassandra", "[cassandra:vars]", "ansible_connection=local", "ansible_become=false"]
    (tmp_path / "hosts.ini").write_text("\n".join(lines) + "\n")
    out = tmp_path / "own_facts.json"
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_BECOME="False", ANSIBLE_CALLBACK_PLUGINS=str(plugins),
               ANSIBLE_CALLBACKS_ENABLED="own_facts", OWN_FACTS_OUT=str(out))
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(tmp_path / "hosts.ini"),
            "-e", "cassandra_operation_confirm=false"] + list(args) + [playbook]
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=600, check=False)
    log = result.stdout.decode(errors="replace")
    assert out.exists(), log
    with open(str(out)) as f:
        return json.load(f), log


def test_own_facts_kept_after_any_run_once(tmp_path):
    # the task the playbooks start with, then run_once tasks that give facts to
    # every host (debug and set_fact, cacheable or not), then each node's own
    # facts: setup, and a cacheable set_fact
    play = [{"hosts": "cassandra", "gather_facts": False, "tasks": [
        OWN,
        {"ansible.builtin.debug": {"msg": "phase line"}, "run_once": True},
        {"ansible.builtin.set_fact": {"_x": 1}, "run_once": True},
        {"ansible.builtin.set_fact": {"_y": 1, "cacheable": True}, "run_once": True},
        {"ansible.builtin.setup": {"gather_subset": ["!all", "!min", "env"]}},
        {"ansible.builtin.set_fact": {"_node": "{{ ansible_facts.env.OWN_FACTS_NODE }}", "cacheable": True}},
        {"ansible.builtin.assert": {"that": [
            "ansible_facts.env.OWN_FACTS_NODE == inventory_hostname",
            "hostvars[inventory_hostname].ansible_facts._node | default(_node) == inventory_hostname"]}},
    ]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    record, log = run(tmp_path, str(tmp_path / "play.yml"), ["n1", "n2", "n3"])
    assert "failed=0" in log and "failed=1" not in log, log
    assert record["shared"] == [], log
    assert record["seen"] == {"n1": ["n1"], "n2": ["n2"], "n3": ["n3"]}, log


@pytest.mark.parametrize("playbook, hosts, args, reached", [
    # its node play: three run_once checks and a phase line, then each node's network facts (then the ring
    # read, which fails here: no Cassandra)
    ("import_cluster", ["n1", "n2", "n3"], [], "Gather the network facts"),
    # two new nodes, without the preflight's facts gathered before (topology runs it so): a phase line, then
    # each node's checks (its packages; it stops after, as no facts were gathered)
    ("add_node", ["n1", "n2", "n3"], ["-e", "cassandra_target_nodes=n2,n3", "-e", "_cassandra_preflight_skip=true"],
     "Read the installed packages"),
])
def test_playbook_nodes_keep_their_own_facts(tmp_path, playbook, hosts, args, reached):
    record, log = run(tmp_path, "community.cassandra." + playbook, hosts, *args)
    assert any(task.endswith(reached) for task in record["tasks"]), log
    assert record["shared"] == [], log
    # every node's facts read are its own
    assert all(nodes == [host] for host, nodes in record["seen"].items()), record["seen"]


def test_nodes_sharing_their_facts_already_stop_the_run(tmp_path):
    # a play of the operator's before the playbook, in the same run, a run_once task first on the nodes: on
    # ansible-core 2.21 they share one facts object from then on, which the playbook's first node play says
    # (and stops); elsewhere it runs on (then fails here: no Cassandra)
    site = [{"hosts": "cassandra", "gather_facts": False,
             "tasks": [{"ansible.builtin.debug": {"msg": "the operator's"}, "run_once": True}]},
            {"ansible.builtin.import_playbook": "community.cassandra.status"}]
    (tmp_path / "site.yml").write_text(yaml.safe_dump(site))
    record, log = run(tmp_path, str(tmp_path / "site.yml"), ["n1", "n2", "n3"])
    said = "the facts of another node (ansible/ansible#87148)" in " ".join(log.split())
    if record["shared"]:
        assert record["shared"][0]["hosts"] == ["n1", "n2", "n3"], record
        assert said and "Gather the network facts" not in record["tasks"], log
    else:
        assert not said and "Gather the network facts" in record["tasks"], log
