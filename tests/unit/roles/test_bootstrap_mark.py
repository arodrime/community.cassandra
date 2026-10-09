from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node marks a bootstrap it starts on a node until the node has joined:
# reset_node_plan.yml then tells a failed bootstrap of this cluster (marked,
# reset by add_node) from a former member (not marked, refused unless
# -e cassandra_add_node_reset=true).

import os

import yaml

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks")
DEFAULTS = os.path.join(TASKS, "..", "defaults", "main.yml")


def load(name):
    with open(os.path.join(TASKS, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def names(tasks):
    out = []
    for t in tasks:
        out.append(t["name"])
        out.extend(names(t.get("block", [])))
    return out


def test_marked_before_the_start_removed_once_joined():
    tasks = load("action_add.yml")
    order = names(tasks)
    assert order.index("Mark the bootstrap as started") < order.index("Start it")
    assert order.index("Follow the bootstrap") < order.index("Remove the mark of the bootstrap")
    mark = next(t for t in tasks if t["name"] == "Mark the bootstrap as started")
    assert mark["ansible.builtin.copy"]["dest"] == "{{ _cassandra_service_bootstrap_mark }}"
    # not a node joining or joined already; only when its end is waited for (the mark removed then)
    assert mark["when"] == ["cassandra_new_node_state | default('new') == 'new'", "cassandra_service_wait_for_normal | bool"]
    wait = next(t for t in tasks if t["name"] == "Wait for the bootstrap")
    remove = next(t for t in wait["block"] if t["name"] == "Remove the mark of the bootstrap")
    assert remove["ansible.builtin.file"] == {"path": "{{ _cassandra_service_bootstrap_mark }}", "state": "absent"}
    with open(DEFAULTS, encoding="utf-8") as f:
        defaults = yaml.safe_load(f)
    assert defaults["_cassandra_service_bootstrap_mark"] == "/var/lib/cassandra/.ansible_bootstrap_started"
    assert defaults["cassandra_add_node_reset"] == "auto"


def test_an_interrupted_run_that_joined_loses_its_mark():
    block = load("new_node_state.yml")[0]["block"]
    remove = next(t for t in block if t["name"] == "Remove the mark of a bootstrap that went through")
    assert remove["when"] == "_state == 'joined'"


def test_the_reset_check_reads_the_mark_and_the_choice():
    tasks = dict((t["name"], t) for t in load("reset_node_plan.yml"))
    look = tasks["Look for a bootstrap add_node left unfinished"]
    assert look["ansible.builtin.stat"]["path"] == "{{ _cassandra_service_bootstrap_mark }}"
    check = tasks["Work out whether add_node may reset it"]["ansible.builtin.set_fact"]["_cassandra_node_reset_check"]
    assert "bootstrap_started=cassandra_node_reset_bootstrap_mark.stat.exists | default(false)" in check
    assert "explicit=cassandra_add_node_reset | default('auto') | community.cassandra.cassandra_reset_choice == 'true'" in check


def test_the_mark_goes_once_the_node_is_seen_up_or_emptied():
    # a later run's preflight, on a node UN in the ring (a wait that stopped before the end left it)
    with open(os.path.join(TASKS, "..", "..", "..", "playbooks", "preflight.yml"), encoding="utf-8") as f:
        play = next(p for p in yaml.safe_load(f) if p.get("name") == "Cassandra preflight checks")
    names = [t["name"] for t in play["tasks"]]
    remove = play["tasks"][names.index("Remove the mark of a bootstrap that went through")]
    assert names.index("Check the running cluster") < names.index("Remove the mark of a bootstrap that went through")
    assert remove["ansible.builtin.file"]["state"] == "absent"
    assert "_cassandra_preflight.ring_address | default('') in _un" in remove["when"]
    assert "selectattr('status', 'equalto', 'U') | selectattr('state', 'equalto', 'N')" in remove["vars"]["_un"]
    # reset_node, once it emptied the node
    reset = next(t for t in load("reset_node.yml") if t["name"] == "Remove the mark of a bootstrap add_node started there")
    assert reset["ansible.builtin.file"] == {"path": "{{ _cassandra_service_bootstrap_mark }}", "state": "absent"}
