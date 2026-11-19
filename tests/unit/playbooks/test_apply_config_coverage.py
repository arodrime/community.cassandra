from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config ignores no setting (plugin_utils cassandra_apply_config_coverage): besides the config files, the OS
# settings and Medusa, a node's Java (update_java's plan, shared), a newer release of its series (the package in its
# restart), what its unit gives the running Cassandra: its ONE restart, whatever the reasons; the repository, the
# packages and tools, the firewall: live, no restart; a version change of another kind, a host to add or remove, a
# joined node's other token: stopped before any change. Rendered by Ansible from the playbook and the task files.

import base64

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (
    EXTRA_SECTIONS, EXTRAS_LIVE, cassandra_apply_config_diff)
from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    FIND, NOTE, find, load, render, with_task_vars)

CONF = "/etc/cassandra"
LIMITS = """Limit                     Soft Limit           Hard Limit           Units
Max processes             32768                32768                processes
Max open files            100000               100000               files
Max locked memory         unlimited            unlimited            bytes
Max address space         unlimited            unlimited            bytes
"""
ACTION = load("roles", "cassandra_operation", "tasks", "action_apply_config.yml")


def needs(changes=(), java=False, version=None, unit_nofile=100000, extras=None, running=True, unit_differs=False,
          timesync=False):
    """What the plan finds for a running node imported (no record of its start): its facts by name."""
    unit = {"stdout_lines": ["user=cassandra", "group=cassandra",
                             "limits=" + base64.b64encode(LIMITS.encode()).decode(), "environment="]} if running else {}
    variables = {"_cassandra_config_changes": list(changes), "_cassandra_config_new": {}, "cassandra_conf_dir": CONF,
                 "_cassandra_config_header": [], "cassandra_medusa_enabled": False,
                 "cassandra_pending_snapshot": {"failed": True}, "_cassandra_config_perm_changes": [],
                 "_cassandra_config_dir_notes": [], "cassandra_config_restart_pending": False,
                 "cassandra_config_newer_files": [], "cassandra_config_unknown_files": [], "_cassandra_config_kept": [],
                 "cassandra_config_keep_files": [], "_cassandra_preflight_running": running,
                 "cassandra_apply_config_unit": {"skipped": True},
                 "cassandra_jvm": {"pid": "42", "start": "100.00"} if running else {}, "inventory_hostname": "n1",
                 "_cassandra_apply_config_as_started": [], "_cassandra_apply_config_conf_switch": False,
                 # the Java plan (java_plan.yml), the version, the unit, the extras
                 "cassandra_update_java_todo": java, "cassandra_update_java_major": "11", "cassandra_java_version": "17",
                 "cassandra_apply_config_version": version or {}, "cassandra_apply_config_unit_read": unit,
                 "cassandra_service_limit_nofile": unit_nofile, "cassandra_service_limit_nproc": 32768,
                 "cassandra_service_limit_memlock": "infinity", "cassandra_service_limit_as": "infinity",
                 "cassandra_service_environment": {}, "cassandra_service_start": "unit",
                 "_cassandra_service_unit_differs": unit_differs,
                 "_cassandra_change_report_seen": {"cassandra_install": {"items": extras or []}},
                 "_cassandra_linux_items": [{"item": "service chronyd", "timesync": True, "before": "inactive",
                                             "after": "active"}] if timesync else []}
    variables = with_task_vars(NOTE, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    keys = ("cassandra_apply_config_todo", "cassandra_apply_config_then", "cassandra_apply_config_steps",
            "cassandra_apply_config_restart_why", "cassandra_apply_config_restart_items", "cassandra_apply_config_extras",
            "cassandra_apply_config_notes", "cassandra_apply_config_kept_running", "cassandra_apply_config_why")
    out = dict((k[len("cassandra_apply_config_"):], render(facts[k], variables)) for k in keys)
    out["todo"] = out["todo"] in (True, "True")
    return out


RELEASE = {"kind": "release", "category": "B", "running": "4.1.11", "wanted": "4.1.12", "refused": [], "unknown": False,
           "notes": []}


def test_import_then_no_op_nothing_of_these_shows():
    # (an import: the running Java is the inventory's, the version too, the unit gives what the JVM got)
    found = needs()
    assert (found["todo"], found["then"], found["restart_items"], found["extras"]) == (False, "none", [], [])
    # a unit file that differs in systemd's handling only: said for the next restart, no restart, nothing to do
    found = needs(unit_differs=True)
    assert found["todo"] is False and found["then"] == "none"
    assert any(n.startswith("next restart: the cassandra unit written") for n in found["notes"])


def test_a_java_move_is_one_restart_installed_first():
    found = needs(java=True)
    assert (found["todo"], found["then"]) == (True, "restart")
    assert found["steps"] == ["install Java 17", "drain and restart Cassandra (Java 11 -> 17)"]
    assert found["restart_items"] == [{"section": "Java", "item": "Java", "before": "11", "after": "17 (installed)"}]


def test_several_restart_reasons_make_one_restart():
    found = needs(changes=["cassandra.yaml"], java=True, version=RELEASE, unit_nofile=1048576)
    assert found["then"] == "restart"
    # one window: the package with Cassandra stopped, once
    assert found["steps"] == ["install Java 17", "write the config", "drain and stop Cassandra",
                              "update Cassandra to 4.1.12, write its config again", "start Cassandra"]
    assert found["restart_why"] == "Java 11 -> 17, Cassandra 4.1.11 -> 4.1.12, its unit: LimitNOFILE"
    assert [i["section"] for i in found["restart_items"]] == ["Java", "Cassandra", "cassandra unit"]
    # with the time sync of the OS settings: the same window, the same restart
    found = needs(version=RELEASE, timesync=True)
    assert found["steps"].count("start Cassandra") == 1 and found["why"].startswith("time sync, Cassandra 4.1.11")


def test_a_unit_that_changes_what_cassandra_got_restarts_it():
    found = needs(unit_nofile=1048576)
    assert (found["then"], found["restart_why"]) == ("restart", "its unit: LimitNOFILE")
    assert found["restart_items"] == [{"section": "cassandra unit", "item": "unit LimitNOFILE", "before": "100000",
                                       "after": "1048576"}]


def test_the_packages_apply_live_jemalloc_at_the_next_restart():
    jemalloc = [{"item": "package jemalloc", "before": "absent", "after": "5.2.1"}]
    found = needs(extras=jemalloc)
    assert (found["todo"], found["then"], found["kept_running"]) == (True, "none", [EXTRAS_LIVE])
    assert "next restart: jemalloc loaded (installed now; no effect now)" in found["notes"]
    assert found["extras"] == [dict(jemalloc[0], section="Packages")]
    # with a restart for something else: applied before it, no note (it loads there)
    found = needs(changes=["cassandra.yaml"], extras=jemalloc)
    assert found["steps"] == ["write the config", "apply the repository, packages, firewall", "drain and restart Cassandra"]
    assert not any("jemalloc" in n for n in found["notes"])


def test_they_show_in_the_settings_that_differ():
    nodes = [{"name": "n1", "extra": [{"section": "Java", "item": "Java", "before": "11", "after": "17 (installed)"},
                                      {"section": "Packages", "item": "package jemalloc", "before": "absent",
                                       "after": "5.2.1"}]},
             {"name": "n2", "extra": []}]
    text = "\n".join(cassandra_apply_config_diff(nodes))
    assert EXTRA_SECTIONS["Java"] in text and EXTRA_SECTIONS["Packages"] in text
    assert text.index("Java (restart)") < text.index("Packages (no restart)")
    assert "17 (installed)" in text and "package jemalloc" in text


def test_the_plan_compares_them_in_check_mode_before_the_needs():
    names = [t.get("name") for t in FIND["tasks"]]
    for name in ("Note the version change", "Note what stops the run before any change", "Stop before any change"):
        assert names.index(name) < names.index("Compare the config")
    for name in ("Compare the repository, the packages and the firewall", "Work out the Java of the nodes running another one",
                 "Compare the cassandra unit", "Read what the running Cassandra got from its unit"):
        assert names.index("Compare the config") < names.index(name) < names.index("Note what this node needs")
    extras = find(FIND["tasks"], "Compare the repository, the packages and the firewall")
    assert extras["ansible.builtin.include_role"]["apply"] == {"check_mode": True}
    assert extras["vars"]["_cassandra_compare"] is True and extras["vars"]["cassandra_install_java"] is False
    java = find(FIND["tasks"], "Work out the Java of the nodes running another one")
    assert java["ansible.builtin.include_role"]["tasks_from"] == "java_plan.yml"
    assert java["vars"]["_cassandra_java_plan_run"] == "apply_config" and java["vars"]["_cassandra_java_plan_running_only"]
    stop = find(FIND["tasks"], "Stop before any change")
    assert stop["when"] == ["_cassandra_apply_config_stops | length > 0", "not ansible_check_mode"]  # --check: its recap


def test_the_node_s_turn_installs_restarts_once_and_checks():
    names = [t.get("name") for t in ACTION]
    assert names.index("Install the Java this node moves to") < names.index("Write the config") \
        < names.index("Drain and restart the node") < names.index("Check it runs the new Java")
    assert find(ACTION, "Install the Java this node moves to")["ansible.builtin.include_tasks"] == "java_install.yml"
    assert find(ACTION, "Check it runs the new Java")["ansible.builtin.include_tasks"] == "java_verify.yml"
    assert find(ACTION, "Check it runs the new release")["ansible.builtin.include_tasks"] == "version_verify.yml"
    restart = find(ACTION, "Drain and restart the node")["vars"]["_cassandra_service_while_stopped"]
    assert render(restart, {"cassandra_apply_config_release": True}) == "apply_config_window.yml"
    assert render(restart, {"cassandra_apply_config_release": False}) == ""
    window = load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")
    assert window[0]["ansible.builtin.include_tasks"] == "package_update.yml"
    # the upgrade's own node uses the same package step and check
    upgrade = load("roles", "cassandra_operation", "tasks", "action_upgrade.yml")
    assert [t.get("ansible.builtin.include_tasks") for t in upgrade if t.get("ansible.builtin.include_tasks")
            in ("package_update.yml", "version_verify.yml")] == ["package_update.yml", "version_verify.yml"]
    extras = find(ACTION, "Apply the repository, the packages and the firewall")
    assert "not ansible_check_mode" in extras["when"]


def test_the_stops_are_said_in_the_check_recap():
    summary = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Summary"][0]
    refused = find(summary["tasks"], "Print the changes")["vars"]["_refused"]
    assert "_cassandra_apply_config_stops" in refused and "_cassandra_apply_config_refused" in refused
