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


def note(os_items=None, changes=(), running=True):
    variables = {"_cassandra_config_changes": list(changes), "cassandra_config_restart_pending": False,
                 "_cassandra_preflight_running": running, "cassandra_jvm": {}, "_cassandra_config_dir_notes": [],
                 "cassandra_apply_config_unit": {"skipped": True} if running else {"status": {"ActiveState": "inactive"}}}
    if os_items is not None:
        variables["_cassandra_linux_items"] = os_items
    for name, value in NOTE["vars"].items():
        variables[name] = render(value, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    return (render(facts["cassandra_apply_config_todo"], variables) in (True, "True"),
            render(facts["cassandra_apply_config_then"], variables),
            render(facts["cassandra_apply_config_os"], variables),
            render(facts["cassandra_apply_config_notes"], variables))


def test_compared_in_check_mode_with_the_config():
    compare = FIND["tasks"][TASKS.index("Compare the OS settings")]
    assert compare["ansible.builtin.include_role"] == {
        "name": "community.cassandra.cassandra_linux", "apply": {"check_mode": True}}
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
    assert apply_os["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_linux"}
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
    disks = load("roles", "cassandra_linux", "tasks", "disks.yml")
    keep = [t for t in disks if t.get("name") == "Keep how the data disks are tuned"][0]
    info = keep["ansible.builtin.set_fact"]["_cassandra_disk_summary"]["info"]
    variables = {"_cassandra_disk_plan": {}, "_cassandra_data_lvm_here": False, "_lvm_by_udev": [],
                 "_cassandra_disk_changes_seen": [{"what": "ra"}], "_cassandra_linux_check": False,
                 "cassandra_data_running": {"stdout": "running"}}
    hint = "restart Cassandra to read the files it has open with the new read-ahead"
    assert render(info, dict(variables, _cassandra_linux_restart_follows=False)) == [hint]
    assert render(info, dict(variables, _cassandra_linux_restart_follows=True)) == []


def test_a_node_with_os_settings_only_is_not_reached_until_they_are_applied():
    nothing = FIND["tasks"][TASKS.index("Nothing to do on this node")]["ansible.builtin.set_fact"]["cassandra_op_result"]
    assert render(nothing, {"cassandra_apply_config_os": [SYSCTL]}) == "not reached"
    assert render(nothing, {"cassandra_apply_config_os": []}) == "nothing to apply"


def test_the_role_s_check_mode_also_when_applied_to_it():
    flag = load("roles", "cassandra_linux", "vars", "main.yml")["_cassandra_linux_check"]
    for check, compare, expected in ((True, False, True), (False, True, True), (False, False, False)):
        assert render(flag, {"ansible_check_mode": check, "_cassandra_linux_compare": compare}) is expected
    for name in ("os.yml", "disks.yml", "report.yml", "main.yml"):
        with open(os.path.join(TOP, "roles", "cassandra_linux", "tasks", name), encoding="utf-8") as f:
            assert "ansible_check_mode" not in f.read(), name


def test_the_disk_warnings_said_under_the_node():
    notes = NOTE["ansible.builtin.set_fact"]["cassandra_apply_config_notes"]
    variables = {"cassandra_config_newer_files": [], "_pending": False, "_running": True, "_then": "none", "_os": [],
                 "_cassandra_config_dir_notes": [], "_cassandra_disk_summary": {"warnings": ["tuned: not switched"]}}
    assert render(notes, variables) == ["WARNING data disks: tuned: not switched"]


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


@pytest.mark.parametrize("config, os_nodes, what", [
    (["n1"], [], "config"), ([], ["n1"], "OS settings"), (["n1"], ["n2"], "config and OS settings")])
def test_the_question_names_what_is_applied(config, os_nodes, what):
    assert render(FIND["vars"]["_what"], {"_config_nodes": config, "_os_nodes": os_nodes}) == what


@pytest.mark.parametrize("live, swapoff, said", [(True, False, True), (True, True, False), (None, None, False)])
def test_swap_left_on_said_under_the_node_once(live, swapoff, said):
    notes = NOTE["ansible.builtin.set_fact"]["cassandra_apply_config_notes"]
    variables = {"cassandra_config_newer_files": [], "_pending": False, "_running": True, "_then": "none", "_os": [],
                 "_cassandra_config_dir_notes": [], "_cassandra_linux_before": {"swap|active": "1"}}
    if live is not None:  # (not set on a node with cassandra_linux_manage false)
        variables.update({"_cassandra_linux_live": live, "_cassandra_linux_swapoff": swapoff})
    warned = [n for n in render(notes, variables) if n.startswith("WARNING swap left on")]
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
