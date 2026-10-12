from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

from ansible_collections.community.cassandra.plugins.filter.cassandra_disks import (
    cassandra_disk_changes,
    cassandra_disk_lv_read_ahead,
    cassandra_disk_plan,
    cassandra_disk_state,
    cassandra_disk_tuned_conflict,
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
RHEL_TUNED = ["active|virtual-guest", "opt|virtual-guest|main|include|throughput-performance",
              "opt|throughput-performance|disk|readahead|>4096", "opt|throughput-performance|vm.thunderx|type|vm"]


def tuned(*extra, **kw):
    return cassandra_disk_tuned_conflict(RHEL_TUNED + list(extra), kw.get("kb", 4))


def test_tuned_conflict():
    assert tuned().startswith("WARNING: the active tuned profile (virtual-guest: [disk] readahead=>4096) sets the"
                              " read-ahead of the disks")
    assert "to 4096 KB" in tuned()
    assert tuned(kb=8192) == ""  # only raises, to less than what is set here


def test_tuned_own_profile_wins_over_what_it_includes():
    # the remedy of the README: a profile of one's own including the default one
    mine = ["active|cassandra", "opt|cassandra|main|include|virtual-guest"] + RHEL_TUNED[1:]
    assert cassandra_disk_tuned_conflict(mine + ["opt|cassandra|disk|readahead|4"], 4) == ""
    assert cassandra_disk_tuned_conflict(mine + ["opt|cassandra|disk|devices|!dm-*, !sdb"], 4) == ""
    assert cassandra_disk_tuned_conflict(mine + ["opt|cassandra|disk|enabled|false"], 4) == ""
    assert "to 4096 KB" in cassandra_disk_tuned_conflict(mine, 4)
    # several active profiles: the last one wins
    assert cassandra_disk_tuned_conflict(RHEL_TUNED[:1] + ["active|virtual-guest cass", "opt|cass|disk|readahead|4"]
                                         + RHEL_TUNED[1:], 4) == ""


def test_tuned_values():
    def one(value, kb=4):
        return cassandra_disk_tuned_conflict(["active|p", "opt|p|disk|readahead|" + value], kb)
    assert "to 4096 KB" in one("8192 s")  # sectors
    assert one(">4") == "" and one("4") == "" and one("<128") == ""
    assert "to 64 KB when tuned starts (at boot) or sees a new device, instead of the 128 KB set here" in one("64", 128)
    assert one("junk") == "" and cassandra_disk_tuned_conflict([], 4) == ""
    assert "[my_disks]" in cassandra_disk_tuned_conflict(["active|p", "opt|p|my_disks|type|disk", "opt|p|my_disks|readahead|512"], 4)


def test_plan_shared_flag_from_extra_vars():
    # -e cassandra_data_tune_shared_devices=false is the string "false": the OS disk stays alone
    with open(os.path.join(FIXTURES, "shared_vg.json")) as f:
        text = f.read()
    mounts = [{"item": {"dir": "/var/lib/cassandra/data", "tune": True}, "rc": 0,
               "stdout": '{"filesystems": [{"maj:min": "253:2", "source": "/dev/x", "fstype": "xfs", "target": "/var/lib/cassandra"}]}'}]
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto", "253|2|rhel|cassandra|auto"]
    for flag, knames in (("false", ["dm-2"]), ("no", ["dm-2"]), (False, ["dm-2"]), ("true", ["dm-2", "sda"])):
        assert [d["kname"] for d in cassandra_disk_plan(text, {"results": mounts}, lvs=lvs, tune_shared=flag)["devices"]] == knames


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
    multipath = {"kname": "dm-5", "kind": "dm", "label": "mpatha1 (dm-5)", "lv": "", "lv_read_ahead": ""}
    assert [c["what"] for c in cassandra_disk_changes([multipath], state, 4)] == ["ra", "scheduler"]
