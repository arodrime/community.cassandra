from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config ignores no setting (plugin_utils cassandra_apply_config_coverage): besides the config files, the OS
# settings and Medusa, a node's Java (update_java's plan, shared), a newer release of its series (the package in its
# restart), what its unit gives the running Cassandra: its ONE restart, whatever the reasons; the repository, the
# packages and tools, the firewall: live, no restart; a version change of another kind, a host to add or remove, a
# joined node's other token: stopped before any change. Rendered by Ansible from the playbook and the task files.

import base64
import fnmatch
import os

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
          timesync=False, firewall=None, ceiling=None, env=None, repository=None):
    """What the plan finds for a running node imported (no record of its start): its facts by name."""
    unit = {"stdout_lines": ["user=cassandra", "group=cassandra",
                             "limits=" + base64.b64encode(LIMITS.encode()).decode(), "environment="]
            + (["ceiling=" + base64.b64encode(ceiling.encode()).decode()] if ceiling else [])
            # env: (unit's {name: value}, the JVM's environ, the unit's mtime)
            + (["environment=" + " ".join("%s=%s" % kv for kv in sorted(env[0].items())),
                "environ=" + base64.b64encode(env[1].encode()).decode(), "unitmtime=" + env[2]] if env else [])} \
        if running else {}
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
                 "cassandra_java_plan_todo": java, "cassandra_java_plan_major": "11", "cassandra_java_version": "17",
                 "cassandra_apply_config_version": version or {}, "cassandra_apply_config_unit_read": unit,
                 "cassandra_service_limit_nofile": unit_nofile, "cassandra_service_limit_nproc": 32768,
                 "cassandra_service_limit_memlock": "infinity", "cassandra_service_limit_as": "infinity",
                 "cassandra_service_environment": env[0] if env else {}, "cassandra_service_start": "unit",
                 "cassandra_service_unit_differs": unit_differs,
                 "_cassandra_repository_items": repository or [],
                 "_cassandra_change_report_seen": {"cassandra_install": {"items": extras or []},
                                                   "cassandra_firewall": {"items": firewall or []}},
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
    assert found["restart_items"] == [{"section": "Java", "item": "Java", "before": "11", "after": "17 (to install)"}]


def test_several_restart_reasons_make_one_restart():
    found = needs(changes=["cassandra.yaml"], java=True, version=RELEASE, unit_nofile=1048576)
    assert found["then"] == "restart"
    # one window: the package with Cassandra stopped, once
    # (the Java first, Cassandra running, its release kept; the package with Cassandra stopped)
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
    nodes = [{"name": "n1", "extra": [{"section": "Java", "item": "Java", "before": "11", "after": "17 (to install)"},
                                      {"section": "Packages", "item": "package jemalloc", "before": "absent",
                                       "after": "5.2.1"}]},
             {"name": "n2", "extra": []}]
    text = "\n".join(cassandra_apply_config_diff(nodes))
    assert EXTRA_SECTIONS["Java"] in text and EXTRA_SECTIONS["Packages"] in text
    assert text.index("Java (restart)") < text.index("Packages (no restart)")
    assert "17 (to install)" in text and "package jemalloc" in text


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
    # (the roles loop over item: the outer loop over item made Ansible warn on screen, 7 lines a run)
    assert extras["loop_control"]["loop_var"] != "item"
    assert extras["loop_control"]["loop_var"] in extras["ansible.builtin.include_role"]["name"]
    # the firewall as create_cluster and add_node run it: only with cassandra_manage_firewall (else a cluster they
    # made got firewalld installed, started and its ports opened by the next apply_config)
    assert render(extras["loop"], {}) == ["cassandra_repository", "cassandra_install"]
    assert render(extras["loop"], {"cassandra_manage_firewall": False}) == ["cassandra_repository", "cassandra_install"]
    assert render(extras["loop"], {"cassandra_manage_firewall": True})[-1] == "cassandra_firewall"
    java = find(FIND["tasks"], "Work out the Java of the nodes running another one")
    assert java["ansible.builtin.include_role"]["tasks_from"] == "java_plan.yml"
    assert java["vars"]["_cassandra_java_plan_run"] == "apply_config" and java["vars"]["_cassandra_java_plan_running_only"]
    stop = find(FIND["tasks"], "Stop before any change")
    # --check stops there too, as the real run (not later, on the state it describes); drift's compare: drift says it
    assert stop["when"] == ["_cassandra_apply_config_stops | length > 0",
                            "not _cassandra_apply_config_compare_only | default(false) | bool"]
    assert "check_mode" not in stop


def test_the_installed_package_is_read_one_line_per_package():
    # rpm -q of two cassandra headers (a transaction left half done) printed them on one line, the last
    # version taken; a Debian package half configured was taken for none
    read = find(FIND["tasks"], "Read the installed Cassandra package")
    assert render(read["ansible.builtin.command"]["argv"], {"ansible_facts": {"os_family": "RedHat"}}) == [
        "rpm", "-q", "--qf", "installed %{VERSION}\\n", "cassandra"]  # (rpm's own \\n: a newline)
    note = find(FIND["tasks"], "Note the version change")

    def noted(lines):
        variables = with_task_vars(note, {"cassandra_apply_config_package": {"stdout_lines": lines}})
        return render("{{ _installed }}", variables), render("{{ _unfinished }}", variables)
    assert noted(["installed 4.1.11"]) == ("4.1.11", "")
    assert noted(["installed 4.1.11", "installed 4.1.12"]) == ("", "installed 4.1.11, installed 4.1.12")
    assert noted(["half-configured 4.1.12"]) == ("", "half-configured 4.1.12")
    assert noted(["package cassandra is not installed"]) == ("", "")
    assert noted(["config-files 4.1.11"]) == ("", "")  # (removed, its config left: not installed)
    assert noted([]) == ("", "")


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
    assert window[0]["block"][2]["ansible.builtin.include_tasks"] == "package_update.yml"
    # the upgrade's own node uses the same package step and check
    upgrade = load("roles", "cassandra_operation", "tasks", "action_upgrade.yml")
    assert [t.get("ansible.builtin.include_tasks") for t in upgrade if t.get("ansible.builtin.include_tasks")
            in ("package_update.yml", "version_verify.yml")] == ["package_update.yml", "version_verify.yml"]
    extras = find(ACTION, "Apply the repository, the packages and the firewall")
    assert "not ansible_check_mode" in extras["when"]
    assert extras["vars"]["cassandra_install_java"] is False  # (a Java move installed it already, or does in its window)


def test_the_node_turns_load_the_java_variables_of_java_install():
    # (a node moving to another Java under --check and in a real run: java_install.yml read cassandra_java_version
    # and _cassandra_java_home, loaded in the plan's play only: undefined at the node's turn)
    plays = dict((p.get("name"), p) for p in load("playbooks", "apply_config.yml"))
    for name, task in (("Apply the config where nothing is restarted, on these nodes at once", "Apply the config on these nodes"),
                       ("Apply the config, one node at a time", "Apply the config on this node")):
        names = [t.get("name") for t in plays[name]["tasks"]]
        defaults = plays[name]["tasks"][names.index("Load the cassandra_install defaults")]
        assert defaults["ansible.builtin.include_role"] == {
            "name": "community.cassandra.cassandra_install", "tasks_from": "defaults.yml", "public": True}
        assert names.index("Load the cassandra_install defaults") < names.index(task)


def test_a_release_update_it_started_is_not_refused_as_an_unfinished_upgrade():
    # (a node updated to the inventory's release, the run stopped before the others: its resume and --check were
    # refused, "the cluster runs 2 Cassandra versions (an upgrade is not finished)", the way out naming upgrade)
    plays = load("playbooks", "preflight.yml")
    task = find([t for p in plays for t in p.get("tasks") or []], "One Cassandra version in the cluster")
    check = "{{ %s }}" % task["ansible.builtin.assert"]["that"]
    mixed = ["5.0.8: [10.0.0.2]", "5.0.9: [10.0.0.1]"]

    def passes(versions, **variables):
        variables = dict(variables, _versions=versions, _cassandra_preflight_release_update=variables.get("apply", False))
        variables.pop("apply", None)
        own = dict((k, v) for k, v in task["vars"].items() if k in ("_releases", "_release_update"))
        return render(check, with_task_vars({"vars": own}, variables))

    assert passes(["5.0.9: [10.0.0.1, 10.0.0.2]"])
    assert passes(mixed, apply=True, cassandra_package_version="5.0.9")
    assert passes(mixed, apply=True, cassandra_package_version="5.0.8")  # (back: refused later, by the plan)
    assert not passes(mixed, cassandra_package_version="5.0.9")  # another playbook
    assert not passes(mixed, apply=True, cassandra_package_version="5.0.10")  # none of them
    assert not passes(mixed, apply=True)  # no release asked
    assert not passes(["4.1.12: [10.0.0.2]", "5.0.9: [10.0.0.1]"], apply=True, cassandra_package_version="5.0.9")  # an upgrade
    assert passes(mixed, cassandra_allow_mixed_versions=True)
    preflight = load("playbooks", "apply_config.yml")[0]
    assert preflight["vars"]["_cassandra_preflight_release_update"] is True


def test_the_stops_are_said_in_the_check_recap():
    summary = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Summary"][0]
    refused = summary["vars"]["_refused"]  # (the play's: the recap's and the report's)
    assert "_cassandra_apply_config_stops" in refused and "_cassandra_apply_config_refused" in refused


def test_the_roles_compared_act_as_under_check_when_applied_to_them():
    # check_mode applied to a role does not set ansible_check_mode: each role reads its own flag
    import glob
    import os
    for role, flag in (("cassandra_install", "_cassandra_install_check"), ("cassandra_repository", "_cassandra_repository_check"),
                       ("cassandra_firewall", "_cassandra_firewall_check")):
        template = load("roles", role, "vars", "main.yml")[flag]
        for check, compare, expected in ((True, False, True), (False, True, True), (False, False, False)):
            assert render(template, {"ansible_check_mode": check, "_cassandra_compare": compare}) is expected
        for path in glob.glob(os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", role, "tasks", "*.yml")):
            with open(path, encoding="utf-8") as f:
                code = [line.split("  # ")[0] for line in f if not line.lstrip().startswith("#")]
            assert not any("ansible_check_mode" in line for line in code), path


def test_a_java_move_with_a_release_installs_java_first_its_release_kept():
    # (cassandra_install refused the other version: the Java installed, then the run stopped mid-way)
    alone = find(ACTION, "Install the Java this node moves to")
    kept = find(ACTION, "Install the Java this node moves to, its Cassandra release kept")
    for task in (alone, kept):
        assert task["ansible.builtin.include_tasks"] == "java_install.yml"

    def runs(task, **variables):
        return all(render("{{ %s }}" % w, variables) for w in task["when"])

    assert runs(alone, cassandra_apply_config_java=True, cassandra_apply_config_release=False)
    assert not runs(alone, cassandra_apply_config_java=True, cassandra_apply_config_release=True)
    assert runs(kept, cassandra_apply_config_java=True, cassandra_apply_config_release=True)
    assert render(kept["vars"]["cassandra_package_version"], {"cassandra_apply_config_version": RELEASE}) == "4.1.11"
    # (the package updated by other means since Cassandra started: kept at that one, which cassandra_install has)
    assert render(kept["vars"]["cassandra_package_version"],
                  {"cassandra_apply_config_version": dict(RELEASE, installed="4.1.12")}) == "4.1.12"
    names = [t.get("name") for t in ACTION]
    assert names.index("Install the Java this node moves to, its Cassandra release kept") < names.index("Drain and restart the node")
    window = load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]["block"]
    update = find(window, "Update the Cassandra package")
    assert update["ansible.builtin.include_tasks"] == "package_update.yml"
    assert update["vars"]["cassandra_install_java"] is False
    install = find(load("roles", "cassandra_operation", "tasks", "package_update.yml"), "Install the target version")
    assert install["vars"]["_cassandra_install_upgrade"] is True


def test_a_release_node_compares_its_extras_and_applies_the_firewall_live():
    port = [{"item": "port 7000/tcp", "before": "absent", "after": "open"}]
    jemalloc = [{"item": "package jemalloc", "before": "absent", "after": "5.2.1"}]
    found = needs(version=RELEASE, extras=jemalloc, firewall=port)
    assert found["extras"] == [dict(jemalloc[0], section="Packages"), dict(port[0], section="Firewall")]
    assert found["steps"] == ["apply the repository, packages, firewall", "drain and stop Cassandra",
                              "update Cassandra to 4.1.12, write its config again", "start Cassandra"]
    found = needs(version=dict(RELEASE, installed="4.1.11"))
    assert found["steps"] == ["drain and stop Cassandra", "update Cassandra to 4.1.12, write its config again",
                              "start Cassandra"]
    # installed already (by other means since Cassandra started): nothing to install in its window
    found = needs(version=dict(RELEASE, installed="4.1.12"))
    assert found["steps"][1] == "Cassandra 4.1.12 installed already, write its config again"
    # compared as the window installs them: the version change allowed there, under check mode
    compare = find(FIND["tasks"], "Compare the repository, the packages and the firewall")
    for kind, compared in (("", True), ("release", True), ("series", False), ("older", False)):
        version = {"kind": kind} if kind else {}
        for role in ("cassandra_repository", "cassandra_install"):
            assert all(render("{{ %s }}" % w, {"cassandra_apply_config_version": version, "_cassandra_compared_role": role})
                       for w in compare["when"]) is compared
    # (e2e r7) a package left half done: its install not compared ("has Cassandra 5.0.8, the inventory asks for
    # 5.0.9" from the first of two packages stopped the plan before the refusal of its start); the others are
    for role, compared in (("cassandra_install", False), ("cassandra_repository", True), ("cassandra_firewall", True)):
        assert all(render("{{ %s }}" % w, {"cassandra_apply_config_version": {}, "_cassandra_compared_role": role,
                                           "cassandra_apply_config_unfinished": "installed 5.0.8, installed 5.0.9"})
                   for w in compare["when"]) is compared, role
        assert render(compare["vars"]["_cassandra_install_upgrade"],
                      {"cassandra_apply_config_version": version}) is (kind == "release")
    # live, before the stop (a failure among the tools never leaves a node stopped): on a release node, the
    # repository and packages with its package kept at the release installed (the package itself in its window)
    extras = find(ACTION, "Apply the repository, the packages and the firewall")
    kept = find(ACTION, "Apply the packages, its Cassandra release kept")
    # the repository of a release node at the top of its turn (with its release check); a node not
    # running (then start, write) has its package updated already: its packages with the others', never with the
    # release it had (cassandra_install would refuse the version its package now has)
    for section, release, then, applied, applied_kept in (
            ("Firewall", True, "restart", True, False), ("Packages", True, "restart", False, True),
            ("Repository", True, "restart", False, False), ("Packages", False, "restart", True, False),
            ("Firewall", False, "restart", True, False), ("Packages", True, "start", True, False),
            ("Packages", True, "write", True, False), ("Repository", True, "start", False, False)):
        variables = {"ansible_check_mode": False, "item": {"section": section}, "cassandra_apply_config_release": release,
                     "cassandra_apply_config_extras": [{"section": section}], "cassandra_apply_config_then": then}
        assert all(render("{{ %s }}" % w, variables) for w in extras["when"]) is applied, (section, release, then)
        assert all(render("{{ %s }}" % w, variables) for w in kept["when"]) is applied_kept, (section, release, then)
    assert kept["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_install"}
    assert render(kept["vars"]["cassandra_package_version"],
                  {"cassandra_apply_config_version": dict(RELEASE, installed="4.1.11")}) == "4.1.11"
    names = [t.get("name") for t in ACTION]
    assert names.index("Apply the packages, its Cassandra release kept") < names.index("Drain and restart the node")
    # its window: nothing to install when the release wanted is installed already
    update = find(load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]["block"],
                  "Update the Cassandra package")
    window_vars = load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]["vars"]
    for installed, runs in (("4.1.11", True), ("4.1.12", False)):
        variables = with_task_vars({"vars": window_vars}, {"cassandra_apply_config_version": dict(RELEASE, installed=installed),
                                                           "cassandra_package_version": "4.1.12"})
        assert render("{{ %s }}" % update["when"], variables) is runs


def test_a_package_update_writes_the_limits_file_again():
    # e2e: the 5.0.9 RPM's update put its own /etc/security/limits.d/cassandra.conf (nofile 100000, a
    # plain file of the package) in place of cassandra_linux's (1048576): the next apply_config --check found it. The
    # window of a running node, a node not running and the upgrade all update the package with package_update.yml
    update = load("roles", "cassandra_operation", "tasks", "package_update.yml")
    names = [t.get("name") for t in update]
    limits = find(update, "Write the limits file of the cassandra user again")
    assert names.index("Install the target version") < names.index("Write the limits file of the cassandra user again")
    assert limits["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_linux",
                                                      "tasks_from": "limits.yml"}
    for manage, runs in ((True, True), (False, False)):
        assert render("{{ %s }}" % limits["when"], {"cassandra_linux_manage": manage}) is runs
    assert render("{{ %s }}" % limits["when"], {}) is True  # (the role's default: managed)
    for path in (("apply_config_window.yml",), ("action_apply_config.yml",), ("action_upgrade.yml",)):
        assert "package_update.yml" in str(load("roles", "cassandra_operation", "tasks", *path))
    # (the one task cassandra_linux's os.yml runs for them too)
    os_tasks = load("roles", "cassandra_linux", "tasks", "os.yml")
    assert {"name": "Limits of the cassandra user", "ansible.builtin.import_tasks": "limits.yml"} in os_tasks


def test_a_package_update_on_a_node_with_its_own_os_settings_says_a_changed_limits_file(monkeypatch):
    # cassandra_linux_manage: false (the node's OS settings its own): the RPM's update replaces its limits file with
    # the package's own; left as it is (never written back), said at once and in the recap as a WARNING, its former
    # content kept on the controller
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import (
        cassandra_limits_changed, cassandra_op_recap, cassandra_warnings)
    update = load("roles", "cassandra_operation", "tasks", "package_update.yml")
    names = [t.get("name") for t in update]
    assert (names.index("Read the limits file before the update") < names.index("Read its content")
            < names.index("Install the target version") < names.index("Read the limits file after the update")
            < names.index("Say the package changed the limits file"))
    for name in ("Read the limits file before the update", "Read the limits file after the update"):
        read = find(update, name)
        assert read["check_mode"] is False and read["ansible.builtin.stat"]["get_checksum"] is True
        # the package's own file (what the RPM writes, not cassandra_linux_limits_file), a link followed
        assert read["ansible.builtin.stat"]["path"] == "/etc/security/limits.d/cassandra.conf"
        assert read["ansible.builtin.stat"]["follow"] is True
        assert render("{{ %s }}" % read["when"], {"cassandra_linux_manage": False}) is True
        assert render("{{ %s }}" % read["when"], {}) is False  # (managed: written again instead)
    content = find(update, "Read its content")
    assert content["ansible.builtin.slurp"]["src"] == "/etc/security/limits.d/cassandra.conf"
    assert content["check_mode"] is False and content["failed_when"] is False
    for exists, runs in ((True, True), (False, False)):
        variables = {"cassandra_linux_manage": False, "cassandra_package_limits_before": {"stat": {"exists": exists}}}
        assert all(render("{{ %s }}" % w, variables) for w in content["when"]) is runs
    said = find(update, "Say the package changed the limits file")

    def changed(before, after, manage=False, link_before=None, link_after=None):
        variables = {"cassandra_linux_manage": manage, "cassandra_package_limits_before": {"stat": before},
                     "cassandra_package_limits_after": {"stat": after},
                     "cassandra_package_limits_link_before": {"stat": link_before or {}},
                     "cassandra_package_limits_link_after": {"stat": link_after or {}}}
        variables = with_task_vars(said, variables)
        return all(render("{{ %s }}" % w, variables) for w in said["when"])
    assert changed({"exists": True, "checksum": "aaa"}, {"exists": True, "checksum": "bbb"})
    # the operator's link replaced by a plain file of the same content: the link is lost, said
    lnk = {"exists": True, "islnk": True, "lnk_target": "/srv/l.conf"}
    assert changed({"exists": True, "checksum": "aaa"}, {"exists": True, "checksum": "aaa"}, link_before=lnk,
                   link_after={"exists": True, "islnk": False})
    assert not changed({"exists": True, "checksum": "aaa"}, {"exists": True, "checksum": "aaa"}, link_before=lnk,
                       link_after=lnk)
    assert not changed({"exists": True, "checksum": "aaa"}, {"exists": True, "checksum": "aaa"})
    assert not changed({"exists": False}, {"exists": False})
    assert changed({"exists": False}, {"exists": True, "checksum": "bbb"})
    assert not changed({"exists": True, "checksum": "aaa"}, {"exists": True, "checksum": "bbb"}, manage=True)
    # its former content: on the controller, 0600, named as the run's report plus the node
    keep = find(update, "Keep its former content on the controller")
    assert keep["delegate_to"] == "localhost" and keep["become"] is False and keep["vars"]["ansible_become"] is False
    write = find(update, "Write the former limits file")["ansible.builtin.copy"]
    assert write["mode"] == "0600" and write["dest"] == "{{ _saved }}"
    variables = {"_dir": "/inv/reports/prod", "cassandra_service_node_action": "upgrade", "inventory_hostname": "n1",
                 "hostvars": {"localhost": {"_cassandra_run_start": "1763600000"}}}
    from ansible_collections.community.cassandra.plugins.filter.cassandra_run_report import cassandra_run_report_name
    stamp = cassandra_run_report_name("upgrade", False, 1763600000)[:-len(".txt")]

    def saved(given):
        return render(keep["vars"]["_saved"], with_task_vars({"vars": {"_started": keep["vars"]["_started"]}}, given))
    assert saved(variables) == "/inv/reports/prod/%s-n1-limits.conf" % stamp
    # apply_config: the same time stamp as its report of the run (its own start, a little after the run's)
    variables["cassandra_service_node_action"] = "apply_config"
    variables["hostvars"]["localhost"]["_cassandra_run_report_started"] = "1763600042"
    stamp = cassandra_run_report_name("apply_config", False, 1763600042)[:-len(".txt")]
    assert saved(variables) == "/inv/reports/prod/%s-n1-limits.conf" % stamp
    for created, read, runs in ((False, True, True), (True, False, False), (False, False, False)):
        assert render("{{ %s }}" % keep["when"], {"_created": created, "_read": read}) is runs
    # the fact: replaced and kept, created, not kept
    note = find(update, "Note the package changed the limits file")
    fact = note["ansible.builtin.set_fact"]["cassandra_package_limits_changed"]

    def noted(variables):
        """the fact as the note sets it: the block's vars (given ones kept), then the note's"""
        block = dict(said, vars=dict((k, v) for k, v in said["vars"].items() if k not in variables))
        return render(fact, with_task_vars(note, with_task_vars(block, variables)))
    kept = {"_created": "False", "cassandra_package_limits_kept": {"dest": "/r/a-n1-limits.conf", "changed": True}}
    after = {"cassandra_package_limits_after": {"stat": {"exists": True, "checksum": "bbb"}}}
    assert noted(dict(kept, **after)) == {
        "path": "/etc/security/limits.d/cassandra.conf", "created": False, "saved": "/r/a-n1-limits.conf", "error": "",
        "removed": False, "link": "", "dangling": False}
    # removed by the package: said so, not "replaced"
    gone = noted(dict(kept, cassandra_package_limits_after={"stat": {"exists": False}}))
    assert gone["removed"] is True
    # a link left in place, its target gone (nothing read through it after): not removed, the link said
    left = noted(dict(kept, cassandra_package_limits_after={"stat": {"exists": False}},
                      cassandra_package_limits_link_before={"stat": {"exists": True, "islnk": True, "lnk_target": "/srv/l.conf"}},
                      cassandra_package_limits_link_after={"stat": {"exists": True, "islnk": True, "lnk_target": "/srv/m.conf"}}))
    assert left["removed"] is False and left["link"] == "/srv/l.conf"
    # a link before (its content read through it): the link to put back, unless it is still the same link
    link = {"cassandra_package_limits_link_before": {"stat": {"exists": True, "islnk": True, "lnk_target": "/srv/l.conf"}}}
    for link_after, said_link in (({"stat": {"exists": True, "islnk": False}}, "/srv/l.conf"),
                                  ({"stat": {"exists": False}}, "/srv/l.conf"),
                                  ({"stat": {"exists": True, "islnk": True, "lnk_target": "/srv/l.conf"}}, ""),
                                  ({"stat": {"exists": True, "islnk": True, "lnk_target": "/srv/m.conf"}}, "/srv/l.conf")):
        variables = dict(kept, cassandra_package_limits_link_after=link_after, **dict(after, **link))
        assert noted(variables)["link"] == said_link, link_after
    assert noted(dict(kept, **after))["link"] == ""  # (a plain file: none)
    for name in ("Read whether it is a link before the update", "Read whether it is a link after the update"):
        read = find(update, name)
        assert read["ansible.builtin.stat"]["follow"] is False and read["check_mode"] is False
        assert read["ansible.builtin.stat"]["path"] == "/etc/security/limits.d/cassandra.conf"
    assert names.index("Read whether it is a link before the update") < names.index("Install the target version") \
        < names.index("Read whether it is a link after the update") < names.index("Say the package changed the limits file")
    created = noted({"_created": "True", "cassandra_package_limits_kept": {"skipped": True}})
    assert created["created"] is True and created["saved"] == ""
    lost = noted({"_created": "False", "cassandra_package_limits_kept": {"failed": True, "msg": "Permission denied"}})
    # a dangling link before (nothing read through it): a link was there, not "created"
    dangling = noted({"cassandra_package_limits_before": {"stat": {"exists": False}},
                      "cassandra_package_limits_after": {"stat": {"exists": True, "checksum": "bbb"}},
                      "cassandra_package_limits_link_before": {"stat": {"exists": True, "islnk": True,
                                                                        "lnk_target": "/srv/gone.conf"}},
                      "cassandra_package_limits_link_after": {"stat": {"exists": True, "islnk": False}},
                      "cassandra_package_limits_kept": {"skipped": True}})
    assert dangling["created"] is False and dangling["link"] == "/srv/gone.conf" and dangling["dangling"] is True
    lines = cassandra_warnings(cassandra_limits_changed(["h-n6"], {"h-n6": {"cassandra_package_limits_changed": dangling}}))
    text = " ".join(" ".join(lines).split())
    assert "it was a link to /srv/gone.conf, which pointed to no file" in text
    assert "former content" not in text and "ln -sfn /srv/gone.conf" in text
    assert lost["saved"] == "" and lost["error"] == "Permission denied"
    # said at once on that node (a later stop or Ctrl-C would lose the recap), and in the recap
    now = find(update, "Say it now")
    assert now["vars"]["cassandra_output"] is True
    assert names.index("Say the package changed the limits file") < names.index("Write the limits file of the cassandra user again")
    block = said["block"]
    assert [t["name"] for t in block] == ["Keep its former content on the controller",
                                          "Note the package changed the limits file", "Say it now"]
    path = "/etc/security/limits.d/cassandra.conf"
    hostvars = {"h-n1": {"cassandra_package_limits_changed": {"path": path, "created": False, "error": "",
                                                              "saved": "/inv/reports/prod/upgrade-x-h-n1-limits.conf"}},
                "h-n2": {},
                "h-n3": {"cassandra_package_limits_changed": {"path": path, "created": True, "saved": "", "error": ""}},
                "h-n4": {"cassandra_package_limits_changed": {"path": path, "created": False, "saved": "",
                                                              "error": "Permission denied"}}}
    lines = render(now["ansible.builtin.debug"]["msg"], {"inventory_hostname": "h-n1", "hostvars": hostvars})
    assert lines == ["WARNING  h-n1: the Cassandra package update replaced",
                     "  /etc/security/limits.d/cassandra.conf",
                     "  left as the package wrote it (cassandra_linux_manage: false)",
                     "  its former content: /inv/reports/prod/upgrade-x-h-n1-limits.conf",
                     "  copy it back to /etc/security/limits.d/cassandra.conf if it held the limits",
                     "    of the cassandra user, or set cassandra_linux_manage: true (cassandra_linux",
                     "    writes it)"]
    # S29: the copy under the run's directory said from it (a path is never cut: 136 columns, the reports' dir
    # absolute); elsewhere, absolute
    deep = {"h-n1": {"cassandra_package_limits_changed": dict(
        hostvars["h-n1"]["cassandra_package_limits_changed"],
        saved="/home/ops/clusters/prod/reports/prod/apply_config-2026-11-21-084425-h-n1-limits.conf")}}
    said = cassandra_warnings(cassandra_limits_changed(["h-n1"], deep, "/home/ops/clusters/prod"))
    assert said[4] == "    reports/prod/apply_config-2026-11-21-084425-h-n1-limits.conf" and max(len(x) for x in said) <= 79
    assert "/home/ops/clusters/prod/reports" in " ".join(cassandra_warnings(cassandra_limits_changed(["h-n1"], deep, "/x")))
    # said from the run's directory (PWD) where it is said: on the node at once, in the recaps (node operations',
    # apply_config's)
    monkeypatch.setenv("PWD", "/home/ops/clusters/prod")
    shown = "    reports/prod/apply_config-2026-11-21-084425-h-n1-limits.conf"
    assert shown in render(now["ansible.builtin.debug"]["msg"], dict(inventory_hostname="h-n1", hostvars=deep))
    summary = load("roles", "cassandra_operation", "tasks", "summary.yml")[0]
    recap_play = [p for p in load("playbooks", "apply_config.yml") if "_more" in (p.get("vars") or {})][0]
    variables = dict(cassandra_service_summary_hosts=["h-n1"], hostvars=deep, groups={"prod": ["h-n1"]},
                     cassandra_hosts="prod", ansible_check_mode=False, _no_return=[])
    variables["_limits_changed"] = render(recap_play["vars"]["_limits_changed"], variables)
    for template in (summary["vars"]["_warnings"], recap_play["vars"]["_more"]):
        assert shown in cassandra_warnings(render(template, variables))
    warnings = cassandra_limits_changed(["h-n1", "h-n2", "h-n3", "h-n4"], hostvars)
    said_all = cassandra_warnings(warnings)
    assert max(len(x) for x in said_all) <= 79
    text = " ".join(" ".join(said_all).split())
    assert "h-n2" not in text
    assert "h-n3: the Cassandra package update created /etc/security/limits.d/cassandra.conf (there was none)" in text
    at = next(i for i, x in enumerate(said_all) if x.startswith("WARNING  h-n3: "))
    end = next(i for i, x in enumerate(said_all) if i > at and x.startswith("WARNING  "))
    created_said = " ".join(" ".join(said_all[at:end]).split())
    assert "copy it back" not in created_said and "check it holds the limits you want" in created_said
    assert "h-n4: the Cassandra package update replaced" in text
    assert "its former content was not saved (Permission denied)" in text
    # removed, or a link replaced: said as such, the way out the link put back
    path_said = {"path": path, "created": False, "error": "", "saved": "/r/upgrade-x-h-n5-limits.conf"}
    said_one = cassandra_warnings(cassandra_limits_changed(["h-n5"], {"h-n5": {
        "cassandra_package_limits_changed": dict(path_said, removed=True)}}))
    assert max(len(x) for x in said_one) <= 79
    one = " ".join(" ".join(said_one).split())
    assert "h-n5: the Cassandra package update removed /etc/security/limits.d/cassandra.conf" in one
    assert "replaced" not in one and "copy it back to /etc/security/limits.d/cassandra.conf" in one
    said_one = cassandra_warnings(cassandra_limits_changed(["h-n5"], {"h-n5": {
        "cassandra_package_limits_changed": dict(path_said, link="/srv/limits/cassandra.conf")}}))
    assert max(len(x) for x in said_one) <= 79
    one = " ".join(" ".join(said_one).split())
    assert "the Cassandra package update replaced /etc/security/limits.d/cassandra.conf" in one
    assert "it was a link to /srv/limits/cassandra.conf" in one
    assert "its former content (read through the link): /r/upgrade-x-h-n5-limits.conf" in one
    assert "put the link back (ln -sfn /srv/limits/cassandra.conf /etc/security/limits.d/cassandra.conf) if it held" \
        " the limits of the cassandra user, or set cassandra_linux_manage: true" in one
    assert "copy it back" not in one
    # removed: not written back (nothing left "as the package wrote it")
    said_one = cassandra_warnings(cassandra_limits_changed(["h-n5"], {"h-n5": {
        "cassandra_package_limits_changed": dict(path_said, removed=True)}}))
    assert "not written back (cassandra_linux_manage: false)" in " ".join(" ".join(said_one).split())
    assert "left as the package wrote it" not in " ".join(said_one)
    # a link target with a space: the command to copy quotes it
    said_one = cassandra_warnings(cassandra_limits_changed(["h-n5"], {"h-n5": {
        "cassandra_package_limits_changed": dict(path_said, link="/srv/my limits.conf")}}))
    assert "ln -sfn '/srv/my limits.conf' /etc/security/limits.d/cassandra.conf" in " ".join(" ".join(said_one).split())
    # the copies are kept, not rotated with the reports (the only copy of the operator's file): the rotation of the
    # reports never picks one, nor does the find of their files
    from ansible_collections.community.cassandra.plugins.filter.cassandra_run_report import cassandra_run_report_old
    stamp = cassandra_run_report_name("upgrade", False, 1763600000)[:-len(".txt")]
    reports = ["/r/" + cassandra_run_report_name("upgrade", False, 1763600000 + 60 * i) for i in range(5)]
    copies = ["/r/%s-h-n%d-limits.conf" % (stamp, i) for i in range(5)]
    files = [{"path": f, "mtime": i} for i, f in enumerate(reports + copies)]
    assert cassandra_run_report_old(files, "upgrade", 0) == reports  # (every report, no copy)
    run_report = load("roles", "cassandra_operation", "tasks", "run_report.yml")
    listed = find(run_report, "List the reports of this operation")["ansible.builtin.find"]
    pattern = render(listed["patterns"], {"_cassandra_run_report_operation": "upgrade"})
    assert fnmatch.fnmatch(os.path.basename(reports[0]), pattern)
    assert not [c for c in copies if fnmatch.fnmatch(os.path.basename(c), pattern)]
    lines = cassandra_op_recap([{"node": "h-n1", "result": "upgrade done in 90s"},
                                {"node": "h-n2", "result": "upgrade done in 95s"}], cluster="zorglub",
                               operation="upgrade", warnings=cassandra_limits_changed(["h-n1", "h-n2"], hostvars))
    assert [x for x in lines if x.startswith("WARNING  h-n1: the Cassandra package update replaced")]
    assert max(len(x) for x in lines) <= 79
    assert cassandra_limits_changed(["h-n2"], hostvars) == []
    assert "WARNING" not in " ".join(cassandra_op_recap([{"node": "h-n2", "result": "upgrade done in 95s"}],
                                                        operation="upgrade", warnings=[]))
    # every recap of a package update: apply_config's (with its other warnings), the node operations' (upgrade)
    summary = load("roles", "cassandra_operation", "tasks", "summary.yml")[0]
    assert "warnings=_warnings" in summary["ansible.builtin.debug"]["msg"]


def test_a_release_with_its_repository_changed_is_looked_for_before_the_stop():
    # the plan cannot look in a repository this run writes: looked for once written live, before the drain
    # and the stop, so that a release it lacks stops the run with Cassandra running and its package unchanged
    # before any other change on the node: its Java, its config, a stopped node's package, the unit's
    # failed starts cleared
    names = [t.get("name") for t in ACTION]
    look = find(ACTION, "Apply the repository of the release, then look for the release in it")
    # (after the checks that change nothing: "Nothing was changed on it" stays true there)
    assert names.index("Clear the unit's failed starts") < names.index("Check the JVM a file is written back for still runs") \
        < names.index("Apply the repository of the release, then look for the release in it") \
        < names.index("Install the Java this node moves to")
    assert names.index("Install the Java this node moves to, its Cassandra release kept") > 0
    assert names.index("Write the config") > names.index("Update the Cassandra package of a node not running") > 0
    for release, sections, check, looked in ((True, ["Repository"], False, True),
                                             (True, ["Packages", "Repository"], False, True),
                                             (True, ["Packages"], False, False), (False, ["Repository"], False, False),
                                             (True, ["Repository"], True, False)):
        variables = {"ansible_check_mode": check, "cassandra_apply_config_release": release,
                     "cassandra_apply_config_extras": [{"section": s} for s in sections]}
        assert all(render("{{ %s }}" % w, variables) for w in look["when"]) is looked, (release, sections, check)
    repository, read, include = look["block"]
    assert repository["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_repository"}
    assert "ansible.builtin.package_facts" in read and read["check_mode"] is False
    assert include["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_install",
                                                       "tasks_from": "release_check.yml"}
    # the release wanted (the inventory's cassandra_package_version, not the installed one the live step kept)
    assert include["vars"]["_cassandra_install_upgrade"] is True and "cassandra_package_version" not in include["vars"]
    assert render(include["vars"]["_cassandra_install_before"], {"ansible_facts": {"packages": {
        "cassandra": [{"version": "4.1.11"}]}}}) == {"cassandra": [{"version": "4.1.11"}]}
    assert include["vars"]["_cassandra_install_again"] == "resume"
    # what the run changed on the node before its turn: said in the stop (observed: the facts the plays set)
    before = include["vars"]["_cassandra_install_changed_before"]
    assert render(before, {}) == []
    assert render(before, {"cassandra_apply_config_os_done": True, "cassandra_apply_config_os_live": [
        {"item": "vm.max_map_count"}], "_cassandra_config_jmx_written": [{}]}) == [
        "its OS settings (vm.max_map_count)", "its JMX password file"]
    assert render(before, {"cassandra_apply_config_os_done": True, "cassandra_apply_config_os_live": []}) == []
    # the check of release_check.yml runs there (not the plan: no check mode), and its stop says the repository
    # file written by this run, the package unchanged
    check = load("roles", "cassandra_install", "tasks", "release_check.yml")
    variables = {"_cassandra_install_upgrade": True, "_cassandra_install_check": False, "cassandra_package_version": "4.1.12",
                 "cassandra_offline": False, "cassandra_install_method": "repository",
                 "_cassandra_install_before": {"cassandra": [{"version": "4.1.11"}]}, "ansible_facts": {"os_family": "RedHat"},
                 "inventory_hostname": "n1", "_cassandra_java_home": "", "_cassandra_repository_items": [{"item": "baseurl"}]}
    assert all(render("{{ %s }}" % w, variables) for w in check[0]["when"])
    stop = check[1]
    said = render(stop["ansible.builtin.fail"]["msg"], with_task_vars(stop, dict(
        variables, cassandra_install_release={"rc": 1, "stdout_lines": ["Error: No matching Packages to list"]}))).split("\n")
    assert said[:2] == ["n1: dnf did not list Cassandra 4.1.12: Error: No matching Packages to list.",
                        "Its Cassandra package was not changed; its repository file was written by this run."]
    # the plan's own stop and a plain run: the way out with its "run again"
    assert said[2] == ("Way out: set cassandra_package_version to a release it lists (dnf --showduplicates list cassandra"
                       " there), or add that release to the repository, then run again.")
    # apply_config's node turn: one way out, as the stop block takes it (STOPPED, 1. fix it, 2. resume),
    # with what the run changed on it before
    said = render(stop["ansible.builtin.fail"]["msg"], with_task_vars(stop, dict(
        variables, cassandra_install_release={"rc": 1, "stdout_lines": ["Error: No matching Packages to list"]},
        _cassandra_install_again="resume", _cassandra_install_changed_before=["its JMX password file"]))).split("\n")
    assert said[1:] == ["Its Cassandra package was not changed; its repository file was written by this run.",
                        "Changed on it by this run before: its JMX password file.",
                        "WAY OUT  set cassandra_package_version to a release it lists (dnf --showduplicates list"
                        " cassandra there), or add that release to the repository, then resume"]
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import cassandra_stop_block
    block = cassandra_stop_block([{"node": "n1", "result": "apply_config FAILED after 3s: x", "stage": "during"}], "n1",
                                 "\n".join(said), operation="apply_config", action="apply_config", cluster="zorglub",
                                 inventory=["inv.yml"], cwd="/x")
    assert block[0] == "STOPPED  apply_config  zorglub  at n1"
    steps = [x for x in block if x.lstrip().startswith(("1.", "2."))]
    assert steps[0].startswith("  1. set cassandra_package_version to a release it lists (dnf")
    assert steps[1:] == ["  2. resume:"] and block.count("WAY OUT") == 1
    # upgrade's node (stopped): its own end, what it changed said
    upgrade = load("roles", "cassandra_operation", "tasks", "action_upgrade.yml")
    install = find(upgrade, "Install the target version")["vars"]
    said = render(stop["ansible.builtin.fail"]["msg"], with_task_vars(stop, dict(
        variables, cassandra_install_release={"rc": 1, "stdout_lines": ["Error: No matching Packages to list"]},
        **install))).split("\n")
    assert said[2:] == ["Changed on it by this run before: Cassandra stopped for its upgrade.",
                        "WAY OUT  set cassandra_package_version to a release it lists (dnf --showduplicates list"
                        " cassandra there), or add that release to the repository, then run this phase again (it says"
                        " how to start this node first)"]


def test_a_failed_release_window_starts_back_or_leaves_the_mark_and_the_way_out():
    window = load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")
    assert len(window) == 1
    block, rescue = window[0]["block"], window[0]["rescue"]
    # the mark before the stop (a resumed run starts a node left stopped, its stop interrupted too), with the reasons
    # (not empty: not a time sync)
    restart = load("roles", "cassandra_operation", "tasks", "action_restart.yml")
    stopped = find(restart, "Do what is done while it is stopped")["block"]
    assert [t["name"] for t in stopped] == ["Mark the node stopped by this run", "Stop cassandra",
                                            "Run what is done while it is stopped"]
    mark = stopped[0]
    assert mark["ansible.builtin.copy"]["dest"] == "/run/cassandra-apply-config.restart"
    assert render("{{ %s }}" % mark["when"], {"_cassandra_service_while_stopped": "apply_config_window.yml"}) is True
    assert render("{{ %s }}" % mark["when"], {"_cassandra_service_while_stopped": "sync_clock_window.yml"}) is False
    assert render(mark["ansible.builtin.copy"]["content"], {"cassandra_apply_config_why": "Cassandra 4.1.11 -> 4.1.12",
                                                            "cassandra_package_version": "4.1.12"}) == "Cassandra 4.1.11 -> 4.1.12\n"
    assert render(mark["ansible.builtin.copy"]["content"], {"cassandra_apply_config_why": "",
                                                            "cassandra_package_version": "4.1.12"}) == "Cassandra 4.1.12\n"
    assert [t["name"] for t in rescue] == ["Note what failed", "Read the installed packages",
                                           "Check the files of the Cassandra package installed",
                                           "Look for files of the package's directories written since the update started",
                                           "Read the package manager's log of the update", "Look for a Cassandra JVM",
                                           "Start Cassandra again on the release it ran", "Note the step that failed",
                                           "Stop the run"]
    assert block[0]["name"] == "Note the files of the package and the time before the update" and "when" not in block[0]
    # a package read that fails is not taken for the old snapshot
    assert rescue[1]["ignore_errors"] is True and "failed_when" not in rescue[1]
    start = rescue[6]
    assert start["when"] == ["_back | bool", "not ansible_check_mode"]  # (under --check nothing was stopped)
    assert [t["name"] for t in start["block"]] == ["Start Cassandra again on the release it ran", "Forget the mark of the stop"]
    assert [t["ansible.builtin.include_tasks"] for t in start["rescue"] if "ansible.builtin.include_tasks" in t] == [
        "start_failed_state.yml"]
    # each step named as what it does; the mark of the update removed once the window went through
    assert [t["name"] for t in block] == [
        "Note the files of the package and the time before the update", "Note that the package is being updated",
        "Update the Cassandra package", "Note that the config is being written", "Write the config again",
        "Note that the config is written", "Forget the mark of the update"]
    # the stop's row says how far it went, as observed
    assert block[3]["ansible.builtin.set_fact"]["_cassandra_op_stage"] == "writing"
    assert block[5]["ansible.builtin.set_fact"] == {"_cassandra_op_stage": "written"}
    assert block[-1]["ansible.builtin.file"] == {"path": "/run/cassandra-apply-config.update", "state": "absent"}
    assert block[-1]["when"] == "not ansible_check_mode"  # (none written under --check: no change shown on /run)
    assert block[1]["ansible.builtin.set_fact"]["_cassandra_window_config"] is False
    assert block[3]["ansible.builtin.set_fact"]["_cassandra_window_config"] is True
    failure = {"msg": "no repo at https://zorglub:s3cr3t@10.0.0.9/el9", "stderr": "", "rc": 1, "attempts": 3}
    noted = dict((k, render(v, {"ansible_failed_task": {"name": "Install the target version"},
                                "ansible_failed_result": failure}))
                 for k, v in rescue[0]["ansible.builtin.set_fact"].items())
    variables = dict(window[0]["vars"], cassandra_apply_config_version=RELEASE, cassandra_package_version="4.1.12",
                     ansible_check_mode=False,
                     cassandra_window_jvm={"rc": 1}, inventory_hostname="n1", ansible_facts={"os_family": "RedHat"},
                     cassandra_window_before={"stdout_lines": ["rpmlog=120", ".M.......  c /etc/x.yaml",
                                                               "S.5....T.    /usr/share/x/by-hand.sh"],
                                              "stdout": "rpmlog=120\n.M.......  c /etc/x.yaml\n"
                                                        "S.5....T.    /usr/share/x/by-hand.sh"},
                     cassandra_window_written={"stdout_lines": []},
                     cassandra_window_verify={"rc": 1, "stdout": "S.5....T.    /usr/share/x/by-hand.sh"},
                     cassandra_window_rpm_log={"stdout": ""}, **noted)
    # (secrets and a URL's login masked: the screen shows it)
    assert variables["_cassandra_window_cause"] == "Install the target version: no repo at https://zorglub:****@10.0.0.9/el9"
    assert variables["_cassandra_window_failure"] == {"stderr": ""}

    def read(installed):
        return {"ansible_facts": {"packages": {"cassandra": [{"version": installed}]}}} if installed else {"failed": True}

    def back(installed, **more):
        return render("{{ _back | bool }}", dict(variables, cassandra_window_packages=read(installed), **more))

    # e2e: rpm's database said 4.1.11 still, rpm -V showed cqlsh files of the new release unpacked
    unpacked = {"rc": 1, "stdout": "S.5....T.    /usr/share/x/by-hand.sh\nS.5....T.    /usr/bin/cqlsh\n"
                                   "missing     /usr/share/x/new.jar"}
    assert back("4.1.11") is True  # its package unchanged (a file changed by hand before: not the update's): back on it
    assert back("4.1.11-1") is True
    assert back("4.1.11", cassandra_window_verify=unpacked) is False  # partly unpacked: left stopped
    assert back("4.1.11", cassandra_window_verify={"rc": 127, "stdout": ""}) is False  # not verified: not known
    assert back("4.1.11", cassandra_window_before={"skipped": True}) is False
    assert back("4.1.12") is False  # updated: left stopped, resumed once fixed
    assert back("") is False  # not read: not known
    assert back("4.1.11", cassandra_window_jvm={"rc": 0}) is False  # another JVM runs
    # the release installed before the stop (updated by other means since Cassandra started): the one to go back to
    assert back("4.1.12", cassandra_apply_config_version=dict(RELEASE, installed="4.1.12")) is True
    # a file new in the target release (the old package's check does not see it), the files' read failed,
    # or the config's write the step that failed (its files half written): left stopped
    assert back("4.1.11", cassandra_window_written={"stdout_lines": ["/usr/share/x/lib/x-4.1.12.jar"]}) is False
    assert back("4.1.11", cassandra_window_written={"stdout_lines": ["unknown"]}) is False
    assert back("4.1.11", _cassandra_window_config=True) is False
    stop = rescue[-1]
    word = rescue[-2]["ansible.builtin.set_fact"]["_cassandra_stop_word"]
    assert render(word, {}) == "package update failed"
    assert render(word, {"_cassandra_window_config": True}) == "config write failed"

    def said(**more):
        return render(stop["ansible.builtin.fail"]["msg"], with_task_vars(stop, dict(variables, **more))).split("\n")

    # one fact per line, then one way out (the stop block: 1. fix it, 2. resume, its command)
    first = ("n1: the update to Cassandra 4.1.12 with Cassandra stopped failed: Install the target version: no repo at"
             " https://zorglub:****@10.0.0.9/el9")
    assert said(cassandra_window_packages=read("4.1.12-1")) == [
        first, "Package installed: Cassandra 4.1.12.", "Cassandra left stopped.", "WAY OUT  fix it, then resume"]
    assert said(cassandra_window_packages=read(""))[1] == "Package installed: Cassandra not known."
    assert said(cassandra_window_packages=read("4.1.11"))[1:] == [
        "Cassandra 4.1.11 installed, its files as its package has them (rpm -V): started again on it.",
        "WAY OUT  fix it, then resume"]
    assert said(cassandra_window_packages=read("4.1.11"), _cassandra_window_start_failed="timeout",
                cassandra_start_failed_state="Cassandra left stopped.")[1:] == [
        "Cassandra 4.1.11 installed, its files as its package has them (rpm -V).", "Its start failed too: timeout.",
        "Cassandra left stopped.", "WAY OUT  fix it, then resume"]
    # its start timed out waiting for the node, the JVM up: never "left stopped"
    assert said(cassandra_window_packages=read("4.1.11"), _cassandra_window_start_failed="timeout",
                cassandra_start_failed_state="A Cassandra JVM runs (its start not confirmed).")[3] \
        == "A Cassandra JVM runs (its start not confirmed)."
    assert said(cassandra_window_packages=read("4.1.12"), _cassandra_window_config=True)[0].startswith(
        "n1: the config written again after the update to Cassandra 4.1.12 with Cassandra stopped failed:")
    # never "4.1.11 still installed" when files of it changed: what was observed, the package manager's own error
    log = ("2026-11-20T03:54:30+0100 SUBDEBUG Upgrade: cassandra-4.1.12-1.noarch\n"
           "2026-11-20T03:54:31+0100 INFO error: unpacking of archive failed on file /usr/share/x/new.jar: cpio: rename\n"
           "2026-11-20T03:54:31+0100 SUBDEBUG Error unpacking rpm package cassandra-4.1.12-1.noarch\n"
           "2026-11-20T03:54:31+0100 INFO error: unpacking of archive failed on file /usr/share/x/new.jar: cpio: rename\n")
    assert said(cassandra_window_packages=read("4.1.11"), cassandra_window_verify=unpacked,
                cassandra_window_rpm_log={"stdout": log}) == [
        first,
        "  dnf: error: unpacking of archive failed on file /usr/share/x/new.jar: cpio: rename",
        "  dnf: Error unpacking rpm package cassandra-4.1.12-1.noarch",
        "Package installed: Cassandra 4.1.11.",
        "Files of that package changed by the failed update (rpm -V): /usr/bin/cqlsh, /usr/share/x/new.jar.",
        "Cassandra left stopped.", "WAY OUT  fix it, then resume"]


def test_the_release_window_under_check_writes_no_mark_and_claims_no_stop():
    # --check stopped nothing: no mark in /run written or removed, no start, never "left stopped" or
    # "started again"
    window = load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]
    note = find(window["block"], "Note the files of the package and the time before the update")["ansible.builtin.shell"]
    scan = find(window["rescue"], "Look for files of the package's directories written since the update started")[
        "ansible.builtin.shell"]
    facts = {"ansible_facts": {"os_family": "RedHat"}}
    for check in (False, True):
        assert ("> /run/cassandra-apply-config.update" in render(note, dict(facts, ansible_check_mode=check))) is not check
        assert ("rm -f $m" in render(scan, dict(facts, ansible_check_mode=check))) is not check
    stop = window["rescue"][-1]
    variables = dict(window["vars"], cassandra_apply_config_version=RELEASE, cassandra_package_version="4.1.12",
                     cassandra_window_jvm={"rc": 1}, inventory_hostname="n1", ansible_facts={"os_family": "RedHat"},
                     cassandra_window_before={"stdout_lines": ["rpmlog=0"], "stdout": "rpmlog=0"},
                     cassandra_window_written={"stdout_lines": []}, cassandra_window_verify={"rc": 0, "stdout": ""},
                     cassandra_window_rpm_log={"stdout": ""}, _cassandra_window_cause="Install the target version: x",
                     _cassandra_window_failure={},
                     cassandra_window_packages={"ansible_facts": {"packages": {"cassandra": [{"version": "4.1.11"}]}}})
    said = render(stop["ansible.builtin.fail"]["msg"], with_task_vars(stop, dict(variables, ansible_check_mode=True)))
    assert said.split("\n") == ["n1: the update to Cassandra 4.1.12 (--check) failed: Install the target version: x",
                                "Under --check: Cassandra was not stopped, no package changed.",
                                "WAY OUT  fix it, then resume"]
    assert "started again" in render(stop["ansible.builtin.fail"]["msg"],
                                     with_task_vars(stop, dict(variables, ansible_check_mode=False)))
    # the state a failed start left, one task file for the window and the start after the update
    state = load("roles", "cassandra_operation", "tasks", "start_failed_state.yml")
    assert state[0]["ansible.builtin.command"]["argv"] == ["pgrep", "-f", "org.apache.cassandra.service.CassandraDaemon"]
    assert state[0]["failed_when"] is False and state[0]["check_mode"] is False
    fact = state[1]["ansible.builtin.set_fact"]["cassandra_start_failed_state"]
    for check, rc, line in ((False, 1, "Cassandra left stopped."), (False, 0, "A Cassandra JVM runs (its start not confirmed)."),
                            (True, 1, "Under --check: Cassandra was not stopped."),
                            # pgrep's error (rc 2, 3) is no "no process": not known, never "left stopped"
                            (False, 2, "Whether a Cassandra JVM runs is not known (pgrep failed)."),
                            (False, 3, "Whether a Cassandra JVM runs is not known (pgrep failed).")):
        assert render(fact, {"ansible_check_mode": check, "cassandra_start_failed_jvm": {"rc": rc}}) == line
    state = window["vars"]
    left = render(stop["vars"]["_state_left"], with_task_vars({"vars": dict(state, **stop["vars"])}, dict(
        variables, ansible_check_mode=False, cassandra_window_jvm={"rc": 2})))
    assert left[-1] == "Cassandra not started again; whether a Cassandra JVM runs is not known (pgrep failed)."
    assert "Cassandra left stopped." not in left


def test_a_failed_start_after_the_package_update_says_the_release_installed():
    # e2e: the 5.0.9 package installed, its start refused (limits): "Start or restart cassandra: Unable to start
    # service" alone, the node row "start or restart cassandra"
    restart = load("roles", "cassandra_operation", "tasks", "action_restart.yml")
    plain, window = find(restart, "Restart it and wait until it has joined"), \
        find(restart, "Start it on the new release and wait until it has joined")
    for task, is_window in ((plain, False), (window, True)):
        for stopped, runs in (("apply_config_window.yml", is_window), ("", not is_window),
                              ("sync_clock_window.yml", not is_window)):
            assert render("{{ %s }}" % task["when"], {"_cassandra_service_while_stopped": stopped}) is runs
    assert window["block"][0]["ansible.builtin.include_role"] == plain["ansible.builtin.include_role"]
    assert window["block"][0]["vars"] == plain["vars"]
    word, state, packages, stop = window["rescue"]
    assert word["ansible.builtin.set_fact"]["_cassandra_stop_word"] == "start failed after the package update"
    assert state["ansible.builtin.include_tasks"] == "start_failed_state.yml"
    assert packages["ignore_errors"] is True and packages["check_mode"] is False
    cause = render(word["ansible.builtin.set_fact"]["_cassandra_restart_cause"], {
        "ansible_failed_task": {"name": "Start or restart cassandra"},
        "ansible_failed_result": {"msg": "Unable to start service cassandra: Job for cassandra.service failed"}})

    def said(state="Cassandra left stopped.", installed="5.0.9-1"):
        packages = {"ansible_facts": {"packages": {"cassandra": [{"version": installed}]}}} if installed else {"failed": True}
        return render(stop["ansible.builtin.fail"]["msg"], with_task_vars(stop, {
            "inventory_hostname": "n1", "cassandra_package_version": "5.0.9", "_cassandra_restart_cause": cause,
            "cassandra_start_failed_state": state, "cassandra_restart_packages": packages})).split("\n")
    assert said() == [
        "n1: the start after the update to 5.0.9 failed: Start or restart cassandra: Unable to start service"
        " cassandra: Job for cassandra.service failed",
        "Package installed: Cassandra 5.0.9.", "Cassandra left stopped.", "WAY OUT  fix it, then resume"]
    # a wait for the node that timed out, its JVM up: as observed
    assert said("A Cassandra JVM runs (its start not confirmed).")[2] == "A Cassandra JVM runs (its start not confirmed)."
    # the package as read after the failed start, not the one asked: not read, not known
    assert said(installed="5.0.8")[1] == "Package installed: Cassandra 5.0.8."
    assert said(installed="")[1] == "Package installed: Cassandra not known."


def test_version_verify_says_the_way_out():
    verify = load("roles", "cassandra_operation", "tasks", "version_verify.yml")
    msg = find(verify, "Check it runs the target version")["ansible.builtin.assert"]["fail_msg"]
    said = render(msg, {"inventory_hostname": "n1", "cassandra_version_verify": {"stdout": "ReleaseVersion: 4.1.11"},
                        "cassandra_package_version": "4.1.12", "ansible_facts": {"os_family": "RedHat"}})
    assert said.startswith("n1 runs ReleaseVersion: 4.1.11, not 4.1.12") and "rpm -q cassandra" in said
    assert said.endswith("fix it, then run the same command again.")


def test_a_limit_a_container_caps_restarts_nothing():
    # (rootless docker, LXC: the unit's LimitNOFILE above the container's cap; the JVM got the cap)
    assert needs(unit_nofile=1048576, ceiling=LIMITS)["then"] == "none"
    assert needs(unit_nofile=1048576)["then"] == "restart"
    read = find(FIND["tasks"], "Read what the running Cassandra got from its unit")["ansible.builtin.shell"]
    assert 'systemd-detect-virt -cq 2>/dev/null && echo "ceiling=$(base64 -w0 < /proc/1/limits)"' in read


def test_import_then_no_op_with_a_unit_environment_the_scripts_rewrite():
    # (Environment=JMX_PORT=7299 and the like: cassandra-env.sh sets its own, the JVM keeps it; the unit not written
    # since the JVM started: no restart, run after run)
    unit = {"JMX_PORT": "7299", "HEAP_NEWSIZE": "800M", "LD_PRELOAD": "/opt/x/libz.so",
            "CASSANDRA_INCLUDE": "/opt/x/in.sh", "CASSANDRA_OWNR": "zorglub", "MAX_HEAP_SIZE": "8G"}
    environ = ("JMX_PORT=7199\0HEAP_NEWSIZE=2048M\0LD_PRELOAD=/usr/lib64/libjemalloc.so.2\0"
               "CASSANDRA_INCLUDE=/usr/share/cassandra/cassandra.in.sh\0CASSANDRA_OWNR=cassandra\0MAX_HEAP_SIZE=16G\0")
    assert needs(env=(unit, environ, "90"))["then"] == "none"  # (the JVM started at 100)
    # the unit written after the start (daemon-reload, no restart): the JVM's, a restart
    assert needs(env=(unit, environ, "150"))["then"] == "restart"
    assert "unitmtime=" in find(FIND["tasks"], "Read what the running Cassandra got from its unit")["ansible.builtin.shell"]


def _unit_mtime(tmp_path, shown, mtimes):
    """The unit read's unitmtime line run against a systemctl that shows these lines and a stat that gives these
    mtimes ({path: seconds})."""
    import subprocess
    read = find(FIND["tasks"], "Read what the running Cassandra got from its unit")["ansible.builtin.shell"]
    start = read.index('echo "unitmtime=')
    line = read[start:read.index(')"', start) + 2]
    for name, body in (("systemctl", "printf '%s\\n' " + " ".join("'%s'" % x for x in shown)),
                       ("stat", 'shift 2; for p in "$@"; do case "$p" in '
                                + " ".join("%s) echo %d;;" % (p, t) for p, t in mtimes.items()) + " esac; done")):
        (tmp_path / name).write_text("#!/bin/sh\n%s\n" % body)
        (tmp_path / name).chmod(0o755)
    out = subprocess.run(["bash", "-c", line], capture_output=True, text=True, check=False, env={"PATH": "%s:/usr/bin:/bin" % tmp_path})
    return out.stdout.strip()


def test_a_generated_unit_rewritten_at_each_reload_is_not_taken_for_a_change(tmp_path):
    # (the package's init script: systemd-sysv-generator rewrites /run/systemd/generator.late/cassandra.service at
    # every daemon-reload; its script and the drop-ins in /etc are what change)
    shown = ["/run/systemd/generator.late/cassandra.service", "/etc/init.d/cassandra",
             "/etc/systemd/system/cassandra.service.d/cassandra-environment.conf /run/systemd/generator/x.conf"]
    mtimes = {"/run/systemd/generator.late/cassandra.service": 9000, "/run/systemd/generator/x.conf": 9000,
              "/etc/init.d/cassandra": 1000, "/etc/systemd/system/cassandra.service.d/cassandra-environment.conf": 1100}
    assert _unit_mtime(tmp_path, shown, mtimes) == "unitmtime=1100"
    # the role's own unit: its file and drop-ins
    assert _unit_mtime(tmp_path, ["/etc/systemd/system/cassandra.service", "", ""],
                       {"/etc/systemd/system/cassandra.service": 1200}) == "unitmtime=1200"


def test_the_node_s_turn_runs_the_java_and_package_steps_only_when_planned():
    def runs(task, **variables):
        when = task["when"] if isinstance(task["when"], list) else [task["when"]]
        variables.setdefault("ansible_check_mode", False)
        return all(render("{{ %s }}" % w, variables) for w in when)

    java = find(ACTION, "Install the Java this node moves to")
    assert runs(java, cassandra_apply_config_java=True) and not runs(java, cassandra_apply_config_java=False)
    assert not runs(java)  # (no Java plan: nothing installed)
    stopped = find(ACTION, "Update the Cassandra package of a node not running")
    # a running node gets its package in its restart window, not here
    assert not runs(stopped, cassandra_apply_config_release=True, cassandra_apply_config_then="restart")
    assert runs(stopped, cassandra_apply_config_release=True, cassandra_apply_config_then="start")
    assert runs(stopped, cassandra_apply_config_release=True, cassandra_apply_config_then="write")
    assert not runs(stopped, cassandra_apply_config_release=False, cassandra_apply_config_then="start")
    assert not runs(stopped, cassandra_apply_config_release=True, cassandra_apply_config_then="start", ansible_check_mode=True)


def test_the_java_plan_of_apply_config_moves_running_nodes_and_checks_only_them():
    plan = load("roles", "cassandra_operation", "tasks", "java_plan.yml")
    note = find(plan, "Note what this node needs")
    variables = {"cassandra_java_version": "17", "_cassandra_java_home": "", "cassandra_java_plan_spec":
                 {"stderr": "    java.specification.version = 11\n"}, "_cassandra_java_plan_running_only": True}
    variables = with_task_vars(note, variables)
    todo = note["ansible.builtin.set_fact"]["cassandra_java_plan_todo"]
    assert render(todo, dict(variables, cassandra_jvm={"pid": "42", "exe": "/usr/lib/jvm/java-11/bin/java"})) is True
    assert render(todo, dict(variables, cassandra_jvm={})) is False  # not running: starts on what its unit gives
    assert render(todo, dict(variables, cassandra_jvm={}, _cassandra_java_plan_running_only=False)) is True
    check = find(plan, "The series supports this Java, and a tarball is on offer for it")
    when = "{{ %s }}" % check["when"]
    assert render(when, {"_cassandra_java_plan_check_all": False, "cassandra_java_plan_todo": False}) is False
    assert render(when, {"_cassandra_java_plan_check_all": False, "cassandra_java_plan_todo": True}) is True
    assert render(when, {"cassandra_java_plan_todo": False}) is True  # update_java: every node checked


def test_the_recap_says_why_live_extras_need_no_restart():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_recap
    node = {"name": "n1", "todo": True, "then": "none", "done": True, "kept_running": [EXTRAS_LIVE], "result": "applied"}
    assert "n1  applied, not restarted (%s)" % EXTRAS_LIVE in "\n".join(cassandra_apply_config_recap([node]))
    assert "would apply, no restart (%s)" % EXTRAS_LIVE in "\n".join(cassandra_apply_config_recap([node], check=True))


def test_the_release_row_says_the_package_installed_when_it_is_another():
    # e2e: rp-n1 left stopped after its package update (start failed): the plan said "Cassandra 5.0.8 -> 5.0.9
    # (package)", its package 5.0.9 already
    rows = [needs(version=dict(RELEASE, installed=installed))["restart_items"]
            for installed in ("4.1.11", "4.1.12", "")]
    assert [(r[0]["before"], r[0]["after"]) for r in rows] == [
        ("4.1.11", "4.1.12 (package)"), ("4.1.11, 4.1.12 installed", "4.1.12 (installed)"), ("4.1.11", "4.1.12 (package)")]


def test_a_secret_in_the_unit_environment_is_a_warning_of_the_plan():
    env = {"JVM_EXTRA_OPTS": "-Dzorglub.trustStorePassword=zorglub"}
    found = needs(env=(env, "JVM_EXTRA_OPTS=-Dzorglub.trustStorePassword=zorglub\n", "50"))
    warned = [n for n in found["notes"] if n.startswith("WARNING  a secret in cassandra_service_environment")]
    assert len(warned) == 1 and "/etc/systemd/system/cassandra.service, mode 0644" in warned[0]
    assert not [n for n in needs()["notes"] if "a secret in" in n]


def test_the_mode_not_read_over_cql_is_in_the_plan_s_warnings_too():
    # e2e: cqlsh missing: its WARNING said while the compare ran, not in the --check recap nor in the report
    said = ["WARNING  n1: the storage_compatibility_mode Cassandra runs could not be read over CQL (cqlsh on 10.0.0.1:9042);"
            " the line it logged at its start tells it instead.", "  command  /opt/x/cqlsh 10.0.0.1 9042", "  rc       2"]
    assert not [n for n in needs()["notes"] if "over CQL" in n]
    variables = {"cassandra_storage_mode_cql_warning": said, "inventory_hostname": "n1"}
    note = NOTE["ansible.builtin.set_fact"]["cassandra_apply_config_notes"]
    expr = "{{ " + note[note.index("(['WARNING  ' ~ ([_cql_warning[0]"):note.index("if _cql_warning else [])") + 24] + " }}"
    rendered = render(expr, with_task_vars({"vars": {k: NOTE["vars"][k] for k in ("_cql_warning", "_lines_nl")}}, variables))
    assert rendered == ["WARNING  the storage_compatibility_mode Cassandra runs could not be read over CQL (cqlsh on"
                        " 10.0.0.1:9042); the line it logged at its start tells it instead.\ncommand  /opt/x/cqlsh"
                        " 10.0.0.1 9042\nrc       2"]


def _written_since(tmp_path, family, mark):
    """The release window's scan of the files written since the update started, run as it is (its shell), the mark
    in tmp_path holding the package's listing (dpkg -L / rpm -ql, as the note before the update writes it)."""
    import os
    import subprocess
    rescue = load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]["rescue"]
    scan = find(rescue, "Look for files of the package's directories written since the update started")
    script = render(scan["ansible.builtin.shell"], {"ansible_facts": {"os_family": family}, "ansible_check_mode": False})
    script = script.replace("/run/cassandra-apply-config.update", str(mark))
    out = subprocess.run(["/bin/sh", "-c", script], env={"PATH": os.environ["PATH"]},
                         stdout=subprocess.PIPE, universal_newlines=True, check=True)
    return out.stdout.split()


def test_the_files_written_since_the_update_are_files_of_the_package_s_dirs(tmp_path):
    # Debian: cassandra_install writes and removes /usr/sbin/policy-rc.d (a directory of the package: the
    # ctime of /usr/sbin moves); dpkg -L lists "/." and directories (/usr, /etc: their dirname /, where /run and /tmp
    # change all the time). RedHat: rpm -ql lists directories too (another package's doc dir next to its own). None
    # of these is a file of the update: the old release is started again. A new jar in its lib dir is one.
    import time
    root = tmp_path / "root"
    lib = root / "usr" / "share" / "cassandra" / "lib"
    sbin = root / "usr" / "sbin"
    doc = root / "usr" / "share" / "doc"
    for d in (lib, sbin, doc / "cassandra-4.1.11"):
        d.mkdir(parents=True)
    (sbin / "cassandra").write_text("#!/bin/sh\n")
    (lib / "apache-cassandra-4.1.11.jar").write_text("jar")
    listed = tmp_path / "listed.txt"
    listed.write_text("\n".join(str(p) for p in (
        root / ".", root / "usr", sbin, sbin / "cassandra", root / "usr" / "share", root / "usr" / "share" / "cassandra",
        lib, lib / "apache-cassandra-4.1.11.jar", doc, doc / "cassandra-4.1.11")) + "\n")
    mark = tmp_path / "mark"
    for family in ("Debian", "RedHat"):
        time.sleep(0.05)
        mark.write_text(listed.read_text())
        time.sleep(0.05)
        # policy-rc.d written and removed; one left behind (its removal failed): the role's own, not the update's
        (sbin / "policy-rc.d").write_text("exit 101\n")
        (sbin / "policy-rc.d").unlink()
        (sbin / "policy-rc.d").write_text("exit 101\n")
        # files of no directory of the package's files: the listing's root ("/" of "/.", /usr), another package's doc
        (root / "zorglub.pid").write_text("1")
        (doc / "zorglub").mkdir(exist_ok=True)
        (doc / "zorglub" / "README").write_text("x")
        (lib / "new-dir").mkdir(exist_ok=True)
        assert _written_since(tmp_path, family, mark) == [], family
        assert not mark.exists()  # (removed once read)
        (sbin / "policy-rc.d").unlink()
    # a jar new in the target release: written by the update; a file of the old package rewritten: its package
    # check tells it (rpm -V / dpkg --verify), not its change time
    mark.write_text(listed.read_text())
    time.sleep(0.05)
    (lib / "apache-cassandra-4.1.12.jar").write_text("jar")
    (sbin / "cassandra").write_text("#!/bin/sh\n# 4.1.12\n")
    assert _written_since(tmp_path, "RedHat", mark) == [str(lib / "apache-cassandra-4.1.12.jar")]
    # no mark (not written: under --check, or its write failed), or an empty one (the listing not read): not known
    assert _written_since(tmp_path, "Debian", mark) == ["unknown"]
    mark.write_text("")
    assert _written_since(tmp_path, "Debian", mark) == ["unknown"]


def test_a_dpkg_rollback_of_the_old_package_s_files_is_no_file_written(tmp_path):
    # apt-get install of the new release failed during its unpack: dpkg hard-links each old file
    # (file.dpkg-tmp) and renames it back on rollback, both bumping its change time; dpkg --verify is clean, the old
    # package installed: no file of the update, the old release started again (it was left stopped)
    import os
    import time
    lib = tmp_path / "root" / "usr" / "share" / "cassandra" / "lib"
    lib.mkdir(parents=True)
    jar = lib / "apache-cassandra-4.1.11.jar"
    jar.write_text("jar")
    listed = tmp_path / "listed.txt"
    listed.write_text("%s\n%s\n" % (lib, jar))
    mark = tmp_path / "mark"
    mark.write_text(listed.read_text())
    time.sleep(0.05)
    os.link(str(jar), str(jar) + ".dpkg-tmp")  # the backup of the old file
    (lib / "x.dpkg-new").write_text("new jar")  # the new release's unpacked in its place
    os.rename(str(lib / "x.dpkg-new"), str(jar))
    os.rename(str(jar) + ".dpkg-tmp", str(jar))  # rolled back
    assert jar.read_text() == "jar"
    assert _written_since(tmp_path, "Debian", mark) == []
    # the note before the update writes the mark as that listing (no mark under --check)
    note = find(load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]["block"],
                "Note the files of the package and the time before the update")["ansible.builtin.shell"]
    for family, listing in (("Debian", "dpkg -L cassandra"), ("RedHat", "rpm -ql cassandra")):
        said = render(note, {"ansible_facts": {"os_family": family}, "ansible_check_mode": False})
        assert "{ %s; } > /run/cassandra-apply-config.update" % listing in said
        unchecked = render(note, {"ansible_facts": {"os_family": family}, "ansible_check_mode": True})
        assert "/run/cassandra-apply-config.update" not in unchecked


def test_the_plan_of_a_release_with_its_repository_says_it_first():
    # the repository of the release written first, the release looked for in it, before the Java and the
    # config: the plan's steps in that order
    repo = [{"item": "baseurl", "before": "a", "after": "b"}]
    jemalloc = [{"item": "package jemalloc", "before": "absent", "after": "5.2.1"}]
    found = needs(version=RELEASE, java=True, changes=[{"item": "%s/cassandra.yaml" % CONF}], repository=repo,
                  extras=jemalloc)
    assert found["steps"][:3] == ["apply the repository, look for Cassandra 4.1.12 in it", "install Java 17",
                                  "write the config"]
    assert "apply the packages, firewall" in found["steps"]
    assert not [x for x in found["steps"] if x == "apply the repository, packages, firewall"]
    found = needs(version=RELEASE, repository=repo)
    assert found["steps"][0] == "apply the repository, look for Cassandra 4.1.12 in it"
    assert "apply the packages, firewall" not in found["steps"]
    # no release: as before
    assert needs(repository=repo)["steps"] == []  # (no restart: no steps)
