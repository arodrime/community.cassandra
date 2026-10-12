from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import os

from ansible_collections.community.cassandra.plugins.module_utils.cassandra_disks import (
    ARROW,
    parse_lsblk,
    mount_targets,
    parse_lvs,
    plan,
    udev_match,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "disks")
DATA, COMMITLOG, HINTS = "/var/lib/cassandra/data", "/var/lib/cassandra/commitlog", "/var/lib/cassandra/hints"


def lsblk(name):
    with open(os.path.join(FIXTURES, name + ".json")) as f:
        return f.read()


def mount(d, majmin, target, fstype="xfs", source="/dev/x", tune=True, commitlog=False):
    """A result of the role's findmnt loop."""
    out = {"filesystems": [{"maj:min": majmin, "source": source, "fstype": fstype, "target": target}]}
    return {"item": {"dir": d, "tune": tune, "commitlog": commitlog}, "rc": 0, "stdout": json.dumps(out)}


def not_found(d, tune=True):
    return {"item": {"dir": d, "tune": tune}, "rc": 1, "stdout": ""}


def ls(target, entries):
    """The results of the role's ls -A loop over the mount points."""
    return [{"item": target, "rc": 0, "stdout_lines": entries}]


def tuned(p):
    return [d["kname"] for d in p["devices"]]


def by(p, kname):
    return next(d for d in p["devices"] if d["kname"] == kname)


# --- parsing ---

def test_parse_lsblk_merges_a_device_listed_under_each_disk():
    nodes = parse_lsblk(lsblk("lvm_two_disks"))
    assert sorted(nodes["dm-3"]["parents"]) == ["sdb", "sdc"]
    assert nodes["sdb"]["children"] == ["dm-3"] and nodes["sdc"]["children"] == ["dm-3"]


def test_parse_lsblk_old_string_values():
    nodes = parse_lsblk(lsblk("lvm_one_disk"))  # lsblk 2.32: strings, null mountpoints
    assert nodes["dm-2"]["maj:min"] == "253:2" and nodes["dm-2"]["mountpoint"] == "/data"
    assert nodes["sdb"]["mountpoint"] == ""


def test_parse_lsblk_garbage():
    assert parse_lsblk("") == {} and parse_lsblk("lsblk: not found") == {} and parse_lsblk(None) == {}


def test_parse_lvs():
    lvs = parse_lvs(["  253|2|vg1|cassandra|auto", "  253|3|vg|[pool_tdata]|8.00", "   -1|-1|vg|off|auto", "junk"])
    assert lvs == {"253:2": {"vg": "vg1", "lv": "cassandra", "hidden": False, "read_ahead": "auto"},
                   "253:3": {"vg": "vg", "lv": "pool_tdata", "hidden": True, "read_ahead": "8"}}


# --- the stacks ---

def test_plain_disk():
    p = plan(lsblk("plain_disk"), [mount(DATA, "8:16", "/var/lib/cassandra"), mount(COMMITLOG, "8:16", "/var/lib/cassandra")])
    assert tuned(p) == ["sdb"]
    assert by(p, "sdb")["kind"] == "disk" and by(p, "sdb")["top"]
    assert p["stacks"] == [DATA + ", " + COMMITLOG + ARROW + "sdb"]
    assert p["notes"] == []


def test_partition_is_read_through_its_disk():
    p = plan(lsblk("partition"), [mount(DATA, "8:17", "/var/lib/cassandra")])
    assert tuned(p) == ["sdb"]  # its disk, even shared: the partition has no read-ahead of its own
    assert p["stacks"] == [DATA + ARROW + "sdb1" + ARROW + "sdb (sdb1 is read through its disk sdb, which also holds [SWAP])"]


def test_lvm_on_one_disk_with_a_shared_mount():
    # the data dir a few levels under the mount point, other things on the mount
    lvs = ["  253|0|rhel|root|auto", "  253|1|rhel|swap|auto", "  253|2|vg1|cassandra|auto"]
    d = "/data/cluster_a/dev/data"
    p = plan(lsblk("lvm_one_disk"), [mount(d, "253:2", "/data")], lvs=lvs,
             contents=ls("/data", ["cluster_a", "other_app", "lost+found"]))
    assert tuned(p) == ["dm-2", "sdb"]
    assert by(p, "dm-2") == {"kname": "dm-2", "label": "vg1-cassandra (dm-2)", "kind": "lvm", "top": True,
                             "where": d, "os": False, "lv": "vg1/cassandra", "lv_read_ahead": "auto", "ra": "", "rota": "",
                             "sched": ""}
    assert by(p, "sdb")["where"] == "under vg1-cassandra (dm-2)"
    assert by(p, "sdb")["kind"] == "disk" and not by(p, "sdb")["top"]
    assert p["stacks"] == [d + ARROW + "vg1-cassandra (dm-2)" + ARROW + "sdb"
                           + " (/data is shared: its other files get the same read-ahead)"]
    assert p["notes"] == []


def test_mount_holding_only_cassandra_is_not_said_shared():
    lvs = ["  253|2|vg1|cassandra|auto"]
    dirs = [mount("/data/c/data", "253:2", "/data"), mount("/data/c/commitlog", "253:2", "/data"),
            mount("/data/c/hints", "253:2", "/data", tune=False)]
    p = plan(lsblk("lvm_one_disk"), dirs, lvs=lvs, contents=ls("/data", ["c", "lost+found"]))
    assert "shared" not in p["stacks"][0]
    p = plan(lsblk("lvm_one_disk"), [mount("/data", "253:2", "/data")], lvs=lvs, contents=ls("/data", ["x"]))
    assert "shared" not in p["stacks"][0]  # the mount point itself is the data dir


def test_lvm_spanning_two_disks():
    p = plan(lsblk("lvm_two_disks"), [mount(DATA, "253:3", "/var/lib/cassandra")], lvs=["253|3|vg_cass|lv_cass|auto"])
    assert tuned(p) == ["dm-3", "sdb", "sdc"]
    assert p["stacks"] == [DATA + ARROW + "vg_cass-lv_cass (dm-3)" + ARROW + "sdb, sdc"]


def test_lvm_on_md_raid1_on_partitions():
    p = plan(lsblk("lvm_on_md"), [mount(DATA, "253:2", "/var/lib/cassandra")], lvs=["253|2|vg_data|lv_cass|auto"])
    assert tuned(p) == ["dm-2", "md0", "sdb", "sdc"]
    assert by(p, "md0")["kind"] == "md"
    assert p["stacks"] == [DATA + ARROW + "vg_data-lv_cass (dm-2)" + ARROW + "md0" + ARROW + "sdb1, sdc1" + ARROW + "sdb, sdc"]
    assert p["notes"] == []  # sdb2, sdc2: empty partitions, no use


def test_crypt_on_lvm():
    p = plan(lsblk("crypt_on_lvm"), [mount(DATA, "253:3", "/var/lib/cassandra")], lvs=["253|2|vg_data|lv_crypt|auto"])
    assert tuned(p) == ["dm-3", "dm-2", "sdb"]
    assert [by(p, k)["kind"] for k in tuned(p)] == ["dm", "lvm", "disk"]
    assert p["stacks"] == [DATA + ARROW + "luks-cass (dm-3)" + ARROW + "vg_data-lv_crypt (dm-2)" + ARROW + "sdb"]


def test_crypt_on_a_partition_of_the_os_disk():
    p = plan(lsblk("crypt_on_shared_disk"), [mount(DATA, "253:0", "/var/lib/cassandra")])
    assert tuned(p) == ["dm-0"]
    assert p["notes"] == ["sda: not tuned, it also holds / (the devices above it are; "
                          "cassandra_data_tune_shared_devices: true tunes it too)"]
    assert p["stacks"][0].endswith("sda (not tuned: also /) (luks-cass (dm-0) may get back the read-ahead of the device"
                                   " under it when it is activated again)")


def test_multipath():
    p = plan(lsblk("multipath"), [mount(DATA, "253:5", "/var/lib/cassandra")])
    assert tuned(p) == ["dm-5", "dm-4", "sdb", "sdc"]  # the kpartx partition is a device of its own
    assert p["stacks"] == [DATA + ARROW + "mpatha1 (dm-5)" + ARROW + "mpatha (dm-4)" + ARROW + "sdb, sdc"]


def test_nvme_partitions_and_a_commitlog_namespace():
    p = plan(lsblk("nvme"), [mount(DATA, "259:2", "/var/lib/cassandra"), mount(COMMITLOG, "259:3", COMMITLOG)])
    assert tuned(p) == ["nvme0n1", "nvme1n1"]   # the data partition's disk also holds /boot: tuned, said
    assert p["stacks"] == [
        DATA + ARROW + "nvme0n1p2" + ARROW + "nvme0n1 (nvme0n1p2 is read through its disk nvme0n1, which also holds /boot)"
        " (/boot: of the OS, on that disk too: read with the same read-ahead)",
        COMMITLOG + ARROW + "nvme1n1"]
    p = plan(lsblk("nvme"), [mount(DATA, "259:2", "/var/lib/cassandra"), mount(COMMITLOG, "259:3", COMMITLOG)], tune_os=False)
    assert tuned(p) == ["nvme1n1"] and p["notes"] == [
        DATA + ": not tuned, on /boot, an OS filesystem (cassandra_data_tune_os_volumes: false)"]


def test_shared_vg_with_root_tunes_the_cassandra_volume_only():
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto", "253|2|rhel|cassandra|auto"]
    p = plan(lsblk("shared_vg"), [mount(DATA, "253:2", "/var/lib/cassandra")], lvs=lvs)
    assert tuned(p) == ["dm-2"]
    assert p["notes"] == ["sda: not tuned, it also holds /, /boot, [SWAP] (the devices above it are; "
                          "cassandra_data_tune_shared_devices: true tunes it too)"]
    assert p["stacks"] == [DATA + ARROW + "rhel-cassandra (dm-2)" + ARROW + "sda2" + ARROW + "sda (not tuned: also /, /boot, [SWAP])"]
    # no note about LVM taking the disk's read-ahead back: lvchange keeps the volume's
    p = plan(lsblk("shared_vg"), [mount(DATA, "253:2", "/var/lib/cassandra")], lvs=lvs, tune_shared=True)
    assert tuned(p) == ["dm-2", "sda"] and p["notes"] == []


def test_data_on_the_root_volume():
    # tuned all the same (Cassandra suffers from a large read-ahead, the OS hardly), said; its PV disk is shared
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto"]
    p = plan(lsblk("shared_vg"), [mount(DATA, "253:0", "/")], lvs=lvs)
    assert tuned(p) == ["dm-0"] and by(p, "dm-0")["os"] is True
    assert p["stacks"][0].endswith("(/: of the OS, its read-ahead set for Cassandra; its other files read with the same)")
    assert p["notes"] == ["sda: not tuned, it also holds /, /boot, /var/lib/cassandra, [SWAP] (the devices above it are; "
                          "cassandra_data_tune_shared_devices: true tunes it too)"]
    p = plan(lsblk("shared_vg"), [mount(DATA, "253:0", "/")], lvs=lvs, tune_os=False)
    assert p["devices"] == [] and p["notes"] == [
        DATA + ": not tuned, on /, an OS filesystem (cassandra_data_tune_os_volumes: false)"]


def test_commitlog_on_the_root_volume_is_tuned():
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto", "253|2|rhel|cassandra|auto"]
    p = plan(lsblk("shared_vg"), [mount("/data/c/data", "253:2", "/data"), mount(COMMITLOG, "253:0", "/", commitlog=True)],
             lvs=lvs)
    assert tuned(p) == ["dm-2", "dm-0"]
    assert p["stacks"][1].startswith(COMMITLOG + ARROW + "rhel-root (dm-0)")


def test_volume_lvs_does_not_list_is_tuned_as_a_dm_device():
    # e.g. a PV missing from the lvm devices file: lvs lists other volumes, not this one
    p = plan(lsblk("lvm_one_disk"), [mount("/data/x", "253:2", "/data")], lvs=["253|0|rhel|root|auto"])
    assert by(p, "dm-2")["kind"] == "dm" and tuned(p) == ["dm-2", "sdb"]


def test_inactive_volumes_in_lvs():
    lvs = ["  -1|-1|vg1|old|auto", "  253|2|vg1|cassandra|auto"]
    p = plan(lsblk("lvm_one_disk"), [mount("/data/x", "253:2", "/data")], lvs=lvs)
    assert by(p, "dm-2")["lv"] == "vg1/cassandra"


def test_lvm_layers_by_name_when_lvs_misses_them():
    def kind(name):
        text = {"blockdevices": [{"name": name, "kname": "dm-9", "maj:min": "253:9", "type": "lvm", "mountpoint": DATA}]}
        return plan(json.dumps(text), [mount(DATA, "253:9", DATA)], lvs=["253|0|vg|other|auto"])
    assert tuned(kind("vg-my--real")) == ["dm-9"]   # a volume named my-real
    assert tuned(kind("vg-pool-tpool")) == [] and tuned(kind("vg-lv-real")) == [] and tuned(kind("vg-pool_tdata")) == []


def test_lsblk_without_json():
    p = plan("", [mount(DATA, "8:16", "/var/lib/cassandra")])
    assert p["devices"] == [] and p["notes"] == ["not tuned: lsblk -J printed no devices (util-linux older than 2.27?)"]


def missing(m):
    """The dir is not there yet: looked up through its parent."""
    return dict(m, stdout="#missing\n" + m["stdout"])


def root_spanning_two_disks():
    """root on sda1 and sdb, the data volume on sdb."""
    root = {"name": "vg-root", "kname": "dm-0", "maj:min": "253:0", "type": "lvm", "mountpoint": "/", "fstype": "xfs"}
    data = {"name": "vg-data", "kname": "dm-1", "maj:min": "253:1", "type": "lvm", "mountpoint": "/data", "fstype": "xfs"}
    return json.dumps({"blockdevices": [
        {"name": "sda", "kname": "sda", "maj:min": "8:0", "type": "disk", "children": [
            {"name": "sda1", "kname": "sda1", "maj:min": "8:1", "type": "part", "fstype": "LVM2_member", "children": [root]}]},
        {"name": "sdb", "kname": "sdb", "maj:min": "8:16", "type": "disk", "fstype": "LVM2_member", "children": [root, data]}]})


def test_cassandra_dirs_on_the_os_filesystem_do_not_make_its_disks_cassandra_s():
    lvs = ["253|0|vg|root|auto", "253|1|vg|data|auto"]
    data = mount("/data/c/data", "253:1", "/data")
    p = plan(root_spanning_two_disks(), [data], lvs=lvs)
    assert tuned(p) == ["dm-1"] and "sdb: not tuned, it also holds /" in p["notes"][0]
    # hints on /: not tuned (read whole), nor making the root disks Cassandra's
    assert tuned(plan(root_spanning_two_disks(), [data, mount(HINTS, "253:0", "/", tune=False)], lvs=lvs)) == ["dm-1"]
    # the commitlog on /: the root volume tuned, not the disks it shares
    p = plan(root_spanning_two_disks(), [data, mount(COMMITLOG, "253:0", "/", commitlog=True)], lvs=lvs)
    assert tuned(p) == ["dm-1", "dm-0"] and "sda" not in tuned(p) and "sdb" not in tuned(p)
    assert tuned(plan(root_spanning_two_disks(), [data, mount(COMMITLOG, "253:0", "/", commitlog=True)], lvs=lvs,
                      tune_os=False)) == ["dm-1"]


def test_a_dir_not_created_yet_on_the_os_filesystem():
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto"]
    # its own filesystem may not be mounted yet: never the OS's by mistake, whatever tune_os
    p = plan(lsblk("shared_vg"), [missing(mount("/data/d", "253:0", "/"))], lvs=lvs)
    assert p["devices"] == [] and p["notes"] == ["/data/d: not tuned, not created yet (it would be on /, an OS filesystem)"]
    # through a parent on its own mount: that one
    p = plan(lsblk("lvm_one_disk"), [missing(mount("/data/c/data", "253:2", "/data"))], lvs=["253|2|vg1|cassandra|auto"])
    assert tuned(p) == ["dm-2", "sdb"]


def test_two_volumes_of_cassandra_on_one_disk():
    # data and commitlog on two volumes of one disk: the disk is only Cassandra's
    text = json.loads(lsblk("lvm_one_disk"))
    text["blockdevices"][1]["children"].append({"name": "vg1-commitlog", "kname": "dm-3", "maj:min": "253:3", "type": "lvm",
                                                "mountpoint": "/commitlog", "fstype": "xfs"})
    p = plan(json.dumps(text), [mount("/data/c/data", "253:2", "/data"), mount("/commitlog", "253:3", "/commitlog", commitlog=True)],
             lvs=["253|2|vg1|cassandra|auto", "253|3|vg1|commitlog|auto"])
    assert tuned(p) == ["dm-2", "sdb", "dm-3"]


def test_hints_on_another_volume_of_the_same_disks_count_as_cassandra():
    # hints and saved caches are not tuned, but the disk under them is still only Cassandra's
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto", "253|2|rhel|cassandra|auto"]
    text = json.loads(lsblk("shared_vg"))
    sda2 = text["blockdevices"][0]["children"][1]
    sda2["children"] = [c for c in sda2["children"] if c["kname"] == "dm-2"]
    sda2["children"].append({"name": "rhel-hints", "kname": "dm-9", "maj:min": "253:9", "type": "lvm",
                             "mountpoint": HINTS, "fstype": "xfs"})
    text["blockdevices"][0]["children"] = [sda2]
    p = plan(json.dumps(text), [mount(DATA, "253:2", "/var/lib/cassandra"), mount(HINTS, "253:9", HINTS, tune=False)],
             lvs=lvs + ["253|9|rhel|hints|auto"])
    assert tuned(p) == ["dm-2", "sda"]


def test_an_unmounted_filesystem_is_another_use():
    text = json.loads(lsblk("lvm_two_disks"))
    text["blockdevices"][2]["children"].append({"name": "sdc_other", "kname": "dm-8", "maj:min": "253:8", "type": "lvm",
                                                "mountpoint": None, "fstype": "ext4"})
    p = plan(json.dumps(text), [mount(DATA, "253:3", "/var/lib/cassandra")], lvs=["253|3|vg_cass|lv_cass|auto"])
    assert tuned(p) == ["dm-3", "sdb"]
    assert "sdc: not tuned, it also holds sdc_other (dm-8) (ext4, not mounted)" in p["notes"][0]


def test_thin_volume_leaves_the_pool_parts_alone():
    lvs = ["253|6|vg|cass|auto", "253|7|vg|pool|auto", "253|3|vg|[pool_tdata]|auto", "253|4|vg|[pool_tmeta]|auto"]
    p = plan(lsblk("lvm_thin"), [mount(DATA, "253:6", "/var/lib/cassandra")], lvs=lvs)
    assert tuned(p) == ["dm-6", "sdb"]
    assert p["stacks"] == [DATA + ARROW + "vg-cass (dm-6)" + ARROW + "vg-pool-tpool (dm-5)" + ARROW
                           + "vg-pool_tdata (dm-3), vg-pool_tmeta (dm-4)" + ARROW + "sdb"]


def test_lvm_without_lvs_is_a_device_mapper_device():
    p = plan(lsblk("lvm_one_disk"), [mount("/data/x", "253:2", "/data")], lvs=[])
    assert by(p, "dm-2")["kind"] == "dm" and by(p, "dm-2")["lv"] == ""


def test_not_block_devices():
    p = plan(lsblk("container"), [
        mount(DATA, "0:52", "/", fstype="overlay", source="overlay"),
        mount(COMMITLOG, "0:60", COMMITLOG, fstype="tmpfs", source="tmpfs"),
        mount("/nfs/data", "0:61", "/nfs", fstype="nfs4", source="filer:/vol/cass"),
        mount("/btr/data", "0:40", "/btr", fstype="btrfs", source="/dev/sdb[/@data]"),
        mount("/tank/data", "0:41", "/tank", fstype="zfs", source="tank/data"),
        not_found("/var/lib/cassandra/other")])
    assert p["devices"] == []
    assert p["notes"] == [
        DATA + ": not tuned, overlay on overlay, not a block device",
        COMMITLOG + ": not tuned, tmpfs on tmpfs, not a block device",
        "/nfs/data: not tuned, nfs4 on filer:/vol/cass, not a block device",
        "/btr/data: not tuned, btrfs reads ahead through its own bdi, not its disks' (/sys/fs/btrfs/<uuid>/bdi/read_ahead_kb)",
        "/tank/data: not tuned, ZFS prefetches on its own (zfs_prefetch_disable), not through the disks' read-ahead",
        "/var/lib/cassandra/other: not tuned, not found (not created yet?)"]


def test_loop_device():
    p = plan(lsblk("container"), [mount(DATA, "7:0", "/snap/core22/2437", fstype="squashfs")])
    assert p["devices"] == [] and p["notes"] == [DATA + ": not tuned, on loop0 (loop)"]


def test_data_on_the_root_partition_of_a_vm():
    p = plan(lsblk("container"), [mount(DATA, "252:1", "/", fstype="ext4")])
    assert tuned(p) == ["vda"] and by(p, "vda")["os"] is True
    assert plan(lsblk("container"), [mount(DATA, "252:1", "/", fstype="ext4")], tune_os=False)["devices"] == []


def test_mount_targets():
    mounts = [mount(DATA, "253:2", "/data"), mount(COMMITLOG, "253:2", "/data"), mount(HINTS, "253:3", "/hints", tune=False),
              mount("/x", "8:1", "/"), not_found("/y")]
    assert mount_targets({"results": mounts + [{"skipped": True}]}) == ["/data"]
    assert mount_targets([missing(mount(DATA, "253:2", "/data"))]) == ["/data"]  # not created yet


# --- the devices set by hand ---

def readlink(given, resolved, rc=0):
    return {"item": given, "rc": rc, "stdout": resolved}


def test_explicit_devices_replace_the_directories():
    p = plan(lsblk("lvm_two_disks"), [mount(DATA, "253:3", "/var/lib/cassandra")],
             explicit=[readlink("/dev/vg_cass/lv_cass", "/dev/dm-3")], lvs=["253|3|vg_cass|lv_cass|auto"])
    assert tuned(p) == ["dm-3", "sdb", "sdc"]
    assert p["stacks"] == ["/dev/vg_cass/lv_cass" + ARROW + "vg_cass-lv_cass (dm-3)" + ARROW + "sdb, sdc"]


def test_explicit_mapper_name_and_partition():
    p = plan(lsblk("partition"), [], explicit=[readlink("/dev/sdb1", "/dev/sdb1")])
    assert tuned(p) == ["sdb"]
    p = plan(lsblk("lvm_one_disk"), [], explicit=[readlink("/dev/mapper/vg1-cassandra", "", rc=1)])
    assert tuned(p) == ["dm-2", "sdb"]


def test_explicit_device_lsblk_does_not_see_is_taken_as_a_disk():
    p = plan("", [not_found(DATA)], explicit=[readlink("/dev/sdz", "/dev/sdz")])
    assert p["devices"] == [{"kname": "sdz", "label": "sdz", "kind": "disk", "top": True, "where": "/dev/sdz",
                             "os": False, "lv": "", "lv_read_ahead": "", "ra": "", "rota": "", "sched": ""}]
    assert p["notes"] == []  # the directories are not looked at


def test_explicit_disk_under_a_volume_says_the_volume_is_not_tuned():
    # what the old README told LVM users to set: the disk, not the volume read with
    p = plan(lsblk("lvm_one_disk"), [mount("/data/c/data", "253:2", "/data")], explicit=[readlink("/dev/sdb", "/dev/sdb")],
             lvs=["253|2|vg1|cassandra|auto"])
    assert tuned(p) == ["sdb"]
    assert p["notes"] == ["vg1-cassandra (dm-2): not tuned, though the Cassandra files on it are read with its read-ahead,"
                          " not with the one of sdb under it: set it in cassandra_data_block_devices, or leave them empty"]


def test_explicit_dm_device_lsblk_does_not_see_is_never_matched_by_name():
    p = plan("", [], explicit=[readlink("/dev/vg/lv", "/dev/dm-3")])
    assert by(p, "dm-3")["kind"] == "dm"
    assert udev_match(by(p, "dm-3"), {}, {}) == ""


def test_a_bind_mount_of_an_os_filesystem_is_said():
    # /var/lib/cassandra bind-mounted from /srv/cass on the root volume: findmnt says /var/lib/cassandra
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto"]
    bound = [mount(DATA, "253:0", "/var/lib/cassandra", source="/dev/mapper/rhel-root[/srv/cass]")]
    p = plan(lsblk("shared_vg"), bound, lvs=lvs)
    assert tuned(p) == ["dm-0"] and p["stacks"][0].endswith(
        "(/: of the OS, its read-ahead set for Cassandra; its other files read with the same)")
    assert plan(lsblk("shared_vg"), bound, lvs=lvs, tune_os=False)["devices"] == []


def test_explicit_partition_on_the_os_disk():
    explicit = [readlink("/dev/nvme0n1p2", "/dev/nvme0n1p2"), readlink("/dev/nvme1n1", "/dev/nvme1n1")]
    p = plan(lsblk("nvme"), [mount(DATA, "259:2", "/var/lib/cassandra")], explicit=explicit)
    assert tuned(p) == ["nvme0n1", "nvme1n1"] and p["unusable"] == []
    p = plan(lsblk("nvme"), [mount(DATA, "259:2", "/var/lib/cassandra")], explicit=explicit, tune_os=False)
    assert tuned(p) == ["nvme1n1"] and p["unusable"] == []


def test_hints_bound_from_the_root_volume_do_not_make_its_disk_cassandra_s():
    lvs = ["253|0|vg|root|auto", "253|1|vg|data|auto"]
    data = mount("/data/c/data", "253:1", "/data")
    hints = mount(HINTS, "253:0", HINTS, tune=False, source="/dev/mapper/vg-root[/srv/hints]")
    p = plan(root_spanning_two_disks(), [data, hints], lvs=lvs)
    assert tuned(p) == ["dm-1"] and "sdb: not tuned, it also holds /" in p["notes"][0]


def test_explicit_root_volume_leaves_its_disk_alone():
    root = {"name": "vg-root", "kname": "dm-0", "maj:min": "253:0", "type": "lvm", "mountpoint": "/", "fstype": "xfs"}
    text = json.dumps({"blockdevices": [{"name": "sdb", "kname": "sdb", "maj:min": "8:16", "type": "disk",
                                         "fstype": "LVM2_member", "children": [root]}]})
    p = plan(text, [], explicit=[readlink("/dev/vg/root", "/dev/dm-0")], lvs=["253|0|vg|root|auto"])
    assert tuned(p) == ["dm-0"] and "sdb: not tuned, it also holds /" in p["notes"][0]


def test_explicit_os_disk_is_tuned_and_said():
    lvs = ["253|0|rhel|root|auto", "253|1|rhel|swap|auto", "253|2|rhel|cassandra|auto"]
    p = plan(lsblk("shared_vg"), [mount(DATA, "253:2", "/var/lib/cassandra")],
             explicit=[readlink("/dev/sda", "/dev/sda")], lvs=lvs)
    assert tuned(p) == ["sda"] and p["unusable"] == []  # no longer refused
    p = plan(lsblk("shared_vg"), [mount(DATA, "253:2", "/var/lib/cassandra")],
             explicit=[readlink("/dev/sda", "/dev/sda")], lvs=lvs, tune_os=False)
    assert p["devices"] == [] and p["unusable"] == [] and "cassandra_data_tune_os_volumes: false" in p["notes"][0]
    assert p["os_skipped"] == ["/dev/sda"]  # the role doesn't fail on it


def test_explicit_unusable():
    p = plan(lsblk("container"), [], explicit=[readlink("/dev/loop0", "/dev/loop0")])
    assert p["unusable"] == ["/dev/loop0"] and p["devices"] == []
    lvs = ["253|6|vg|cass|auto", "253|3|vg|[pool_tdata]|auto"]
    p = plan(lsblk("lvm_thin"), [], explicit=[readlink("/dev/mapper/vg-pool_tdata", "/dev/dm-3")], lvs=lvs)
    assert p["unusable"] == ["/dev/mapper/vg-pool_tdata"]


# --- udev ---

def test_udev_match_disks():
    disk = {"kname": "sdb", "kind": "disk"}
    san = {"ID_PATH": "pci-0000:00:10.0-scsi-0:0:0:1", "ID_SERIAL": "3600a0980383030", "ID_WWN": "0x600a098038303053",
           "ID_WWN_WITH_EXTENSION": "0x600a098038303053453f463045727a6b"}
    assert udev_match(disk, san, {}) == 'ENV{ID_WWN_WITH_EXTENSION}=="0x600a098038303053453f463045727a6b"'
    assert udev_match(disk, {"ID_PATH": "pci-0000:00:05.0"}, {}) == 'ENV{ID_PATH}=="pci-0000:00:05.0"'
    assert udev_match(disk, {}, {}) == ""  # sdX may be another disk after a reboot
    assert udev_match(disk, {}, {}, by_name=True) == 'KERNEL=="sdb"'  # a test tree
    assert udev_match(disk, {"ID_SERIAL": "a*b"}, {}) == ""  # a pattern character: not that id


def test_udev_match_dm_and_md():
    dm = {"kname": "dm-3", "kind": "dm"}
    assert udev_match(dm, {}, {"dm_uuid": "CRYPT-LUKS2-0a1b-luks-cass", "dm_name": "luks-cass"}) == \
        'KERNEL=="dm-*", ATTR{dm/uuid}=="CRYPT-LUKS2-0a1b-luks-cass"'
    assert udev_match(dm, {}, {"dm_uuid": "", "dm_name": "data"}) == 'KERNEL=="dm-*", ATTR{dm/name}=="data"'
    assert udev_match(dm, {}, {}) == ""  # never by dm-N
    assert udev_match(dm, {}, {"dm_uuid": "CRYPT-x*", "dm_name": "a[b]"}) == ""  # pattern characters
    md = {"kname": "md0", "kind": "md"}
    assert udev_match(md, {"MD_UUID": "3a1b:c2d3:e4f5:0617"}, {"md_uuid": "3a1bc2d3-..."}) == \
        'KERNEL=="md*", ENV{MD_UUID}=="3a1b:c2d3:e4f5:0617"'
    assert udev_match(md, {}, {"md_uuid": "3a1bc2d3-e4f5-0617-2839-4a5b6c7d8e9f"}) == \
        'KERNEL=="md*", ATTR{md/uuid}=="3a1bc2d3-e4f5-0617-2839-4a5b6c7d8e9f"'
    assert udev_match(md, {}, {}) == ""  # never by mdN
