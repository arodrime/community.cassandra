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
          timesync=False, firewall=None, ceiling=None, env=None):
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
    assert window[0]["block"][0]["ansible.builtin.include_tasks"] == "package_update.yml"
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
    assert load("roles", "cassandra_operation", "tasks", "package_update.yml")[1]["vars"]["_cassandra_install_upgrade"] is True


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
        assert render("{{ %s }}" % compare["when"], {"cassandra_apply_config_version": version}) is compared
        assert render(compare["vars"]["_cassandra_install_upgrade"],
                      {"cassandra_apply_config_version": version}) is (kind == "release")
    # live, before the stop (a failure among the tools never leaves a node stopped): on a release node, the
    # repository and packages with its package kept at the release installed (the package itself in its window)
    extras = find(ACTION, "Apply the repository, the packages and the firewall")
    kept = find(ACTION, "Apply the repository and the packages, its Cassandra release kept")
    for section, release, applied, applied_kept in (
            ("Firewall", True, True, False), ("Packages", True, False, True), ("Repository", True, False, True),
            ("Packages", False, True, False), ("Firewall", False, True, False)):
        variables = {"ansible_check_mode": False, "item": {"section": section}, "cassandra_apply_config_release": release,
                     "cassandra_apply_config_extras": [{"section": section}]}
        assert all(render("{{ %s }}" % w, variables) for w in extras["when"]) is applied, (section, release)
        if section != "Firewall" or applied_kept:
            assert all(render("{{ %s }}" % w, variables) for w in kept["when"]) is applied_kept, (section, release)
    assert [i["section"] for i in kept["loop"]] == ["Repository", "Packages"]
    assert render(kept["vars"]["cassandra_package_version"],
                  {"cassandra_apply_config_version": dict(RELEASE, installed="4.1.11")}) == "4.1.11"
    names = [t.get("name") for t in ACTION]
    assert names.index("Apply the repository and the packages, its Cassandra release kept") \
        < names.index("Drain and restart the node")
    # its window: nothing to install when the release wanted is installed already
    update = find(load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]["block"],
                  "Update the Cassandra package")
    window_vars = load("roles", "cassandra_operation", "tasks", "apply_config_window.yml")[0]["vars"]
    for installed, runs in (("4.1.11", True), ("4.1.12", False)):
        variables = with_task_vars({"vars": window_vars}, {"cassandra_apply_config_version": dict(RELEASE, installed=installed),
                                                           "cassandra_package_version": "4.1.12"})
        assert render("{{ %s }}" % update["when"], variables) is runs


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
    assert [t["name"] for t in rescue] == ["Note what failed", "Read the installed packages", "Look for a Cassandra JVM",
                                           "Start Cassandra again on the release it ran", "Stop the run"]
    # a package read that fails is not taken for the old snapshot
    assert rescue[1]["ignore_errors"] is True and "failed_when" not in rescue[1]
    start = rescue[3]
    assert start["when"] == "_back | bool"
    assert [t["name"] for t in start["block"]] == ["Start Cassandra again on the release it ran", "Forget the mark of the stop"]
    variables = dict(window[0]["vars"], cassandra_apply_config_version=RELEASE, cassandra_package_version="4.1.12",
                     cassandra_window_jvm={"rc": 1}, inventory_hostname="n1",
                     _cassandra_window_cause=render(rescue[0]["ansible.builtin.set_fact"]["_cassandra_window_cause"], {
                         "ansible_failed_task": {"name": "Install the target version"},
                         "ansible_failed_result": {"msg": "no repo at https://zorglub:s3cr3t@10.0.0.9/el9"}}))
    # (secrets and a URL's login masked: the screen shows it)
    assert variables["_cassandra_window_cause"] == "Install the target version: no repo at https://zorglub:****@10.0.0.9/el9"

    def read(installed):
        return {"ansible_facts": {"packages": {"cassandra": [{"version": installed}]}}} if installed else {"failed": True}

    def back(installed, **more):
        return render("{{ _back | bool }}", dict(variables, cassandra_window_packages=read(installed), **more))

    assert back("4.1.11") is True  # its package unchanged: started again on it
    assert back("4.1.11-1") is True
    assert back("4.1.12") is False  # updated: left stopped, resumed once fixed
    assert back("") is False  # not read: not known
    assert back("4.1.11", cassandra_window_jvm={"rc": 0}) is False  # another JVM runs
    # the release installed before the stop (updated by other means since Cassandra started): the one to go back to
    assert back("4.1.12", cassandra_apply_config_version=dict(RELEASE, installed="4.1.12")) is True
    stop = rescue[-1]

    def said(**more):
        return render(stop["ansible.builtin.fail"]["msg"], with_task_vars(stop, dict(variables, **more))).split("\n")

    # one fact per line, the way out on its own line
    assert said(cassandra_window_packages=read("4.1.12-1")) == [
        "n1: the update to Cassandra 4.1.12 with Cassandra stopped failed: Install the target version: no repo at"
        " https://zorglub:****@10.0.0.9/el9",
        "Cassandra not started (installed: 4.1.12).",
        "Way out: fix it, then resume: the same command plus -e cassandra_rolling_resume=true."]
    assert said(cassandra_window_packages=read(""))[1] == "Cassandra not started (installed: not known)."
    assert said(cassandra_window_packages=read("4.1.11"))[1:] == [
        "Cassandra 4.1.11 still installed, started again on it.", "Way out: fix it, then run apply_config again."]
    assert said(cassandra_window_packages=read("4.1.11"), _cassandra_window_start_failed="timeout")[1:] == [
        "Cassandra 4.1.11 still installed, its start failed too: timeout.",
        "Way out: fix it, then resume: the same command plus -e cassandra_rolling_resume=true."]

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
    read = find(FIND["tasks"], "Read what the running Cassandra got from its unit")["ansible.builtin.shell"]
    assert "systemctl show cassandra -p FragmentPath -p DropInPaths --value" in read


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
