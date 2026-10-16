from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_disks import (
    cassandra_disk_changes,
    cassandra_disk_lv_read_ahead,
    cassandra_disk_plan,
    cassandra_disk_state,
    cassandra_disk_summary,
    cassandra_disk_tuned_conflict,
    cassandra_disk_tuned_profile,
    cassandra_disk_udev_rules,
)

READ = {"results": [
    {"item": "dm-2", "stdout_lines": ["ra=4096", "sched=none", "choices=1", "rot=0", "dm_uuid=LVM-abcDEF", "dm_name=vg1-cassandra"]},
    {"item": "sdb", "stdout_lines": ["ra=4096", "sched=mq-deadline", "choices=4", "rot=1"]},
    {"item": "nvme0n1", "stdout_lines": ["ra=128", "sched=none", "choices=3", "rot=0"]},
    {"item": "md0", "stdout_lines": ["ra=8192", "sched=none", "choices=1", "rot=0", "md_uuid=3a1bc2d3-e4f5-0617-2839-4a5b6c7d8e9f"]},
    {"item": "dm-5", "stdout_lines": ["ra=128", "sched=mq-deadline", "choices=4", "rot=0", "dm_uuid=part1-mpath-3600a09"]},
    {"item": "skipped", "skipped": True},
]}
UDEV = {"results": [
    {"item": "sdb", "stdout_lines": ["DEVNAME=/dev/sdb", "ID_PATH=pci-0000:03:00.0-scsi-0:0:1:0", "ID_SERIAL=36000c29a"]},
    {"item": "md0", "stdout_lines": ["MD_UUID=3a1bc2d3:e4f50617:28394a5b:6c7d8e9f"]},
]}
DM_FLAGS = ', ENV{DM_UDEV_DISABLE_OTHER_RULES_FLAG}!="1", ENV{DM_SUSPENDED}!="1"'
FIXTURES = os.path.join(os.path.dirname(__file__), "..", "module_utils", "fixtures", "disks")
HEAD = 'ACTION=="add|change", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", '


def test_state():
    state = cassandra_disk_state(READ)
    assert state["dm-2"] == {"ra": "4096", "sched": "none", "choices": "1", "rot": "0",
                             "dm_uuid": "LVM-abcDEF", "dm_name": "vg1-cassandra"}
    assert "skipped" not in state


def test_lv_read_ahead():
    assert cassandra_disk_lv_read_ahead(4) == "8"
    assert cassandra_disk_lv_read_ahead("128") == "256"
    assert cassandra_disk_lv_read_ahead(0) == "none"  # 4 KB with 64 KB pages reads back 0


def test_udev_rules_lvm_kept_by_lvm():
    devices = [{"kname": "dm-2", "kind": "lvm", "label": "vg1-cassandra (dm-2)"}, {"kname": "sdb", "kind": "disk", "label": "sdb"}]
    state = cassandra_disk_state(READ)
    assert cassandra_disk_udev_rules(devices, state, UDEV, 4, ["dm-2"]) == [
        HEAD + 'ENV{ID_SERIAL}=="36000c29a", ATTR{queue/read_ahead_kb}="4"',
        HEAD + 'ENV{ID_SERIAL}=="36000c29a", ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"']
    # not by lvchange (not live, lvchange failed): the udev rule keeps it
    assert cassandra_disk_udev_rules(devices, state, UDEV, 4)[0] == \
        HEAD + 'KERNEL=="dm-*", ATTR{dm/uuid}=="LVM-abcDEF"' + DM_FLAGS + ', ATTR{queue/read_ahead_kb}="4"'


def test_udev_rules_by_name_only_on_a_test_tree():
    devices = [{"kname": "sdb", "kind": "disk", "label": "sdb"}]
    assert cassandra_disk_udev_rules(devices, {}, {}, 4) == ["# sdb: no stable id to match it by, not kept across reboots"]
    assert cassandra_disk_udev_rules(devices, {}, {}, 4, by_name=True) == [
        HEAD + 'KERNEL=="sdb", ATTR{queue/read_ahead_kb}="4"',
        HEAD + 'KERNEL=="sdb", ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"']


def test_udev_rules_disk_already_at_none_keeps_its_scheduler_line():
    # once set, the scheduler file of a disk may list "none" alone: the rule still sets it at boot
    devices = [{"kname": "sdz", "kind": "disk", "label": "sdz"}]
    state = {"sdz": {"ra": "4", "sched": "none", "choices": "1", "rot": "0"}}
    assert cassandra_disk_udev_rules(devices, state, {}, 4, by_name=True)[1] == \
        HEAD + 'KERNEL=="sdz", ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"'


def test_udev_rules_multipath_paths_once():
    devices = [{"kname": "sdb", "kind": "disk", "label": "sdb"}, {"kname": "sdc", "kind": "disk", "label": "sdc"}]
    udev = {"results": [{"item": k, "stdout_lines": ["ID_WWN_WITH_EXTENSION=0x6000"]} for k in ("sdb", "sdc")]}
    assert len(cassandra_disk_udev_rules(devices, {}, udev, 4)) == 2  # read-ahead, scheduler: once


def test_udev_rules_md_multipath_and_no_id():
    devices = [{"kname": "dm-5", "kind": "dm", "label": "mpatha1 (dm-5)"}, {"kname": "md0", "kind": "md", "label": "md0"},
               {"kname": "dm-9", "kind": "dm", "label": "x (dm-9)"}]
    lines = cassandra_disk_udev_rules(devices, cassandra_disk_state(READ), UDEV, 4)
    assert lines == [
        HEAD + 'KERNEL=="dm-*", ATTR{dm/uuid}=="part1-mpath-3600a09"' + DM_FLAGS + ', ATTR{queue/read_ahead_kb}="4"',
        # request-based multipath: its scheduler is the one used
        HEAD + 'KERNEL=="dm-*", ATTR{dm/uuid}=="part1-mpath-3600a09"' + DM_FLAGS
        + ', ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"',
        # md: no scheduler of its own
        HEAD + 'KERNEL=="md*", ENV{MD_UUID}=="3a1bc2d3:e4f50617:28394a5b:6c7d8e9f", ATTR{queue/read_ahead_kb}="4"',
        "# x (dm-9): no stable id to match it by, not kept across reboots"]


# what the role reads of RHEL's default tuned profiles (virtual-guest includes throughput-performance)
RHEL_TUNED = ["state|active", "version|tuned-adm 2.22.1", "active|virtual-guest", "opt|virtual-guest|main|include|throughput-performance",
              "opt|throughput-performance|disk|readahead|>4096", "opt|throughput-performance|vm.thunderx|type|vm"]
SDB = {"kname": "sdb", "kind": "disk", "label": "sdb", "lv": "", "lv_read_ahead": ""}
LV = {"kname": "dm-2", "kind": "lvm", "label": "vg1-cassandra (dm-2)", "lv": "vg1/cassandra", "lv_read_ahead": "8"}
TUNED_STATE = {"dm-2": {"dm_uuid": "LVM-AbC.d", "dm_name": "vg1-cassandra"}, "sdb": {}}
TUNED_UDEV = {"results": [{"item": "sdb", "rc": 0, "stdout_lines": ["ID_PATH=pci-0000:03:00.0-scsi-0:0:1:0", "ID_SERIAL=36000c29a"]}]}
REGEX = r"^(DM_UUID=LVM-AbC\.d|ID_SERIAL=36000c29a)$"


def warn(lines, kb=4, devices=None, state=None, udev=None, managed=False, live=True):
    return cassandra_disk_tuned_conflict(lines, kb, devices or [LV, SDB], state or TUNED_STATE, udev or TUNED_UDEV, managed, live)


def test_tuned_conflict():
    assert warn(RHEL_TUNED).startswith(
        "WARNING - the active tuned profile (virtual-guest: [disk] readahead=>4096) sets vg1-cassandra (dm-2), and the"
        " other devices its [disk] section takes, to a read-ahead of 4096 KB when tuned starts (at boot)")
    assert warn(RHEL_TUNED).endswith("cassandra_linux_tuned_manage: true keeps them off the Cassandra devices.")
    assert warn(RHEL_TUNED, kb=8192) == ""  # only raises, to less than what is set here


def test_tuned_values():
    def one(value, kb=4):
        return warn(["active|p", "opt|p|disk|readahead|" + value], kb)
    assert "a read-ahead of 4096 KB" in one("8192 s")  # sectors
    assert one(">4") == "" and one("4") == "" and one("<128") == ""
    assert "to a read-ahead of 64 KB when tuned starts (at boot) or sees a new device, instead of the 128 KB set here" in one("64", 128)
    assert one("junk") == "" and warn([]) == ""
    assert "[my_disks]" in warn(["active|p", "opt|p|my_disks|type|disk", "opt|p|my_disks|readahead|512"])
    assert "[my_disks]" not in warn(["active|p", "opt|p|my_disks|type|disk_x", "opt|p|my_disks|readahead|512"])


def test_tuned_scheduler_on_an_ssd_only():
    lines = ["active|p", "opt|p|disk|elevator|mq-deadline"]
    ssd, hdd = {"sdb": {"rot": "0", "choices": "3"}}, {"sdb": {"rot": "1", "choices": "3"}}
    assert "to the IO scheduler mq-deadline when tuned starts (at boot) or sees a new device, instead of none." in \
        warn(lines, devices=[SDB], state=ssd)
    assert warn(lines, devices=[SDB], state=hdd) == ""  # a spinning disk keeps its scheduler anyway
    assert warn(["active|p", "opt|p|disk|elevator|none"], devices=[SDB], state=ssd) == ""  # what the role sets


def test_tuned_merges_as_tuned_does():
    base = ["active|mine", "opt|mine|main|include|throughput-performance", "opt|throughput-performance|disk|readahead|>4096"]
    # a profile of one's own: its [disk] options win over the included one's
    assert warn(base + ["opt|mine|disk|readahead|4"]) == ""
    # several active profiles: the last one wins
    assert warn(["active|throughput-performance cass", "opt|cass|disk|readahead|4"] + RHEL_TUNED[3:]) == ""
    # enabled=false in the included one, the unit again in the child: enabled (tuned's defaults)
    assert "WARNING" in warn(["active|mine", "opt|mine|main|include|base", "opt|base|disk|enabled|false",
                              "opt|base|disk|readahead|>4096", "opt|mine|disk|readahead|>8192"])
    assert warn(base + ["opt|mine|disk|enabled|false"]) == ""
    assert warn(base + ["opt|mine|disk|enabled|yes"]) == ""  # tuned: only true, True, 1 enable
    assert warn(base + ["opt|mine|disk|replace|1", "opt|mine|disk|type|disk"]) == ""  # the read-ahead dropped with it
    assert warn(base + ["opt|mine|disk|drop|readahead"]) == ""


def test_tuned_devices_as_tuned_matches_them():
    base = RHEL_TUNED[1:]
    assert "WARNING" in warn(base + ["opt|throughput-performance|disk|devices|!sda"], devices=[SDB], state={"sdb": {}})
    assert warn(base + ["opt|throughput-performance|disk|devices|sda, nvme*"], devices=[SDB], state={"sdb": {}}) == ""
    assert "WARNING" in warn(base + ["opt|throughput-performance|disk|devices|sd*"], devices=[SDB], state={"sdb": {}})
    assert warn(base + ["opt|throughput-performance|disk|devices_udev_regex|ID_SERIAL=OTHER"], devices=[SDB], state={"sdb": {}}) == ""
    assert "WARNING" in warn(base + ["opt|throughput-performance|disk|devices_udev_regex|ID_SERIAL=36000"], devices=[SDB],
                             state={"sdb": {}})
    # an instance of the admin's, first by priority, setting nothing: tuned gives it the device
    assert warn(base + ["opt|throughput-performance|mine|type|disk", "opt|throughput-performance|mine|priority|-5",
                        "opt|throughput-performance|mine|devices|sdb"], devices=[SDB], state={"sdb": {}}) == ""


def test_tuned_warning_says_why_the_role_does_not_keep_them_off():
    assert warn(RHEL_TUNED, managed=True) == ""  # the role adds its profile
    assert warn(["state|enabled"] + RHEL_TUNED[1:], managed=True).endswith("tuned is not running: the role can't keep them off.")
    assert "older than 2.10" in warn(RHEL_TUNED + ["version|tuned-adm 2.9.0"], managed=True)
    assert "said no version" in warn(RHEL_TUNED[:1] + ["version|tuned-adm: unrecognized"] + RHEL_TUNED[2:], managed=True)
    assert "applies no setting live" in warn(RHEL_TUNED, managed=True, live=False)
    warning = "version|/usr/lib/python3.12/site-packages/tuned/x.py:3: SyntaxWarning"  # not a version
    assert "said no version" in warn(RHEL_TUNED[:1] + [warning] + RHEL_TUNED[2:], managed=True)
    assert cassandra_disk_tuned_profile(RHEL_TUNED + ["version|tuned-adm 2.9.0"], [SDB], {}, TUNED_UDEV, 4) == {}
    assert cassandra_disk_tuned_profile(RHEL_TUNED + ["version|tuned-adm 2.22.1"], [SDB], {}, TUNED_UDEV, 4)["name"]
    assert "no stable id" in warn(RHEL_TUNED, devices=[SDB], state={"sdb": {}}, udev={"results": []}, managed=True)
    failed = {"results": TUNED_UDEV["results"] + [{"item": "sdc", "rc": 1, "stdout_lines": []}]}
    assert "udevadm could not read" in warn(RHEL_TUNED, udev=failed, managed=True)  # no profile from half the ids


def test_plan_os_flag_from_extra_vars():
    # -e cassandra_data_tune_os_volumes=false is the string "false": the OS disk under the data stays alone
    with open(os.path.join(FIXTURES, "shared_vg.json")) as f:
        text = f.read()
    mounts = [{"item": {"dir": "/var/lib/cassandra/data", "tune": True}, "rc": 0,
               "stdout": '{"filesystems": [{"maj:min": "253:2", "source": "/dev/x", "fstype": "xfs", "target": "/var/lib/cassandra"}]}'}]
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto", "253|2|rhel|cassandra|auto"]
    for flag, knames in (("false", ["dm-2"]), ("no", ["dm-2"]), (False, ["dm-2"]), ("true", ["dm-2", "sda"]), (True, ["dm-2", "sda"])):
        assert [d["kname"] for d in cassandra_disk_plan(text, {"results": mounts}, lvs=lvs, tune_os=flag)["devices"]] == knames


def test_changes():
    devices = [{"kname": "dm-2", "kind": "lvm", "label": "vg1-cassandra (dm-2)", "lv": "vg1/cassandra", "lv_read_ahead": "auto"},
               {"kname": "sdb", "kind": "disk", "label": "sdb", "lv": "", "lv_read_ahead": ""},
               {"kname": "nvme0n1", "kind": "disk", "label": "nvme0n1", "lv": "", "lv_read_ahead": ""},
               {"kname": "md0", "kind": "md", "label": "md0", "lv": "", "lv_read_ahead": ""}]
    state = cassandra_disk_state(READ)
    changes = cassandra_disk_changes(devices, state, "4")
    assert [(c["kname"], c["what"], c["before"], c["after"]) for c in changes] == [
        ("dm-2", "lvm", "auto", "8"), ("dm-2", "ra", "4096", "4"), ("sdb", "ra", "4096", "4"),
        ("nvme0n1", "ra", "128", "4"), ("md0", "ra", "8192", "4")]
    # sdb rotational keeps mq-deadline; nvme0n1 is at none already; md0 has no scheduler to choose
    assert not [c for c in changes if c["what"] == "scheduler"]
    assert changes[0]["item"] == "LVM read-ahead vg1/cassandra (sectors)"
    # done: nothing; LVM not by lvchange (not live): no LVM change; 64 KB pages: 0 is "none"
    done = dict((k, dict(v, ra="4")) for k, v in state.items())
    assert cassandra_disk_changes([dict(devices[0], lv_read_ahead="8")] + devices[1:], done, 4) == []
    assert [c["what"] for c in cassandra_disk_changes(devices[:1], done, 4, lvm=False)] == []
    assert cassandra_disk_changes(devices[:1], done, 0)[0]["after"] == "none"
    # an OS volume: set through sysfs, never reloaded by lvchange
    root = dict(devices[0], os=True)
    assert [c["what"] for c in cassandra_disk_changes([root], state, 4)] == ["ra"]
    assert cassandra_disk_changes([root], state, 4)[0]["report"] is True
    multipath = {"kname": "dm-5", "kind": "dm", "label": "mpatha1 (dm-5)", "lv": "", "lv_read_ahead": ""}
    assert [c["what"] for c in cassandra_disk_changes([multipath], state, 4)] == ["ra", "scheduler"]


# --- the tuned profile of the role ---

def test_tuned_profile_runs_after_the_active_one_and_takes_the_cassandra_devices():
    p = cassandra_disk_tuned_profile(RHEL_TUNED, [LV, SDB], TUNED_STATE, TUNED_UDEV, 4)
    assert p["name"] == "cassandra-disks" and p["profiles"] == ["virtual-guest", "cassandra-disks"] and p["unmatched"] == []
    assert p["lines"][2:] == [
        "[main]",
        "summary=Managed by Ansible (community.cassandra.cassandra_linux): the Cassandra devices taken off the [disk]"
        " sections of the profiles run before it",
        "",
        "# a lower priority number: given its devices first, it sets nothing on them",
        "[cassandra_disks]",
        "type=disk",
        "priority=-100",
        "dynamic=0",
        "devices_udev_regex=" + REGEX]


def test_tuned_profile_matches_what_tuned_matches():
    # tuned: re.search(regex, "KEY=value\n" for each udev property, re.MULTILINE)
    import re
    assert re.search(REGEX, "DEVNAME=/dev/dm-2\nDM_NAME=vg1-cassandra\nDM_UUID=LVM-AbC.d\n", re.MULTILINE)
    assert re.search(REGEX, "DEVNAME=/dev/sdb\nID_SERIAL=36000c29a\n", re.MULTILINE)
    assert not re.search(REGEX, "DEVNAME=/dev/sda\nID_SERIAL=36000c29a0\n", re.MULTILINE)  # whole values only
    assert not re.search(REGEX, "DM_UUID=LVM-AbCxd\n", re.MULTILINE)


ON_IT = ["state|active", "version|tuned-adm 2.22.1", "active|virtual-guest cassandra-disks",
         "opt|cassandra-disks|main|summary|Managed by Ansible (community.cassandra.cassandra_linux): x",
         "opt|cassandra-disks|cassandra_disks|type|disk", "opt|cassandra-disks|cassandra_disks|priority|-100",
         "opt|cassandra-disks|cassandra_disks|devices_udev_regex|" + REGEX] + RHEL_TUNED[3:]


def test_tuned_already_running_the_profile():
    assert cassandra_disk_tuned_profile(ON_IT, [LV, SDB], TUNED_STATE, TUNED_UDEV, 4) == {}
    for managed in (True, False):
        assert warn(ON_IT, managed=managed) == ""  # it keeps these devices off, managed or not
    # a new Cassandra device: not kept off yet, the profile is written again for all
    md = {"kname": "md0", "kind": "md", "label": "md0"}
    udev = {"results": TUNED_UDEV["results"] + [{"item": "md0", "rc": 0, "stdout_lines": ["MD_UUID=1a:2b"]}]}
    p = cassandra_disk_tuned_profile(ON_IT, [LV, SDB, md], TUNED_STATE, udev, 4)
    assert p["profiles"] == ["virtual-guest", "cassandra-disks"] and p["active"] == ["virtual-guest", "cassandra-disks"]
    assert p["lines"][-1] == r"devices_udev_regex=^(DM_UUID=LVM-AbC\.d|ID_SERIAL=36000c29a|MD_UUID=1a:2b)$"


@pytest.mark.parametrize("times, reload", [
    (["now|300", "loaded|100", "file|200"], True), (["now|300", "loaded|200", "file|200"], False),
    (["now|300", "loaded|300", "file|200"], False), (["now|300", "loaded|100"], False), ([], False),
    # a profile dated ahead of the clock (written before it was stepped back): not taken for a change
    (["now|150", "loaded|100", "file|200"], False)])
def test_tuned_running_the_profile_it_loaded_before_the_role_changed_it(times, reload):
    # the role's profile changed since tuned loaded the profiles (an earlier run under a running Cassandra): what
    # tuned runs is not the file, a reload to do
    p = cassandra_disk_tuned_profile(ON_IT + times, [LV, SDB], TUNED_STATE, TUNED_UDEV, 4)
    if reload:
        assert p["reload"] is True and p["active"] == p["profiles"] == ["virtual-guest", "cassandra-disks"]
    else:
        assert p == {}
    # a profile to add: its reload said either way
    assert cassandra_disk_tuned_profile(RHEL_TUNED + times, [LV, SDB], TUNED_STATE, TUNED_UDEV, 4)["reload"] is reload


def test_tuned_a_section_of_the_admin_before_the_role_s_found_also_when_the_profile_changed():
    # (a reload would not help: said, no profile)
    lines = ON_IT + ["opt|throughput-performance|disk|priority|-200", "now|300", "loaded|100", "file|200"]
    assert cassandra_disk_tuned_profile(lines, [LV, SDB], TUNED_STATE, TUNED_UDEV, 4) == {}


def test_tuned_a_section_of_the_admin_before_the_role_s():
    lines = ON_IT + ["opt|throughput-performance|disk|priority|-200"]
    assert cassandra_disk_tuned_profile(lines, [LV, SDB], TUNED_STATE, TUNED_UDEV, 4) == {}
    assert "its [disk] section takes them before the role's profile" in warn(lines, managed=True)
    tie = ON_IT + ["opt|throughput-performance|disk|priority|-100"]  # the same: the one merged first
    assert "as low or lower" in warn(tie, managed=True)


def test_tuned_profile_not_needed():
    assert cassandra_disk_tuned_profile(["state|enabled"] + RHEL_TUNED[1:], [LV], TUNED_STATE, TUNED_UDEV, 4) == {}
    assert cassandra_disk_tuned_profile(RHEL_TUNED[:1] + RHEL_TUNED[2:], [LV], TUNED_STATE, TUNED_UDEV, 4) == {}  # no version
    assert cassandra_disk_tuned_profile(RHEL_TUNED, [LV], TUNED_STATE, TUNED_UDEV, 8192) == {}  # no conflict
    assert cassandra_disk_tuned_profile(["state|active", "active|balanced"], [LV], TUNED_STATE, TUNED_UDEV, 4) == {}
    assert cassandra_disk_tuned_profile(RHEL_TUNED, [], TUNED_STATE, TUNED_UDEV, 4) == {}
    assert cassandra_disk_tuned_profile([], [LV], TUNED_STATE, TUNED_UDEV, 4) == {}


def test_tuned_profile_several_active_ones():
    lines = ["state|active", "version|tuned-adm 2.22.1", "active|virtual-guest my-site", "opt|my-site|vm|transparent_hugepages|never"] + RHEL_TUNED[3:]
    assert cassandra_disk_tuned_profile(lines, [SDB], TUNED_STATE, TUNED_UDEV, 4)["profiles"] == \
        ["virtual-guest", "my-site", "cassandra-disks"]


def test_tuned_profile_devices_without_a_usable_id():
    md = {"kname": "md0", "kind": "md", "label": "md0"}
    udev = {"results": [{"item": "sdb", "rc": 0, "stdout_lines": ["ID_SERIAL=a#b"]}]}  # "#": a comment for tuned
    p = cassandra_disk_tuned_profile(RHEL_TUNED, [LV, SDB, md], TUNED_STATE, udev, 4)
    assert p["unmatched"] == ["sdb", "md0"] and p["lines"][-1] == r"devices_udev_regex=^(DM_UUID=LVM-AbC\.d)$"
    assert cassandra_disk_tuned_profile(RHEL_TUNED, [SDB], TUNED_STATE, udev, 4) == {}


# --- the summary of the run ---

ARROW = u" \u2192 "
STACK = "/data/c/dev/{data,commitlog}" + ARROW + "datavg-lv_data" + ARROW + "sdb"


def node(name, changed=False, kb=4, brief=None, warnings=(), info=(), notes=(), stacks=None):
    """What a node keeps for the summary (_cassandra_disk_summary)."""
    brief = [STACK] if brief is None else brief
    return {"name": name, "kb": kb, "changed": changed, "brief": brief, "warnings": list(warnings), "info": list(info),
            "notes": list(notes), "stacks": stacks if stacks is not None else [b.replace("lv_data", "lv_data (dm-4)") for b in brief]}


def test_summary_nothing_changed_two_identical_nodes():
    assert cassandra_disk_summary([node("node1"), node("node2")]) == [
        "Data disks: read-ahead 4 KB, already set (nothing changed)",
        "  node1, node2:",
        "    " + STACK]


def test_summary_headline():
    def head(nodes, check=False):
        return cassandra_disk_summary(nodes, check=check)[0]
    assert head([node("node1", True), node("node2", True)]) == "Data disks: read-ahead 4 KB, set now on 2 nodes"
    assert head([node("node1", True), node("node2")]) == "Data disks: read-ahead 4 KB, set now on 1 node (already set on 1)"
    assert head([node("node1", True), node("node2")], check=True) == \
        "Data disks: read-ahead 4 KB, would be set on 1 node (already set on 1)"
    assert head([node("node1")], check=True) == "Data disks: read-ahead 4 KB, already set (nothing changed)"
    # several read-aheads (a host variable, 64 KB pages): said per group
    lines = cassandra_disk_summary([node("node1"), node("node2", kb=0)])
    assert lines == ["Data disks: already set (nothing changed)", "  node1 (read-ahead 4 KB):", "    " + STACK,
                     "  node2 (read-ahead 0 KB):", "    " + STACK]


def test_summary_groups_the_nodes_with_the_same_lines():
    other = "/data/c/dev/{data,commitlog}" + ARROW + "datavg-lv_data" + ARROW + "sdc"
    nodes = [node("node%d" % i) for i in range(1, 91)]
    nodes[41] = node("node42", brief=[other])
    lines = cassandra_disk_summary(nodes)
    assert lines == ["Data disks: read-ahead 4 KB, already set (nothing changed)",
                     "  89 nodes: node1..node41, node43..node90:", "    " + STACK,
                     "  node42:", "    " + other]
    # a node with nothing to tune (its filesystems not on a block device): said, -v says why
    assert cassandra_disk_summary([node("node1"), node("node2", brief=[], notes=["x"])])[3:] == ["  node2: nothing to tune"]


def test_summary_problems_and_what_to_do_once_with_their_nodes():
    lvm = "lvchange failed on datavg/lv_data: its read-ahead is set now, kept by the udev rule"
    restart = "restart Cassandra to read the files it has open with the new read-ahead"
    lines = cassandra_disk_summary([node("node1", True, warnings=[lvm], info=[restart]),
                                    node("node2", True, warnings=[lvm], info=[restart]), node("node3", True, info=[restart])])
    assert lines == ["Data disks: read-ahead 4 KB, set now on 3 nodes",
                     "  node1..node3:", "    " + STACK,
                     "  " + restart + " (node1..node3)",
                     "WARNING - " + lvm + " (node1, node2)"]


def test_summary_explanations_and_device_names_with_v_only():
    shared = "/data is shared: its other files get the same read-ahead"
    full = "/data/c/dev/data, /data/c/dev/commitlog" + ARROW + "datavg-lv_data (dm-4)" + ARROW + "sdb (" + shared + ")"
    nodes = [node("node1", notes=["the commitlog alone on /"], stacks=[full]), node("node2", stacks=[full])]
    lines = cassandra_disk_summary(nodes)
    assert "dm-4" not in "".join(lines) and "shared" not in "".join(lines) and "commitlog alone" not in "".join(lines)
    assert cassandra_disk_summary(nodes, verbose=True) == [
        "Data disks: read-ahead 4 KB, already set (nothing changed)",
        "  node1:", "    " + full, "    note: the commitlog alone on /",
        "  node2:", "    " + full]


def test_summary_of_no_node():
    assert cassandra_disk_summary([]) == cassandra_disk_summary([{}]) == []


def test_summary_of_nodes_with_nothing_to_tune():
    nfs = "/data: not tuned, nfs4 on filer:/vol/cass, not a block device"
    assert cassandra_disk_summary([node("node1", brief=[], warnings=[nfs])]) == [
        "Data disks: nothing to tune", "  node1: nothing to tune", "WARNING - " + nfs + " (node1)"]
    # not counted as already set
    assert cassandra_disk_summary([node("node1", True), node("node2", brief=[])])[0] == \
        "Data disks: read-ahead 4 KB, set now on 1 node"
    assert cassandra_disk_summary([node("node1"), node("node2", brief=[])])[0] == \
        "Data disks: read-ahead 4 KB, already set (nothing changed)"
