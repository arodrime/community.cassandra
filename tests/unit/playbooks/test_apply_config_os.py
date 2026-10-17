from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config covers cassandra_linux's OS settings too: compared in check mode
# with the config (nothing on a node with cassandra_linux_manage false), shown
# with the settings that differ, applied first on their nodes at once, live:
# no restart for them, a note when the read-ahead of a running Cassandra that
# is not restarted changes.

import os
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def render(template, variables):
    return Templar(loader=DataLoader(), variables=trust(variables)).template(trust_as_template(template))


PLAYS = load("playbooks", "apply_config.yml")
NAMES = [p.get("name") for p in PLAYS]
FIND = PLAYS[NAMES.index("Find what each node needs")]
TASKS = [t.get("name") for t in FIND["tasks"]]
NOTE = FIND["tasks"][TASKS.index("Note what this node needs")]
OS_PLAY = PLAYS[NAMES.index("Apply the OS settings, on these nodes at once")]
RA = {"item": "read_ahead_kb vg_data/cassandra", "before": "128", "after": "4"}
SYSCTL = {"item": "sysctl vm.swappiness (/etc/sysctl.d/60-cassandra.conf)", "before": "60", "after": "1"}
NOTE_RA = "read-ahead set live: the files Cassandra has open keep the old one until it restarts"


def note(os_items=None, changes=(), running=True, clock=(), resume=False, full=False, unit="inactive"):
    variables = {"_cassandra_config_changes": list(changes), "cassandra_config_restart_pending": False,
                 "cassandra_apply_config_clock": {"stdout_lines": list(clock)}, "cassandra_rolling_resume": resume,
                 "_cassandra_preflight_running": running, "cassandra_jvm": {}, "_cassandra_config_dir_notes": [],
                 "cassandra_apply_config_unit": {"skipped": True} if running else {"status": {"ActiveState": unit}}}
    if os_items is not None:
        variables["_cassandra_linux_items"] = os_items
    for name, value in NOTE["vars"].items():
        variables[name] = render(value, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    if full:
        return dict((k, render(v, variables)) for k, v in facts.items())
    return (render(facts["cassandra_apply_config_todo"], variables) in (True, "True"),
            render(facts["cassandra_apply_config_then"], variables),
            render(facts["cassandra_apply_config_os"], variables),
            render(facts["cassandra_apply_config_notes"], variables))


def test_compared_in_check_mode_with_the_config():
    compare = FIND["tasks"][TASKS.index("Compare the OS settings")]
    assert compare["ansible.builtin.include_role"] == {
        "name": "community.cassandra.cassandra_linux", "apply": {"check_mode": True, "diff": "{{ ansible_diff_mode | bool and ansible_verbosity | int > 0 }}"}}
    # nothing reported nor shown by the role: the view of the settings that differ says them
    # (check mode applied to the role: ansible_check_mode does not tell it, its own flag does)
    assert compare["vars"] == {"cassandra_change_report_dir": "", "_cassandra_change_report_quiet": True,
                               "_cassandra_linux_quiet": True, "_cassandra_linux_compare": True,
                               "_cassandra_linux_swap_said": True}
    # a node failing there would be left out of the plan
    assert FIND["any_errors_fatal"] is True
    assert TASKS.index("Compare the config") < TASKS.index("Compare the OS settings") < TASKS.index("Note what this node needs")
    assert "when" not in compare  # cassandra_linux_manage false: the role itself leaves the node alone


def test_the_role_leaves_a_node_kept_as_it_is_with_nothing_to_apply():
    main = load("roles", "cassandra_linux", "tasks", "main.yml")
    forget, os_tasks = main[0], main[1]
    # (set on every node, also one whose OS settings are kept: nothing left from an earlier run of the role)
    assert "when" not in forget and forget["ansible.builtin.set_fact"]["_cassandra_linux_items"] == []
    assert os_tasks["ansible.builtin.import_tasks"] == "os.yml" and os_tasks["when"] == "cassandra_linux_manage | bool"
    quiet = [t for t in main if "_cassandra_linux_quiet | default(false) | bool" in str(t.get("when"))]
    assert [t["name"] for t in quiet] == ["Say the OS settings are left as they are", "Say how the data disks are tuned"]


@pytest.mark.parametrize("running", [True, False])
def test_os_settings_alone_no_restart(running):
    todo, then, os_items, notes = note([SYSCTL], running=running)
    assert (todo, then, os_items) == (False, "none" if running else "write", [SYSCTL])
    assert notes == []
    assert note()[2] == [] and note([])[2] == []


def test_the_read_ahead_note_only_where_cassandra_keeps_running():
    assert note([RA, SYSCTL])[3] == [NOTE_RA]
    # lvchange run live (cassandra_data_lvchange_running): its item alone
    assert note([{"item": "LVM read-ahead vg_data/cassandra (sectors)", "before": "256", "after": "8"}])[3] == [NOTE_RA]
    # restarted by this run (after its OS settings): it reads them all; not running: at its start
    assert note([RA], changes=["cassandra.yaml"])[1:] == ("restart", [RA], [])
    assert note([RA], running=False)[3] == []


def test_applied_first_at_once_before_any_config_write():
    # (after the progress file, whose checks refuse a run before anything is changed)
    order = [NAMES.index(n) for n in ("Find what each node needs", "Set up the progress file",
                                      "Apply the OS settings, on these nodes at once", "Apply the config where nothing is restarted, on these nodes at once",
                                      "Apply the config, one node at a time")]
    assert order == sorted(order)
    assert "serial" not in OS_PLAY and OS_PLAY["any_errors_fatal"] is True
    assert "cassandra_apply_config_os_True" in OS_PLAY["hosts"]
    block = [t for t in OS_PLAY["tasks"] if "block" in t][0]
    assert block["when"] == "not ansible_check_mode"  # --check: compared already, nothing written
    names = [t.get("name") for t in block["block"]]
    apply_os = block["block"][names.index("Apply the OS settings")]
    # the Cassandra directories the disk tuning looks under, as in the compare (cassandra_config's, public)
    defaults = block["block"][names.index("Load the cassandra_config defaults")]
    assert defaults["ansible.builtin.include_role"] == {
        "name": "community.cassandra.cassandra_config", "tasks_from": "defaults.yml", "public": True}
    assert names.index("Load the cassandra_config defaults") < names.index("Apply the OS settings")
    # its file diffs (device ids in the tuned profile and the udev rule) not shown: the view said its changes (-v: shown)
    assert apply_os["ansible.builtin.include_role"] == {
        "name": "community.cassandra.cassandra_linux", "apply": {"diff": "{{ ansible_diff_mode | bool and ansible_verbosity | int > 0 }}"}}
    for verbosity, diff, shown in ((0, True, False), (1, True, True), (1, False, False)):
        assert render(apply_os["ansible.builtin.include_role"]["apply"]["diff"],
                      {"ansible_diff_mode": diff, "ansible_verbosity": verbosity}) is shown
    assert apply_os["vars"]["_cassandra_linux_swap_said"] is True  # under the node, not again by the role
    # a failure: the summary, then a stop saying no config was written
    rescue = [t.get("name") for t in block["rescue"]]
    assert rescue == ["Record the failure", "Print the summary", "Stop here"]
    record = block["rescue"][0]["ansible.builtin.set_fact"]["cassandra_op_result"]
    stop = block["rescue"][2]["ansible.builtin.fail"]["msg"]
    failed = {"inventory_hostname": "node1", "ansible_failed_task": {"name": "Set the read-ahead"},
              "ansible_failed_result": {"msg": "timeout"}}
    assert render(record, failed) == "OS settings FAILED: timeout"
    assert render(stop, failed).startswith("Stopped at node1 (Set the read-ahead): no config was written")
    # a node stopped because another one failed (ansible-core 2.17+ rescues it too): not said failed
    assert render(record, {}) == "OS settings not finished: the run stopped at another node"
    assert render(stop, {"inventory_hostname": "node2"}) == "Stopped with the node that failed."
    follows = apply_os["vars"]["_cassandra_linux_restart_follows"]
    for todo, then, expected in ((True, "restart", True), (True, "none", False), (False, "none", False)):
        assert render(follows, {"cassandra_apply_config_todo": todo, "cassandra_apply_config_then": then}) is expected


def test_the_role_s_restart_hint_left_to_the_restart_that_follows():
    disks = load("roles", "cassandra_linux", "tasks", "disks_summary.yml")
    keep = [t for t in disks if t.get("name") == "Keep how the data disks are tuned"][0]
    info = keep["ansible.builtin.set_fact"]["_cassandra_disk_summary"]["info"]
    variables = {"_cassandra_disk_plan": {}, "_cassandra_data_lvm_here": False, "_lvm_by_udev": [],
                 "_cassandra_disk_changes_seen": [{"what": "ra"}], "_cassandra_linux_check": False,
                 "cassandra_data_running": {"stdout": "running"}}
    hint = "optional: restart Cassandra to read the files it has open with the new read-ahead"
    assert render(info, dict(variables, _cassandra_linux_restart_follows=False)) == [hint]
    assert render(info, dict(variables, _cassandra_linux_restart_follows=True)) == []


def test_a_node_with_os_settings_only_is_not_reached_until_they_are_applied():
    nothing = FIND["tasks"][TASKS.index("Nothing to do on this node")]["ansible.builtin.set_fact"]["cassandra_op_result"]
    assert render(nothing, {"cassandra_apply_config_os_live": [SYSCTL]}) == "not reached"
    assert render(nothing, {"cassandra_apply_config_os_live": []}) == "nothing to apply"


def test_the_role_s_check_mode_also_when_applied_to_it():
    flag = load("roles", "cassandra_linux", "vars", "main.yml")["_cassandra_linux_check"]
    for check, compare, expected in ((True, False, True), (False, True, True), (False, False, False)):
        assert render(flag, {"ansible_check_mode": check, "_cassandra_linux_compare": compare}) is expected
    for name in ("os.yml", "disks.yml", "disks_find.yml", "disks_apply.yml", "disks_summary.yml", "report.yml", "main.yml"):
        with open(os.path.join(TOP, "roles", "cassandra_linux", "tasks", name), encoding="utf-8") as f:
            assert "ansible_check_mode" not in f.read(), name


def test_the_disk_warnings_said_under_the_node():
    notes = NOTE["ansible.builtin.set_fact"]["cassandra_apply_config_notes"]
    variables = {"cassandra_config_newer_files": [], "_newer": [], "_as_started": [], "_pending": False, "_running": True,
                 "_then": "none", "_os": [],
                 "_cassandra_config_dir_notes": [], "_cassandra_disk_summary": {"warnings": ["tuned: not switched"]},
                 "_stopped": False, "_timesync": False, "_no_unit": [], "_keep_missing": []}
    assert render(notes, variables) == ["WARNING - data disks: tuned: not switched"]


# the role, run again and again by apply_config on running nodes: what it reports must be what it does

def test_swap_entries_removed_by_type_as_counted():
    import re
    tasks = load("roles", "cassandra_linux", "tasks", "os.yml")
    remove = [t for t in tasks if t.get("name") == "Remove swap partitions"][0]["ansible.builtin.lineinfile"]
    fstab = {"UUID=1 none swap sw 0 0": True, "/dev/mapper/rhel-swap swap swap defaults 0 0": True,
             "/swapfile none swap sw": True, "# UUID=2 none swap sw 0 0": False, "UUID=3 /mnt/swap ext4 defaults 0 2": False,
             "UUID=4 / xfs defaults 0 0": False}
    for line, removed in fstab.items():
        assert bool(re.search(remove["regexp"], line)) is removed, line
    assert remove["state"] == "absent"
    snapshot = load("roles", "cassandra_linux", "tasks", "snapshot.yml")[0]["ansible.builtin.shell"]
    assert """awk '$1 !~ /^#/ && $3 == "swap"' /etc/fstab""" in snapshot


@pytest.mark.parametrize("active, used, available, expected", [
    ("1", "100", "1000", True), ("1", "600", "1000", False), ("0", "0", "1000", False)])
def test_swap_turned_off_only_when_it_fits_in_memory(active, used, available, expected):
    decide = load("roles", "cassandra_linux", "tasks", "snapshot.yml")[-1]["ansible.builtin.set_fact"]["_cassandra_linux_swapoff"]
    before = {"swap|active": active, "swap|used": used, "mem|available": available}
    assert render(decide, {"_cassandra_linux_before": before}) is expected


@pytest.mark.parametrize("todo, check, before, expected", [
    (False, False, "not reached", "OS settings applied"),
    (False, True, "not reached", "OS settings would be applied (--check: nothing was changed)"),
    (True, False, "not reached", "not reached, its OS settings applied"),
    (True, True, "not reached", "not reached")])
def test_the_result_said_if_a_node_fails_later(todo, check, before, expected):
    task = [t for t in OS_PLAY["tasks"] if t.get("name") == "Note the result"][0]["ansible.builtin.set_fact"]
    assert render(task["cassandra_op_result"], {"cassandra_apply_config_todo": todo, "ansible_check_mode": check,
                                                "cassandra_op_result": before}) == expected


@pytest.mark.parametrize("config, os_nodes, ts_nodes, ts_any, what", [
    (["n1"], [], [], [], "config"), ([], ["n1"], [], [], "OS settings"), (["n1"], ["n2"], [], [], "config and OS settings"),
    (["n1"], [], ["n1"], ["n1"], "OS settings"), (["n1", "n2"], [], ["n1"], ["n1"], "config and OS settings"),
    (["n1"], [], [], ["n1"], "config and OS settings")])
def test_the_question_names_what_is_applied(config, os_nodes, ts_nodes, ts_any, what):
    variables = {"_config_nodes": config, "_os_nodes": os_nodes, "_ts_nodes": ts_nodes, "_ts_any": ts_any}
    assert render(FIND["vars"]["_what"], variables) == what


@pytest.mark.parametrize("live, swapoff, said", [(True, False, True), (True, True, False), (None, None, False)])
def test_swap_left_on_said_under_the_node_once(live, swapoff, said):
    notes = NOTE["ansible.builtin.set_fact"]["cassandra_apply_config_notes"]
    variables = {"cassandra_config_newer_files": [], "_newer": [], "_as_started": [], "_pending": False, "_running": True,
                 "_then": "none", "_os": [],
                 "_cassandra_config_dir_notes": [], "_cassandra_linux_before": {"swap|active": "1"},
                 "_stopped": False, "_timesync": False, "_no_unit": [], "_keep_missing": []}
    if live is not None:  # (not set on a node with cassandra_linux_manage false)
        variables.update({"_cassandra_linux_live": live, "_cassandra_linux_swapoff": swapoff})
    warned = [n for n in render(notes, variables) if n.startswith("WARNING - swap left on")]
    assert bool(warned) is said
    tasks = load("roles", "cassandra_linux", "tasks", "os.yml")
    role_says = [t for t in tasks if t.get("name") == "Say swap is left on"][0]
    assert "not _cassandra_linux_swap_said | default(false) | bool" in role_says["when"]


@pytest.mark.parametrize("config, os_nodes, check, first, second", [
    (["n1"], [], False, "Writes the config", ""),
    ([], ["n1"], False, "", "The OS settings that differ are applied on their nodes at once: live, no restart for them."),
    ([], ["n1"], True, "", "The OS settings that differ would be applied on their nodes at once: live, no restart for"
                           " them; they are in the recap at the end."),
    (["n1"], ["n2"], False, "Writes the config", "The OS settings that differ are applied first, on their nodes at once:"
                                                 " live, no restart for them.")])
def test_the_intro_says_what_is_done(config, os_nodes, check, first, second):
    screen = FIND["tasks"][TASKS.index("Show the plan and confirm the run")]
    intro = screen["vars"]["cassandra_screen"]["intro"]
    variables = {"_config_nodes": config, "_os_nodes": os_nodes, "ansible_check_mode": check}
    one, two = [" ".join(render(i, variables).split()) for i in intro]
    assert one.startswith(first) if first else one == ""
    assert two == second


# time sync (chrony, timesyncd) installed or started under a running Cassandra: a restart, its window

TIMESYNC = {"item": "chronyd", "before": "inactive", "after": "active", "timesync": True}


def test_time_sync_under_a_running_cassandra_is_a_restart():
    facts = note([TIMESYNC, SYSCTL], clock=["ahead=-0.25", "source=chronyd -Q"], full=True)
    assert facts["cassandra_apply_config_todo"] in (True, "True") and facts["cassandra_apply_config_then"] == "restart"
    assert facts["cassandra_apply_config_timesync"] in (True, "True")
    assert facts["cassandra_apply_config_timesync_only"] in (True, "True")
    assert note([TIMESYNC], changes=["cassandra.yaml"], full=True)["cassandra_apply_config_timesync_only"] in (False, "False")
    # shown with the others, applied live but the time sync
    assert facts["cassandra_apply_config_os"] == [TIMESYNC, SYSCTL]
    assert facts["cassandra_apply_config_os_live"] == [SYSCTL]
    assert facts["cassandra_apply_config_notes"] == ["clock 0.250 s behind its time source (chronyd -Q)"]
    # its clock ahead: writes with future timestamps
    notes = note([TIMESYNC], clock=["ahead=1.5", "source=chrony"])[3]
    assert notes[0] == "clock 1.500 s ahead of its time source (chrony)" and notes[1].startswith("WARNING - clock ahead")
    # a normal skew: no warning
    assert note([TIMESYNC], clock=["ahead=0.004", "source=chrony"])[3] == ["clock 0.004 s ahead of its time source (chrony)"]
    assert note([TIMESYNC])[3] == ["clock offset to its time source not readable"]
    # Cassandra not running: applied with the others, nothing restarted
    stopped = note([TIMESYNC], running=False, full=True)
    assert stopped["cassandra_apply_config_then"] == "write" and stopped["cassandra_apply_config_os_live"] == [TIMESYNC]
    assert stopped["cassandra_apply_config_todo"] in (False, "False")


def test_a_node_stopped_for_its_time_sync_is_started_by_the_resumed_run():
    facts = note([], running=False, clock=["mark=yes"], resume=True, full=True)
    assert facts["cassandra_apply_config_then"] == "start" and facts["cassandra_apply_config_todo"] in (True, "True")
    assert facts["cassandra_apply_config_notes"] == ["stopped by the interrupted run (time sync): started once written"]
    # not resumed: as any node stopped (left stopped); running: the mark alone changes nothing
    assert note([], running=False, clock=["mark=yes"])[:2] == (False, "write")
    # ... but said, with the way to start it
    assert note([], running=False, clock=["mark=yes"])[3] == [
        "WARNING - stopped by an interrupted run for its time sync: left stopped; to start it, run again with"
        " -e cassandra_rolling_resume=true"]
    assert note([], clock=["mark=yes"], resume=True)[:2] == (False, "none")


def test_the_restart_window_reuses_the_restart():
    tasks = load_tasks(os.path.join(TOP, "roles", "cassandra_service", "tasks", "action_restart.yml"))
    names = [t["name"] for t in tasks]
    assert names.index("Drain the node") < names.index("Start the time sync with Cassandra stopped") \
        < names.index("Restart it and wait until it has joined")
    window = tasks[names.index("Start the time sync with Cassandra stopped")]
    assert window["ansible.builtin.include_tasks"] == "timesync_window.yml"
    # its own include var: not a fact another restart of the same run (rolling_restart...) would read
    assert window["when"] == ["_cassandra_restart_timesync | default(false) | bool", "not ansible_check_mode"]
    action = load("roles", "cassandra_service", "tasks", "action_apply_config.yml")
    anames = [t["name"] for t in action]
    restart = action[anames.index("Drain and restart the node")]
    assert restart["vars"] == {"_cassandra_restart_timesync": "{{ cassandra_apply_config_timesync | default(false) }}"}
    # a node started (not restarted): the same window before its start
    before = action[anames.index("Start the time sync before Cassandra starts")]
    assert before["ansible.builtin.include_tasks"] == "timesync_window.yml"
    assert before["vars"] == {"_cassandra_timesync_wait_only": "{{ not cassandra_apply_config_timesync | default(false) | bool }}"}
    assert "cassandra_apply_config_resumed_stop | default(false) | bool" in str(before["when"])
    assert anames.index("Start the time sync before Cassandra starts") < anames.index("Start the node and wait until it has joined")
    forget = action[-1]
    assert forget["ansible.builtin.file"] == {"path": "/run/cassandra-apply-config.restart", "state": "absent"}
    assert "cassandra_apply_config_then | default('restart') in ['restart', 'start']" in forget["when"]
    window_tasks = load("roles", "cassandra_service", "tasks", "timesync_window.yml")
    win, report_ts = window_tasks
    steps = [t["name"] for t in win["block"]]
    assert steps == ["Mark the node stopped by this run", "Stop Cassandra", "Check no Cassandra JVM still runs",
                     "Install and start the time sync", "Let the clock sync", "Say the clock is not synced yet"]
    assert win["block"][3]["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_linux",
                                                               "tasks_from": "timesync.yml"}
    # the node an interrupted run stopped there: only its clock let sync
    for i in (0, 1, 3):
        assert win["block"][i]["when"] == "not _cassandra_timesync_wait_only | default(false) | bool"
    # a failure: Cassandra started again (not left down, also drained when its stop failed), the mark removed, the
    # run stopped; not a second JVM next to one the unit did not start
    assert [t["name"] for t in win["rescue"]] == ["Look for a Cassandra JVM", "Start Cassandra again, without its time sync",
                                                  "Forget the mark of the stop", "Stop the run"]
    again = win["rescue"][1]
    assert again["vars"] == {"cassandra_service_state": "restarted"}
    for jvm, checked, expected in ((1, None, True), (0, None, True), (0, "failed", False), (1, "ok", True)):
        variables = {"cassandra_service_timesync_jvm": {"rc": jvm}}
        if checked:
            variables["cassandra_service_timesync_no_jvm"] = {"failed": checked == "failed", "rc": 1 if checked == "failed" else 0}
        assert render(win["vars"]["_restart"], variables) is expected, (jvm, checked)
    assert again["when"] == "_restart | bool"
    # its own report file (the OS settings applied at once have cassandra_linux's), written once it went well
    assert report_ts["vars"]["cassandra_change_report_role"] == "cassandra_linux_timesync"
    # the OS play leaves it to that window, its items not taken for this run's
    apply_os = [t for t in OS_PLAY["tasks"] if "block" in t][0]["block"][1]
    assert apply_os["vars"]["_cassandra_linux_timesync_later"] == "{{ cassandra_apply_config_timesync_later }}"
    os_tasks = load("roles", "cassandra_linux", "tasks", "os.yml")
    sync = [t for t in os_tasks if t.get("name") == "Time sync"][0]
    assert sync["ansible.builtin.include_tasks"] == "timesync.yml"
    assert sync["when"] == "not _cassandra_linux_timesync_later | default(false) | bool"
    report = load("roles", "cassandra_linux", "tasks", "report.yml")
    for name in ("Add the time sync package to the change list", "Add the time sync service to the change list"):
        compare = [t for t in report if t.get("name") == name][0]
        assert compare["when"][0] == "not _cassandra_linux_timesync_later | default(false) | bool"
    # the change report without apply_config's mark
    items = [t for t in report if t.get("name") == "Report"][0]["vars"]["cassandra_change_report_items"]
    assert render(items, {"_cassandra_linux_items": [TIMESYNC]}) == [{"item": "chronyd", "before": "inactive", "after": "active"}]


def test_a_time_sync_that_cannot_start_or_only_to_enable_is_no_restart():
    # a unit whose condition fails here (timesyncd in a container): it would never start, left out and said
    package = {"item": "package chrony", "before": "absent", "after": "present", "timesync": True}
    facts = note([package, TIMESYNC, SYSCTL], clock=["condition=no"], full=True)
    assert facts["cassandra_apply_config_then"] == "none" and facts["cassandra_apply_config_os"] == [SYSCTL]
    assert facts["cassandra_apply_config_notes"] == ["chronyd cannot run here (the condition of its unit): left as it is"]
    # nor touched live by the role
    assert facts["cassandra_apply_config_timesync_later"] in (True, "True")
    assert note([TIMESYNC, SYSCTL], full=True)["cassandra_apply_config_timesync_later"] in (True, "True")
    assert note([SYSCTL], full=True)["cassandra_apply_config_timesync_later"] in (False, "False")
    # running already, only to enable: live
    enable = dict(TIMESYNC, before="active")
    facts = note([enable], full=True)
    assert facts["cassandra_apply_config_then"] == "none" and facts["cassandra_apply_config_os_live"] == [enable]


def test_a_node_started_anyway_gets_its_time_sync_before_its_start():
    variables = dict(running=False, full=True)
    # its unit failed and its config changes: started once written, its time sync before
    failed = note([TIMESYNC], changes=["cassandra.yaml"], unit="failed", **variables)
    assert failed["cassandra_apply_config_then"] == "start" and failed["cassandra_apply_config_timesync"] in (True, "True")
    # its unit failed, nothing else to apply: not started for it, its time sync applied live (Cassandra not running)
    alone = note([TIMESYNC], unit="failed", **variables)
    assert alone["cassandra_apply_config_todo"] in (False, "False") and alone["cassandra_apply_config_timesync"] in (False, "False")
    # stopped on purpose: its time sync applied live (Cassandra not running), left stopped
    assert note([TIMESYNC], changes=["cassandra.yaml"], **variables)["cassandra_apply_config_then"] == "write"
    resumed = note([TIMESYNC], clock=["mark=yes"], resume=True, **variables)
    assert resumed["cassandra_apply_config_then"] == "start" and resumed["cassandra_apply_config_timesync"] in (True, "True")
    assert resumed["cassandra_apply_config_os_live"] == []


def test_a_mark_left_by_a_run_stopped_after_the_start_is_removed():
    forget = FIND["tasks"][TASKS.index("Forget a mark of a restart done")]
    assert forget["ansible.builtin.file"] == {"path": "/run/cassandra-apply-config.restart", "state": "absent"}
    assert "not ansible_check_mode" in forget["when"] and "_cassandra_preflight_running | default(true) | bool" in forget["when"]


def test_the_clock_read(tmp_path):
    import subprocess
    script = FIND["tasks"][TASKS.index("Read the clock and the mark of a restart in progress")]["ansible.builtin.shell"]

    def fake(name, body):
        (tmp_path / name).write_text("#!/bin/sh\n%s\n" % body)
        (tmp_path / name).chmod(0o755)

    def run(service="chronyd"):
        env = {"PATH": "%s:/usr/bin:/bin" % tmp_path, "CASSANDRA_TIMESYNC": service}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False).stdout.split()

    def systemctl(load, result, stamp):
        fake("systemctl", "printf 'LoadState=%s\\nConditionResult=%s\\nConditionTimestampMonotonic=%s\\n'" % (load, result, stamp))

    # its condition failed at a start: said; not found, or never started since boot ("no", timestamp 0): not
    systemctl("loaded", "no", "8459838")
    fake("chronyc", "exit 1")
    assert run() == ["condition=no"]
    for unit in (("not-found", "no", "0"), ("loaded", "no", "0"), ("loaded", "yes", "123")):
        systemctl(*unit)
        assert run() == [], unit
    # chronyc tracking: its Leap status after its System time, as chrony prints them
    fake("chronyc", "echo 'Reference ID    : C0A80001'; echo 'System time     : 0.512000 seconds fast of NTP time';"
                    " echo 'Leap status     : Normal'")
    assert run() == ["ahead=0.512000", "source=chrony"]
    # chrony not synced yet: its 0 is no offset
    fake("chronyc", "echo 'Reference ID    : 00000000 ()'; echo 'System time     : 0.000000000 seconds slow of NTP time';"
                    " echo 'Leap status     : Not synchronised'")
    assert run() == []
    fake("chronyc", "exit 1")
    fake("chronyd", "echo 'System clock wrong by -0.300000 seconds (ignored)'")
    assert run() == ["ahead=0.3", "source=chronyd", "-Q"]
    # no time sync to start: nothing read (no timedatectl either: it would start timesyncd through D-Bus)
    assert run("") == []
    assert "timedatectl" not in script


def test_the_reason_of_the_restart_is_said():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import cassandra_apply_config_outcomes
    nodes = [{"name": "n1", "todo": True, "then": "restart", "why": "time sync"}, {"name": "n2", "todo": True, "then": "restart"}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == [
        "n1  will apply, then restart (time sync)", "n2  will apply, then restart"]
    # under --check: would
    assert cassandra_apply_config_outcomes(nodes, plan=True, check=True)[0] == "n1  would apply, then restart (time sync)"


def test_the_jvm_check_does_not_find_its_own_shell():
    # pgrep -f reads the command line of a shell running it too: run with no shell
    win = load("roles", "cassandra_service", "tasks", "timesync_window.yml")[0]
    for task in (win["block"][2], win["rescue"][0]):
        assert task["ansible.builtin.command"] == {"argv": ["pgrep", "-f", "org.apache.cassandra.service.CassandraDaemon"]}
    assert win["block"][2]["failed_when"] == "cassandra_service_timesync_no_jvm.rc != 1"  # a pgrep error: not known, stop
    # a node left down keeps its mark: the resume the message says starts it
    forget = [t for t in win["rescue"] if t["name"] == "Forget the mark of the stop"][0]
    assert forget["when"] == "_restart | bool"
    assert "cassandra_rolling_resume=true" in win["rescue"][-1]["ansible.builtin.fail"]["msg"]
    assert win["rescue"][0]["failed_when"] is False
