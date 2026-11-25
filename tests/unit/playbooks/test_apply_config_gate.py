from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config's compare of cassandra_config: the role's plan (tasks/plan.yml) on every node, the rest of the role
# (tasks/apply_plan.yml) only where the plan lists something it would write (_cassandra_config_plan_writes). The
# guards of that skip: every task of the rest that writes is covered by an item of the plan's list (or never writes
# in the compare); every message and stop of the rest is said only when the plan lists something (or never in the
# compare); every fact of the rest the run reads is set by the gate as the rest leaves it, or checked by
# apply_verify. A task added to the rest fails here until it is classified.

import glob
import grp
import os
import pwd
import re

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_gate import (
    PARTS, cassandra_apply_gate_lines, cassandra_apply_only, cassandra_apply_only_stops, cassandra_check_groups,
    cassandra_config_user_files, cassandra_config_user_request)
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
    "Seed the alternative conf dir from the one in use": "files",  # (a conf dir that moves: every file written)
    "Select the alternative conf dir": "files",
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
        assert task["ansible.builtin.include_role"]["apply"] == {"check_mode": True}
    for full, verify, writes, runs in [(False, False, [], False), (False, False, ["files"], True),
                                       (True, False, [], True), (False, True, [], True)]:
        assert render(COMPARE["vars"]["_rest"], _cassandra_apply_full=full, _cassandra_apply_verify=verify,
                      _cassandra_config_plan_writes=writes) is runs


def test_the_other_compares_follow_apply_only():
    play = find_play(PLAYBOOK, "Find what each node needs")
    assert "'linux' in _cassandra_apply_parts.parts" in str(find_task(play, "Compare the OS settings")["when"])
    assert "'medusa' in _cassandra_apply_parts.parts" in find_task(play, "Compare Medusa")["when"]
    assert "'java' in _cassandra_apply_parts.parts" in find_task(play, "Work out the Java of the nodes running another one")["when"]
    # (java left out: the running JVM read all the same, the unit's read and the restart pending need it)
    jvm = find_task(play, "Find the running Cassandra")
    assert jvm["when"] == "'java' not in _cassandra_apply_parts.parts"
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
    said = cassandra_apply_only("config,linus")["refused"]
    assert said[0] == "apply_only: linus: not a part of apply_config"
    assert said[1] == "its parts: " + ", ".join(PARTS)
    assert said[2] == "WAY OUT  -e apply_only=config"
    said = cassandra_apply_only("linux")["refused"]
    assert said[0] == "apply_only: linux: config is not among them"
    assert said[-1] == "WAY OUT  -e apply_only=config,linux"
    assert all(len(line) <= 77 for line in said)  # (indented 2 in the refusal)


def test_apply_only_refuses_to_leave_out_what_a_node_needs():
    hostvars = {"n1": {"cassandra_apply_config_version": {"kind": "release"}},
                "n2": {"cassandra_apply_config_version": {"kind": ""}, "cassandra_axonops_enabled": True},
                "n3": {"cassandra_axonops_enabled": "false"}}
    assert cassandra_apply_only_stops(list(hostvars), hostvars, list(PARTS)) == []
    said = cassandra_apply_only_stops(list(hostvars), hostvars, ["config", "unit"])
    assert said == ["n1: a newer release of its series to install: apply_only leaves install out",
                    "WAY OUT  add install to apply_only, or run without apply_only",
                    "n2: cassandra-env.sh loads the AxonOps agent: apply_only leaves axonops out",
                    "WAY OUT  add axonops to apply_only, or run without apply_only"]


def test_the_report_says_the_compare_in_one_line():
    gates = ["unchanged", "unchanged", "role", ""]
    assert cassandra_apply_gate_lines(gates) == ["config: 2 of 3 nodes found unchanged by its plan, 1 compared by the role"]
    assert cassandra_apply_gate_lines(gates, full=True) == ["config: every node compared by the role (apply_full)"]
    assert cassandra_apply_gate_lines(gates, ["linux", "install"])[1] == "not checked (apply_only): linux, install"
    assert cassandra_apply_gate_lines([]) == []
    lines = cassandra_apply_gate_lines(["unchanged"] * 300, list(PARTS[2:]))
    assert all(len(line) <= 79 for line in lines)


def test_the_parts_left_out_are_said_in_the_plan():
    play = find_play(PLAYBOOK, "Find what each node needs")
    note = find_task(play, "Say the parts left out")
    assert note["ansible.builtin.include_role"]["tasks_from"] == "note.yml"
    assert note["vars"]["_cassandra_notes_in_plan"] is True
    assert render(note["vars"]["_note"], _cassandra_apply_parts=cassandra_apply_only("config")) == (
        "WARNING  not checked (apply_only): java, linux, install, repository, medusa, firewall, axonops")


# --- the checks once for the nodes that share their values (plan.yml, _cassandra_config_checks_once) -----------

# names checks.yml looks up by name (lookup('ansible.builtin.vars', ...), query varnames): written out here
CHECKS_BY_NAME = sorted(["cassandra_env_extra_lines", "cassandra_logback_extra_lines"] + [
    prefix + name for prefix in ("cassandra_jvm_", "cassandra_jvm8_", "cassandra_jvm11_", "cassandra_jvm17_")
    for name in ("max_tenuring_threshold", "g1_heap_region_size", "g1_new_size_percent",
                 "g1_rset_updating_pause_time_percent", "max_gc_pause_millis", "initiating_heap_occupancy_percent",
                 "removed_options", "extra_options")])


# words of its messages, not variables it reads (cassandra_jvm, the running JVM's fact, would tell every node apart)
NOT_READ = {"cassandra_config": "the role's name", "cassandra_jvm": "cassandra_jvm<N>_gc",
            # the nodes the checks run for: the group itself, named in its messages
            "_cassandra_config_checked": "", "_cassandra_config_checked_names": ""}


def checks_read():
    """The variables checks.yml reads: the names it writes (comments left out), those it looks up by name, and the
    role variables they read, recursively (the filters it calls left out)."""
    role = os.path.join(TOP, "roles", "cassandra_config")
    defs = {}
    for part in ("defaults", "vars"):
        with open(os.path.join(role, part, "main.yml"), encoding="utf-8") as f:
            for name, value in (yaml.safe_load(f) or {}).items():
                defs[name] = value if isinstance(value, str) else yaml.safe_dump(value)
    filters = set()
    for path in glob.glob(os.path.join(TOP, "plugins", "filter", "*.py")):
        with open(path, encoding="utf-8") as f:
            filters |= set(re.findall(r'"(cassandra_\w+)":', f.read()))

    def words(text):
        text = re.sub(r"(?m)^\s*#.*$", "", text)
        return set(re.findall(r"\b_?cassandra_[a-z0-9_]*[a-z0-9]\b", text)) - filters
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        todo = words(f.read()) | set(CHECKS_BY_NAME)
    seen = set()
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo |= words(defs.get(name, "")) - seen
    return seen - {"_cassandra_config_check_vars"} - set(NOT_READ)


def test_the_checks_values_are_all_read():
    with open(os.path.join(TOP, "roles", "cassandra_config", "vars", "main.yml"), encoding="utf-8") as f:
        listed = yaml.safe_load(f)["_cassandra_config_check_vars"]
    assert listed == sorted(listed)
    assert sorted(checks_read() - set(CHECKS_BY_NAME)) == listed  # (those looked up by name: plan.yml adds them)
    key = find_task(PLAN, "Note the values the checks read")
    by_name = key["vars"]["_by_name"]
    assert "query('ansible.builtin.varnames', '^cassandra_(env|logback)_extra_lines$')" in by_name
    jvm = ["%s%s" % (p, n) for p in re.findall(r"'(cassandra_jvm[0-9]*_)'", by_name) for n in key["vars"]["_names"]]
    assert sorted(jvm + ["cassandra_env_extra_lines", "cassandra_logback_extra_lines"]) == CHECKS_BY_NAME
    # its lookups by name: only the names written out above
    with open(os.path.join(TASKS, "checks.yml"), encoding="utf-8") as f:
        text = f.read()
    looked = re.findall(r"lookup\('ansible\.builtin\.vars', ([^,)]+)", text)
    assert sorted(set(looked)) == ["_read_file | community.cassandra.cassandra_jvm_options_variable", "prefix ~ name",
                                   "var"], looked
    assert re.findall(r"query\('ansible\.builtin\.varnames', '([^']+)'", text) == ["^cassandra_(env|logback)_extra_lines$"]


def test_the_checks_run_once_for_the_nodes_that_share_their_values():
    group = find_task(PLAN, "Group the nodes whose checks read the same values")
    hostvars = {"n1": {"_cassandra_config_check_key": "a"}, "n2": {"_cassandra_config_check_key": "b"},
                "n3": {"_cassandra_config_check_key": "a"}, "n4": {}}  # (n4: no key, a group of its own)
    groups = cassandra_check_groups(list(hostvars), hostvars)
    assert groups == {"n1": ["n1", "n3"], "n2": ["n2"], "n4": ["n4"]}
    assert group["run_once"] is True and "cassandra_check_groups(hostvars)" in str(group["ansible.builtin.set_fact"])
    checks = find_task(PLAN, "Check the variables of these nodes")
    assert checks["when"] == "inventory_hostname in _cassandra_config_check_groups"
    # per node elsewhere (a play whose nodes do not all run the plan), once in apply_config's compare
    assert find_task(PLAN, "Check the variables")["when"] == "not _cassandra_config_checks_once | default(false) | bool"
    assert COMPARE["vars"]["_cassandra_config_checks_once"] is True
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
    record = find_task(play, "Record the parts of the run with its progress file")
    refuse = find_task(play, "Refuse to resume with other parts than the interrupted run")
    names = [t["name"] for t in play["tasks"]]
    assert names.index("Set up the progress file") < names.index(refuse["name"]) < names.index(record["name"])
    done = tmp_path / "c-apply_config-20261125T120000.done"
    parts = cassandra_apply_only("config,linux")
    assert render(record["ansible.builtin.copy"]["dest"], cassandra_progress_file=str(done)) == str(
        tmp_path / "c-apply_config-20261125T120000.parts")
    assert render(record["ansible.builtin.copy"]["content"], _cassandra_apply_parts=parts) == "config,unit,linux\n"
    assert render(record["ansible.builtin.copy"]["content"], _cassandra_apply_parts=cassandra_apply_only("")) == "all\n"
    assert "not cassandra_rolling_resume | default(false) | bool" in record["when"]

    def said(given, recorded):
        if recorded is not None:
            (tmp_path / "c-apply_config-20261125T120000.parts").write_text(recorded)
        variables = dict(cassandra_progress_file=str(done), _cassandra_apply_parts=cassandra_apply_only(given))
        rec, cur = (render(refuse["vars"][n], **variables) for n in ("_recorded", "_current"))
        return None if rec == cur else [render(line, _recorded=rec, _current=cur) for line in refuse["vars"]["_said"]]
    assert said("config,linux", None) == ["the interrupted run applied every part", "this one: apply_only=config,unit,linux",
                                          "a resumed run applies the parts the interrupted one did",
                                          "WAY OUT  resume without -e apply_only"]  # (no record: an earlier version's run)
    assert said("", None) is None
    assert said("linux,config", "config,unit,linux\n") is None
    assert said("", "config,unit,linux\n")[-1] == "WAY OUT  resume with -e apply_only=config,unit,linux"


def test_the_users_files_in_the_differences():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_diff
    items = [{"item": "/root/.cassandra/cqlshrc (owner:group mode)", "before": "root:root 0644", "after": "root:root 0600"},
             {"item": "/root/.cassandra/credentials", "before": "absent", "after": "written whole", "whole": True}]
    lines = cassandra_apply_config_diff([{"name": "n2", "items": items}, {"name": "n1", "items": []}])
    text = "\n".join(lines)
    assert "cqlshrc" in text and "0644" in text and "0600" in text and "credentials" in text
    assert "points to" not in text
