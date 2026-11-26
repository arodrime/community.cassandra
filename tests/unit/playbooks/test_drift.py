from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The drift playbook. -e drift_nodes=false: run by ansible-playbook on a neutral inventory whose nodes can't be reached
# (192.0.2.0/24, TEST-NET-1): the inventory alone renders OFF STOCK and BETWEEN NODES, secrets masked, and writes
# its report. Its node part is apply_config's compare (import_playbook, _cassandra_apply_config_compare_only): every
# stop, write and question of a real run is off there, and the compare's facts are summed up as IN SYNC (the
# expressions read from the playbooks, rendered by Ansible with neutral data).

import glob
import os
import subprocess
import warnings

import pytest
import yaml

from ansible.plugins.loader import init_plugin_loader

from ansible_collections.community.cassandra.plugins.filter.cassandra_drift import cassandra_drift_sync
from ansible_collections.community.cassandra.tests.unit.helpers import render_as_given as render, run_playbook, trusted
from ansible_collections.community.cassandra.tests.unit.task_files import find_play, find_task

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
GUARD = "not _cassandra_apply_config_compare_only | default(false) | bool"
SECRET = "zorglub-s3cr3t"


def load(name):
    with open(os.path.join(TOP, "playbooks", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def whens(task):
    when = task.get("when") or []
    return [when] if isinstance(when, str) else list(when)


# --- the run, the inventory alone ---------------------------------------------------------------------------------

HOSTS = """\
all:
  children:
    prod:
      children:
        prod_dc1:
          hosts:
            db1: {ansible_host: 192.0.2.11}
            db2: {ansible_host: 192.0.2.12}
            db3: {ansible_host: 192.0.2.13, cassandra_heap_size: 16G}
"""
CLUSTER_VARS = """\
cassandra_cluster_name: Prod
cassandra_version: 41x
cassandra_concurrent_reads: 64
cassandra_heap_size: 8G
cassandra_seeds: [192.0.2.11]
cassandra_hints_dir: "{{ cassandra_data_root | default('/var/lib/cassandra') }}/hints"
"""


def inventory(tmp_path):
    inv = tmp_path / "inventories"
    (inv / "group_vars" / "prod").mkdir(parents=True)
    (inv / "host_vars" / "db1").mkdir(parents=True)
    (inv / "prod.yml").write_text(HOSTS)
    (inv / "group_vars" / "prod" / "main.yml").write_text(CLUSTER_VARS)
    (inv / "group_vars" / "prod" / "secrets.yml").write_text("cassandra_server_keystore_password: %s\n" % SECRET)
    (inv / "group_vars" / "all.yml").write_text("cassandra_linux_swap_off: false\n")
    (inv / "host_vars" / "db1" / "main.yml").write_text(
        "cassandra_data_dir: /data/cassandra\ncassandra_config_keep_files: [logback.xml]\n")
    return inv


def run(tmp_path, *extra):
    env = dict(ANSIBLE_INVENTORY_UNPARSED_WARNING="0", ANSIBLE_TIMEOUT="3", ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               COLUMNS="160")
    unset = ("ANSIBLE_VAULT_PASSWORD_FILE", "ANSIBLE_BECOME", "ANSIBLE_CONFIG", "ANSIBLE_INVENTORY", "ANSIBLE_FORCE_COLOR",
             "CASSANDRA_CLUSTER")
    return run_playbook(["-i", "inventories/prod.yml", "community.cassandra.drift"] + list(extra), env, unset,
                        cwd=tmp_path, stdin=subprocess.DEVNULL)


@pytest.mark.playbook
def test_inventory_alone_renders_and_writes_its_report(tmp_path):
    inventory(tmp_path)
    rc, output = run(tmp_path, "-e", "drift_nodes=false")
    assert rc == 0, output
    assert "UNREACHABLE" not in output and "Preflight" not in output  # no node contacted
    # the screen: the verdict first, each section summed up (every setting in the report)
    shown = output.splitlines()
    for line in ["DRIFT  prod  3 nodes  not checked",
                 "NOT CHECKED  the nodes", "  -e drift_nodes=false: the inventory alone, no node contacted",
                 "BETWEEN NODES  2 settings differ",
                 "  cassandra_heap_size", "    8G  on db1, db2 (2 nodes)", "    16G  on db3 (1 node)",
                 "OFF STOCK  7 settings not at the collection's default",
                 "    move data by hand          1   data_dir",
                 "  kept by hand (cassandra_config_keep_files): logback.xml on db1 (1 node)"]:
        assert line in shown, (line, output)
    assert max(len(line) for line in shown[shown.index("DRIFT  prod  3 nodes  not checked"):]
               if not line.startswith("Report: ") and "ansible-playbook " not in line and "grep -Rnw" not in line) <= 79, \
        output  # (a command whole)
    # each setting's lines: in the report, and on the screen with -e drift_details=true
    details = ["  cassandra_concurrent_reads   64 on all (3 nodes)", "      default: 32",
               "      where: grep -Rnw cassandra_concurrent_reads %s %s %s" % tuple(
                   tmp_path / "inventories" / p for p in ("prod.yml", "group_vars", "host_vars")),
               "  cassandra_heap_size   8G on db1, db2 (2 nodes); 16G on db3 (1 node)", "      default: \"\"",
               "  logback.xml   kept on db1 (1 node)"]
    assert not set(details) & set(shown), output
    # cassandra_hints_dir resolves to its default: not off stock
    assert "cassandra_hints_dir" not in output
    assert SECRET not in output
    reports = glob.glob(str(tmp_path / "reports" / "prod" / "drift-*.txt"))
    assert len(reports) == 1, output
    with open(reports[0], encoding="utf-8") as f:
        report = f.read()
    assert report.startswith("drift  prod  7 settings off stock, 2 differ between nodes, 1 file kept by hand, nodes not"
                             " checked\n"), report
    assert "  run:      read only, nothing was changed (-e drift_nodes=false: no node contacted)\n" in report
    assert "\nOFF STOCK" in report and "\nBETWEEN NODES" in report and "\nNOT CHECKED" in report
    for line in details + [u"NOT CHECKED — the nodes (-e drift_nodes=false: the inventory alone, no node contacted)"]:
        assert line in report.splitlines(), (line, report)
    assert SECRET not in report
    rc, output = run(tmp_path, "-e", "drift_nodes=false", "-e", "drift_details=true")
    assert rc == 0, output
    for line in details + ["OFF STOCK %s the settings not at the collection's default" % u"\u2014"]:
        assert line in output.splitlines(), (line, output)
    assert not any(line.startswith("OFF STOCK  ") for line in output.splitlines())  # (the summary)
    # an unknown drift_ option is refused, as the collection's others
    rc, output = run(tmp_path, "-e", "drift_node=false")
    assert rc != 0 and "-e drift_node: not a variable of this collection" in output, output


# --- the node part: apply_config's compare, read only -------------------------------------------------------------

def test_the_nodes_are_compared_by_apply_config_read_only():
    plays = load("drift.yml")
    names = [p.get("name") for p in plays]
    imported = plays[names.index("Compare the nodes with the inventory")]
    assert imported["ansible.builtin.import_playbook"] == "community.cassandra.apply_config"
    assert imported["vars"]["_cassandra_apply_config_compare_only"] is True
    assert imported["when"] == "drift_nodes | default(true) | bool"
    # the inventory read before the compare sets facts of its own (some named as variables)
    assert names.index("Read the inventory") < names.index("Compare the nodes with the inventory") < names.index("Drift")
    # -e drift_nodes=false: the preflight's plays on no host either
    skip = imported["vars"]["_cassandra_preflight_skip"]
    assert render(skip, {"drift_nodes": False}) is True and render(skip, {}) is False


def test_apply_config_compare_only_stops_writes_and_asks_nothing():
    plays = load("apply_config.yml")
    find = find_play(plays, "Find what each node needs")
    guarded = []
    for task in find["tasks"]:
        module = [k for k in task if k.startswith("ansible.builtin.")]
        real_run_only = "not ansible_check_mode" in whens(task)
        # a stop, a write, or what a real run alone does: off in drift's compare (or for the nodes to do: none there)
        if real_run_only or module and module[0] in ("ansible.builtin.fail", "ansible.builtin.file",
                                                     "ansible.builtin.group_by", "ansible.builtin.copy"):
            assert GUARD in whens(task) or "_todo | length > 0" in whens(task), task["name"]
            guarded.append(task["name"])
    for name in ("Refuse a start into another storage_compatibility_mode than one step from the one it last ran",
                 "Check the Prometheus exporter's port is free", "Keep the plan for the report of the run"):
        assert GUARD in whens(find_task(find, name)), name
    assert len(guarded) >= 9, guarded
    # the question: only for nodes to do, none in drift's compare (no group of nodes to do)
    screen = find_task(find, "Show the plan and confirm the run")
    assert "_todo | length > 0" in whens(screen)
    # the plays that apply: on the groups of nodes to do (none), the summary on no host
    for play in plays[plays.index(find) + 1:]:
        hosts = play["hosts"]
        if play["name"] == "Summary":
            assert hosts.startswith("{{ [] if _cassandra_apply_config_compare_only | default(false) | bool else")
        else:
            assert "groups['cassandra_apply_config_" in hosts, play["name"]
    # preflight's one write (a stale bootstrap mark): off too
    preflight = load("preflight.yml")
    mark = [t for p in preflight for t in p.get("tasks") or [] if t.get("name") == "Remove the mark of a bootstrap that went through"]
    assert mark and GUARD in whens(mark[0])


HOSTVARS = {
    "db1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
            "_cassandra_config_items": [{"item": "/etc/cassandra/cassandra.yaml", "diff": "x"},
                                        {"item": "/etc/cassandra/cassandra.yaml", "diff": "y"}],
            "cassandra_apply_config_os": [], "_cassandra_apply_config_stops": []},
    "db2": {"cassandra_apply_config_todo": False, "cassandra_apply_config_then": "none",
            "cassandra_apply_config_os": [{"item": "vm.swappiness"}], "_cassandra_apply_config_stops": []},
    "db3": {"cassandra_apply_config_todo": False, "cassandra_apply_config_then": "none",
            "_cassandra_apply_config_stops": []},
}


def test_the_compare_summed_up_in_sync():
    play = find_play(load("drift.yml"), "Drift")
    variables = dict(trusted(play["vars"]), hostvars=HOSTVARS, _nodes=["db1", "db2", "db3"])
    sync = render(play["vars"]["_sync"], variables)
    assert [(n["name"], n["todo"], n["files"], n["os"]) for n in sync] == [
        ("db1", True, ["cassandra.yaml"], []), ("db2", False, [], ["vm.swappiness"]), ("db3", False, [], [])]
    assert cassandra_drift_sync(sync, render(play["vars"]["_stops"], variables), "CMD")[1:] == [
        "  db1  cassandra.yaml; then restart", "  db2  OS settings: vm.swappiness; no restart", "  db3  in sync",
        "  the settings themselves, before and after (changing nothing):", "    CMD"]
    # the head: how many nodes are not in sync (the OS-only one too)
    verdict = render(play["vars"]["_verdict"], dict(variables, _compared=True,
                                                    _cassandra_drift_report={"counts": {"stock": 2, "between": 0, "kept": 1}}))
    assert verdict == "2 settings off stock, nodes alike, 1 file kept by hand, 2 nodes differ from the inventory"
    alike = dict((n, {"cassandra_apply_config_todo": False, "_cassandra_apply_config_stops": []}) for n in HOSTVARS)
    verdict = render(play["vars"]["_verdict"], dict(variables, hostvars=alike, _compared=True,
                                                    _cassandra_drift_report={"counts": {"stock": 0, "between": 1, "kept": 0}}))
    assert verdict == "at stock, 1 differs between nodes, in sync"
    verdict = render(play["vars"]["_verdict"], dict(variables, _compared=False,
                                                    _cassandra_drift_report={"counts": {"stock": 1, "between": 0, "kept": 0}}))
    assert verdict == "1 setting off stock, nodes alike, nodes not checked"


def test_a_node_the_compare_did_not_reach_is_not_in_sync():
    # (unreachable with cassandra_allow_down_nodes, or its compare failed: no facts of the compare)
    play = find_play(load("drift.yml"), "Drift")
    hostvars = dict(HOSTVARS, db1={}, db2={"cassandra_apply_config_todo": False, "_cassandra_apply_config_stops": []})
    variables = dict(trusted(play["vars"]), hostvars=hostvars, _nodes=["db1", "db2", "db3"])
    sync = render(play["vars"]["_sync"], variables)
    assert [(n["name"], n["compared"]) for n in sync] == [("db1", False), ("db2", True), ("db3", True)]
    # (audit A6: what was observed only, and the way out; every node compared in sync: "not all compared")
    assert cassandra_drift_sync(sync, [], "CMD", screen=True) == [
        "NOT ALL COMPARED  1 node not compared", "  db1  not compared: the compare gave no result for it",
        "  db2, db3  in sync", "  WAY OUT  run drift again, then look at the run's output above for it"]
    verdict = render(play["vars"]["_verdict"], dict(variables, _compared=True,
                                                    _cassandra_drift_report={"counts": {"stock": 0, "between": 0, "kept": 0}}))
    assert verdict == "at stock, nodes alike, 1 node not compared"


def test_a_stop_ends_the_compare_and_is_said_not_in_sync():
    # e2e: an inventory asking another series (the upgrade planned) failed drift at cassandra_config's series check;
    # the compare now ends where a real run stops, its nodes said not compared, the stop with its command
    find = find_play(load("apply_config.yml"), "Find what each node needs")
    names = [t.get("name") for t in find["tasks"]]
    end = find["tasks"][names.index("End the compare where a real run stops")]
    assert end["ansible.builtin.meta"] == "end_play"
    assert whens(end) == ["_cassandra_apply_config_stops | length > 0",
                          "_cassandra_apply_config_compare_only | default(false) | bool"]
    assert names.index("Stop before any change") < names.index("End the compare where a real run stops") \
        < names.index("Compare the config")
    play = find_play(load("drift.yml"), "Drift")
    stops = ["db1, db2: runs Cassandra 4.1.12, the inventory asks for 5.0.9: another series is an upgrade."]
    stopped = dict((n, {"_cassandra_apply_config_stops": stops}) for n in ("db1", "db2"))
    variables = dict(trusted(play["vars"]), hostvars=stopped, _nodes=["db1", "db2"])
    sync = render(play["vars"]["_sync"], variables)
    assert cassandra_drift_sync(sync, render(play["vars"]["_stops"], variables), "CMD")[1:] == [
        "  db1, db2  not compared (apply_config stops first)",
        "  apply_config would stop before any change (a procedure of its own first):", "    " + stops[0]]
    verdict = render(play["vars"]["_verdict"], dict(variables, _compared=True,
                                                    _cassandra_drift_report={"counts": {"stock": 0, "between": 0, "kept": 0}}))
    assert verdict == "at stock, nodes alike, apply_config would stop before any change, 2 nodes not compared"


def test_in_sync_names_a_unit_that_differs_and_its_command_reads_as_the_others():
    play = find_play(load("drift.yml"), "Drift")
    # e2e: "ansible-playbook --check --diff community.cassandra.apply_config", the others' options after the playbook
    assert play["vars"]["_command"] == \
        "{{ 'apply_config' | community.cassandra.cassandra_command(extra=['--check', '--diff'], **_args) }}"
    hostvars = {"db1": {"cassandra_apply_config_todo": True, "cassandra_service_unit_compare": {"changed": True},
                        "cassandra_service_dropin_compare": {"skipped": True, "changed": False}},
                "db2": {"cassandra_apply_config_todo": False, "cassandra_service_unit_compare": {"skipped": True},
                        "cassandra_service_dropin_compare": {"changed": True}}}
    sync = render(play["vars"]["_sync"], dict(trusted(play["vars"]), hostvars=hostvars, _nodes=["db1", "db2"]))
    assert [n["unit"] for n in sync] == [["cassandra.service"], ["zz-ansible-environment.conf"]]


# --- FIXES: the hand edits, nothing written ----------------------------------------------------------------------

FIX_VARS = {"host_vars/db1/main.yml": "# db1, by hand\ncassandra_concurrent_compactors: 4\n",
            "host_vars/db2/main.yml": "cassandra_concurrent_compactors: 4\n"}


@pytest.mark.playbook
def test_fixes_names_the_files_to_edit_and_writes_none(tmp_path):
    inv = inventory(tmp_path)
    (inv / "host_vars" / "db1" / "main.yml").unlink()
    for path, text in FIX_VARS.items():
        (inv / path).parent.mkdir(parents=True, exist_ok=True)
        (inv / path).write_text(text)
    before = dict((p, (inv / p).read_text()) for p in FIX_VARS)
    rc, output = run(tmp_path, "-e", "drift_nodes=false")
    assert rc == 0, output
    shown = output.splitlines()
    at = shown.index("FIXES  by hand: to give every node one value, edit where the inventory sets it")
    assert shown[at + 1:at + 4] == ["  cassandra_concurrent_compactors", "    4 on db1, db2",
                                    "    '' (default) on db3"], output
    grep = next(line for line in shown[at:] if "grep -RnwE" in line)
    assert grep.endswith(" %s %s %s" % tuple(tmp_path / "inventories" / p for p in ("prod.yml", "group_vars", "host_vars"))) \
        and "cassandra_concurrent_compactors" in grep, output
    assert "  then: review the edits (git diff in a git repository), then" in shown[at:], output
    assert all(len(line) <= 79 for line in shown[at:at + 7] if "grep" not in line), output
    assert dict((p, (inv / p).read_text()) for p in FIX_VARS) == before
    with open(os.path.join(TOP, "playbooks", "drift.yml"), encoding="utf-8") as f:
        assert "ansible.builtin.copy" not in f.read()  # (no file of the inventory written: the edits are yours)
