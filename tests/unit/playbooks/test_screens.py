from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The screens the operation playbooks show before a change (screen.yml and the
# filter cassandra_screen): the expressions are read from the playbooks and the
# role, rendered by Ansible with neutral data.

import os
import re
import warnings

import pytest
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
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
SESSION = ("this run is not inside tmux or screen: if the SSH session to this machine drops, the run stops"
           " (the operation itself goes on, unwatched). Run it inside tmux or screen.")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


SCREEN = load("roles", "cassandra_service", "tasks", "screen.yml")[0]


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


DESCRIBE = "Cluster Information:\n\tName: Orders\n\tDatabase versions:\n\t\t4.1.5: [10.0.0.1:7000, 10.0.0.2:7000]\n\n"


def ring_entry(i, rack, load="40.2 GiB", owns="33.4%"):
    return {"address": "10.0.0.%d" % i, "rack": rack, "load": load, "owns": owns, "status": "U", "state": "N"}


def decommission(racks, leaving=("node7", "node8"), states=None, check=False, confirm=True, keyspaces=None,
                 force=False, problems_rf=3):
    hosts = ["node%d" % i for i in range(1, 9)]
    rack_of = dict((h, racks[i % len(racks)]) for i, h in enumerate(hosts))
    hostvars = {}
    for i, h in enumerate(hosts, 1):
        hostvars[h] = {"_cassandra_preflight": {"ring_address": "10.0.0.%d" % i, "address": "10.0.0.%d" % i,
                                                "cassandra_dc": "dc1", "cassandra_rack": rack_of[h],
                                                "cassandra_cluster_name": "Orders",
                                                "live_seeds": "10.0.0.1,10.0.0.2"},
                       "_cassandra_service_names": [h, "10.0.0.%d" % i],
                       "cassandra_leaving_node": {"state": (states or {}).get(h, "normal")}}
    ring = {"dc1": {"nodes": [ring_entry(i, rack_of[h], load="%d.1 GiB" % (40 + i)) for i, h in enumerate(hosts, 1)
                              if (states or {}).get(h) != "decommissioned"]}}
    hostvars[leaving[0]]["cassandra_leaving_ring"] = {"cluster_status": ring}
    rows = keyspaces if keyspaces is not None else [
        '{"keyspace_name": "orders", "replication": {"class": "org.apache.cassandra.locator.NetworkTopologyStrategy", "dc1": "%d"}}'
        % problems_rf,
        '{"keyspace_name": "system", "replication": {"class": "org.apache.cassandra.locator.LocalStrategy"}}']
    task_, play = task("decommission_node.yml", "Show the plan and confirm the removal")
    variables = {"hostvars": hostvars, "groups": {"prod": hosts}, "cassandra_hosts": "prod",
                 "ansible_play_hosts_all": list(leaving), "_cassandra_preflight": hostvars[leaving[0]]["_cassandra_preflight"],
                 "cassandra_preflight_describe": {"stdout": DESCRIBE},
                 "cassandra_decommission_keyspaces": {"out": "\n".join(rows)},
                 "cassandra_decommission_force": force, "cassandra_seeds": ["10.0.0.1", "10.0.0.2"]}
    return screen(variables, task_["vars"], play["vars"], check=check, confirm=confirm)


DECOMMISSION = """\
decommission_node on cluster 'Orders' (Cassandra 4.1.5): remove node7, node8

2 nodes to remove, one after the other: first node7, then node8. Each one streams its data to the
nodes that stay (hours on a big node), then Cassandra is stopped and disabled on it.

  node7  10.0.0.7  dc1 / rack1
    not a seed
    load 47.1 GiB, owns 33.4%
    data goes to the other nodes of rack1 (3 racks in dc1, its replication factor): node1, node4
    runs on node7 (nodetool decommission), the ring checked from node1 before and after
    end state: out of the ring, Cassandra stopped and disabled, its data left on disk

  node8  10.0.0.8  dc1 / rack2
    not a seed
    load 48.1 GiB, owns 33.4%
    data goes to the other nodes of rack2 (3 racks in dc1, its replication factor): node2, node5
    runs on node8 (nodetool decommission), the ring checked from node1 before and after
    end state: out of the ring, Cassandra stopped and disabled, its data left on disk

Afterwards dc1 keeps 6 nodes: node1, node2, node3, node4, node5, node6.
Then remove node7, node8 from the inventory; wipe the data directories before reusing the hosts.

WARNING - session: this run is not inside tmux or screen: if the SSH session to this machine drops,
  the run stops (the operation itself goes on, unwatched). Run it inside tmux or screen."""


def test_decommission_screen():
    assert decommission(["rack1", "rack2", "rack3"]) == DECOMMISSION


def test_decommission_screen_under_check():
    text = decommission(["rack1", "rack2", "rack3"], check=True)
    head, rest = DECOMMISSION.split("\n", 1)
    assert text == head + "\n--check: nothing will be changed (the plan only, no question).\n" + \
        rest.rsplit("\n\nWARNING - session:", 1)[0] + "\n\n(A real run would also warn about: session.)"


def test_decommission_screen_without_question():
    text = decommission(["rack1", "rack2", "rack3"], confirm=False)
    assert text.split("\n")[1] == "cassandra_operation_confirm is false: no question, the run goes on."
    assert "WARNING - session:" in text


def test_decommission_one_rack_names_the_datacenter_and_the_next_node():
    text = decommission(["rack1"])
    assert "    data goes to the other nodes of dc1: node1, node2, node3, node4, node5, node6, node8\n" \
           "    node8 is removed later and hands this data on again\n" in text
    assert "    data goes to the other nodes of dc1: node1, node2, node3, node4, node5, node6\n" \
           "    runs on node8" in text


def test_decommission_earlier_run_states():
    text = decommission(["rack1", "rack2", "rack3"], states={"node7": "decommissioned", "node8": "leaving"})
    assert "  node7  10.0.0.7  dc1 / rack1\n    not a seed\n" \
           "    already out of the ring (an earlier run): Cassandra only stopped and disabled on it\n\n" in text
    assert "    still leaving (a decommission an earlier run started): waited for\n" \
           "    load 48.1 GiB, owns 33.4%\n" in text
    assert "    followed on node8, the ring checked from node1 before and after\n" in text
    assert "runs on node8" not in text


def test_decommission_forced_replication_warning_shows_under_check_too():
    for check in (False, True):
        text = decommission(["rack1"], leaving=("node7", "node8", "node6", "node5", "node4", "node3"), force=True,
                            problems_rf=3, check=check)
        assert "\n\nWARNING - replication: cassandra_decommission_force is true: the removal goes on although too few\n" \
               "  nodes are left for some keyspaces; those replicas are lost and QUORUM can fail:\n" \
               "  - orders needs 3 replicas in dc1, which would keep 2 node(s)\n" in text


def test_decommission_simple_strategy_noted():
    rows = ['{"keyspace_name": "system_auth", "replication": {"class": "org.apache.cassandra.locator.SimpleStrategy",'
            ' "replication_factor": "1"}}',
            '{"keyspace_name": "orders", "replication": {"class": "org.apache.cassandra.locator.NetworkTopologyStrategy",'
            ' "dc1": "3"}}']
    # racks = RF: the rack, but SimpleStrategy ignores racks
    text = decommission(["rack1", "rack2", "rack3"], keyspaces=rows)
    assert "    data goes to the other nodes of rack1 (3 racks in dc1, its replication factor): node1, node4;\n" \
           "      SimpleStrategy keyspaces (system_auth): any node of the cluster\n" in text
    # one datacenter, no rack: nothing to add
    assert "SimpleStrategy" not in decommission(["rack1"], keyspaces=rows)


# The other screens: rendered with neutral data, header and blocks as expected

def preflight(i, rack="rack1", **extra):
    p = {"address": "10.0.0.%d" % i, "ring_address": "10.0.0.%d" % i, "cassandra_dc": "dc1", "cassandra_rack": rack,
         "cassandra_cluster_name": "Orders", "cassandra_version": "41x", "cassandra_endpoint_snitch": "GossipingPropertyFileSnitch",
         "layout": {"seed": i == 1}}
    p.update(extra)
    return p


def test_replace_node_screen():
    t, play = task("replace_node.yml", "Show the plan and confirm the replacement")
    dead = {"address": "10.0.0.4", "rack": "rack2", "load": "50 GiB", "status": "D", "state": "N"}
    variables = {"inventory_hostname": "node9", "_cassandra_preflight": preflight(9, "rack2"),
                 "groups": {"prod": ["node1", "node9"]}, "cassandra_hosts": "prod", "ansible_play_hosts_all": ["node9"],
                 "cassandra_replace_address": "10.0.0.4", "cassandra_preflight_describe": {"stdout": DESCRIBE},
                 "cassandra_replace_ring": {"cluster_status": {"dc1": {"nodes": [dead]}}}}
    text = screen(variables, t["vars"], play.get("vars"))
    assert text == (
        "replace_node on cluster 'Orders' (Cassandra 4.1.5): replace the dead node 10.0.0.4 with node9\n\n"
        "  node9  10.0.0.9  dc1 / rack2\n"
        "    replaces 10.0.0.4 (DN in dc1 / rack2, load 50 GiB, as node1 sees it)\n"
        "    takes over its tokens and streams their data from the other replicas (hours on a big node)\n"
        "    end state: node9 up and normal (UN) in its place\n\n"
        "WARNING - session: this run is not inside tmux or screen: if the SSH session to this machine drops,\n"
        "  the run stops (the operation itself goes on, unwatched). Run it inside tmux or screen.")
    # the reset's plan (reset_node_plan.yml) on this screen: the one question covers it
    plan = {"delete": ["/var/lib/cassandra/data/system"], "stop": True, "disable": False,
            "dirs": ["/var/lib/cassandra/data (data; from the inventory): 1 entries: system"]}
    reset_vars = dict(variables, cassandra_replace_node_reset=True, _cassandra_node_reset_plan=plan)
    reset = screen(reset_vars, t["vars"], play.get("vars"))
    assert reset.endswith(
        "WARNING - data loss: node9: Cassandra stopped, then 1 entries DELETED for good (no snapshot, no\n"
        "  backup), in:\n"
        "    /var/lib/cassandra/data (data; from the inventory): 1 entries: system\n\n"
        "WARNING - session: this run is not inside tmux or screen: if the SSH session to this machine drops,\n"
        "  the run stops (the operation itself goes on, unwatched). Run it inside tmux or screen."), reset
    reset = screen(reset_vars, t["vars"], play.get("vars"), check=True)
    assert reset.endswith("\n\n(A real run would also warn about: data loss, session.)")


def test_resets_asked_on_the_operation_screen_only():
    # add_node and replace_node: the reset is planned before their screen, applied after their
    # one question without a question of its own; reset_node alone asks its own
    tasks = load("roles", "cassandra_service", "tasks", "reset_node.yml")
    confirm = next(x for x in tasks if x.get("name") == "Confirm the reset")
    assert "not _cassandra_node_reset_confirmed | default(false) | bool" in confirm["when"]
    for playbook, screen_task in (("add_node.yml", "Show the plan and confirm the add"),
                                  ("replace_node.yml", "Show the plan and confirm the replacement")):
        names = [t.get("name") for play in load("playbooks", playbook) for t in walk(play.get("tasks", []))]
        plan = [i for i, n in enumerate(names) if n and n.startswith("Work out the reset of")]
        apply = [i for i, n in enumerate(names) if n and n.startswith("Reset the ")]
        assert len(plan) == 1 and len(apply) == 1 and plan[0] < names.index(screen_task) < apply[0], playbook
        t, play = task(playbook, names[apply[0]])
        assert t["vars"]["_cassandra_node_reset_confirmed"] is True, playbook
        assert task(playbook, names[plan[0]])[0]["ansible.builtin.include_role"]["tasks_from"] == "reset_node_plan.yml"
    assert "_cassandra_node_reset_confirmed" not in open(os.path.join(TOP, "playbooks", "reset_node.yml")).read()


@pytest.mark.parametrize("method, line, warning", [
    ("removenode", "    removenode from node1: its ranges are streamed from the other replicas (hours on a big node)", None),
    ("assassinate", "    assassinate: out of gossip, no streaming",
     "WARNING - data loss: assassinate: data only this node held is LOST."),
])
def test_remove_dead_node_screen(method, line, warning):
    t, play = task("remove_dead_node.yml", "Show the plan and confirm")
    dead = {"address": "10.0.0.4", "rack": "rack2", "load": "50 GiB", "status": "D", "state": "N"}
    variables = {"ansible_play_hosts": ["node1", "node2"], "cassandra_dead_node_address": "10.0.0.4",
                 "cassandra_dead_node_method": method, "cassandra_dead_node": dead,
                 "cassandra_dead_ring": {"cluster_status": {"dc1": {"nodes": [dead]}}},
                 "cassandra_dead_removal": {"state": "start"}, "_cassandra_preflight": preflight(1),
                 "hostvars": {"node1": {"_cassandra_preflight": preflight(1)}, "node2": {"_cassandra_preflight": preflight(2)}},
                 "cassandra_dead_keyspaces": {"out": ""}}
    text = screen(variables, t["vars"], play["vars"])
    lines = text.split("\n")
    assert lines[0] == "remove_dead_node on cluster 'Orders': take 10.0.0.4 out of the ring with %s" % method
    assert "  10.0.0.4  DN  dc1 / rack2, load 50 GiB" in lines
    assert line in lines
    forced = screen(dict(variables, cassandra_decommission_force=True,
                         cassandra_dead_keyspaces={"out": '{"keyspace_name": "orders", "replication": {"class":'
                                                   ' "org.apache.cassandra.locator.NetworkTopologyStrategy", "dc1": "3"}}'}),
                    t["vars"], play["vars"])
    assert "WARNING - replication: cassandra_decommission_force is true: the removal goes on although too few\n" \
           "  nodes are left for some keyspaces:\n  - orders needs 3 replicas in dc1, which would keep 2 node(s)" in forced
    assert "WARNING - replication" not in text
    if warning:
        assert warning in lines
        # what the method does: shown under --check too
        checked = screen(variables, t["vars"], play["vars"], check=True).split("\n")
        assert warning in checked and "(A real run would also warn about: session.)" in checked
    else:
        assert "WARNING - data loss" not in text


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
           "  replica of some data down:\n  - already down in dc1: 10.0.0.9\n\n" in forced
    text = screen(variables, t["vars"], play["vars"])
    assert text == (
        "stop_rack on cluster 'Orders': stop dc1 / rack2\n\n"
        "Stops Cassandra on every node of the rack at once; they stay down until start_rack.\n\n"
        "  node2  10.0.0.2  dc1 / rack2\n\n"
        "WARNING - replication: orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails\n\n"
        "WARNING - down nodes: down in another datacenter: 10.0.1.5")


def test_apply_config_and_update_java_screens():
    t, play = task("apply_config.yml", "Show the plan and confirm the run")
    hostvars = {"node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_reasons": ["cassandra.yaml to change"],
                          "cassandra_apply_config_then": "restart"},
                "node2": {"cassandra_apply_config_todo": False},
                "node3": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "none",
                          "cassandra_apply_config_reasons": ["owner, group or mode to change: cassandra.yaml"]},
                "node4": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "start",
                          "cassandra_apply_config_reasons": ["owner, group or mode to change: cassandra-env.sh"]},
                "node5": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "write",
                          "cassandra_apply_config_reasons": ["cassandra.yaml to change"]}}
    text = screen({"ansible_play_hosts": ["node1", "node2", "node3", "node4", "node5"], "hostvars": hostvars}, t["vars"],
                  play["vars"], check=True)
    assert text == ("apply_config: would apply the config on node1, node3, node4, node5\n"
                    "--check: nothing will be changed (the plan only, no question).\n\n"
                    "A real run would write each node in turn, then restart or start it as said below.\n\n"
                    "  node1\n    cassandra.yaml to change\n    then drained and restarted\n\n"
                    "  node3\n    owner, group or mode to change: cassandra.yaml\n    no restart (Cassandra reads its config at start)\n\n"
                    "  node4\n    owner, group or mode to change: cassandra-env.sh\n"
                    "    its Cassandra is not running: started once written (down over max_hint_window? repair it)\n\n"
                    "  node5\n    cassandra.yaml to change\n    its Cassandra is stopped: written, left stopped (read when it starts)")
    text = screen({"ansible_play_hosts": ["node1", "node2"], "hostvars": hostvars}, t["vars"], play["vars"])
    assert text.startswith("apply_config: apply the config on node1\n\n"
                           "Writes the config one node at a time, then restarts or starts it as said below.\n\n")
    t, play = task("update_java.yml", "Show the plan and confirm the run")
    text = screen({"groups": {"cassandra_update_java_True": ["node2"]}, "cassandra_java_version": 17,
                   "hostvars": {"node2": {"cassandra_update_java_running": "Java 11"}}}, t["vars"], play.get("vars"))
    assert text == ("update_java: move node2 to Java 17\n\nMoves these nodes to the new Java, one at a time (install, config, drain, restart).\n\n"
                    "  node2\n    runs Java 11")


def test_change_seeds_and_upgrade_screens():
    t, play = task("change_seeds.yml", "Show the new seed list and confirm")
    hostvars = {"node1": {"inventory_hostname": "node1", "_cassandra_preflight": {"live_seeds": "10.0.0.1"}},
                "node2": {"inventory_hostname": "node2", "_cassandra_preflight": {"live_seeds": "10.0.0.1,10.0.0.2"}}}
    text = screen({"ansible_play_hosts": ["node1", "node2"], "hostvars": hostvars, "_new": "10.0.0.1,10.0.0.2"}, t["vars"])
    assert text == ("change_seeds: the seed list in cassandra.yaml, reloaded live (no restart)\n\n"
                    "  node1\n    from: 10.0.0.1\n    to:   10.0.0.1,10.0.0.2")
    t, play = task("upgrade.yml", "Show the plan and confirm")
    text = screen({"cassandra_upgrade_from": "4.0.20", "cassandra_package_version": "4.1.5"}, t["vars"])
    assert text.startswith("upgrade: phase prepare, 4.0.20 -> 4.1.5\n\nBefore the upgrade, check that:\n"
                           "  - backups are paused")
    assert text.endswith("\n\nprepare now snapshots every node (pre-upgrade-4.0.20) and copies its config.")


def test_datacenter_screens():
    t, play = task("remove_datacenter.yml", "Show the plan and confirm")
    hostvars = {"node5": {"_cassandra_preflight": preflight(5)}}
    variables = {"_leaving": ["node5"], "_staying": ["node1"], "hostvars": hostvars, "cassandra_target_dc": "dc2",
                 "cassandra_remove_dc_alter": ["ALTER KEYSPACE \"orders\" WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3};"]}
    text = screen(variables, t["vars"], check=True)
    assert text == (
        "remove_datacenter: remove dc2\n--check: nothing will be changed (the plan only, no question).\n\n"
        "First the replication changes, from node1:\n"
        "  ALTER KEYSPACE \"orders\" WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3};\n"
        "Then these nodes are removed, one at a time:\n\n"
        "  node5  10.0.0.5  dc1 / rack1\n\n"
        "WARNING - clients: clients still using dc2 will fail.\n\n"
        "(A real run would also warn about: session.)")
    t, play = task("add_datacenter.yml", "Show the plan and confirm")
    hostvars = {"node9": {"_cassandra_preflight": preflight(9, cassandra_dc="dc2")}}
    variables = {"ansible_play_hosts_all": ["node9"], "hostvars": hostvars, "cassandra_rebuild_source_dc": "dc1",
                 "cassandra_new_dc_alter": ["ALTER KEYSPACE \"orders\" ..."], "cassandra_new_dc_tokens": {"lines": ["+ new node", "dc2 ring"]}}
    variables.update(trust(play["vars"]))
    variables["_p0"] = {"cassandra_num_tokens": 1}
    variables["_new_dc"] = "dc2"
    text = screen(variables, t["vars"])
    assert "  node9  10.0.0.9  dc2 / rack1\n    streams its data from dc1 (nodetool rebuild)\n" in text
    assert "One token per node, the new datacenter's ring:\n  dc2 ring\n" in text


def test_move_node_and_create_cluster_screens():
    t, play = task("move_node.yml", "Show the plan and confirm the moves")
    plan = {"steps": [{"name": "node2"}], "lines": ["dc1 ring"], "warnings": ["dc1: loads not all known"]}
    text = screen({"_plan": plan, "_cassandra_preflight": preflight(1), "cassandra_move_cleanup": "none"}, t["vars"])
    assert text.split("\n", 1)[0] == "move_node on cluster 'Orders': move node2"
    assert text.split("\n\n")[2:] == ["dc1 ring\nCleanup afterwards (cassandra_move_cleanup=none).",
                                      "WARNING - tokens: dc1: loads not all known", "WARNING - session: this run is not inside"
                                      " tmux or screen: if the SSH session to this machine drops,\n  the run stops (the operation"
                                      " itself goes on, unwatched). Run it inside tmux or screen."]
    t, play = task("create_cluster.yml", "Show the ring and confirm the tokens")
    text = screen({"_plan": {"tokens": {"node1": "0"}, "lines": ["dc1 ring *"], "warnings": []}, "_running": [],
                   "ansible_play_hosts": ["node1"], "_cassandra_preflight": preflight(1)}, t["vars"])
    assert text == ("create_cluster on cluster 'Orders': the tokens of the new nodes (one token per node)\n\n"
                    "The ring (RF of the shares: cassandra_token_rf):\n\ndc1 ring *")


def test_create_cluster_reset_screen():
    t, play = task("create_cluster.yml", "Show what the reset does")
    hostvars = {"node1": {"_cassandra_preflight": preflight(1), "_cassandra_preflight_running": True}}
    variables = {"ansible_play_hosts": ["node1"], "hostvars": hostvars, "_cassandra_preflight": preflight(1),
                 "_cassandra_create_reset_check": {"info": ["node1 holds data"], "problems": []}}
    # no question here (the name is typed next): no word about cassandra_operation_confirm
    assert "cassandra_operation_confirm" not in screen(variables, t["vars"], confirm=False)
    for check, end in ((False, "WARNING - data loss: every node is stopped and emptied: ALL THE DATA of cluster 'Orders' is LOST."),
                       (True, "(A real run would also warn about: data loss.)")):
        text = screen(variables, t["vars"], check=check)
        assert text.startswith("create_cluster on cluster 'Orders': wipe the cluster and create it again")
        assert "\n\n  node1  10.0.0.1  dc1 / rack1\n    Cassandra running now, then joins as above, a seed\n\n" \
               "node1 holds data\n\n" in text
        assert text.endswith(end)


def test_reset_screen():
    tasks = load("roles", "cassandra_service", "tasks", "reset_node.yml")
    t = next(x for x in tasks if x.get("name") == "Confirm the reset")
    plan = {"delete": ["a", "b"], "stop": True, "disable": True, "dirs": ["/var/lib/cassandra/data (2 entries)"]}
    text = screen({"ansible_play_hosts": ["node7"], "hostvars": {"node7": {"_cassandra_node_reset_plan": plan}}}, t["vars"])
    assert text == (
        "reset_node: empty node7\n\n"
        "  node7\n    stop Cassandra\n    keep it from starting at boot\n    2 entries to delete, in:\n"
        "    /var/lib/cassandra/data (2 entries)\n\n"
        "WARNING - data loss: the reset DELETES everything these directories hold, for good (no snapshot, no\n"
        "  backup). Only for a node that never joined the cluster, or one being replaced.")


def test_every_confirmation_goes_through_the_screen():
    # no playbook builds its own prompt without its plan: through screen.yml, or (the plan layout of
    # module_utils cassandra_output, OUTPUT Q5) confirm.yml right after the task that prints the plan
    for name in os.listdir(os.path.join(TOP, "playbooks")):
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


def test_apply_config_recap():
    # the end of the run: what each node got, from what the role computed (diffs masked by it)
    t, play = task("apply_config.yml", "Print the changes")
    perms = [{"item": "/etc/cassandra/conf/cassandra.yaml (owner:group mode)", "before": "root:cassandra 0640",
              "after": "cassandra:dbgrp 0640"}]
    diff = "--- /etc/cassandra/conf/cassandra.yaml (live)\n+++ (new)\n@@ -1 +1 @@\n-concurrent_writes: 32\n+concurrent_writes: 48\n"
    hostvars = {"node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "none", "cassandra_op_done": True,
                          "cassandra_op_result": "would apply", "_cassandra_config_items": perms},
                "node2": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart", "cassandra_op_done": True,
                          "cassandra_apply_config_pending": "True", "cassandra_config_newer_files": ["jvm11-server.options"],
                          "_cassandra_config_items": [{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}]},
                "node3": {"cassandra_apply_config_todo": False, "cassandra_op_result": "nothing to apply"},
                "node4": {"cassandra_apply_config_todo": True},
                "node5": {}}
    variables = {"hostvars": hostvars, "groups": {"prod": list(hostvars)}, "cassandra_hosts": "prod",
                 "ansible_play_hosts_all": ["node1", "node2", "node3", "node4"]}

    def recap(check):
        variables.update(trust(t["vars"]), ansible_check_mode=check)
        return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(t["ansible.builtin.debug"]["msg"]))

    variables["cassandra_cluster_name"] = "my_cluster"
    assert recap(True) == [
        "CHECK  apply_config  my_cluster  2 would apply, 1 nothing to apply, 1 not reached, 1 not in this run",
        "node1  would apply, no restart",
        "  cassandra.yaml    owner/group/mode  root:cassandra 0640 -> cassandra:dbgrp 0640",
        "node2  would apply, then restart",
        "  restart pending: jvm11-server.options changed since the running Cassandra started",
        "  cassandra.yaml",
        "    - concurrent_writes: 32",
        "    + concurrent_writes: 48",
        "node3  nothing to apply",
        "node4  not reached",
        "node5  not in this run (--limit)"]
    # a real run showed the changes in its plan, before its question: the outcomes here
    assert recap(False) == [
        "DONE  apply_config  my_cluster  2 applied, 1 nothing to apply, 1 not reached, 1 not in this run",
        "node1  applied, no restart",
        "node2  applied, restarted",
        "  restart pending: jvm11-server.options changed since the running Cassandra started",
        "node3  nothing to apply",
        "node4  not reached",
        "node5  not in this run (--limit)"]
    # how long the run took, from its start (the first play)
    hostvars["node3"]["_cassandra_apply_config_start"] = 0
    assert re.match(r"^DONE  apply_config  my_cluster  2 applied, .* not in this run [(][0-9dhms]+[)]$", recap(False)[0])


def test_apply_config_plan_shows_the_changes_before_the_question():
    t, play = task("apply_config.yml", "Show the plan and confirm the run")
    diff = "--- /etc/cassandra/conf/cassandra.yaml (live)\n+++ (new)\n@@ -1 +1 @@\n-concurrent_reads: 32\n+concurrent_reads: 48\n"
    hostvars = {"node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
                          "cassandra_apply_config_reasons": ["cassandra.yaml to change"],
                          "_cassandra_config_items": [{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}]},
                "node2": {"cassandra_apply_config_todo": False}}

    def blocks(check):
        variables = dict(trust(t["vars"]), **trust(play["vars"]), hostvars=hostvars, ansible_play_hosts=["node1", "node2"],
                         ansible_check_mode=check)
        return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(t["vars"]["_blocks"]))

    assert blocks(False) == [{"title": "node1", "lines": ["cassandra.yaml to change", "then drained and restarted",
                                                         {"pre": ["  cassandra.yaml", "    - concurrent_reads: 32",
                                                                  "    + concurrent_reads: 48"]}]}]
    # --check asks nothing: the changes are in the recap
    assert blocks(True) == [{"title": "node1", "lines": ["cassandra.yaml to change", "then drained and restarted"]}]


# node_operation.yml and restart_batch.yml: test_check_mode_results.py
@pytest.mark.parametrize("tasks_file,would,done", [("cleanup_batch.yml", "would clean up", "cleanup done in ")])
def test_batch_result_under_check(tasks_file, would, done):
    tasks = load("roles", "cassandra_service", "tasks", tasks_file)
    record = next(t for t in walk(tasks) if t.get("name") == "Record the result")["ansible.builtin.set_fact"]

    def result(check):
        variables = {"ansible_check_mode": check, "_cassandra_restart_start": 0, "_cassandra_cleanup_start": 0}
        return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(record["cassandra_op_result"]))

    assert result(True) == would
    assert result(False).startswith(done)


def test_apply_config_shows_the_changes_once():
    # the changes in the plan of a real run (before its question), in the recap under --check (an ops
    # callback message): not by the role while comparing or writing, nor by the change report
    t, play = task("apply_config.yml", "Print the changes")
    assert t["vars"]["cassandra_output"] is True
    compare, play = task("apply_config.yml", "Compare the config")
    assert compare["vars"]["_cassandra_change_report_quiet"] is True
    assert compare["vars"]["_cassandra_config_quiet"] is True
    write = next(t for t in load("roles", "cassandra_service", "tasks", "action_apply_config.yml")
                 if t.get("name") == "Write the config")
    assert write["vars"]["_cassandra_config_quiet"] is True and write["vars"]["_cassandra_change_report_quiet"] is True

    role = list(walk(load("roles", "cassandra_config", "tasks", "main.yml")))
    report = load("roles", "cassandra_change_report", "tasks", "main.yml")
    shows = [next(t for t in role if t.get("name") == "Show the config changes"),
             next(t for t in role if t.get("name") == "Show the owner, group and mode changes"),
             next(t for t in report if t.get("name", "").startswith("Show changes"))]

    def shown(show, **quiet):
        variables = dict(quiet, _cassandra_config_changes=["cassandra.yaml"], cassandra_change_report_items=[{"item": "x"}],
                         _cassandra_config_perm_changes=[{"item": "cassandra.yaml (owner:group mode)"}],
                         _cassandra_config_dir_changes=[], _cassandra_config_dir_notes=[])
        templar = Templar(loader=DataLoader(), variables=variables)
        return all(templar.template(trust_as_template("{{ (%s) | bool }}" % c)) for c in show["when"])

    assert [shown(s) for s in shows] == [True, True, True]  # site.yml and the other callers: as before
    assert [shown(s, _cassandra_change_report_quiet=True) for s in shows] == [True, True, False]  # compare step
    assert [shown(s, _cassandra_config_quiet=True, _cassandra_change_report_quiet=True) for s in shows] == [False, False, False]


def test_apply_config_counts_a_directory_owner_change():
    # a data dir Cassandra cannot write: an owner or mode change (no restart of a running node)
    t, play = task("apply_config.yml", "Note what this node needs")
    dirs = [{"item": "data dir /srv/data (owner:group mode)", "path": "/srv/data", "dir": "data dir",
             "before": "root:cassandra 0750", "after": "cassandra:cassandra 0750"}]
    variables = dict(trust(t["vars"]), _cassandra_config_changes=[], _cassandra_config_perm_changes=[],
                     _cassandra_config_dir_changes=dirs, cassandra_config_restart_pending=False, cassandra_jvm={},
                     _cassandra_preflight_running=True)
    templar = Templar(loader=DataLoader(), variables=variables)
    facts = t["ansible.builtin.set_fact"]
    assert templar.template(trust_as_template(facts["cassandra_apply_config_todo"])) is True
    assert templar.template(trust_as_template(facts["cassandra_apply_config_then"])) == "none"
    assert templar.template(trust_as_template(facts["cassandra_apply_config_reasons"])) == [
        "owner, group or mode to change: data dir /srv/data"]
