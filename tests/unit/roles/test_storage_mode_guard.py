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
    assert check["vars"]["cassandra_output"] is True and check["when"] == tasks[0]["when"]

    def run(host, **extra):
        variables = dict(check["vars"], inventory_hostname="n1", cassandra_version="50x",
                         cassandra_storage_mode_read=host["cassandra_storage_mode_read"],
                         cassandra_storage_mode_cql=host.get("cassandra_storage_mode_cql", {}), **extra)
        return (_render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
                _render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n"))
    # a passing node renders its message too (ansible-core 2.19+ templates it anyway)
    ok, msg = run(_host("NONE", logged="NONE"), _cassandra_storage_mode_target="NONE")
    assert ok is True and len(msg) == 2
    ok, msg = run(_host("NONE", logged="NONE"), _cassandra_storage_mode_target="UPGRADING",
                  _cassandra_storage_mode_way_out="run this playbook again (it writes that mode)")
    assert ok is False and msg == [
        "n1 runs storage_compatibility_mode NONE; UPGRADING would be in its cassandra.yaml and its restart would take"
        " it back to that earlier mode (5.0 moves CASSANDRA_4, UPGRADING, NONE, never back). Nothing was changed.",
        "WAY OUT  set cassandra_storage_compatibility_mode: NONE for n1 in the inventory, then run this playbook again"
        " (it writes that mode)."]
    ok, msg = run(_host("UPGRADING", logged="NONE"))
    assert ok is False and msg == [
        "n1 runs storage_compatibility_mode NONE; the UPGRADING it has would be in its cassandra.yaml and its restart"
        " would take it back to that earlier mode (5.0 moves CASSANDRA_4, UPGRADING, NONE, never back). Nothing was"
        " changed.",
        "WAY OUT  set cassandra_storage_compatibility_mode: NONE for n1 in the inventory, then run apply_config (it"
        " writes it back), then this run again."]


def test_where_it_runs():
    # cassandra_config: after the compare (its identity check first), before the question and any write
    block = next(t for t in _load("roles/cassandra_config/tasks/main.yml") if t.get("name") == "Configure")["block"]
    names = [t["name"] for t in block]
    assert names.index("Compare the new files with the live ones") < names.index(
        "Refuse a storage_compatibility_mode before the one Cassandra runs") < names.index(
        "Show the changes and confirm them") < names.index("Write the files and directories")
    guard = block[names.index("Refuse a storage_compatibility_mode before the one Cassandra runs")]
    assert guard["ansible.builtin.include_role"]["tasks_from"] == "storage_mode_guard.yml"
    assert guard["vars"]["_cassandra_storage_mode_target"] == "{{ cassandra_storage_compatibility_mode }}"
    # rolling_restart, rolling_reboot: before the question
    for playbook in ("playbooks/rolling_restart.yml", "playbooks/rolling_reboot.yml"):
        play = next(p for p in _load(playbook) if p.get("name") == "Set up the progress file")
        names = [t["name"] for t in play["tasks"]]
        assert names.index("Refuse a restart into an earlier storage_compatibility_mode") < names.index(
            "Show the plan and confirm the run"), playbook
