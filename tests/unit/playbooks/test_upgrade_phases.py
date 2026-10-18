from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# upgrade, run again after an interruption: the cleanup phase removes the pre-upgrade tags each node lists (not
# a tag named after the version the node runs by then) once every node is upgraded, a prepare run again keeps the
# snapshot it took (its date and the way out said), a node nodetool fails on is said with its error and the way
# out, and the schema may disagree while the versions are mixed. An unset phase and a series the playbook does
# not upgrade from are said in a sentence.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as _f:
    PLAYS = yaml.safe_load(_f)


def play(name):
    return next(p for p in PLAYS if p.get("name") == name)


def task(play_name, name):
    return next(t for t in play(play_name)["tasks"] if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_cleanup_removes_the_tags_each_node_lists():
    # cassandra_upgrade_from is read from the running node: the target by then, the tags were named after the source
    assert "cassandra_upgrade_from" not in str(play("Cleanup")["tasks"])
    listing = task("Cleanup", "List the snapshots")
    assert listing["ansible.builtin.command"].endswith(" listsnapshots") and listing["check_mode"] is False
    remove = task("Cleanup", "Remove the pre-upgrade snapshots")
    assert remove["ansible.builtin.command"].endswith(" clearsnapshot -t {{ item }}")
    assert "cassandra_snapshot_names('pre-upgrade-')" in remove["loop"]


def test_cleanup_says_what_it_removed_on_each_node():
    say = task("Cleanup", "Say what was removed")
    assert say["vars"]["cassandra_output"] is True

    def said(tags, check=False):
        variables = dict(say["vars"], _tags=tags, inventory_hostname="n2", ansible_check_mode=check)
        return " ".join(render(say["ansible.builtin.debug"]["msg"], **variables).split())

    assert said(["pre-upgrade-4.1.5"]) == "n2: 1 pre-upgrade snapshot removed: pre-upgrade-4.1.5"
    assert said(["pre-upgrade-4.1.5", "pre-upgrade-4.0.1"]) == (
        "n2: 2 pre-upgrade snapshots removed: pre-upgrade-4.1.5, pre-upgrade-4.0.1")
    assert said([]) == "n2: no pre-upgrade snapshot found"
    assert said(["pre-upgrade-4.1.5"], check=True) == "n2: 1 pre-upgrade snapshot would be removed (--check): pre-upgrade-4.1.5"


def test_cleanup_keeps_the_snapshots_until_every_node_is_upgraded():
    # a rolling phase interrupted, then cleanup: the way back stays; the nodes not on the target named, the way out
    names = [t["name"] for t in play("Cleanup")["tasks"]]
    assert names.index("Check every node runs the target version") < names.index("List the snapshots")
    check = task("Cleanup", "Check every node runs the target version")
    assert check["run_once"] is True
    hostvars = {"n1": {"cassandra_upgrade_done": True}, "n2": {"cassandra_upgrade_done": False}, "n3": {"cassandra_upgrade_done": "False"}}
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts_all=list(hostvars), cassandra_package_version="5.0.4")
    assert render(check["vars"]["_left"], **variables) == ["n2", "n3"]
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is False
    text = " ".join(render(check["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert text == ("n2, n3 do not run 5.0.4 yet: the pre-upgrade snapshots are kept until every node does. "
                    "Run the rolling phase first (then sstables). Nothing was removed.")
    hostvars["n3"]["cassandra_upgrade_done"] = True
    assert " ".join(render(check["ansible.builtin.assert"]["fail_msg"], **variables).split()).startswith("n2 does not run 5.0.4 yet")
    hostvars["n2"]["cassandra_upgrade_done"] = "True"
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is True


def test_prepare_run_again_keeps_the_snapshot_it_took():
    # nodetool snapshot refuses a tag that exists: the one an interrupted prepare took is kept, said so
    snapshot = task("Prepare", "Snapshot every keyspace")
    already = task("Prepare", "Say the snapshot is already there")
    assert "cassandra_snapshot_names('pre-upgrade-')" in snapshot["vars"]["_tags"]

    def takes(tags):
        variables = dict(snapshot["vars"], **dict(already["vars"], _tags=tags, cassandra_upgrade_from="4.1.5", inventory_hostname="n2"))
        return render("{{ %s }}" % snapshot["when"], **variables), render("{{ %s }}" % already["when"], **variables)

    assert takes([]) == (True, False)
    assert takes(["pre-upgrade-4.0.1"]) == (True, False)
    assert takes(["pre-upgrade-4.1.5"]) == (False, True)
    # a weeks-old one is the operator's call: its date where 4.1+ lists one, and the way out
    assert "cassandra_snapshot_dates('pre-upgrade-')" in already["vars"]["_dates"]

    def says(dates):
        variables = dict(already["vars"], _tags=["pre-upgrade-4.1.5"], _dates=dates, cassandra_upgrade_from="4.1.5", inventory_hostname="n2")
        return " ".join(render(already["ansible.builtin.debug"]["msg"], **variables).split())

    assert says({"pre-upgrade-4.1.5": "2026-10-02T09:12:41.317Z"}) == (
        "n2: snapshot pre-upgrade-4.1.5 already there (an earlier prepare took it, taken on 2026-10-02T09:12:41.317Z), kept. "
        "If it is stale (an old attempt from the same version): nodetool clearsnapshot -t pre-upgrade-4.1.5 on n2, then run prepare again.")
    assert says({}).startswith("n2: snapshot pre-upgrade-4.1.5 already there (an earlier prepare took it), kept. If it is stale")


def test_a_node_nodetool_fails_on_is_said_with_its_error_and_the_way_out():
    # an interrupted run may leave a node stopped (package upgraded, start failed), or JMX refuses nodetool there:
    # its last error line, per node, not a nodetool stack trace; the way out for a node not running
    read = task("Upgrade checks", "Read the version each node runs")
    assert read["failed_when"] is False
    names = [t["name"] for t in play("Upgrade checks")["tasks"]]
    assert names.index("Read the version each node runs") < names.index("Every node answers nodetool") < names.index("Note the versions")
    check = task("Upgrade checks", "Every node answers nodetool")
    assert check["run_once"] is True
    refused = "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused (Connection refused)'."
    hostvars = {"n1": {"inventory_hostname": "n1", "cassandra_upgrade_running": {"rc": 0, "stdout": "ReleaseVersion: 4.1.5", "stderr": ""}},
                "n2": {"inventory_hostname": "n2", "cassandra_upgrade_running": {"rc": 1, "stdout": "", "stderr": refused + "\n"}},
                "n3": {"inventory_hostname": "n3", "cassandra_upgrade_running": {
                    "rc": 1, "stdout": "", "stderr": "error: Authentication failed! Invalid username or password\n-- StackTrace --\n"
                                                     "java.lang.SecurityException: Authentication failed! Invalid username or password\n"}},
                "n4": {"inventory_hostname": "n4", "cassandra_upgrade_running": {"rc": 255, "stdout": "", "stderr": ""}}}
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars))
    assert render(check["vars"]["_failed"], **variables) == [
        "nodetool version failed on n2 (%s)" % refused,
        "nodetool version failed on n3 (java.lang.SecurityException: Authentication failed! Invalid username or password)",
        "nodetool version failed on n4 (no output)"]
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is False
    text = " ".join(render(check["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert text.startswith("nodetool version failed on n2 (%s); nodetool version failed on n3 (java.lang.SecurityException" % refused)
    assert "(no output). A node not running Cassandra: start it (systemctl start cassandra there, or apply_config --limit <node>" in text
    assert text.endswith("then run this phase again; a node already upgraded is skipped. Nothing was done.")
    del hostvars["n4"]
    variables["ansible_play_hosts"] = list(hostvars)
    hostvars["n2"]["cassandra_upgrade_running"]["rc"] = hostvars["n3"]["cassandra_upgrade_running"]["rc"] = 0
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is True


def test_an_unset_phase_is_said_before_anything_else():
    names = [t["name"] for t in play("Upgrade checks")["tasks"]]
    assert names[:2] == ["Give each node its own facts", "Check the phase"]
    check = task("Upgrade checks", "Check the phase")
    assert check["run_once"] is True
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], _phase="") is False
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], _phase="canary") is True


def test_a_series_the_playbook_does_not_upgrade_from_is_said():
    # 3.11 (or a series newer than the table): a sentence, not a template error on _paths[series]
    names = [t["name"] for t in play("Upgrade checks")["tasks"]]
    assert names.index("Note the versions") < names.index("This playbook upgrades from the series it knows") < names.index("Check the target")
    check = task("Upgrade checks", "This playbook upgrades from the series it knows")
    variables = dict(play("Upgrade checks")["vars"], inventory_hostname="n1", cassandra_upgrade_from="3.11.17",
                     cassandra_upgrade_from_series="311x")
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is False
    text = " ".join(render(check["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert text == "n1 runs 3.11.17 (series 311x): this playbook upgrades from 40x, 41x, 50x only. Nothing was done."
    variables.update(cassandra_upgrade_from="4.1.5", cassandra_upgrade_from_series="41x")
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is True


def test_the_schema_may_disagree_while_the_versions_are_mixed():
    # from the canary's restart to the last node's: preflight lets the versions differ from the rolling phase on
    # (the canary phase starts on one version); the health checks around each node take a schema disagreement as
    # a warning only while some nodes of the run are on the target and some not, each node's cassandra_upgrade_done
    # turned true by its upgrade: the canary's check before its restart and the last rolling node's check after
    # its own stop on one
    allowed = play("Preflight")["vars"]["cassandra_allow_mixed_versions"]
    assert [render(allowed, cassandra_upgrade_phase=phase) for phase in ["preflight", "prepare", "canary", "rolling", "sstables", "cleanup"]] == [
        False, False, False, True, True, True]
    # every node of the run, noted by the checks play: the canary play's hosts are the canary alone
    noted = task("Upgrade checks", "Note the nodes of the run")["ansible.builtin.set_fact"]
    assert noted == {"cassandra_upgrade_run_nodes": "{{ ansible_play_hosts_all }}"}
    mixed = play("Upgrade, one node at a time")["vars"]["cassandra_allow_mixed_versions"]
    assert "ansible_play_hosts" not in mixed

    def allowed_for(**done):
        hostvars = dict((node, {"cassandra_upgrade_done": value}) for node, value in done.items())
        return render(mixed, hostvars=hostvars, cassandra_upgrade_run_nodes=list(hostvars), ansible_play_hosts_all=["n1"])

    assert allowed_for(n1=False, n2=False, n3=False) is False  # the canary's check before its restart: nobody done yet
    assert allowed_for(n1=True, n2=False, n3="False") is True  # its check after: the canary on the target, the others not
    assert allowed_for(n1="True", n2=True, n3=False) is True  # the rolling phase, up to the last node's check before
    assert allowed_for(n1=True, n2=True, n3=True) is False  # the last node's check after: everybody done
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "cluster_health_read.yml"), encoding="utf-8") as f:
        text = f.read()
    assert "mixed_versions=cassandra_allow_mixed_versions | default(false) | bool" in text


def test_a_node_is_noted_as_upgraded_once_it_runs_the_target():
    # the flag above reads it: set after the version check of its restart (not under --check: nothing was upgraded)
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "action_upgrade.yml"), encoding="utf-8") as f:
        tasks = yaml.safe_load(f)
    names = [t["name"] for t in tasks]
    assert names.index("Check it runs the target version") == names.index("Note it as upgraded") - 1
    noted = tasks[names.index("Note it as upgraded")]
    assert noted["ansible.builtin.set_fact"] == {"cassandra_upgrade_done": True} and noted["when"] == "not ansible_check_mode"
    # its own play reads the flag too: a node already on the target is said so before its upgrade would turn it
    names = [t["name"] for t in play("Upgrade, one node at a time")["tasks"]]
    assert names.index("Already upgraded") < names.index("Upgrade this node")
