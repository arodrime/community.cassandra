from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config's compare of cassandra_config: the role's plan (tasks/plan.yml) on every node, the rest of the role
# (tasks/apply_plan.yml) only where the plan lists something it would write (_cassandra_config_plan_writes). The
# guards of that skip: every task of the rest that writes is covered by an item of the plan's list (or never writes
# in the compare); every message and stop of the rest is said only when the plan lists something (or never in the
# compare); every fact of the rest the run reads is set by the gate as the rest leaves it, or checked by
# apply_verify. A task added to the rest fails here until it is classified.

import glob
import sys
import grp
import os
import pwd
import re

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_gate import (
    PARTS, cassandra_apply_gate_lines, cassandra_apply_only, cassandra_apply_only_stops, cassandra_check_groups,
    cassandra_apply_resume_refusal, cassandra_config_user_files, cassandra_cqlsh_credential_refusals, cassandra_config_user_request)
from ansible_collections.community.cassandra.tests.unit.helpers import TOP, render
from ansible_collections.community.cassandra.tests.unit.task_files import find_play, find_task, load_tasks

TASKS = os.path.join(TOP, "roles", "cassandra_config", "tasks")
PLAN = load_tasks(os.path.join(TASKS, "plan.yml"))
REST = load_tasks(os.path.join(TASKS, "apply_plan.yml"))
with open(os.path.join(TOP, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
    PLAYBOOK = yaml.safe_load(f)
COMPARE = find_task(find_play(PLAYBOOK, "Find what each node needs"), "Compare the config")
VERDICT = find_task(PLAN, "Note what the rest of the role would write")
ITEMS = list(VERDICT["vars"]["_found"])

WRITERS = ("ansible.builtin.template", "ansible.builtin.copy", "ansible.builtin.file", "ansible.builtin.lineinfile",
           "ansible.builtin.get_url", "ansible.builtin.command", "ansible.builtin.shell", "ansible.builtin.include_role",
           "community.general.alternatives", "community.cassandra.cassandra_reload")
# what the compare does not run, or what changes nothing in it
NEVER = {
    "check mode": "skipped in check mode (a command, a write the module does not do), or when not ansible_check_mode",
    "read": "a read: changed_when false",
}
# every task of the rest that may write: the item of the plan's list that covers it, or why it never writes in
# the compare
WRITES = {
    "Look for the AxonOps Java agent's options file": "read",
    "Record the ones not written since it started": "check mode",
    "Seed the alternative conf dir from the one in use": "conf directory",
    "Select the alternative conf dir": "conf directory",
    "Template Cassandra config files": "files",
    "Set the owner and mode of the files left as they are": "owner, group, mode",
    "Add the role's header line to the files left as they are": "header",
    "Record them with their header as the files the running Cassandra runs with": "header",
    "Find the running Cassandra JVM": "read",
    "Create the parents of the missing ones": "directories",
    "Create the missing ones": "directories",
    "Set the owner and mode of the existing ones": "directories",
    "Reload the seed list on the running node": "files",
    "Find what the exporter's agent no longer needs": "read",
    "Remove them": "exporter",
    "Forget the jars removed": "exporter",
    "Remove the exporter's directory once empty": "exporter",
    "Create the directories of the JMX files": "JMX files",
    "Create the directory of the JMX password file": "JMX files",
    "Write jmxremote.password": "JMX files",
    "Write jmxremote.access": "JMX files",
    "Set the owner and mode of the JMX files left as they are": "JMX files",
    "Create ~/.cassandra": "users' files",
    "Write ~/.cassandra/cqlshrc": "users' files",
    "Write ~/.cassandra/credentials (4.1+)": "users' files",
    "Create their ~/.cassandra": "users' files",
    "Write their ~/.cassandra/nodetool-ssl.properties": "users' files",
    "Report": "check mode",  # (cassandra_change_report: its file only with cassandra_change_report_dir, "" here)
}
# every message, question and stop of the rest: the item it needs, or why the compare never says it
SAID = {
    "Warn that the audit events would move to audit.log": "files",  # (logback.xml among the changes)
    "Warn that a file kept by hand is missing": "quiet",
    "Show the config changes": "quiet",
    "Show the config changes to confirm": "check mode",
    "Say which files are left as they are": "no operator message",
    "Say which files are kept as they are (cassandra_config_keep_files)": "no operator message",
    "Show the owner, group and mode changes": "quiet",
    "Confirm config changes": "check mode",
    "Stop unless the changes were confirmed": "check mode",
    "Refuse to load an AxonOps Java agent not installed": "check mode",  # (its look is a command: skipped)
    "Say the change cannot be told pending": "check mode",
    "Say the files the header was not added to": "header",
    "Say the seed reload failed": "files",
    "Warn that a restart is needed": "quiet",
    "Say what the exporter's agent in a file still needs is left in place": "exporter",
    "Say which JMX files are kept as they are (cassandra_config_keep_files)": "no operator message",
    "Report": "quiet",  # (cassandra_change_report: its "Show changes")
    "Record them with their header as the files the running Cassandra runs with": "header",
}
SILENT = {"quiet": "said by apply_config itself (_cassandra_config_quiet, _cassandra_change_report_quiet, no restart warning)",
          "no operator message": "no cassandra_output: the ops callback does not print it",
          "check mode": "not under check mode"}


def walk(tasks, exporter=False):
    """(task, under the exporter's own tasks) of a task list, blocks and static includes opened."""
    for task in tasks:
        if not isinstance(task, dict):
            continue
        inner = [t for key in ("block", "rescue", "always") for t in task.get(key) or []]
        under = exporter or task.get("name") == "The Prometheus exporter's jar and config"
        if inner:
            yield from walk(inner, under)
        else:
            yield task, under


def module(task):
    return next((k for k in task if k.startswith(("ansible.builtin.", "community."))), "")


def test_every_task_of_the_rest_that_writes_is_covered_by_the_plan():
    found = {}
    for task, exporter in walk(REST):
        if module(task) not in WRITERS or exporter:  # (the exporter on: the rest runs, "exporter")
            continue
        if module(task) in ("ansible.builtin.command", "ansible.builtin.shell") and task.get("changed_when") is False:
            found[task["name"]] = "read"
            continue
        found[task["name"]] = WRITES.get(task["name"], "NOT CLASSIFIED")
    assert sorted(n for n, item in found.items() if item == "NOT CLASSIFIED") == []
    assert set(found.values()) <= set(ITEMS) | set(NEVER)
    assert sorted(set(WRITES) - set(found)) == []  # (a task gone: its line too)


def test_every_message_and_stop_of_the_rest_needs_an_item_of_the_plan():
    found = {}
    for task, exporter in walk(REST):
        if exporter or module(task) not in ("ansible.builtin.debug", "ansible.builtin.assert", "ansible.builtin.fail",
                                            "ansible.builtin.pause", "ansible.builtin.include_role"):
            continue
        found[task["name"]] = SAID.get(task["name"], "NOT CLASSIFIED")
    assert sorted(n for n, item in found.items() if item == "NOT CLASSIFIED") == []
    assert set(found.values()) <= set(ITEMS) | set(SILENT)
    assert sorted(set(SAID) - set(found)) == []
    # (the ones said by apply_config itself are so because of its vars)
    vars_ = COMPARE["vars"]
    assert vars_["_cassandra_config_quiet"] is True and vars_["_cassandra_change_report_quiet"] is True
    assert vars_["_cassandra_config_no_restart_warning"] is True


def sets(tasks):
    out = set()
    for task, dummy in walk(tasks):
        out |= set(k for k in task.get("ansible.builtin.set_fact") or {} if k != "cacheable")
        out |= {task["register"]} if "register" in task else set()
        role = task.get("ansible.builtin.include_role")
        if role and not role["name"].endswith(".cassandra_config"):
            path = os.path.join(TOP, "roles", role["name"].split(".")[-1], "tasks", role.get("tasks_from", "main.yml"))
            out |= sets(load_tasks(path))
    return out


def read_by_the_run():
    """The names the playbook, the operation's tasks and the filters may read (a superset)."""
    words = set()
    for path in (glob.glob(os.path.join(TOP, "playbooks", "apply_config.yml"))
                 + glob.glob(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "*.yml"))
                 + glob.glob(os.path.join(TOP, "plugins", "filter", "*.py"))
                 + glob.glob(os.path.join(TOP, "plugins", "plugin_utils", "*.py"))):
        with open(path, encoding="utf-8") as f:
            words |= set(re.findall(r"\b_?cassandra_[a-z0-9_]+\b", f.read()))
    return words


# facts of the rest only the exporter's own tasks set: the rest runs whenever the exporter is on
EXPORTER_FACTS = {"_cassandra_config_exporter_jar_state", "cassandra_config_exporter_download"}


def test_every_fact_of_the_rest_the_run_reads_is_set_by_the_gate_or_checked():
    rest, plan, read = sets(REST), sets(PLAN), read_by_the_run()
    gate = find_task(COMPARE, "Note what the rest of the config role leaves on a node it would not change")
    gate_sets = set(gate["ansible.builtin.set_fact"])
    checked = set(COMPARE["vars"]["_gate_facts"])
    # set by the rest only: the gate sets them (as the rest leaves them: _sums' own values)
    assert ((rest - plan) & read) - EXPORTER_FACTS <= gate_sets <= rest
    # set by both, or by the gate: apply_verify checks them
    assert ((rest & plan) & read) | gate_sets == checked
    assert set(COMPARE["vars"]["_gate_values"]) == gate_sets


def test_the_gate_sets_what_the_rest_leaves():
    gate = find_task(COMPARE, "Note what the rest of the config role leaves on a node it would not change")
    facts = gate["ansible.builtin.set_fact"]
    assert facts["_cassandra_config_items"] == [] and facts["_cassandra_config_exporter_changes"] == []
    seen = render(COMPARE["vars"]["_seen_config"], _cassandra_change_report_seen={"cassandra_linux": {"items": []}})
    assert seen == {"cassandra_linux": {"items": []}, "cassandra_config": {"items": [], "state": {}}}
    # (the change report's own fact, rendered with no item: the same)
    report = find_task(load_tasks(os.path.join(TOP, "roles", "cassandra_change_report", "tasks", "main.yml")),
                       "Keep what changed ({{ cassandra_change_report_role }})")
    said = render(report["ansible.builtin.set_fact"]["_cassandra_change_report_seen"], cassandra_change_report_role="cassandra_config",
                  cassandra_change_report_items=[], _cassandra_change_report_seen={"cassandra_linux": {"items": []}})
    assert said == seen
    # the role's own items when its plan lists nothing: none
    record = find_task(REST, "Record the changes for the report")
    items = render(record["ansible.builtin.set_fact"]["_cassandra_config_items"], **dict(
        record["vars"], cassandra_rpm_conf_alternative="", cassandra_config_conf_target={"skipped": True},
        cassandra_config_preview={"results": [{"cassandra_config_file": "cassandra.yaml", "stdout": "", "rc": 0}]},
        _cassandra_config_kept=[], _cassandra_config_perm_changes=[], _cassandra_config_dir_changes=[],
        _cassandra_config_header=[], _cassandra_config_header_items=[], cassandra_conf_dir="/etc/cassandra"))
    assert items == []


UNCHANGED = {
    "cassandra_config_pkg_conf": {"stat": {"exists": True}}, "_cassandra_config_changes": [], "_cassandra_config_write": [],
    "_cassandra_config_kept": [], "_cassandra_config_perm_changes": [], "_cassandra_config_dir_changes": [],
    "cassandra_config_dirs": {"results": [{"item": "/var/lib/cassandra", "stat": {"exists": True}}]},
    "_cassandra_config_header": [], "cassandra_jmx_users": [], "cassandra_prometheus_exporter_enabled": False,
    "cassandra_config_exporter_unused": {"stdout_lines": []},
}


def verdict(**changed):
    variables = dict(UNCHANGED, **changed)
    found = dict((k, render(v, **variables)) for k, v in VERDICT["vars"]["_found"].items())
    return render(VERDICT["ansible.builtin.set_fact"]["_cassandra_config_plan_writes"], _found=found)


@pytest.mark.parametrize("changed, said", [
    ({}, []),
    ({"cassandra_config_pkg_conf": {"stat": {"exists": False}}}, ["not installed"]),
    ({"_cassandra_config_changes": ["cassandra.yaml"]}, ["files"]),
    ({"_cassandra_config_write": ["cassandra.yaml"]}, ["files"]),  # (a link, a conf dir that moves, a diff that failed)
    ({"_cassandra_config_write": ["cassandra.yaml"], "_cassandra_config_kept": ["cassandra.yaml"]}, []),
    ({"_cassandra_config_perm_changes": [{"item": "x"}]}, ["owner, group, mode"]),
    ({"_cassandra_config_dir_changes": [{"item": "/d"}]}, ["directories"]),
    ({"cassandra_config_dirs": {"results": [{"item": "/d", "stat": {"exists": False}}]}}, ["directories"]),
    ({"_cassandra_config_header": [{"file": "cassandra.yaml"}]}, ["header"]),
    ({"cassandra_jmx_users": [{"name": "ops"}]}, ["JMX files"]),  # (not compared: a node blank)
    ({"cassandra_jmx_users": [{"name": "ops"}], "cassandra_config_jmx_same": {"stdout": '["password", "access"]'},
      "_cassandra_config_jmx_changes": []}, []),
    ({"cassandra_jmx_users": [{"name": "ops"}], "cassandra_config_jmx_same": {"stdout": '["password"]'},
      "_cassandra_config_jmx_changes": [{"file": "jmxremote.access"}]}, ["JMX files"]),
    ({"cassandra_jmx_users": [{"name": "ops"}], "cassandra_config_jmx_same": {"stdout": '["password", "access"]'},
      "_cassandra_config_jmx_changes": [], "cassandra_config_jmx_header": {"stdout": '[{"file": "jmxremote.access"}]'}},
     ["JMX files"]),
    ({"cassandra_prometheus_exporter_enabled": True}, ["exporter"]),
    # audit B3: the conf dir switched, every file kept (its item said by confirm.yml)
    ({"cassandra_config_conf_target": {"stat": {"lnk_source": "/etc/cassandra/default.conf"}},
      "cassandra_rpm_conf_alternative": "/etc/cassandra/ansible.conf"}, ["conf directory"]),
    ({"cassandra_config_conf_target": {"stat": {"lnk_source": "/etc/cassandra/ansible.conf"}},
      "cassandra_rpm_conf_alternative": "/etc/cassandra/ansible.conf"}, []),
    ({"cassandra_config_conf_target": {"skipped": True}, "cassandra_rpm_conf_alternative": ""}, []),
    ({"cassandra_config_exporter_unused": {"skipped": True}}, ["exporter"]),  # (a run that writes: read later)
    ({"cassandra_config_exporter_unused": {"stdout_lines": ["kept|cassandra-env.sh|conf"]}}, ["exporter"]),
])
def test_the_plan_lists_what_the_rest_would_write(changed, said):
    assert verdict(**changed) == said


def test_the_plan_reads_the_exporter_only_in_a_run_that_looks():
    tell = find_task(PLAN, "Tell a run that writes from one that only looks")
    assert tell["ansible.builtin.command"] == "true" and tell["register"] == "_cassandra_config_exporter_writes"
    plan_find = find_task(PLAN, "Find what the exporter's agent no longer needs (a run that only looks)")
    assert plan_find["when"] == "_cassandra_config_exporter_writes is skipped"
    rest_find = find_task(REST, "Find what the exporter's agent no longer needs (a run that writes)")
    assert rest_find["when"] == "_cassandra_config_exporter_writes is not skipped"
    assert plan_find["ansible.builtin.include_tasks"] == rest_find["ansible.builtin.include_tasks"]


def test_main_runs_the_plan_then_the_rest():
    with open(os.path.join(TASKS, "main.yml"), encoding="utf-8") as f:
        main = yaml.safe_load(f)
    assert [t["ansible.builtin.import_tasks"] for t in main] == ["plan.yml", "apply_plan.yml"]


def test_the_compare_runs_the_rest_where_the_plan_lists_something():
    plan, rest = (find_task(COMPARE, n) for n in ("Work out what the config role would change",
                                                  "Compare the config with the rest of the role"))
    assert plan["ansible.builtin.include_role"]["tasks_from"] == "plan.yml" and "when" not in plan
    assert rest["ansible.builtin.include_role"]["tasks_from"] == "apply_plan.yml" and rest["when"] == "_rest | bool"
    for task in (plan, rest):
        assert task["ansible.builtin.include_role"]["apply"] == {"check_mode": True, "diff": False}
    for full, verify, writes, runs in [(False, False, [], False), (False, False, ["files"], True),
                                       (True, False, [], True), (False, True, [], True)]:
        assert render(COMPARE["vars"]["_rest"], _cassandra_apply_full=full, _cassandra_apply_verify=verify,
                      _cassandra_config_plan_writes=writes) is runs


def test_the_other_compares_follow_apply_only():
    play = find_play(PLAYBOOK, "Find what each node needs")
    assert "'linux' in _cassandra_apply_parts.parts" in str(find_task(play, "Compare the OS settings")["when"])
    assert "'medusa' in _cassandra_apply_parts.parts" in find_task(play, "Compare Medusa")["when"]
    # (audit C10: the Java plan read whatever apply_only says: a move with java left out stops, below)
    java = find_task(play, "Work out the Java of the nodes running another one")
    assert "when" not in java
    names = [t.get("name") for t, dummy in walk(play["tasks"])]
    assert names.index(java["name"]) < names.index("Refuse to leave out a part a node needs") < names.index(
        "Write the JMX password file where it is missing or other")
    task = find_task(play, "Compare the repository, the packages and the firewall")

    def roles(given, **variables):
        return render(task["loop"], _roles=render(task["vars"]["_roles"], **variables),
                      _cassandra_apply_parts=cassandra_apply_only(given))
    assert roles("config,install,axonops", cassandra_manage_firewall=True, cassandra_axonops_enabled=True) == [
        "cassandra_install", "cassandra_axonops"]
    assert roles("", cassandra_manage_firewall=True, cassandra_axonops_enabled=False) == [
        "cassandra_repository", "cassandra_install", "cassandra_firewall"]
    # the unit and the config: always
    assert "when" not in COMPARE
    assert "apply_parts" not in str(find_task(play, "Compare the cassandra unit").get("when"))


@pytest.mark.parametrize("given, parts, left_out", [
    ("", list(PARTS), []),
    (None, list(PARTS), []),
    ("config", ["config", "unit"], ["java", "linux", "install", "repository", "medusa", "firewall", "axonops"]),
    ("config, linux", ["config", "unit", "linux"], ["java", "install", "repository", "medusa", "firewall", "axonops"]),
    (["linux", "config", "unit"], ["config", "unit", "linux"],
     ["java", "install", "repository", "medusa", "firewall", "axonops"]),
])
def test_apply_only_gives_the_parts(given, parts, left_out):
    said = cassandra_apply_only(given)
    assert said == {"parts": parts, "left_out": left_out, "refused": []}


def test_apply_only_refuses_an_unknown_part_or_no_config():
    said = cassandra_apply_only("config,linus", "ansible-playbook -i inv community.cassandra.apply_config --check")["refused"]
    assert said == (["apply_only: not a part of apply_config:", "  linus", "its parts:"] + ["  " + p for p in PARTS]
                    + ["  all (every part, given alone)", "WAY OUT  run:",
                       "  ansible-playbook -i inv community.cassandra.apply_config --check -e apply_only=config"])
    said = cassandra_apply_only("linux", "CMD")["refused"]
    assert said == ["apply_only: config is not among the parts given",
                    "config is always compared: the restart decision reads it", "WAY OUT  run:",
                    "  CMD -e apply_only=config,linux"]
    assert cassandra_apply_only("all") == cassandra_apply_only("")  # (to resume a run of every part)
    # (round 3, C3: all with parts: refused, given alone)
    assert cassandra_apply_only("all,linux", "CMD")["refused"] == [
        "apply_only: all stands for every part: give it alone", "WAY OUT  run:", "  CMD -e apply_only=all"]


def test_apply_only_refuses_to_leave_out_what_a_node_needs():
    hostvars = {"n1": {"cassandra_apply_config_version": {"kind": "release"}},
                "n2": {"cassandra_apply_config_version": {"kind": ""}, "cassandra_axonops_enabled": True},
                "n3": {"cassandra_axonops_enabled": "false", "cassandra_java_plan_todo": True},
                "n4": {"cassandra_java_plan_todo": True}}
    assert cassandra_apply_only_stops(list(hostvars), hostvars, list(PARTS), "CMD") == []
    said = cassandra_apply_only_stops(list(hostvars), hostvars, ["config", "unit", "linux"], "CMD")
    assert said == ["apply_only leaves install out; a newer release of its series to install, on these nodes:", "  n1",
                    "apply_only leaves axonops out; cassandra-env.sh loads the AxonOps agent, on these nodes:", "  n2",
                    "apply_only leaves java out; another Java to move to (cassandra_java_version), on these nodes:",  # (audit C10)
                    "  n3, n4",
                    "WAY OUT  run with those parts too:", "  CMD -e apply_only=config,unit,java,linux,install,axonops"]
    # (round 2, B4: a resume keeps the interrupted run's parts: start over)
    said = cassandra_apply_only_stops(["n4"], hostvars, ["config", "unit"], "CMD -e cassandra_rolling_resume=true", "OVER")
    assert said[-3:] == ["a resumed run keeps the parts of the interrupted one: start over",
                         "WAY OUT  start over with those parts too:", "  OVER -e apply_only=config,unit,java"]


def test_the_report_says_the_compare_in_one_line():
    gates = ["unchanged", "unchanged", "role", ""]
    assert cassandra_apply_gate_lines(gates) == ["config: 2 of 3 nodes found unchanged by its plan, 1 compared by the role"]
    assert cassandra_apply_gate_lines(gates, full=True) == ["config: every node compared by the role (apply_full)"]
    left_out = cassandra_apply_gate_lines(gates, ["linux", "install"])[1:]
    assert left_out == ["not checked (apply_only):", "  linux", "  install"]  # (round 2, C4)
    assert cassandra_apply_gate_lines([]) == []
    lines = cassandra_apply_gate_lines(["unchanged"] * 300, list(PARTS[2:]))
    assert all(len(line) <= 79 for line in lines)


def test_the_parts_left_out_are_said_in_the_plan():
    play = find_play(PLAYBOOK, "Find what each node needs")
    note = find_task(play, "Say the parts left out")
    assert note["ansible.builtin.include_role"]["tasks_from"] == "note.yml"
    assert note["vars"]["_cassandra_notes_in_plan"] is True
    assert render(note["vars"]["_note"], _nl="\n", _cassandra_apply_parts=cassandra_apply_only("config")) == (
        "WARNING  not checked (apply_only):\n  java\n  linux\n  install\n  repository\n  medusa\n  firewall\n  axonops")


# --- the checks once for the nodes that share their values (plan.yml, _cassandra_config_checks_once) -----------

def role_vars():
    with open(os.path.join(TOP, "roles", "cassandra_config", "vars", "main.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def by_name(name):
    """Whether a variable is one checks.yml looks up by a name it builds: in the key by its pattern."""
    return any(re.search(p, name) for p in role_vars()["_cassandra_config_check_names"])


# words of its messages, not variables it reads (cassandra_jvm, the running JVM's fact, would tell every node apart)
NOT_READ = {"cassandra_config": "the role's name", "cassandra_jvm": "cassandra_jvm<N>_gc",
            # the nodes the checks run for: the group itself, named in its messages
            "_cassandra_config_checked": "", "_cassandra_config_checked_names": ""}


def filter_names():
    """The names of the collection's filters, as their FilterModule gives them (never a word of a docstring or a dict
    key: a variable of that name would be left out)."""
    import importlib
    names = set()
    for path in glob.glob(os.path.join(TOP, "plugins", "filter", "*.py")):
        module = importlib.import_module("ansible_collections.community.cassandra.plugins.filter."
                                         + os.path.basename(path)[:-3])
        if hasattr(module, "FilterModule"):
            names |= set(module.FilterModule().filters())
    return names


def checks_read():
    """The variables checks.yml reads: the names it writes (comments left out), those it looks up by name, and the
    role variables they read, recursively (the filters it calls left out)."""
    role = os.path.join(TOP, "roles", "cassandra_config")
    defs = {}
    for part in ("defaults", "vars"):
        with open(os.path.join(role, part, "main.yml"), encoding="utf-8") as f:
            for name, value in (yaml.safe_load(f) or {}).items():
                defs[name] = value if isinstance(value, str) else yaml.safe_dump(value)
    filters = filter_names()

    def words(text):
        text = re.sub(r"(?m)^\s*#.*$", "", text)
        return set(re.findall(r"\b_?cassandra_[a-z0-9_]*[a-z0-9]\b", text)) - filters
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        todo = words(f.read())
    seen = set()
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo |= words(defs.get(name, "")) - seen
    return seen - {"_cassandra_config_check_vars"} - set(NOT_READ)


def test_the_checks_values_are_all_read():
    listed = role_vars()["_cassandra_config_check_vars"]
    assert listed == sorted(listed)
    assert sorted(n for n in checks_read() if not by_name(n)) == listed  # (those by name: their patterns)
    key = find_task(PLAN, "Note the variables the checks read")
    assert "query('ansible.builtin.varnames', *_cassandra_config_check_names)" in key["vars"]["_names"]
    assert "_cassandra_config_check_vars | select('in'" in key["vars"]["_names"]
    # (round 3, A1: names and values, not values alone)
    assert "dict(_names | zip(query('ansible.builtin.vars', *_names" in key["ansible.builtin.set_fact"]["_cassandra_config_check_key"]


def test_every_name_the_checks_build_is_in_their_key():
    # audit round 2, B1: the names checks.yml builds to look a variable up (each Java's JVM options, a G1 value of
    # each Java, the former extra lines) all match the key's patterns, whatever Java or value is added
    from ansible_collections.community.cassandra.plugins.filter.cassandra_jvm_options import cassandra_jvm_options_variable
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        text = f.read()
    tasks = yaml.safe_load(text)
    looked = re.findall(r"lookup\('ansible\.builtin\.vars', ([^,)]+)", text)
    assert sorted(set(looked)) == ["_read_file | community.cassandra.cassandra_jvm_options_variable", "prefix ~ name",
                                   "var"], looked  # (a new way of building a name: its names checked here too)
    jvm_files = sorted(set(f for files in role_vars()["_cassandra_config_files"].values() for f in files
                           if re.match(r"jvm[0-9]*-server[.]options$", f)) | {"jvm21-server.options"})
    built = ["cassandra_" + f[:-len("-server.options")] + "_removed_options" for f in jvm_files]  # (var)
    built += [cassandra_jvm_options_variable(f) for f in jvm_files]  # (_read_file | ...)
    g1 = next(t for t in tasks if "prefix ~ name" in str(t))
    built += [p + "x" for p in re.findall(r"'(cassandra_[a-z0-9_]*_)'", str(g1["vars"]))]  # (prefix ~ name)
    built += ["cassandra_env_extra_lines", "cassandra_logback_extra_lines"]
    assert [n for n in built if not by_name(n)] == []
    assert re.findall(r"query\('ansible\.builtin\.varnames', '([^']+)'", text) == ["^cassandra_(env|logback)_extra_lines$"]


def test_the_checks_run_once_for_the_nodes_that_share_their_values():
    group = find_task(PLAN, "Group the nodes whose checks read the same variables")
    hostvars = {"n1": {"_cassandra_config_check_key": "a"}, "n2": {"_cassandra_config_check_key": "b"},
                "n3": {"_cassandra_config_check_key": "a"}, "n4": {}}  # (n4: no key, a group of its own)
    groups = cassandra_check_groups(list(hostvars), hostvars)
    assert groups == {"n1": ["n1", "n3"], "n2": ["n2"], "n4": ["n4"]}
    key = find_task(PLAN, "Note the variables the checks read")
    assert key["ignore_errors"] is True and key["no_log"] is True  # (round 2, C1)
    assert group["run_once"] is True and "cassandra_check_groups(hostvars)" in str(group["ansible.builtin.set_fact"])
    checks = find_task(PLAN, "Check the variables of these nodes")
    assert checks["when"] == ("inventory_hostname in _cassandra_config_check_groups"
                              " or inventory_hostname not in _grouped")
    # per node elsewhere (a play whose nodes do not all run the plan), once in apply_config's compare
    assert find_task(PLAN, "Check the variables")["when"] == "not _cassandra_config_checks_once | default(false) | bool"
    once = COMPARE["vars"]["_cassandra_config_checks_once"]
    for full, verify, said in [(False, False, True), (True, False, False), (False, True, False)]:
        assert render(once, _cassandra_apply_full=full, _cassandra_apply_verify=verify) is said  # (audit B2)
    # the messages name the nodes checked
    with open(os.path.join(TOP, "roles", "cassandra_config", "vars", "main.yml"), encoding="utf-8") as f:
        names = yaml.safe_load(f)["_cassandra_config_checked_names"]
    assert render(names, inventory_hostname="n2") == "n2"
    assert render(names, inventory_hostname="n1", _cassandra_config_checked=["n1", "n3"]) == "n1, n3"
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        assert "inventory_hostname" not in f.read()


# --- the users' ~/.cassandra files (cqlsh, nodetool): in the plan, a difference schedules the node -----------------

def test_the_users_files_are_compared_as_written(tmp_path, monkeypatch):
    import collections
    from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_config_read as reader
    entry = collections.namedtuple("pw", "pw_name pw_dir pw_uid pw_gid")
    accounts = {"ops": entry("ops", str(tmp_path / "ops"), os.getuid(), os.getgid())}

    def getpwnam(name):
        return accounts[name]  # (KeyError: no such user, as pwd says it)
    monkeypatch.setattr(reader.pwd, "getpwnam", getpwnam)
    text = "credentials = " + reader.HOME + "/.cassandra/credentials\n"
    req = {"users": [{"user": "ops", "files": [{"name": "cqlshrc", "text": text}]}, {"user": "gone", "files": []}]}
    top = tmp_path / "ops" / ".cassandra"
    group = grp.getgrgid(os.getgid()).gr_name
    assert reader.step_users(req)["changes"] == [
        {"item": str(top), "before": "absent", "after": "written whole", "whole": True},
        {"item": str(top / "cqlshrc"), "before": "absent", "after": "written whole", "whole": True},
        {"item": "~gone/.cassandra", "before": "no such user", "after": "written whole", "whole": True}]
    top.mkdir(parents=True)
    top.chmod(0o755)
    (top / "cqlshrc").write_text("credentials = %s/.cassandra/credentials\n" % (tmp_path / "ops"))
    (top / "cqlshrc").chmod(0o600)
    req["users"].pop()
    me = pwd.getpwuid(os.getuid()).pw_name
    assert reader.step_users(req)["changes"] == [{"item": str(top) + " (owner:group mode)",
                                                  "before": "%s:%s 0755" % (me, group), "after": "ops:%s 0700" % group}]
    top.chmod(0o700)
    assert reader.step_users(req)["changes"] == []  # (its home put in: the same text)
    (top / "cqlshrc").chmod(0o644)
    assert reader.step_users(req)["changes"] == [{"item": str(top / "cqlshrc") + " (owner:group mode)",
                                                  "before": "%s:%s 0644" % (me, group), "after": "ops:%s 0600" % group}]
    (top / "cqlshrc").write_text("other\n")
    (top / "cqlshrc").chmod(0o600)
    assert reader.step_users(req)["changes"] == [{"item": str(top / "cqlshrc"), "before": "other content",
                                                  "after": "written whole", "whole": True}]


def test_the_plan_and_the_rest_render_the_same_users_files():
    files = cassandra_config_user_files([{"os_user": "ops", "username": "u", "password": "p"}], "41x", ["root", "cassandra"])
    assert [(f["user"], f["name"], f["template"]) for f in files] == [
        ("ops", "cqlshrc", "cqlshrc.j2"), ("ops", "credentials", "cqlsh_credentials.j2"),
        ("root", "nodetool-ssl.properties", "nodetool-ssl.properties.j2"),
        ("cassandra", "nodetool-ssl.properties", "nodetool-ssl.properties.j2")]
    assert [f["name"] for f in cassandra_config_user_files([{"os_user": "ops"}], "40x", [])] == ["cqlshrc"]
    assert cassandra_config_user_request(files, ["a", "b", "c", "d"]) == [
        {"user": "ops", "files": [{"name": "cqlshrc", "text": "a"}, {"name": "credentials", "text": "b"}]},
        {"user": "root", "files": [{"name": "nodetool-ssl.properties", "text": "c"}]},
        {"user": "cassandra", "files": [{"name": "nodetool-ssl.properties", "text": "d"}]}]
    # the rest writes the same templates, at the paths the plan compares, with its mode
    access = load_tasks(os.path.join(TASKS, "access.yml"))
    for name, template in (("Write ~/.cassandra/cqlshrc", "cqlshrc.j2"),
                           ("Write ~/.cassandra/credentials (4.1+)", "cqlsh_credentials.j2"),
                           ("Write their ~/.cassandra/nodetool-ssl.properties", "nodetool-ssl.properties.j2")):
        copy = find_task(access, name)["ansible.builtin.copy"]
        assert copy["content"] == "{{ lookup('ansible.builtin.template', '%s') }}" % template
        assert copy["mode"] == "0600" and copy["owner"] == "{{ item.os_user }}"
    # the plan's home token is the read script's
    plan = find_task(load_tasks(os.path.join(TASKS, "access_plan.yml")), "Compare the users' ~/.cassandra files with the role's")
    from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_config_read as reader
    assert "'_home': '%s'" % reader.HOME in plan["vars"]["_texts"]


def test_a_users_file_to_write_schedules_the_node():
    play = find_play(PLAYBOOK, "Find what each node needs")
    note = find_task(play, "Note what this node needs")
    users = note["vars"]["_users"]
    assert render(users, _cassandra_config_user_file_changes=[{"item": "/root/.cassandra/cqlshrc"}]) == [
        "/root/.cassandra/cqlshrc"]
    for name in ("_todo", "_steps", "_stage_other_changes"):
        assert "_jmx + _users + _exporter" in note["vars"][name], name
    assert verdict(_cassandra_config_user_file_changes=[{"path": "x"}]) == ["users' files"]


def test_a_resumed_run_keeps_the_parts_of_the_interrupted_one(tmp_path):
    play = find_play(PLAYBOOK, "Set up the progress file")
    setup = find_task(play, "Set up the progress file")
    assert render(setup["vars"]["_cassandra_progress_parts"], _cassandra_apply_parts=cassandra_apply_only("config,linux")) == (
        "config,unit,linux")
    progress = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "progress.yml"))
    names = [t["name"] for t, dummy in walk(progress)]
    # audit B5: written first, with the progress file
    assert names.index("Record what the run applies with the progress file") < names.index("Create the progress file")
    record = find_task(progress, "Record what the run applies with the progress file")
    find = find_play(PLAYBOOK, "Find what each node needs")
    refuse = find_task(find, "Refuse to resume with other parts than the interrupted run")
    pick = find_task(find, "Find the progress file a resume picks")
    assert pick["ansible.builtin.include_role"]["tasks_from"] == "progress_find.yml"
    done = tmp_path / "c-apply_config-20261125T120000.done"
    parts = cassandra_apply_only("config,linux")
    assert render(record["ansible.builtin.copy"]["dest"], cassandra_progress_file=str(done)) == str(
        tmp_path / "c-apply_config-20261125T120000.parts")
    assert render(record["ansible.builtin.copy"]["content"], _cassandra_progress_parts="config,unit,linux") == (
        "config,unit,linux\n")
    # (round 2, C5: a resume writes it when there is none, keeps it when there is one)
    assert record["ansible.builtin.copy"]["force"] == "{{ not _progress_resuming | bool }}"
    del parts
    found = {"files": [{"path": str(tmp_path / "old.done"), "mtime": 1}, {"path": str(done), "mtime": 2}]}
    assert render(refuse["vars"]["_resumed"], cassandra_progress_found=found) == str(done)
    assert render(refuse["vars"]["_resumed"], cassandra_progress_found={"skipped": True}) == ""

    def said(given, recorded):
        if recorded is not None:
            (tmp_path / "c-apply_config-20261125T120000.parts").write_text(recorded)
        variables = dict(_resumed=str(done), _cassandra_apply_parts=cassandra_apply_only(given), apply_only=given)
        rec, cur = (render(refuse["vars"][n], **variables) for n in ("_recorded", "_current"))
        return cassandra_apply_resume_refusal(str(done), rec, cur, "RESUME", "OVER")
    # audit B5: no record (an earlier version's run, or one stopped before it): refused, two exact ways out
    assert said("config,linux", None) == []  # (given: the operator says which)
    assert said("", None) == ["c-apply_config-20261125T120000.done: no record of the parts of the interrupted run",
                              "WAY OUT  resume with every part:", "  RESUME", "WAY OUT  or start over without resuming:",
                              "  OVER"]  # (round 3, B4: what was observed only)
    assert said("linux,config", "config,unit,linux\n") == []
    assert said("", "all\n") == []
    assert said("all", "all\n") == []
    assert said("", "config,unit,linux\n") == ["the interrupted run applied apply_only=config,unit,linux",
                                               "this one: every part",
                                               "a resumed run applies the parts the interrupted one did",
                                               "WAY OUT  resume with them:", "  RESUME"]
    # (round 3, B3: one source for the command, --check kept: this run's, without resuming)
    assert refuse["vars"]["_resume"] == ("{{ _cassandra_apply_command_over ~ ' -e cassandra_rolling_resume=true"
                                         " -e apply_only=' ~ (_recorded or 'all') }}")
    assert "_cassandra_apply_command_over) }}" in refuse["vars"]["_said"]


WRITERS_ANYWHERE = WRITERS + ("ansible.builtin.include_tasks", "ansible.builtin.include_role", "ansible.builtin.import_tasks")


def test_a_refused_resume_writes_nothing():
    # the check of a resume's parts comes before any task of the run that may write (the JMX password file, a mark
    # forgotten, the progress file and its parts, the plays that apply): it reads only, before them
    find = find_play(PLAYBOOK, "Find what each node needs")
    tasks = [t for t, dummy in walk(find["tasks"])]
    names = [t.get("name") for t in tasks]
    at = names.index("Refuse to resume with other parts than the interrupted run")
    before = [t for t in tasks[:at] if module(t) in WRITERS_ANYWHERE]
    reads = {"Load the role defaults", "Read the installed Cassandra package", "Read the tokens of the ring",
             "Find the progress file a resume picks"}
    assert sorted(t["name"] for t in before if t["name"] not in reads) == []
    for t in before:
        if module(t) == "ansible.builtin.command":
            assert t.get("changed_when") is False and t.get("check_mode") is False, t["name"]
    # the shared find reads only
    for t, dummy in walk(load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "progress_find.yml"))):
        assert module(t) in ("ansible.builtin.stat", "ansible.builtin.set_fact", "ansible.builtin.find"), t["name"]
    # and the plays after it are where the run writes
    assert PLAYBOOK.index(find) == 1 and PLAYBOOK[0]["ansible.builtin.import_playbook"] == "community.cassandra.preflight"


def test_the_users_files_in_the_differences():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_diff
    items = [{"item": "/root/.cassandra/cqlshrc (owner:group mode)", "before": "root:root 0644", "after": "root:root 0600"},
             {"item": "/root/.cassandra/credentials", "before": "absent", "after": "written whole", "whole": True}]
    lines = cassandra_apply_config_diff([{"name": "n2", "items": items}, {"name": "n1", "items": []}])
    text = "\n".join(lines)
    assert "cqlshrc" in text and "0644" in text and "0600" in text and "credentials" in text
    assert "points to" not in text


# names checks.yml's templates read that are no variable of the node's checks: Ansible's functions and constants
JINJA_GLOBALS = {"lookup", "query", "q", "now", "range", "dict", "lipsum", "cycler", "joiner", "namespace", "undef",
                 "role_path", "omit", "true", "false", "none", "True", "False", "None"}


EXPRESSIONS = ("when", "that", "failed_when", "changed_when", "until")


def template_names(value, expression=False):
    """The variables a task's value reads (its Jinja parsed, the names it does not set): the strings with Jinja, and
    the plain ones of its conditions (when, that...), which are expressions."""
    from jinja2 import Environment, meta
    if isinstance(value, dict):
        return set().union(set(), *[template_names(v, k in EXPRESSIONS) for k, v in value.items()])
    if isinstance(value, list):
        return set().union(set(), *[template_names(v, expression) for v in value])
    if not isinstance(value, str) or not ("{{" in value or "{%" in value or expression):
        return set()
    text = value if ("{{" in value or "{%" in value) else "{{ %s }}" % value

    class Any(dict):  # (every filter and test known: Ansible's and the collection's, not parsed here)
        def __contains__(self, name):
            return True

        def get(self, name, default=None):
            return dict.get(self, name) or (lambda *args, **kwargs: None)
    env = Environment(extensions=["jinja2.ext.do"])
    env.filters, env.tests = Any(env.filters), Any(env.tests)
    # (a name a {% set %} of the same string sets, in a loop or a branch: its own, not the node's)
    return meta.find_undeclared_variables(env.parse(text)) - set(re.findall(r"\{%-?\s*set\s+(\w+)\s*=", text))


def test_every_name_the_checks_read_is_in_their_key():
    # parsed from each task's Jinja (not a grep): a variable a check reads and the key misses would group nodes
    # whose verdicts differ (audit A1: cassandra_heap_newsize)
    with open(os.path.join(TOP, "roles", "cassandra_config", "vars", "main.yml"), encoding="utf-8") as f:
        listed = set(yaml.safe_load(f)["_cassandra_config_check_vars"])
    keyed = listed
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        tasks = yaml.safe_load(f)
    missing = {}
    for task in tasks:
        local = set(task.get("vars") or {}) | {"_cassandra_config_checked_names"}
        read = template_names({k: v for k, v in task.items() if k not in ("name",)}) - local - JINJA_GLOBALS - {"_", "community"}
        lost = sorted(n for n in read if n not in keyed and not by_name(n))
        if lost:
            missing[task["name"]] = lost
    assert missing == {}


@pytest.mark.parametrize("name, value", [("cassandra_heap_newsize", "800M"), ("cassandra_heap_size", "4G"),
                                         ("cassandra_config_keep_files", ["cassandra.yaml"]),
                                         ("cassandra_extra_settings", {"x": 1}), ("cassandra_rack", "r2"),
                                         ("cassandra_logback_root_extra_refs", ["X"]), ("cassandra_version", "41x"),
                                         ("cassandra_jvm17_max_gc_pause_millis", "300ms"),  # (by name)
                                         ("cassandra_jvm21_extra_options", ["-Xfoo"])])  # (a Java to come)
def test_two_nodes_differing_in_a_value_the_checks_read_are_two_groups(name, value):
    # audit round 2, C2: the key itself (the task's template, its lookups), the groups from it
    task = find_task(PLAN, "Note the variables the checks read")
    roles = role_vars()
    base = {"_cassandra_config_check_vars": roles["_cassandra_config_check_vars"],
            "_cassandra_config_check_names": roles["_cassandra_config_check_names"], "cassandra_heap_newsize": "",
            "cassandra_jvm17_max_gc_pause_millis": "", "cassandra_jvm21_extra_options": []}

    def key(**more):
        variables = dict(base, inventory_hostname="n", **more)
        variables["_names"] = render(task["vars"]["_names"], **variables)
        variables["_unread"] = render(task["vars"]["_unread"], **variables)
        return render(task["ansible.builtin.set_fact"]["_cassandra_config_check_key"], **variables)
    hostvars = {"n1": {"_cassandra_config_check_key": key()}, "n2": {"_cassandra_config_check_key": key(**{name: value})},
                "n3": {"_cassandra_config_check_key": key()}}
    assert cassandra_check_groups(list(hostvars), hostvars) == {"n1": ["n1", "n3"], "n2": ["n2"]}


@pytest.mark.parametrize("one, other", [
    ({"cassandra_jvm17_parallel_gc_threads": "300ms"}, {"cassandra_jvm17_max_gc_pause_millis": "300ms"}),
    ({"cassandra_jvm17_max_gc_pause_millis": "300ms"}, {"cassandra_jvm11_max_gc_pause_millis": "300ms"}),
    ({"cassandra_env_extra_lines": "x"}, {"cassandra_logback_extra_lines": "x"}),
])
def test_the_same_value_under_other_names_is_another_group(one, other):
    # audit round 3, A1: the key holds the names with their values
    task = find_task(PLAN, "Note the variables the checks read")
    roles = role_vars()

    def key(more):
        variables = dict({"_cassandra_config_check_vars": roles["_cassandra_config_check_vars"],
                          "_cassandra_config_check_names": roles["_cassandra_config_check_names"],
                          "inventory_hostname": "n"}, **more)
        variables["_names"] = render(task["vars"]["_names"], **variables)
        variables["_unread"] = render(task["vars"]["_unread"], **variables)
        return render(task["ansible.builtin.set_fact"]["_cassandra_config_check_key"], **variables)
    assert key(one) != key(other)


def test_a_node_in_no_group_is_checked_on_its_own():
    # audit round 3, B1: one not among the play's hosts when the groups were made (2.17+, an earlier failure handled)
    checks = find_task(PLAN, "Check the variables of these nodes")
    assert checks["when"] == "inventory_hostname in _cassandra_config_check_groups or inventory_hostname not in _grouped"
    groups = {"n1": ["n1", "n2"]}
    assert render(checks["vars"]["_cassandra_config_checked"], _cassandra_config_check_groups=groups,
                  inventory_hostname="n3") == ["n3"]
    rescue = next(t for t in checks["rescue"])
    # (round 4, B1: fails closed: lets a node go on only when its checks ended, or no failure of its own came since)
    assert rescue["when"] == ["not _cassandra_config_checks_ended | bool",
                              "ansible_failed_result | default(none) | to_json | hash('sha1') != _cassandra_config_failed_before"]
    assert [t["name"] for t in checks["block"]][-1] == "Note the checks of these nodes ended"


def test_the_checks_are_plain_top_level_tasks():
    # round 4, B1: every check a task of its own at the top of checks.yml, its name plain text (no block, no include:
    # a check's failure is said under its name)
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        tasks = yaml.safe_load(f)
    for task in tasks:
        assert not set(task) & {"block", "rescue", "always", "include_tasks", "ansible.builtin.include_tasks",
                                "import_tasks", "ansible.builtin.import_tasks", "include_role",
                                "ansible.builtin.include_role"}, task.get("name")
        assert "{{" not in task.get("name", "{{"), task.get("name")


CHECKS_PLAYBOOK = """
- hosts: all
  gather_facts: true
  any_errors_fatal: true  # (as apply_config's compare: audit round 2, A1)
  tasks:{before}
    - name: The checks
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_config
        tasks_from: {tasks}
"""
# (audit round 3, B1: an earlier failure of n3, handled: on 2.17+ the play's hosts shrink, its failed result stays)
EARLIER = """
    - name: An earlier failure of n3, handled
      {fatal}block:
        - name: A command that fails on n3
          ansible.builtin.command: "false"
          when: inventory_hostname == 'n3'
      rescue:
        - name: Handled
          ansible.builtin.debug: {{msg: handled}}"""


def run_checks(tmp_path, tasks, hosts, before=""):
    """checks.yml per node, or checks_once.yml, on local hosts with these variables -> (rc, output)."""
    from ansible_collections.community.cassandra.tests.unit.helpers import run_playbook
    lines = ["[c]"] + ["%s %s" % (h, " ".join("%s=%s" % kv for kv in v.items())) for h, v in hosts.items()]
    lines += ["[c:vars]", "ansible_connection=local", "ansible_python_interpreter=" + sys.executable,
              "cassandra_version=50x"]
    (tmp_path / "inv.ini").write_text("\n".join(lines) + "\n")
    (tmp_path / "play.yml").write_text(CHECKS_PLAYBOOK.format(tasks=tasks, before=before))
    return run_playbook(["-i", str(tmp_path / "inv.ini"), str(tmp_path / "play.yml")])


def said(out):
    """What the checks said, from the output of either run: {(the nodes it names, the variables it names)} of each
    stop (a check's failure, its rescue's) and each warning; nothing else (the key's ignored failure, the earlier one)."""
    found, task = set(), ""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("TASK ["):
            task = line
            continue
        if "cassandra_config :" not in task or "Note the variables the checks read" in task:
            continue
        failed = re.match(r"fatal: \[(n\d)\]: FAILED! => (.*)$", line)
        msgs = []
        if failed and not (i + 1 < len(lines) and lines[i + 1].startswith("...ignoring")):
            text = re.search(r'"msg": "((?:[^"\\]|\\.)*)"', failed.group(2))
            msgs.append((failed.group(1), text.group(1) if text else ""))
        warned = re.match(r'\s+"msg": "(WARNING  .*)"$', line)
        if warned:
            msgs.append(("", warned.group(1)))
        for host, text in msgs:
            nodes = set(re.findall(r"\bn\d\b", text.split(":")[0])) or {host}
            found.add((tuple(sorted(nodes)), tuple(sorted(set(re.findall(r"\bcassandra_\w+", text))))))
    return found


def recap_failed(out):
    """The hosts the recap says failed."""
    return set(re.findall(r"^(n\d)\s+: .*failed=[1-9]", out, re.M))


def per_node_said(found):
    """the same, each stop or warning split by node (a group's said once for its nodes)."""
    return set((node, names) for nodes, names in found for node in nodes)


@pytest.mark.playbook
@pytest.mark.parametrize("hosts, before", [
    ({"n1": {}, "n2": {"cassandra_heap_newsize": "800M"}}, ""),  # (audit A1: a warning for n2 only)
    ({"n1": {}, "n2": {"cassandra_disk_access_mode": "bogus"}}, ""),  # a refusal for n2 only
    ({"n1": {"cassandra_disk_access_mode": "bogus"}, "n2": {"cassandra_disk_access_mode": "bogus"}}, ""),  # for both
    ({"n1": {}, "n2": {}}, ""),  # nothing
    ({"n1": {}, "n2": {"cassandra_jvm17_max_gc_pause_millis": "300ms"}}, ""),  # (round 2, B2: a value looked up by name)
    # (round 2, C1: n2's key not made, ignored: n2 checked on its own, n1's checks still run, its warning said)
    ({"n1": {"cassandra_heap_newsize": "800M"}, "n2": {"cassandra_heap_newsize": "{{ no_such_variable }}"}}, ""),
    # (round 2, A1: 2.17+ with any_errors_fatal, the healthy group's first node came to the rescue too)
    ({"n1": {"cassandra_disk_access_mode": "bogus"}, "n2": {"cassandra_disk_access_mode": "bogus"},
      "n3": {"cassandra_rack": "r2"}, "n4": {"cassandra_rack": "r2"}}, ""),
    # (round 3, A1: the same value under other names: other variables, other verdicts)
    ({"n1": {"cassandra_jvm17_parallel_gc_threads": "300ms"}, "n2": {"cassandra_jvm17_max_gc_pause_millis": "300ms"}}, ""),
    ({"n1": {"cassandra_jvm17_max_gc_pause_millis": "300ms"}, "n2": {"cassandra_jvm11_max_gc_pause_millis": "300ms"}}, ""),
    ({"n1": {"cassandra_env_extra_lines": "x"}, "n2": {"cassandra_logback_extra_lines": "x"}}, ""),
    # (round 3, B1: an earlier failure of n3, handled, the play going on; and the same with its own any_errors_fatal)
    ({"n1": {"cassandra_disk_access_mode": "bogus"}, "n2": {}, "n3": {}}, EARLIER.format(fatal="")),
    ({"n1": {}, "n2": {}, "n3": {"cassandra_heap_newsize": "800M"}}, EARLIER.format(fatal="")),
    ({"n1": {"cassandra_disk_access_mode": "bogus"}, "n2": {}, "n3": {}},
     EARLIER.format(fatal="any_errors_fatal: false\n      ")),
])
def test_once_or_per_node_the_checks_say_the_same(tmp_path, hosts, before):
    from ansible.release import __version__
    rc_per_node, out_per_node = run_checks(tmp_path, "checks.yml", hosts, before)
    rc_once, out_once = run_checks(tmp_path, "checks_once.yml", hosts, before)
    per_node, once = per_node_said(said(out_per_node)), per_node_said(said(out_once))
    # (round 4, B1: the run stops, or not, the same way: rc, and a failed host in the recap when the checks failed)
    assert rc_once == rc_per_node
    assert bool(recap_failed(out_once)) == bool(recap_failed(out_per_node))
    if __version__.startswith("2.16."):
        # (round 3, B2: on 2.16 the other groups' first nodes finish their checks before the stop: a grouped run can
        # say more, all true)
        assert per_node <= once
    else:
        assert once == per_node


def test_apply_full_compares_as_before_the_gate():
    # audit B2: the rest everywhere, the checks per node, the users' files not compared (as before)
    users = render(COMPARE["vars"]["_cassandra_config_user_files_compare"], _cassandra_apply_full=True)
    assert users is False
    block = find_task(load_tasks(os.path.join(TASKS, "access_plan.yml")), "Compare the cqlsh and nodetool files of the users")
    assert "_cassandra_config_user_files_compare | default(true) | bool" in block["when"]


def test_the_users_files_stop_says_why_and_the_way_out():
    # audit B4: an operator stop (cassandra_output), its cause, then its way out
    stop = find_task(load_tasks(os.path.join(TASKS, "access_plan.yml")), "Stop when the users' files could not be compared")
    assert stop["vars"]["cassandra_output"] is True
    lines = [render(line, inventory_hostname="n2", _read={"error": "KeyError: 'x'"}, _answered=True,
                    _cassandra_config_python="python3") for line in stop["vars"]["_said"]]
    assert lines == ["n2: the users' ~/.cassandra files (cqlsh, nodetool) could not be compared",
                     "the compare said: KeyError: 'x'", "WAY OUT  fix what it says on n2, then run again"]


def test_the_old_progress_files_cleanup_keeps_the_parts_of_a_live_run(tmp_path):
    # audit B5: a .parts stays while its .done is not old (written to since: its mtime moves on, the .parts' does not)
    progress = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "progress.yml"))
    remove = find_task(progress, "Remove old progress files")
    old = [{"path": str(tmp_path / "a.done")}, {"path": str(tmp_path / "a.parts")}, {"path": str(tmp_path / "b.parts")},
           {"path": str(tmp_path / "c.parts")}]
    (tmp_path / "b.done").write_text("n1\n")  # (b: a live run, its .done new)

    def removed(item):
        done = render(remove["vars"]["_done"], item=item)
        return render("{{ %s }}" % remove["when"], item=item, _done=done, cassandra_progress_old={"files": old})
    assert [removed(i) for i in old] == [True, True, False, True]  # (c: no .done at all)


def test_a_resume_with_an_apply_only_typo_is_told_to_resume():
    # round 2, B4: the way out keeps -e cassandra_rolling_resume=true; the start over command has none
    note = find_task(find_play(PLAYBOOK, "Find what each node needs"), "Note the parts of the run")
    variables = dict(note["vars"], cassandra_rolling_resume=True, ansible_check_mode=False)
    variables = dict(variables, _options=render(note["vars"]["_options"], **variables),
                     _extra=render(note["vars"]["_extra"], **variables))
    assert variables["_extra"] == ["-e cassandra_rolling_resume=true"]
    assert "extra=_extra" in note["vars"]["_command"]
    over = note["ansible.builtin.set_fact"]["_cassandra_apply_command_over"]
    assert "extra" not in over and "if cassandra_rolling_resume | default(false) | bool else ''" in over


def test_apply_full_writes_the_users_files_as_before():
    # round 2, C6: the node's turn too (the role run in full there)
    action = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "action_apply_config.yml"))
    write = find_task(action, "Write the config")
    assert render(write["vars"]["_cassandra_config_user_files_compare"], _cassandra_apply_full=True) is False
    assert render(write["vars"]["_cassandra_config_user_files_compare"]) is True


def test_a_users_files_compare_that_crashed_is_said():
    # round 2, C7: no answer from the script (its Python failing): its last line of error, not hidden by no_log
    plan = load_tasks(os.path.join(TASKS, "access_plan.yml"))
    read = find_task(plan, "Compare the users' ~/.cassandra files with the role's")
    assert read["failed_when"] is False
    stop = find_task(plan, "Stop when the users' files could not be compared")
    crashed = {"rc": 127, "stdout": "", "stderr": "Traceback...\n/usr/bin/python3.11: not found"}
    variables = dict(cassandra_config_users_read=crashed, _nl="\n", inventory_hostname="n2", _cassandra_config_python="/usr/bin/python3")
    variables["_answered"] = render(stop["vars"]["_answered"], **variables)
    said = render(stop["vars"]["_read"], **variables)
    assert said == {"error": "/usr/bin/python3.11: not found"}
    lines = [render(line, _read=said, **variables) for line in stop["vars"]["_said"]]
    assert lines == ["n2: the users' ~/.cassandra files (cqlsh, nodetool) could not be compared",
                     "/usr/bin/python3 failed: /usr/bin/python3.11: not found",
                     "WAY OUT  make /usr/bin/python3 run on n2, then run again"]
    # (round 3, C4: nothing on stderr: said so)
    quiet = dict(variables)
    quiet["cassandra_config_users_read"] = {"rc": 1, "stdout": "", "stderr": ""}
    assert render(stop["vars"]["_read"], **quiet) == {"error": "no error output, rc 1"}
    answered = dict(variables, _answered=True)
    answered["cassandra_config_users_read"] = {"rc": 0, "stdout": '{"changes": []}'}
    assert render(stop["vars"]["_read"], **answered) == {"changes": []}


def jinja_var_lookups(value):
    """The calls of the vars and varnames lookups in a task's Jinja, any spelling (lookup, query, q; the plugin's
    short or full name): their count."""
    from jinja2 import Environment, nodes
    if isinstance(value, dict):
        return sum(jinja_var_lookups(v) for v in value.values())
    if isinstance(value, list):
        return sum(jinja_var_lookups(v) for v in value)
    if not isinstance(value, str) or not ("{{" in value or "{%" in value):
        return 0
    env = Environment(extensions=["jinja2.ext.do"])
    count = 0
    for call in env.parse(value).find_all(nodes.Call):
        name = getattr(call.node, "name", "")
        first = call.args[0] if call.args else None
        if name in ("lookup", "query", "q") and isinstance(first, nodes.Const) and str(first.value) in (
                "vars", "varnames", "ansible.builtin.vars", "ansible.builtin.varnames"):
            count += 1
    return count


def test_the_checks_look_variables_up_only_as_the_name_test_reads():
    # audit round 3, C1: a lookup of a variable by name in another spelling (q('vars', ...)) would escape the regex
    # of test_every_name_the_checks_build_is_in_their_key: every one parsed here is one that test reads
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        text = f.read()
    parsed = jinja_var_lookups(yaml.safe_load(text))
    read = len(re.findall(r"lookup\('ansible\.builtin\.vars', ", text)) + len(
        re.findall(r"query\('ansible\.builtin\.varnames', ", text))
    assert parsed == read and parsed > 0
    assert jinja_var_lookups("{{ q('vars', 'cassandra_' ~ x) }}") == 1  # (the parse sees other spellings)


def test_bad_cqlsh_credentials_are_refused_before_they_are_rendered():
    # round 4, C1: a missing field said with the entry (not "censored" by the no_log task that renders them)
    plan = load_tasks(os.path.join(TASKS, "access_plan.yml"))
    names = [t["name"] for t, dummy in walk(plan)]
    assert names.index("Refuse cqlsh credentials without an OS user, a username or a password") < names.index(
        "Compare the users' ~/.cassandra files with the role's")
    refuse = find_task(plan, "Refuse cqlsh credentials without an OS user, a username or a password")
    assert "no_log" not in refuse and refuse["vars"]["cassandra_output"] is True
    said = cassandra_cqlsh_credential_refusals([{"os_user": "root", "username": "u", "password": "secret"},
                                                {"os_user": "ops", "password": "secret"}, {"username": "u"}, "x"])
    assert said == ["  entry 2 (ops): no username", "  entry 3 (no os_user): no os_user, no password",
                    "  entry 4 (no os_user): no os_user, no username, no password"]
    assert not any("secret" in line for line in said)
    lines = render(refuse["vars"]["_said"], _wrong=said)
    assert lines[0] == "cassandra_cqlsh_credentials: entries the role cannot write:" and lines[-1].startswith("WAY OUT  ")


@pytest.mark.parametrize("given", ["all,linus", "ALL,linus", "linus,All"])
def test_a_typo_with_all_is_told_to_give_all(given):
    # round 4, C2
    assert cassandra_apply_only(given, "CMD")["refused"][-1] == "  CMD -e apply_only=all"
