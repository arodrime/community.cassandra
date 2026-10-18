from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The screens the operation playbooks show before a change (screen.yml and the
# filter cassandra_screen): the expressions are read from the playbooks and the
# role, rendered by Ansible with neutral data.

import os
import warnings

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import PARAGRAPH as P

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
SESSION = ("this run is not inside tmux or screen: if the SSH session to this machine drops, the run stops"
           " (the operation itself goes on, unwatched). Run it inside tmux or screen.")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


SCREEN = load("roles", "cassandra_operation", "tasks", "screen.yml")[0]


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def task(playbook, name):
    """The task and its play (blocks looked into)."""
    for play in load("playbooks", playbook):
        for t in walk(play.get("pre_tasks", []) + play.get("tasks", [])):
            if t.get("name") == name:
                return t, play
    raise AssertionError("no task %s in %s" % (name, playbook))


def screen(variables, task_vars, play_vars=None, check=False, confirm=True):
    """The text of screen.yml for the task's vars."""
    variables = dict(variables, ansible_check_mode=check, cassandra_operation_confirm=confirm,
                     _cassandra_session_warning=SESSION)
    variables.update(trust(play_vars or {}))
    variables.update(trust(task_vars))
    variables.update(trust(SCREEN["vars"]))
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template("{{ _cassandra_screen_text }}"))


# The screens: rendered with neutral data, header and blocks as expected

def preflight(i, rack="rack1", **extra):
    p = {"address": "10.0.0.%d" % i, "ring_address": "10.0.0.%d" % i, "cassandra_dc": "dc1", "cassandra_rack": rack,
         "cassandra_cluster_name": "Orders", "cassandra_version": "41x", "cassandra_endpoint_snitch": "GossipingPropertyFileSnitch",
         "layout": {"seed": i == 1}}
    p.update(extra)
    return p


def test_stop_rack_screen():
    t, play = task("stop_rack.yml", "Show the plan and confirm")
    hosts = ["node1", "node2", "node3"]
    hostvars = dict((h, {"inventory_hostname": h, "_cassandra_preflight": preflight(i, "rack%d" % i)})
                    for i, h in enumerate(hosts, 1))
    variables = {"ansible_play_hosts_all": hosts, "hostvars": hostvars, "cassandra_target_dc": "dc1",
                 "cassandra_target_rack": "rack2", "_cassandra_preflight": preflight(1),
                 "_cassandra_rack_check": {"problems": [], "warnings": ["orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails"]},
                 "_cassandra_rack_down_here": [], "_cassandra_rack_down_elsewhere": ["10.0.1.5"]}
    forced = screen(dict(variables, cassandra_rack_force=True, _cassandra_rack_down_here=["10.0.0.9"]), t["vars"], play["vars"])
    assert "\n\nWARNING - replicas: cassandra_rack_force is true: the rack goes down although it takes more than one\n" \
           "  replica of some data down:\n  - already down in dc1: 10.0.0.9\n" + P + "\nWARNING - replication" in forced
    text = screen(variables, t["vars"], play["vars"])
    assert text == (
        "stop_rack on cluster 'Orders': stop dc1 / rack2\n\n"
        "Stops Cassandra on every node of the rack at once; they stay down until start_rack.\n\n"
        "  node2  10.0.0.2  dc1 / rack2\n\n"
        "WARNING - replication: orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails\n" + P + "\n"
        "WARNING - down nodes: down in another datacenter: 10.0.1.5")


def test_create_cluster_screen():
    t, play = task("create_cluster.yml", "Show the ring and confirm the tokens")
    text = screen({"_plan": {"tokens": {"node1": "0"}, "lines": ["dc1 ring *"], "warnings": []}, "_running": [],
                   "ansible_play_hosts": ["node1"], "_cassandra_preflight": preflight(1)}, t["vars"])
    assert text == ("create_cluster on cluster 'Orders': the tokens of the new nodes (one token per node)\n\n"
                    "The ring (RF of the shares: cassandra_token_rf):\n\ndc1 ring *")


def test_every_confirmation_goes_through_the_screen():
    # no playbook builds its own prompt without its plan: through screen.yml, or (the plan layout of
    # plugin_utils cassandra_output, OUTPUT Q5) confirm.yml right after the task that prints the plan
    for name in sorted(n for n in os.listdir(os.path.join(TOP, "playbooks")) if n.endswith(".yml")):
        with open(os.path.join(TOP, "playbooks", name), encoding="utf-8") as f:
            plays = yaml.safe_load(f)
        for play in plays:
            tasks = play.get("tasks") or []
            for index, t in enumerate(tasks):
                if (t.get("ansible.builtin.include_role") or {}).get("tasks_from") != "confirm.yml":
                    continue
                shown = tasks[index - 1] if index else {}
                assert (shown.get("vars") or {}).get("cassandra_output") is True, name
                assert "ansible.builtin.debug" in shown and shown.get("when") == t.get("when"), name
