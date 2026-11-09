from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# upgrade, found on a 4.1 -> 5.0 run: the prepare's config copy holds the files (copy's force: false copied
# none from a directory), the canary and rolling phases refuse nodes without the pre-upgrade snapshot, the
# sstables phase refuses a 5.0 target not in storage_compatibility_mode NONE, the rolling phase says the next
# mode step from the inventory (a re-run too), --check says what a real run would do, cleanup suggests no way
# back, a node down with the target package installed is pointed to apply_config, and apply_config's plan says
# NONE is the point of no return.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()  # the community.cassandra filters, whatever ran before

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as _f:
    PLAYS = yaml.safe_load(_f)


def play(name):
    return next(p for p in PLAYS if p.get("name") == name)


def task(play_name, name):
    return next(t for t in play(play_name)["tasks"] if t.get("name") == name)


def names(play_name):
    return [t["name"] for t in play(play_name)["tasks"]]


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def text(template, **variables):
    return " ".join(str(render(template, **variables)).split())


def test_prepare_copies_the_config_files_and_checks_the_copy():
    copy = task("Prepare", "Copy the config")["ansible.builtin.copy"]
    assert "force" not in copy  # force: false copies nothing from a directory (an empty copy, said done)
    assert copy["remote_src"] is True and copy["src"].endswith("/") and copy["dest"].endswith("/")
    order = names("Prepare")
    assert (order.index("Look for an earlier config copy") < order.index("Copy the config")
            < order.index("Read the config copy") < order.index("Check the config copy holds cassandra.yaml")
            < order.index("Say what the next phase is"))
    # an earlier prepare's copy (with its cassandra.yaml) is kept: the canary's config may be the new one by then
    assert task("Prepare", "Copy the config")["when"] == "not cassandra_upgrade_conf_copied.stat.exists"
    assert task("Prepare", "Say the config copy is already there")["when"] == "cassandra_upgrade_conf_copied.stat.exists"
    check = task("Prepare", "Check the config copy holds cassandra.yaml")
    assert check["when"] == "not ansible_check_mode"
    variables = dict(check["vars"], inventory_hostname="n1", cassandra_conf_dir="/etc/cassandra/conf",
                     cassandra_upgrade_from="4.1.12")
    assert text(check["ansible.builtin.assert"]["fail_msg"], **variables) == (
        "n1: /etc/cassandra/conf.pre-upgrade-4.1.12 has no cassandra.yaml after the copy from /etc/cassandra/conf. "
        "The snapshot pre-upgrade-4.1.12 is taken. Way out: look at /etc/cassandra/conf on n1 (cassandra_conf_dir: "
        "the directory Cassandra reads), then run prepare again.")


def test_prepare_under_check_says_what_a_real_run_would_do():
    say = task("Prepare", "Say what the next phase is")["ansible.builtin.debug"]["msg"]
    variables = dict(cassandra_upgrade_from="4.1.12", cassandra_conf_dir="/etc/cassandra/conf")
    assert text(say, ansible_check_mode=True, **variables) == (
        "--check: nothing was done. A real run would take the snapshots pre-upgrade-4.1.12 and copy the config to "
        "/etc/cassandra/conf.pre-upgrade-4.1.12.")
    assert text(say, ansible_check_mode=False, **variables) == (
        "Snapshots pre-upgrade-4.1.12 and config copies in /etc/cassandra/conf.pre-upgrade-4.1.12. "
        "Next: -e cassandra_upgrade_phase=canary.")


LISTED = ("Snapshot Details: \n"
          "Snapshot name     Keyspace name Column family name True size Size on disk\n"
          "pre-upgrade-4.1.12 ks1          t1                 0 bytes   1.2 KiB\n")


def test_canary_and_rolling_refuse_a_node_without_its_pre_upgrade_snapshot():
    order = names("Upgrade checks")
    assert order.index("Note the versions") < order.index("List the snapshots of the nodes to upgrade") \
        < order.index("Every node to upgrade has its pre-upgrade snapshot")
    listing = task("Upgrade checks", "List the snapshots of the nodes to upgrade")
    assert listing["check_mode"] is False  # read under --check too: the canary --check before prepare refuses
    assert "not cassandra_upgrade_done | bool" in listing["when"]
    check = task("Upgrade checks", "Every node to upgrade has its pre-upgrade snapshot")
    assert check["run_once"] is True and check["when"] == "_phase in ['canary', 'rolling']"
    hostvars = {"n1": {"inventory_hostname": "n1", "cassandra_upgrade_from": "4.1.12",
                       "cassandra_upgrade_prepared": {"stdout": LISTED}},
                "n2": {"inventory_hostname": "n2", "cassandra_upgrade_from": "4.1.12",
                       "cassandra_upgrade_prepared": {"stdout": "Snapshot Details: \nThere are no snapshots\n"}},
                "n3": {"inventory_hostname": "n3", "cassandra_upgrade_from": "5.0.9",  # upgraded: not listed
                       "cassandra_upgrade_prepared": {"skipped": True}}}
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_upgrade_from="4.1.12")
    assert render(check["vars"]["_unprepared"], **variables) == ["n2"]
    assert text(check["ansible.builtin.assert"]["fail_msg"], **variables) == (
        "No snapshot pre-upgrade-4.1.12 on n2 (the prepare phase takes it). Way out: ansible-playbook "
        "community.cassandra.upgrade -e cassandra_upgrade_phase=prepare, then this phase again. Nothing was done.")
    hostvars["n2"]["cassandra_upgrade_prepared"]["stdout"] = LISTED
    assert render(check["vars"]["_unprepared"], **variables) == []


def _modes(modes):
    """[(inventory mode, cassandra.yaml mode)] -> hostvars n1.. (None: not set / not read)."""
    hostvars = {}
    for i, (inventory, yaml_mode) in enumerate(modes, 1):
        h = {"inventory_hostname": "n%d" % i}
        if inventory is not None:
            h["cassandra_storage_compatibility_mode"] = inventory
        if yaml_mode is not None:
            h["cassandra_storage_mode_read"] = {"rc": 0, "stdout": yaml_mode}
        hostvars["n%d" % i] = h
    return hostvars


def test_sstables_refuse_a_50_target_until_storage_compatibility_mode_is_none():
    read = task("Upgrade checks", "Read storage_compatibility_mode in its cassandra.yaml")
    check = task("Upgrade checks", "The sstables are rewritten once storage_compatibility_mode is NONE")
    assert read["ansible.builtin.include_role"]["tasks_from"] == "storage_mode_read.yml"
    assert check["when"] == read["when"] == ["_phase == 'sstables'", "cassandra_version == '50x'"]
    assert check["run_once"] is True

    def said(*modes):
        hostvars = _modes(modes)
        variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars))
        return (render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
                render(check["ansible.builtin.assert"]["fail_msg"], **variables))

    assert said(("NONE", "NONE"), (None, "NONE"))[0] is True
    ok, msg = said(("CASSANDRA_4", "CASSANDRA_4"), ("NONE", "NONE"))
    assert ok is False and msg.split("\n") == [
        "storage_compatibility_mode is not NONE on n1:",
        "  n1  inventory CASSANDRA_4  cassandra.yaml CASSANDRA_4",
        "In that mode 5.0 writes the sstables in the 4.x format: the sstables phase would rewrite them in it."
        " Nothing was done.",
        "WAY OUT  Now set cassandra_storage_compatibility_mode: UPGRADING in the inventory and run apply_config, then"
        " NONE and apply_config again (NONE is the point of no return). Then run this phase again."]
    # the inventory at NONE, apply_config not run: what the nodes read is still CASSANDRA_4 (it passed before)
    ok, msg = said(("NONE", "CASSANDRA_4"), ("NONE", "CASSANDRA_4"))
    assert ok is False and msg.split("\n")[:3] == [
        "storage_compatibility_mode is not NONE on n1, n2:", "  n1  inventory NONE  cassandra.yaml CASSANDRA_4",
        "  n2  inventory NONE  cassandra.yaml CASSANDRA_4"]
    ok, msg = said(("UPGRADING", "UPGRADING"))
    assert ok is False and "WAY OUT  Now set cassandra_storage_compatibility_mode: NONE in the inventory and run" in msg


def test_the_storage_mode_read_script(tmp_path):
    import subprocess
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "storage_mode_read.yml"), encoding="utf-8") as f:
        read = yaml.safe_load(f)[0]
    assert read["check_mode"] is False and read["register"] == "cassandra_storage_mode_read"
    path = tmp_path / "cassandra.yaml"

    def run(content):
        if content is not None:
            path.write_text(content)
        out = subprocess.run(["/bin/sh", "-c", read["ansible.builtin.shell"]], env={"F": str(path), "PATH": os.environ["PATH"]},
                             stdout=subprocess.PIPE, universal_newlines=True, check=False)
        return out.returncode, out.stdout.strip()
    assert run(None)[0] == 3  # no file: not read
    assert run("cluster_name: x\nstorage_compatibility_mode: UPGRADING  # the step\n") == (0, "UPGRADING")
    assert run("# storage_compatibility_mode: NONE\ncluster_name: x\n") == (0, "")


def next_phase(phase, check=False, modes=None, version="50x", read=None):
    say = task("Summary", "Say what the next phase is")
    modes = modes or [None]
    hostvars = _modes(zip(modes, read or [None] * len(modes)))
    variables = dict(say["vars"], hostvars=hostvars, ansible_play_hosts_all=list(hostvars), cassandra_version=version,
                     cassandra_upgrade_phase=phase, cassandra_package_version="5.0.9", ansible_check_mode=check)
    return text(say["ansible.builtin.debug"]["msg"], **variables)


def test_rolling_says_the_next_mode_step_from_the_inventory_a_re_run_too():
    # a rolling phase run again once every node runs 5.0 (cassandra_upgrade_from_series 50x by then): the same step
    assert "cassandra_upgrade_from_series" not in str(task("Summary", "Say what the next phase is"))
    assert next_phase("rolling", modes=["CASSANDRA_4"] * 3) == (
        "Every node runs 5.0.9. Now set cassandra_storage_compatibility_mode: UPGRADING in the inventory and run "
        "apply_config, then NONE and apply_config again (NONE is the point of no return). "
        "Then -e cassandra_upgrade_phase=sstables.")
    assert next_phase("rolling", modes=["UPGRADING"] * 3) == (
        "Every node runs 5.0.9. Now set cassandra_storage_compatibility_mode: NONE in the inventory and run "
        "apply_config (NONE is the point of no return). Then -e cassandra_upgrade_phase=sstables.")
    assert next_phase("rolling", modes=["NONE", None]) == "Every node runs 5.0.9. Then -e cassandra_upgrade_phase=sstables."
    assert next_phase("rolling", modes=["CASSANDRA_4"], version="41x") == (
        "Every node runs 5.0.9. Then -e cassandra_upgrade_phase=sstables.")
    # the inventory at NONE, a node still reading UPGRADING (apply_config not run): the step to NONE again
    assert next_phase("rolling", modes=["NONE", "NONE"], read=["UPGRADING", "NONE"]) == (
        "Every node runs 5.0.9. Now set cassandra_storage_compatibility_mode: NONE in the inventory and run "
        "apply_config (NONE is the point of no return). Then -e cassandra_upgrade_phase=sstables.")
    read = task("Summary", "Read storage_compatibility_mode in its cassandra.yaml")
    assert read["when"] == ["cassandra_upgrade_phase == 'rolling'", "cassandra_version == '50x'", "not ansible_check_mode"]


def test_check_mode_says_what_a_real_run_would_do():
    assert next_phase("canary", check=True).startswith("--check: nothing was done. A real run upgrades the canary;")
    assert next_phase("rolling", check=True, modes=["CASSANDRA_4"]).startswith(
        "--check: nothing was done. A real run upgrades the nodes listed above. Now set cassandra_storage_compatibility_mode: UPGRADING")
    assert next_phase("sstables", check=True) == (
        "--check: nothing was done. A real run rewrites the sstables of the nodes listed above. "
        "Then -e cassandra_upgrade_phase=cleanup.")
    assert next_phase("sstables") == "sstables rewritten. Last: -e cassandra_upgrade_phase=cleanup."
    for phase in ("canary", "rolling", "sstables"):
        assert "nothing was done" in next_phase(phase, check=True) and "nothing was done" not in next_phase(phase)


def test_cleanup_suggests_no_way_back():
    say = task("Cleanup", "Say what is left to do")
    variables = dict(say["vars"], cassandra_conf_dir="/etc/cassandra/conf")
    real = text(say["ansible.builtin.debug"]["msg"], ansible_check_mode=False, **variables)
    assert "roll back" not in real
    assert real == ("Upgrade over. Resume backups and repairs. The config copies (/etc/cassandra/conf.pre-upgrade-*) "
                    "are kept, for reference: with the pre-upgrade snapshots removed they are no way back to the former "
                    "version. Remove them when you no longer need them.")
    assert text(say["ansible.builtin.debug"]["msg"], ansible_check_mode=True, **variables) == (
        "--check: nothing was removed. A real run removes the pre-upgrade snapshots listed above.")


REFUSED = "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused (Connection refused)'."


def test_a_down_node_with_the_target_package_is_pointed_to_apply_config():
    # a rolling phase interrupted after the node's package upgrade: systemctl start fails on the former config
    read = task("Upgrade checks", "Read the installed Cassandra package where nodetool failed")
    assert read["check_mode"] is False and read["failed_when"] is False
    assert read["when"] == "cassandra_upgrade_running.rc | default(0) != 0"
    order = names("Upgrade checks")
    assert order.index("Read the installed Cassandra package where nodetool failed") < order.index("Every node answers nodetool")
    check = task("Upgrade checks", "Every node answers nodetool")

    def node(name, rc, installed=None):
        h = {"inventory_hostname": name, "cassandra_package_version": "5.0.9",
             "cassandra_upgrade_running": {"rc": rc, "stdout": "ReleaseVersion: 4.1.12" if rc == 0 else "",
                                           "stderr": "" if rc == 0 else REFUSED + "\n"}}
        if installed is not None:
            h["cassandra_upgrade_installed"] = {"rc": 0, "stdout": installed}
        return h

    hostvars = {"n1": node("n1", 0), "n2": node("n2", 1, "5.0.9"), "n3": node("n3", 1, "4.1.12")}
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_package_version="5.0.9")
    assert render(check["vars"]["_on_target"], **variables) == ["n2"]
    assert text(check["ansible.builtin.assert"]["fail_msg"], **variables) == (
        "nodetool version failed on n2 (%s); nodetool version failed on n3 (%s). Package cassandra 5.0.9 (the target) "
        "is installed on n2: run ansible-playbook community.cassandra.apply_config --limit n2 (it writes the "
        "config of 5.0.9 and leaves Cassandra stopped there), then start Cassandra there (systemctl start cassandra), "
        "then run this phase again. A node not running Cassandra: start it "
        "(systemctl start cassandra there, or apply_config --limit <node>, which starts a node whose unit failed), "
        "then run this phase again. A node already upgraded is skipped. Nothing was done." % (REFUSED, REFUSED))
    del hostvars["n3"]
    variables["ansible_play_hosts"] = list(hostvars)
    hostvars["n2"]["cassandra_upgrade_installed"]["stdout"] = "5.0.9-1"  # a Debian revision
    msg = text(check["ansible.builtin.assert"]["fail_msg"], **variables)
    # apply_config leaves a node stopped by the upgrade's drain stopped (its unit not failed): the start is said
    assert "apply_config --limit n2 (it writes" in msg and "A node not running Cassandra" not in msg
    assert "then start Cassandra there (systemctl start cassandra), then run this phase again" in msg


def test_apply_config_says_none_is_the_point_of_no_return():
    with open(os.path.join(TOP, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
        content = f.read()
    assert ("cassandra_apply_config_no_return: \"{{ _nodes | community.cassandra.cassandra_apply_config_no_return }}\""
            in content)
    plays = yaml.safe_load(content)
    screen = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Show the plan and confirm the run")
    warnings = screen["vars"]["cassandra_screen"]["warnings"]
    assert "cassandra_apply_config_no_return" in warnings and "the point of no return" in warnings


def test_the_nodes_outside_the_canary_are_said_so():
    # a canary run that stops: the other nodes are left to the rolling phase, not "left out by --limit"
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import cassandra_stop_block
    note = task("Pick the nodes and their order", "Note the nodes the canary phase leaves to the rolling phase")
    assert note["when"] == ["cassandra_upgrade_phase == 'canary'", "inventory_hostname != _canary"]
    result = note["ansible.builtin.set_fact"]["cassandra_op_result"]
    lines = cassandra_stop_block([{"node": "n1", "result": result}, {"node": "n2", "result": "upgrade FAILED after 5s: x"},
                                  {"node": "n3", "result": result}], "n2", "boom", operation="upgrade", resume=False)
    assert "n1, n3 not touched (left to the rolling phase)" in [" ".join(x.split()) for x in lines]
    assert not any("--limit" in x for x in lines)
