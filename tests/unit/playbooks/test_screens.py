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

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import PARAGRAPH as P
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

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


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    """The views laid out at the terminal's width (width='terminal'): a wide one here, whatever runs the tests."""
    from ansible_collections.community.cassandra.plugins.filter import cassandra_apply_config
    monkeypatch.setattr(cassandra_apply_config, "terminal_width", lambda: 200)


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


APPLY_HOSTVARS = {
    "node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart"},
    "node2": {"cassandra_apply_config_todo": False, "cassandra_op_result": "nothing to apply"},
    "node3": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "none",
              "cassandra_apply_config_notes": ["WARNING - data dir /srv/data: not owned by cassandra: /srv/data/ks1"]},
    "node4": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "start"},
    "node5": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "write"}}
# the group "Group the nodes to do" puts them in
APPLY_GROUPS = {"cassandra_apply_config_True": [h for h, v in sorted(APPLY_HOSTVARS.items()) if v["cassandra_apply_config_todo"]]}
APPLY_DIFF = ["cassandra.yaml", "  concurrent_reads: 32   node2   (inventory)",
              u"                    48   node1   \u2190 differs"]


def test_apply_config_screen():
    t, play = task("apply_config.yml", "Show the plan and confirm the run")
    hosts = ["node1", "node2", "node3", "node4", "node5"]
    text = screen({"ansible_play_hosts": hosts, "hostvars": APPLY_HOSTVARS, "groups": APPLY_GROUPS,
                   "cassandra_apply_config_diff": APPLY_DIFF}, t["vars"], play["vars"], check=True)
    # --check: what each node would get; the settings that differ in the recap (no question)
    assert text == ("apply_config: would apply the config on node1, node3..node5\n\n"
                    "A real run would write the nodes not restarted at once, then each other node in turn, restarting\n"
                    "or starting it as said below; the settings that differ are in the recap at the end.\n\n"
                    "node1  would apply, then restart\n"
                    "node3  would apply, no restart (an owner, group or mode only: Cassandra reads them when it starts)\n"
                    "node4  would apply, then start (its Cassandra is not running: started once written (down over"
                    " max_hint_window? repair it))\n"
                    "node5  would apply, left stopped (its Cassandra is stopped: left stopped, reads its config when it starts)\n"
                    "node2  nothing to apply\n\n"
                    "WARNING - node3: data dir /srv/data: not owned by cassandra: /srv/data/ks1\n\n"
                    "--check: nothing will be changed (the plan only, no question).")
    # a real run: the settings that differ, then the outcomes, before its question
    text = screen({"ansible_play_hosts": ["node1", "node2"], "hostvars": APPLY_HOSTVARS, "groups": APPLY_GROUPS,
                   "cassandra_apply_config_diff": APPLY_DIFF}, t["vars"], play["vars"])
    assert text == ("apply_config: apply the config on node1\n\n"
                    "Writes the config on the nodes not restarted at once, then on the others one node at a time,\n"
                    "restarting or starting each as said below.\n\n"
                    + "\n".join(APPLY_DIFF) + "\n\n"
                    "node1  will apply, then restart\nnode2  nothing to apply")
    question = Templar(loader=DataLoader(), variables=dict(trust(t["vars"]), **trust(play["vars"]), ansible_play_hosts=hosts,
                                                           hostvars=APPLY_HOSTVARS, groups=APPLY_GROUPS))
    assert question.template(trust_as_template(t["vars"]["cassandra_screen_question"])) == \
        "Apply the config on node1, node3..node5?"


def test_change_seeds_screen():
    t, play = task("change_seeds.yml", "Show the new seed list and confirm")
    hostvars = {"node1": {"inventory_hostname": "node1", "_cassandra_preflight": {"live_seeds": "10.0.0.1"}},
                "node2": {"inventory_hostname": "node2", "_cassandra_preflight": {"live_seeds": "10.0.0.1,10.0.0.2"}}}
    text = screen({"ansible_play_hosts": ["node1", "node2"], "hostvars": hostvars, "_new": "10.0.0.1,10.0.0.2",
                   "_cassandra_change_seeds": {"auto": [], "warnings": []}}, t["vars"], {"_changed": play["vars"]["_changed"]})
    assert text == ("change_seeds: the seed list in cassandra.yaml, reloaded live (no restart)\n\n"
                    "  node1\n    from: 10.0.0.1\n    to:   10.0.0.1,10.0.0.2")
    # a datacenter cassandra_seeds leaves with no seed: the seeds picked there, in the plan, with a warning
    auto = {"auto": [{"dc": "dc2", "labels": ["node3 (dc2/r1)"]}],
            "warnings": ["dc2 would be left with no seed (node4 leaves the seed list): node3 (dc2/r1) made its seed, live"
                         " only: put it in cassandra_seeds (apply_config would put the inventory's list back)"]}
    text = screen({"ansible_play_hosts": ["node1", "node2"], "hostvars": hostvars, "_new": "10.0.0.1,10.0.0.3",
                   "_cassandra_change_seeds": auto}, t["vars"], {"_changed": play["vars"]["_changed"]})
    assert "\nChange seeds: dc2 gets node3 (dc2/r1), not in cassandra_seeds.\n" in text
    assert "WARNING - seeds: dc2 would be left with no seed (node4 leaves the seed list): node3 (dc2/r1) made" in text


def test_apply_config_recap():
    # the end of the run: under --check the settings that differ (computed before the plan), then the outcomes
    t, play = task("apply_config.yml", "Print the changes")
    hostvars = {"node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "none", "cassandra_op_done": True,
                          "cassandra_op_result": "would apply", "cassandra_apply_config_diff": APPLY_DIFF},
                "node2": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart", "cassandra_op_done": True,
                          "cassandra_apply_config_notes": ["restart pending (jvm11-server.options changed since the running"
                                                           " Cassandra started)"], "cassandra_apply_config_diff": APPLY_DIFF},
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
        "CHECK  apply_config  my_cluster  2 would apply, 1 nothing to apply, 1 not reached, 1 not in this run", ""] + APPLY_DIFF + [
        "",
        "node1  would apply, no restart",
        "node2  would apply, then restart",
        "  restart pending (jvm11-server.options changed since the running Cassandra started)",
        "node3  nothing to apply",
        "node4  not reached",
        "node5  not in this run (--limit)"]
    # a real run showed the settings in its plan, before its question: the outcomes here (node2 restarted by
    # this run: no restart pending left)
    assert recap(False) == [
        "DONE  apply_config  my_cluster  2 applied, 1 nothing to apply, 1 not reached, 1 not in this run",
        "node1  applied, no restart",
        "node2  applied, restarted",
        "node3  nothing to apply",
        "node4  not reached",
        "node5  not in this run (--limit)"]
    # how long the run took, from its start (the first play)
    hostvars["node3"]["_cassandra_apply_config_start"] = 1000  # an epoch (0 would read as no start)
    assert re.match(r"^DONE  apply_config  my_cluster  2 applied, .* not in this run [(][0-9dhms]+[)]$", recap(False)[0])
    # the read-ahead set live on node1, node3 (no restart): the rolling restart to take it, optional, for the cluster
    # as the run named it
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import READ_AHEAD_LIVE
    for h in ("node1", "node3"):
        hostvars[h].update(cassandra_apply_config_os=[{"item": "read_ahead_kb sdb"}], cassandra_apply_config_os_done=True,
                           cassandra_apply_config_notes=[READ_AHEAD_LIVE])
    lines = recap(False)
    assert lines[-4:] == [
        "",
        "TO DO",
        "  1. optional, when convenient: restart node1, node3 so that Cassandra reads the files it has open with the new"
        " read-ahead (one node at a time; the new files use it already):",
        "     ansible-playbook community.cassandra.rolling_restart -e cassandra_hosts=prod --limit node1,node3"]
    assert "TO DO" not in recap(True)


def test_apply_config_reads_every_node_and_puts_the_settings_side_by_side():
    # the live files that differ on some node, read on each node (no_log, also under --check), then the view
    import base64
    listed, play = task("apply_config.yml", "List the files that differ on a node")
    assert listed["run_once"] is True  # once for all
    read, play = task("apply_config.yml", "Read the live files that differ on a node")
    assert read["no_log"] is True and read["check_mode"] is False
    side, play = task("apply_config.yml", "Put the settings that differ side by side")
    assert side["run_once"] is True
    diff = ("--- /etc/cassandra/conf/cassandra.yaml (live)\n+++ (new)\n@@ -1,2 +1,2 @@\n-concurrent_reads: 48\n"
            "+concurrent_reads: 32\n keystore_password: ****\n")
    items = {"node1": [{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}], "node2": [], "node3": []}
    live = {"node1": "concurrent_reads: 48\nkeystore_password: s3cret\n", "node2": "concurrent_reads: 32\nkeystore_password: s3cret\n",
            "node3": "concurrent_reads: 32\nkeystore_password: s3cret\n"}
    hostvars = dict((h, {"_cassandra_config_items": items[h], "cassandra_apply_config_live": {"results": [
        {"cassandra_apply_config_file": "cassandra.yaml", "content": base64.b64encode(live[h].encode()).decode()}]}})
        for h in items)
    variables = {"ansible_play_hosts": list(items), "hostvars": hostvars, "ansible_verbosity": 0}
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template(listed["ansible.builtin.set_fact"]["_cassandra_apply_config_differ"])) == ["cassandra.yaml"]
    templar = Templar(loader=DataLoader(), variables=dict(variables, **trust(side["vars"])))
    lines = templar.template(trust_as_template(side["ansible.builtin.set_fact"]["cassandra_apply_config_diff"]))
    assert lines == ["cassandra.yaml",
                     "  concurrent_reads: 32   node2, node3   (inventory)",
                     u"                    48   node1          \u2190 differs"]


def test_apply_config_shows_the_changes_once():
    # the changes in the plan of a real run (before its question), in the recap under --check (an ops
    # callback message): not by the role while comparing or writing, nor by the change report
    t, play = task("apply_config.yml", "Print the changes")
    assert t["vars"]["cassandra_output"] is True
    compare, play = task("apply_config.yml", "Compare the config")
    assert compare["vars"]["_cassandra_change_report_quiet"] is True
    assert compare["vars"]["_cassandra_config_quiet"] is True
    write = next(t for t in load("roles", "cassandra_operation", "tasks", "action_apply_config.yml")
                 if t.get("name") == "Write the config")
    assert write["vars"]["_cassandra_config_quiet"] is True and write["vars"]["_cassandra_change_report_quiet"] is True

    role = list(walk(load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "main.yml"))))
    report = load("roles", "cassandra_change_report", "tasks", "main.yml")
    shows = [next(t for t in role if t.get("name") == "Show the config changes"),
             next(t for t in role if t.get("name") == "Show the owner, group and mode changes"),
             next(t for t in report if t.get("name", "").startswith("Show changes"))]

    def shown(show, **quiet):
        variables = dict(quiet, _cassandra_config_changes=["cassandra.yaml"], cassandra_change_report_items=[{"item": "x"}],
                         _cassandra_config_perm_changes=[{"item": "cassandra.yaml (owner:group mode)"}],
                         _cassandra_config_dir_changes=[], _cassandra_config_dir_notes=[], _cassandra_config_ask=False,
                         ansible_check_mode=False)
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
    assert templar.template(trust_as_template(facts["cassandra_apply_config_notes"])) == []
