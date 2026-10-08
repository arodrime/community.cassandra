from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The seeds follow the inventory when nodes come and go: a new node listed in
# cassandra_seeds joins with the other seeds (join_seeds.yml) and is made a
# seed once up; a node leaving the list is dropped from the other nodes' seed
# lists before it leaves (seeds_apply.yml, change_seeds' tasks). The
# expressions are read from the playbooks and task files, rendered by Ansible.

import os
import warnings

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the inventory_hostnames and cassandra_nodes lookups, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def play(playbook, name):
    return next(p for p in load("playbooks", playbook) if p.get("name") == name)


def flat(tasks):
    """The tasks, those of their blocks too, in order."""
    out = []
    for t in tasks:
        out.append(t)
        for key in ("block", "rescue", "always"):
            out.extend(flat(t.get(key) or []))
    return out


def task(tasks, name):
    for t in flat(tasks):
        if t.get("name") == name:
            return t
    raise AssertionError(name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


GROUP = ["n1", "n2", "n3", "n5"]


def node(i, running=True, live="10.0.0.1,10.0.0.2", seed_entry="", dc="dc1", rack="r1"):
    address = "10.0.0.%d" % i
    return {"inventory_hostname": "n%d" % i, "_cassandra_service_seed_entry": seed_entry,
            "_cassandra_service_is_seed": bool(seed_entry), "_cassandra_service_names": ["n%d" % i, address],
            "_cassandra_preflight_running": running,
            "_cassandra_preflight": {"address": address, "ring_address": address, "cassandra_dc": dc,
                                     "cassandra_rack": rack, "live_seeds": live}}


def ring(*numbers, joining=()):
    return {"cluster_status": {"dc1": {"nodes": [{"address": "10.0.0.%d" % i, "status": "U", "rack": "r1",
                                                  "state": "J" if i in joining else "N"} for i in numbers]}}}


# preflight: a seed not in the ring is refused unless this run adds it

PREFLIGHT = play("preflight.yml", "Cassandra preflight checks")
SEED_UP = task(task(PREFLIGHT["tasks"], "Check the running cluster")["block"], "Every seed is up")


def seed_up(item, **extra):
    hostvars = {"n1": node(1, seed_entry="10.0.0.1"), "n2": node(2), "n3": node(3),
                "n5": node(5, running=False, live="", seed_entry="10.0.0.5")}
    extra.setdefault("cassandra_preflight_status", ring(1, 2, 3))
    variables = dict(SEED_UP["vars"], item=item, hostvars=hostvars, ansible_play_hosts=GROUP, _from="n1",
                     groups={"all": GROUP, "prod": GROUP}, cassandra_hosts="prod", **extra)
    return render("{{ %s }}" % SEED_UP["ansible.builtin.assert"]["that"], **variables)


def test_preflight_refuses_a_seed_out_of_the_ring_the_run_does_not_add():
    assert seed_up("10.0.0.1") is True
    assert seed_up("10.0.0.5") is False  # e.g. rolling_restart, or add_node of another host
    assert seed_up("10.0.0.5", cassandra_new_nodes="n3") is False
    assert seed_up("10.0.0.9", cassandra_new_nodes="n5") is False  # no host of the cluster


def test_preflight_lets_add_node_and_topology_add_a_seed():
    assert seed_up("10.0.0.5", cassandra_new_nodes="n5") is True
    assert seed_up("10.0.0.5", cassandra_new_nodes=["n5"]) is True
    assert seed_up("10.0.0.5", _cassandra_preflight_adding=["n1", "n2", "n3", "n5"]) is True
    # still bootstrapping (an earlier run): waited for again
    assert seed_up("10.0.0.5", cassandra_new_nodes="n5", cassandra_preflight_status=ring(1, 2, 3, 5, joining=(5,))) is True
    assert seed_up("10.0.0.5", cassandra_preflight_status=ring(1, 2, 3, 5, joining=(5,))) is False
    imports = load("playbooks", "topology.yml")[0]
    assert imports["ansible.builtin.import_playbook"] == "community.cassandra.preflight"
    assert "cassandra_nodes" in imports["vars"]["_cassandra_preflight_adding"]


# join_seeds.yml: the seeds a new node starts with, never itself

JOIN = load("roles", "cassandra_service", "tasks", "join_seeds.yml")[0]


def join_seeds(seeds, members=(1, 2, 3), joining=()):
    hostvars = {"n1": node(1), "n2": node(2), "n3": node(3), "n5": node(5, running=False, live="")}
    variables = dict(JOIN["vars"], cassandra_seeds=seeds, hostvars=hostvars, cassandra_preflight_status=ring(*members, joining=joining),
                     groups={"all": GROUP, "prod": GROUP}, cassandra_hosts="prod")
    return render(JOIN["ansible.builtin.set_fact"]["_cassandra_join_seeds"], **variables)


def test_a_new_node_never_joins_as_its_own_seed():
    assert join_seeds(["10.0.0.1", "10.0.0.5"]) == ["10.0.0.1"]
    assert join_seeds("10.0.0.1:7000, n5:7000") == ["10.0.0.1:7000"]  # by name, port left out
    # every seed is a node not in the ring yet: two up nodes of the ring
    assert join_seeds(["10.0.0.5"]) == ["10.0.0.1", "10.0.0.2"]
    # a node still joining (UJ, an earlier run) is no seed either
    assert join_seeds(["10.0.0.2", "10.0.0.3"], joining=(3,)) == ["10.0.0.2"]


def test_join_seeds_keep_cassandra_seeds_as_written_when_nothing_is_left_out():
    assert join_seeds("10.0.0.1, 10.0.0.2") == "10.0.0.1, 10.0.0.2"
    assert join_seeds(["10.0.0.1", "10.0.0.2"]) == ["10.0.0.1", "10.0.0.2"]


def test_the_add_and_its_checks_use_the_join_seeds():
    add = load("roles", "cassandra_service", "tasks", "action_add.yml")
    assert add[0]["ansible.builtin.include_tasks"] == "join_seeds.yml"
    config = task(add, "Write the config")
    assert config["vars"]["cassandra_seeds"] == "{{ _cassandra_join_seeds }}"
    assert task(add, "Start it")["vars"]["cassandra_service_allow_new_seed"] is True
    checks = load("roles", "cassandra_service", "tasks", "new_node_checks.yml")
    block = task(checks, "Check the host before installing anything")["block"]
    names = [t["name"] for t in flat(block)]
    assert names.index("Work out the seeds it joins with") < names.index("Work out what to check")
    assert task(block, "Work out what to check")["vars"]["cassandra_seeds"] == "{{ _cassandra_join_seeds }}"


# add_node: a new seed is added, then cassandra_seeds goes live everywhere

ADD_PLAYS = load("playbooks", "add_node.yml")


def test_add_node_applies_the_seeds_after_the_adds():
    names = [p["name"] for p in ADD_PLAYS]
    assert names.index("Add the new nodes, one at a time") < names.index("Apply cassandra_seeds now that the new seeds are up") < names.index("Summary")
    step = play("add_node.yml", "Apply cassandra_seeds now that the new seeds are up")
    apply = step["tasks"][0]
    assert apply["ansible.builtin.include_role"]["tasks_from"] == "seeds_apply.yml"
    hostvars = {"n1": node(1, seed_entry="10.0.0.1"), "n2": node(2), "n3": node(3),
                "n5": node(5, seed_entry="10.0.0.5")}
    variables = dict(apply["vars"], hostvars=hostvars, groups={"all": GROUP, "prod": GROUP}, cassandra_hosts="prod")
    assert render(apply["vars"]["_new_seeds"], cassandra_new_nodes="n5", **variables) == ["n5"]
    assert render(apply["vars"]["_new_seeds"], cassandra_new_nodes="n3", **variables) == []
    # every node of the cluster, standalone; topology runs its own seed step
    variables = {"groups": {"all": GROUP, "prod": GROUP}, "cassandra_hosts": "prod", "hostvars": hostvars,
                 "cassandra_new_nodes": "n5"}
    assert render(step["hosts"], **variables) == GROUP
    assert render(step["hosts"], _cassandra_screen_asked_by="topology", **variables) == "localhost:!localhost"


# decommission_node: a node the others list as a seed is dropped from their lists first

DECO = play("decommission_node.yml", "Check the nodes to remove")


def deco(leaving, seeds, asked_by=""):
    hostvars = {"n1": node(1, seed_entry="10.0.0.1"), "n2": node(2), "n3": node(3), "n5": node(5)}
    variables = dict(DECO["vars"], ansible_play_hosts_all=leaving, hostvars=hostvars, cassandra_seeds=seeds,
                     groups={"all": GROUP, "prod": GROUP}, cassandra_hosts="prod")
    if asked_by:
        variables["_cassandra_screen_asked_by"] = asked_by
    return render("{{ [_dn_seed_step, _dn_seeds.problems] }}", **variables)


def test_decommission_drops_a_seed_from_the_others_first():
    # n2 is in the seed lists the nodes run with, not in cassandra_seeds any more
    assert deco(["n2"], ["10.0.0.1", "10.0.0.3"]) == [["n2"], []]
    assert deco(["n5"], ["10.0.0.1", "10.0.0.2"]) == [[], []]  # not a seed: nothing to change
    assert deco(["n2"], ["10.0.0.1", "10.0.0.3"], asked_by="topology") == [[], []]  # topology's own step
    names = [p["name"] for p in load("playbooks", "decommission_node.yml")]
    assert names.index("Check the nodes to remove") < names.index("Take the nodes to remove out of the seed lists") \
        < names.index("Remove the nodes, one at a time")


def test_decommission_applies_the_seeds_only_for_a_leaving_seed_or_to_reload():
    note = task(DECO["tasks"], "Note whether the seeds are applied first")["ansible.builtin.set_fact"]
    hostvars = {"n1": node(1, seed_entry="10.0.0.1"), "n2": node(2), "n3": node(3), "n5": node(5)}

    def applies(leaving, seeds, lives=None):
        for name, live in (lives or {}).items():
            hostvars[name]["_cassandra_preflight"]["live_seeds"] = live
        variables = dict(DECO["vars"], ansible_play_hosts_all=leaving, hostvars=hostvars, cassandra_seeds=seeds,
                         groups={"all": GROUP, "prod": GROUP}, cassandra_hosts="prod")
        return render(note["_cassandra_decommission_seeds_apply"], **variables)

    assert applies(["n2"], ["10.0.0.1", "10.0.0.3"]) is True  # n2 leaves the lists
    assert applies(["n5"], ["10.0.0.1", "10.0.0.2"]) is True  # files right already: a reload only
    # another difference, n5 no seed: left as it is (change_seeds' job)
    assert applies(["n5"], ["10.0.0.1", "10.0.0.2", "10.0.0.3"]) is False
    step = play("decommission_node.yml", "Take the nodes to remove out of the seed lists")
    assert step["any_errors_fatal"] is True
    when = step["tasks"][0]["when"]
    assert "not ansible_check_mode" in when
    for value in (True, False):
        hv = {"n2": {"_cassandra_decommission_seeds_apply": value}}
        assert render("{{ %s }}" % when[0], hostvars=hv, cassandra_leaving_nodes="n2", groups={"all": ["n2"]}) is value


def test_a_failed_seed_step_stops_the_run():
    assert play("topology.yml", "Change the seeds")["any_errors_fatal"] is True
    assert play("add_node.yml", "Apply cassandra_seeds now that the new seeds are up")["any_errors_fatal"] is True


def test_the_add_refuses_a_node_among_its_own_join_seeds():
    guard = task(load("roles", "cassandra_service", "tasks", "action_add.yml"), "It is not one of the seeds it joins with")
    own = guard["vars"]["_own"]
    names = ["n5", "10.0.0.5"]
    assert render(own, _cassandra_join_seeds=["10.0.0.1"], _cassandra_service_names=names) == []
    assert render(own, _cassandra_join_seeds="10.0.0.1, 10.0.0.5:7000", _cassandra_service_names=names) == ["10.0.0.5"]
    assert guard["ansible.builtin.assert"]["that"] == "_own | length == 0"


def test_decommission_refusals_on_seeds():
    still = task(task(DECO["tasks"], "Check this node")["block"], "A node in cassandra_seeds can't be removed")
    assert still["ansible.builtin.assert"]["that"] == "not _cassandra_service_is_seed | bool"
    assert "take it out of cassandra_seeds in the inventory" in still["ansible.builtin.assert"]["fail_msg"]
    keeps = task(task(DECO["tasks"], "Check this node")["block"], "Every datacenter keeps a seed")
    assert keeps["ansible.builtin.assert"]["that"] == "_dn_seed_step | length == 0 or _dn_seeds.problems | length == 0"
    # the only seed left in the list is gone with n2: no seed at all
    step, problems = deco(["n2"], [])
    assert step == ["n2"] and problems[0].startswith("cassandra_seeds is empty")


def test_decommission_of_a_whole_datacenter_needs_no_seed_there():
    # dc2 = n6 (a seed until now) and n7; dc1 = n1 (seed), n2
    lives = "10.0.0.1,10.0.0.6"
    hostvars = {"n1": node(1, live=lives, seed_entry="10.0.0.1"), "n2": node(2, live=lives),
                "n6": node(6, live=lives, dc="dc2"), "n7": node(7, live=lives, dc="dc2")}
    group = ["n1", "n2", "n6", "n7"]

    def check(leaving):
        variables = dict(DECO["vars"], ansible_play_hosts_all=leaving, hostvars=hostvars, cassandra_seeds=["10.0.0.1"],
                         groups={"all": group, "prod": group}, cassandra_hosts="prod")
        return render("{{ [_dn_seed_step, _dn_seeds.problems] }}", **variables)

    assert check(["n6", "n7"]) == [["n6"], []]  # dc2 gone for good
    step, problems = check(["n6"])  # n7 stays in dc2 with no seed: refused
    assert step == ["n6"] and problems == ["dc2 would be left with no seed (n6 leaves the seed list): put a node of"
                                           " dc2 in cassandra_seeds."]


# change_seeds and the steps share seeds_apply.yml

def test_change_seeds_uses_the_shared_tasks():
    tasks = play("change_seeds.yml", "Update the seed list")["tasks"]
    assert task(tasks, "Apply the new seed list")["ansible.builtin.include_role"]["tasks_from"] == "seeds_apply.yml"
    apply = load("roles", "cassandra_service", "tasks", "seeds_apply.yml")
    block = task(apply, "Apply the seed list")["block"]
    reload = task(block, "Reload the seeds on the running node")
    # read when applied: a node this run added was not running at the preflight
    assert "ansible_facts.services['cassandra.service'].state | default('') == 'running'" in reload["when"]
    assert [t["name"] for t in block].index("Check whether Cassandra is running") \
        < [t["name"] for t in block].index("Reload the seeds on the running node")


def test_preflight_names_a_seed_marked_absent():
    groups = {"all": GROUP, "prod": GROUP + ["n2b"]}
    hostvars = {"n1": node(1, seed_entry="10.0.0.1"), "n2": node(2), "n3": node(3), "n5": node(5),
                "n2b": {"cassandra_node_state": "absent", "ansible_host": "10.0.0.22"}}
    variables = dict(SEED_UP["vars"], item="10.0.0.22:7000", hostvars=hostvars, ansible_play_hosts=GROUP,
                     cassandra_preflight_status=ring(1, 2, 3), _from="n1", groups=groups, cassandra_hosts="prod")
    assert render("{{ %s }}" % SEED_UP["ansible.builtin.assert"]["that"], **variables) is False
    assert render(SEED_UP["ansible.builtin.assert"]["fail_msg"], **variables).strip().endswith(
        "n2b is marked cassandra_node_state: absent: take it out of cassandra_seeds in the inventory (topology then"
        " drops it from the seed lists before it removes it).")


def _var_names(node):
    """Every name defined under a vars: of a playbook or task file."""
    names = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "vars" and isinstance(value, dict):
                names.update(value)
            names.update(_var_names(value))
    elif isinstance(node, list):
        for item in node:
            names.update(_var_names(item))
    return names


def test_the_shared_seed_tasks_vars_are_their_own():
    # the vars of the include that runs them win over their own task vars: add_node's _pending (a count) once
    # broke join_seeds.yml's list of the same name
    elsewhere = set()
    for name in os.listdir(os.path.join(TOP, "playbooks")):
        elsewhere.update(_var_names(load("playbooks", name)))
    tasks_dir = os.path.join(TOP, "roles", "cassandra_service", "tasks")
    for name in os.listdir(tasks_dir):
        if name not in ("join_seeds.yml", "seeds_apply.yml"):
            elsewhere.update(_var_names(load("roles", "cassandra_service", "tasks", name)))
    for name in ("join_seeds.yml", "seeds_apply.yml"):
        own = _var_names(load("roles", "cassandra_service", "tasks", name))
        assert not own & elsewhere, (name, own & elsewhere)
