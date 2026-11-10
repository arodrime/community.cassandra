from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# 5.0: no restart into an earlier storage_compatibility_mode than the one Cassandra runs (a cassandra.yaml edited
# back by hand, an inventory behind the node): cassandra_config (apply_config, site.yml, the upgrade's writes),
# rolling_restart and rolling_reboot refuse it, on evidence only (the mode read over CQL or logged at its start).

import os

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import cassandra_storage_mode_back
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _load(path):
    with open(os.path.join(TOP, path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _host(yaml_mode, logged=None, cql=None):
    lines = ["file=/etc/cassandra/conf/cassandra.yaml", "yaml_read=yes", "yaml=" + yaml_mode, "running=yes"]
    if logged:
        lines.append("logged=" + logged)
    host = {"inventory_hostname": "n1", "cassandra_storage_mode_read": {"stdout_lines": lines}}
    if cql:
        host["cassandra_storage_mode_cql"] = {"changed": True, "transformed_output": "value\n----\n %s\n\n(1 rows)" % cql}
    return host


def test_back_on_evidence_only():
    # it runs NONE (logged, or read over CQL), UPGRADING to be written: back
    assert cassandra_storage_mode_back(_host("NONE", logged="NONE"), "UPGRADING") == "NONE"
    assert cassandra_storage_mode_back(_host("NONE", cql="NONE"), "UPGRADING") == "NONE"
    # a restart alone (no target): its file edited back to UPGRADING, it runs NONE
    assert cassandra_storage_mode_back(_host("UPGRADING", logged="NONE")) == "NONE"
    assert cassandra_storage_mode_back(_host("", cql="UPGRADING")) == "UPGRADING"  # no such line: CASSANDRA_4
    # forward or the same: nothing
    assert cassandra_storage_mode_back(_host("CASSANDRA_4", logged="CASSANDRA_4"), "UPGRADING") == ""
    assert cassandra_storage_mode_back(_host("NONE", logged="NONE"), "NONE") == ""
    assert cassandra_storage_mode_back(_host("UPGRADING", logged="CASSANDRA_4")) == ""
    # the mode it runs not known (nothing logged, CQL not read): never refused
    assert cassandra_storage_mode_back(_host("UPGRADING"), "CASSANDRA_4") == ""
    failed = dict(_host("UPGRADING"), cassandra_storage_mode_cql={"failed": True, "msg": "module execution failed"})
    assert cassandra_storage_mode_back(failed, "CASSANDRA_4") == ""


def _render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_the_guard_says_why_and_the_way_out():
    tasks = _load("roles/cassandra_operation/tasks/storage_mode_guard.yml")
    assert tasks[0]["ansible.builtin.include_tasks"] == "storage_mode_read.yml"
    check = tasks[1]
    # once for the nodes of the play: one refused stops the run before any node is restarted
    assert check["run_once"] is True and check["vars"]["cassandra_output"] is True

    def run(hosts, **extra):
        hostvars = {}
        for name, (host, inventory) in hosts.items():
            hostvars[name] = dict(host, cassandra_version="50x", cassandra_storage_compatibility_mode=inventory)
        variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_version="50x",
                         **extra)
        return (_render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
                _render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n"))
    # every node fine (a passing run renders its message too, as ansible-core 2.19+ does)
    ok, msg = run({"n1": (_host("NONE", logged="NONE"), "NONE")}, _cassandra_storage_mode_written=True)
    assert ok is True
    # cassandra_config: the inventory behind the mode it runs
    ok, msg = run({"n1": (_host("NONE", logged="NONE"), "NONE"), "n2": (_host("NONE", logged="NONE"), "UPGRADING")},
                  _cassandra_storage_mode_written=True)
    assert ok is False and msg == [
        "A restart would take n2 back to an earlier storage_compatibility_mode than the one Cassandra runs (5.0 moves"
        " CASSANDRA_4, UPGRADING, NONE, never back). Nothing was changed.",
        "  n2  runs NONE, its cassandra.yaml would have UPGRADING",
        "WAY OUT  On n2, set cassandra_storage_compatibility_mode: NONE in the inventory (apply_config then writes it)."
        " Then run this again."]
    # a restart alone: its file edited back by hand, the inventory right: apply_config writes it back
    ok, msg = run({"n1": (_host("NONE", logged="NONE"), "NONE"), "n2": (_host("UPGRADING", logged="NONE"), "NONE")})
    assert ok is False and msg[1:] == [
        "  n2  runs NONE, its cassandra.yaml has UPGRADING",
        "WAY OUT  On n2, run apply_config --limit n2 (it writes NONE back). Then run this again."]
    # the inventory behind too: set it first
    ok, msg = run({"n2": (_host("UPGRADING", logged="NONE"), "UPGRADING")})
    assert msg[-1] == ("WAY OUT  On n2, set cassandra_storage_compatibility_mode: NONE in the inventory, then run"
                       " apply_config --limit n2 (it writes it back). Then run this again.")


def test_where_it_runs():
    # cassandra_config: after the compare (its identity check first), before the question and any write
    block = next(t for t in _load("roles/cassandra_config/tasks/main.yml") if t.get("name") == "Configure")["block"]
    names = [t["name"] for t in block]
    assert names.index("Compare the new files with the live ones") < names.index(
        "Refuse a storage_compatibility_mode before the one Cassandra runs") < names.index(
        "Show the changes and confirm them") < names.index("Write the files and directories")
    guard = block[names.index("Refuse a storage_compatibility_mode before the one Cassandra runs")]
    assert guard["ansible.builtin.include_role"]["tasks_from"] == "storage_mode_guard.yml"
    assert guard["vars"]["_cassandra_storage_mode_written"] is True
    # rolling_restart, rolling_reboot: before the question
    for playbook in ("playbooks/rolling_restart.yml", "playbooks/rolling_reboot.yml"):
        play = next(p for p in _load(playbook) if p.get("name") == "Set up the progress file")
        names = [t["name"] for t in play["tasks"]]
        assert names.index("Refuse a restart into an earlier storage_compatibility_mode") < names.index(
            "Show the plan and confirm the run"), playbook
