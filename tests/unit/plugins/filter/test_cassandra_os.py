from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import hashlib
import json
import os
import re

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_os import (
    _limits,
    _records,
    _sysctl_line,
    _sysctl_order,
    cassandra_firewall_changes,
    cassandra_linux_changes,
    cassandra_os_import,
    thp_unit,
)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout

HERE = os.path.dirname(__file__)
FIXTURES = os.path.join(HERE, "fixtures", "os")
ROLES = os.path.join(HERE, "..", "..", "..", "..", "roles")


def defaults(role):
    with open(os.path.join(ROLES, role, "defaults", "main.yml")) as f:
        return yaml.safe_load(f)


LINUX, SERVICE = defaults("cassandra_linux"), defaults("cassandra_service")


def wanted(**changes):
    """What import_cluster hands the filter: the roles' defaults."""
    w = {"sysctl": LINUX["cassandra_linux_sysctl"], "sysctl_file": LINUX["cassandra_linux_sysctl_file"],
         "limits": LINUX["cassandra_linux_limits"], "readahead_kb": LINUX["cassandra_data_readahead_kb"],
         "timesync": LINUX["cassandra_linux_timesync"],
         "service_limits": dict((k, SERVICE["cassandra_service_limit_" + k]) for k in ("nofile", "nproc", "memlock", "as")),
         "ports": ["7000/tcp", "9042/tcp", "7199/tcp"], "own": False}
    w.update(changes)
    return w


def file_records(kind, path, fixture=None, text=None):
    """The records import_cluster's script prints for a file: its lines,
    without comments and blank lines."""
    if text is None:
        with open(os.path.join(FIXTURES, fixture)) as f:
            text = f.read()
    return "".join("%s|%s|%s\n" % (kind, path, line) for line in text.splitlines()
                   if not re.match(r"^\s*([#;]|$)", line))


SITE_SYSCTL = file_records("sysctl", "/etc/sysctl.d/99-cassandra.conf", "99-cassandra.conf")
VENDOR_SYSCTL = file_records("sysctl", "/usr/lib/sysctl.d/50-default.conf", "50-default.conf")
SYSCTL_CONF = file_records("sysctl", "/etc/sysctl.conf", "sysctl.conf")
SITE_LIMITS = file_records("limits", "/etc/security/limits.d/95-cassandra-limits.conf", "95-cassandra-limits.conf")
NPROC = file_records("limits", "/etc/security/limits.d/20-nproc.conf", "20-nproc.conf")
UDEV = file_records("udev", "/etc/udev/rules.d/99-readahead.rules", "99-readahead.rules")


def disk_records(devices, mounts, lvs=()):
    """What import_cluster's script prints for the data devices: the lsblk
    tree, the LVM volumes, each directory's filesystem."""
    out = "lsblk|%s\n" % json.dumps({"blockdevices": devices})
    out += "".join("lvs|  %s\n" % line for line in lvs)
    for d, majmin, target, fstype in mounts:
        fs = json.dumps({"filesystems": [{"maj:min": majmin, "source": "/dev/x", "fstype": fstype, "target": target}]})
        out += "mnt|data|%s|%s\n" % (d, fs if majmin else "")
    return out


def blk(name, kname, majmin, typ, ra, rota, sched, mount=None, children=None):
    d = {"name": name, "kname": kname, "maj:min": majmin, "type": typ, "mountpoint": mount, "fstype": None,
         "ra": ra, "rota": rota, "sched": sched}
    if children:
        d["children"] = children
    return d


SDB1_SSD = disk_records([blk("sdb", "sdb", "8:16", "disk", 8, False, "none",
                             children=[blk("sdb1", "sdb1", "8:17", "part", 8, False, "none", "/data")])],
                        [("/data/c", "8:17", "/data", "xfs")])
SDB_HDD = disk_records([blk("sdb", "sdb", "8:16", "disk", 128, True, "mq-deadline", "/data")], [("/data/c", "8:16", "/data", "xfs")])


def found(text, **changes):
    return cassandra_os_import(text, wanted(**changes))


def part(r, *prefixes):
    """r's variables of these parts (the records of a test leave the others out)."""
    return dict((k, v) for k, v in r["vars"].items() if k.startswith(prefixes))


def sysctl_vars(r):
    return dict((k, v) for k, v in r["vars"].items() if k.startswith("cassandra_linux_sysctl"))


# --- the parsers ---

def test_sysctl_line():
    assert _sysctl_line("vm.swappiness=10") == ("vm.swappiness", "10")
    assert _sysctl_line("  net.ipv4.tcp_rmem =  4096\t87380   16777216 ") == ("net.ipv4.tcp_rmem", "4096 87380 16777216")
    assert _sysctl_line("-net.ipv4.tcp_keepalive_time = 300") == ("net.ipv4.tcp_keepalive_time", "300")
    assert _sysctl_line("net/core/somaxconn = 4096") == ("net.core.somaxconn", "4096")
    # systemd: first separator a slash -> slashes and dots swapped (an interface name with a dot)
    assert _sysctl_line("net/ipv4/conf/eth0.100/forwarding = 1") == ("net.ipv4.conf.eth0/100.forwarding", "1")
    assert _sysctl_line("net.ipv4.conf.*.rp_filter = 2") is None  # a glob: not followed
    assert _sysctl_line("# vm.swappiness = 1") is None
    assert _sysctl_line("no value") is None


def test_sysctl_order():
    """By file name whatever the dir, a name in /etc hides the same one in /usr/lib, sysctl.conf last."""
    paths = ["/etc/sysctl.conf", "/usr/lib/sysctl.d/99-a.conf", "/etc/sysctl.d/10-b.conf", "/usr/lib/sysctl.d/10-b.conf",
             "/run/sysctl.d/50-c.conf", "/etc/sysctl.d/zz.conf"]
    assert _sysctl_order(paths) == ["/etc/sysctl.d/10-b.conf", "/run/sysctl.d/50-c.conf", "/usr/lib/sysctl.d/99-a.conf",
                                    "/etc/sysctl.d/zz.conf", "/etc/sysctl.conf"]


def test_records_keep_pipes_in_the_last_field():
    assert _records("udev|/etc/x.rules|ACTION==\"add|change\"\nnoise\n") == [("udev", "/etc/x.rules|ACTION==\"add|change\"")]


# --- sysctl ---

def test_sysctl_site_file_carried_into_its_file():
    r = found(VENDOR_SYSCTL + SITE_SYSCTL + SYSCTL_CONF + "sysctl_live|vm.swappiness|60\nsysctl_live|vm.max_map_count|1048575\n")
    # exactly the keys of the file the role is to write in (the role then adds nor moves none)
    assert r["vars"]["cassandra_linux_sysctl"] == {"vm.max_map_count": 1048575, "vm.swappiness": 10,
                                                   "net.core.somaxconn": 4096, "net.ipv4.tcp_keepalive_time": 300}
    # /etc/sysctl.conf, read last: not moved
    assert any(c.startswith("NOT carried: net.core.rmem_max, set in other files than /etc/sysctl.d/99-cassandra.conf")
               for c in r["carried"])
    assert r["vars"]["cassandra_linux_sysctl_file"] == "/etc/sysctl.d/99-cassandra.conf"
    lines = r["lines"]
    assert "sysctl /etc/sysctl.d/99-cassandra.conf: vm.swappiness = 10 (cassandra_linux: 1)" in lines
    assert "sysctl /etc/sysctl.d/99-cassandra.conf: vm.max_map_count = 1048575 (same as cassandra_linux)" in lines
    assert ("sysctl /usr/lib/sysctl.d/50-default.conf: vm.swappiness = 30 (cassandra_linux: 1), overridden by"
            " /etc/sysctl.d/99-cassandra.conf") in lines
    assert "sysctl live: vm.swappiness = 60 (files say 10)" in lines  # not applied live
    assert not any("max_map_count" in line and "live" in line for line in lines)
    assert not any("sysrq" in line for line in lines)
    assert any(c.startswith("cassandra_linux_sysctl_file: /etc/sysctl.d/99-cassandra.conf") for c in r["carried"])


def test_sysctl_vendor_values_not_carried():
    """/usr/lib belongs to packages: shown, not carried."""
    r = found(VENDOR_SYSCTL + "sysctl_live|vm.swappiness|30\n")
    # (none of the role's keys written: none of the admin's files sets them)
    assert part(r, "cassandra_linux_sysctl") == {"cassandra_linux_sysctl": {}}
    assert "sysctl /usr/lib/sysctl.d/50-default.conf: vm.swappiness = 30 (cassandra_linux: 1)" in r["lines"]


def test_sysctl_untuned_live_value():
    r = found("sysctl_live|vm.max_map_count|65530\nsysctl_live|vm.swappiness|1\n")
    assert r["lines"][0] == "sysctl live: vm.max_map_count = 65530 (no file sets it; cassandra_linux: 1048575)"
    assert not any("vm.swappiness" in line for line in r["lines"])
    assert part(r, "cassandra_linux_sysctl") == {"cassandra_linux_sysctl": {}}


def test_sysctl_same_values_still_name_the_file():
    """Same values as the role, in another file: the role writes into that file (not a second one it overrides)."""
    r = found(file_records("sysctl", "/etc/sysctl.d/99-db.conf", text="vm.max_map_count = 1048575\nvm.swappiness = 1\n"))
    assert part(r, "cassandra_linux_sysctl") == {
        "cassandra_linux_sysctl_file": "/etc/sysctl.d/99-db.conf",
        # its keys only (the role's others not added)
        "cassandra_linux_sysctl": {"vm.max_map_count": 1048575, "vm.swappiness": 1}}


def test_sysctl_own_file_keeps_its_values():
    """A node the role set up: its own file's values, even when a later file overrides them (the role run
    again changes nothing), and the file stays the role's."""
    own = file_records("sysctl", "/etc/sysctl.d/60-cassandra.conf", text="vm.swappiness = 1\nvm.max_map_count = 262144\n")
    r = found(own + file_records("sysctl", "/etc/sysctl.d/99-z.conf", text="vm.swappiness = 20\n"), own=True)
    assert r["vars"]["cassandra_linux_sysctl"]["vm.max_map_count"] == 262144
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 1
    assert "cassandra_linux_sysctl_file" not in r["vars"]
    assert ("sysctl /etc/sysctl.d/60-cassandra.conf: vm.swappiness = 1 (same as cassandra_linux), overridden by"
            " /etc/sysctl.d/99-z.conf") in r["lines"]


def test_sysctl_own_string_false():
    """own comes templated: "False" is false."""
    r = found(file_records("sysctl", "/etc/sysctl.d/99-db.conf", text="vm.swappiness = 5\n"), own="False")
    assert r["vars"]["cassandra_linux_sysctl_file"] == "/etc/sysctl.d/99-db.conf"


def test_sysctl_most_keys_file_wins_the_name():
    a = file_records("sysctl", "/etc/sysctl.d/10-a.conf", text="vm.swappiness = 5\n")
    b = file_records("sysctl", "/etc/sysctl.d/70-b.conf", text="vm.max_map_count = 2000000\nvm.zone_reclaim_mode = 0\n")
    assert found(a + b)["vars"]["cassandra_linux_sysctl_file"] == "/etc/sysctl.d/70-b.conf"


# --- limits ---

def test_limits_not_the_roles_file_none_written():
    # (a file the role writes would be a change: the package's limits.d/cassandra.conf rewritten, a later one...)
    r = found(NPROC + SITE_LIMITS + "groups|cassandra cassandra_admins\n")
    assert part(r, "cassandra_linux_limits") == {"cassandra_linux_limits": {}}
    assert any(c.startswith("cassandra_linux_limits: {}, the cassandra user's limits come from the files") for c in r["carried"])
    assert part(found(""), "cassandra_linux_limits") == {"cassandra_linux_limits": {}}


def test_limits_user_entries_carried():
    # a node the role set up, without its file: the limits in effect
    r = found(NPROC + SITE_LIMITS + "groups|cassandra cassandra_admins\n", own=True)
    limits = r["vars"]["cassandra_linux_limits"]
    assert limits["nofile"] == 500000
    assert limits["nproc"] == 65536  # the user's entry wins over "*"
    assert limits["memlock"] == "unlimited"  # from its group
    assert "as" not in limits  # soft and hard differ
    assert any("NOT carried: the cassandra user's as" in c for c in r["carried"])
    assert ("limits /etc/security/limits.d/95-cassandra-limits.conf: cassandra nofile = 500000"
            " (cassandra_linux: 1048576)") in r["lines"]
    assert ("limits /etc/security/limits.d/95-cassandra-limits.conf: @cassandra_admins memlock = unlimited"
            " (same as cassandra_linux)") in r["lines"]


def test_limits_group_not_member_ignored():
    r = found(SITE_LIMITS + "groups|cassandra\n", own=True)
    assert "memlock" not in [line.split()[3] for line in r["lines"] if line.startswith("limits")]
    assert "memlock" not in r["vars"]["cassandra_linux_limits"]


def test_limits_wildcard_shown_not_carried():
    """A distro's "*" entry applies to cassandra but is not Cassandra tuning."""
    r = found(NPROC, own=True)
    assert part(r, "cassandra_linux_limits") == {"cassandra_linux_limits": {}}
    assert "limits /etc/security/limits.d/20-nproc.conf: * soft nproc = 4096 (cassandra_linux: 32768)" in r["lines"]


def test_limits_later_file_same_rank_wins_and_limits_conf_first():
    recs = _records(file_records("limits", "/etc/security/limits.d/10-a.conf", text="cassandra - nofile 1000\n")
                    + file_records("limits", "/etc/security/limits.d/90-b.conf", text="cassandra - nofile 2000\n")
                    + file_records("limits", "/etc/security/limits.conf", text="cassandra - nofile 3000\n"))
    dummy, out, dummy2 = _limits(recs, wanted(), True)
    assert out["cassandra_linux_limits"]["nofile"] == 2000


def test_limits_user_beats_later_group_and_wildcard():
    recs = _records(file_records("limits", "/etc/security/limits.d/10-a.conf", text="cassandra - nofile 1000\n")
                    + file_records("limits", "/etc/security/limits.d/90-b.conf", text="@cassandra - nofile 2000\n* - nofile 3000\n")
                    + "groups|cassandra\n")
    dummy, out, dummy2 = _limits(recs, wanted(), True)
    assert out["cassandra_linux_limits"]["nofile"] == 1000


def test_limits_same_as_role_nothing_carried():
    role_file = "".join("cassandra - %s %s\n" % kv for kv in LINUX["cassandra_linux_limits"].items())
    r = found(file_records("limits", "/etc/security/limits.d/cassandra.conf", text=role_file), own=True)
    assert "cassandra_linux_limits" not in r["vars"]


# --- the unit ---

def test_unit_limits_last_wins_and_carried():
    text = ("unit|/etc/systemd/system/cassandra.service|LimitNOFILE=100000\n"
            "unit|/etc/systemd/system/cassandra.service|LimitMEMLOCK=infinity\n"
            "unit|/etc/systemd/system/cassandra.service.d/limits.conf|LimitNOFILE=200000\n"
            "proc_limit|Max open files|200000|200000\nproc_limit|Max locked memory|unlimited|unlimited\n")
    r = found(text)
    assert part(r, "cassandra_service") == {"cassandra_service_limit_nofile": 200000}
    assert "unit /etc/systemd/system/cassandra.service.d/limits.conf: LimitNOFILE=200000 (cassandra_service: 1048576)" in r["lines"]
    assert "unit /etc/systemd/system/cassandra.service: LimitMEMLOCK=infinity (same as cassandra_service)" in r["lines"]
    assert not any(line.startswith("running Cassandra: nofile") for line in r["lines"])  # as its unit says
    assert not any("running Cassandra: memlock" in line for line in r["lines"])


# --- disks ---

def test_udev_readahead_carried():
    r = found(UDEV.splitlines()[0] + "\n" + SDB1_SSD)
    assert part(r, "cassandra_data") == {"cassandra_data_readahead_kb": 8}
    assert any(line.startswith("udev /etc/udev/rules.d/99-readahead.rules: read_ahead_kb 8 (cassandra_linux: 4),"
                               " scheduler none") for line in r["lines"])
    assert "disk sdb (/data/c, SSD): read_ahead_kb 8 (cassandra_linux: 4), scheduler none (same as cassandra_linux)" in r["lines"]


def test_udev_several_readaheads_not_carried():
    r = found(UDEV)  # 8, and --setra 16 sectors = 8 KB... same
    assert part(r, "cassandra_data") == {"cassandra_data_readahead_kb": 8}
    r = found(UDEV + "udev|/etc/udev/rules.d/70-os.rules|ACTION==\"add\", KERNEL==\"sda\", ATTR{queue/read_ahead_kb}=\"128\"\n")
    assert part(r, "cassandra_data") == {}
    assert any("several read-aheads (8, 128)" in c for c in r["carried"])


def test_udev_rule_not_the_live_value_not_carried():
    r = found(UDEV.splitlines()[0] + "\n" + SDB_HDD)
    assert part(r, "cassandra_data") == {}
    # its read-ahead left as it is on this node (host_vars)
    assert r["keep"] == {"cassandra_data_disk_tuning": False}
    assert any("NOT carried: read_ahead_kb 8" in c and "128" in c for c in r["carried"])
    assert "disk sdb (/data/c, rotational): read_ahead_kb 128 (cassandra_linux: 4), scheduler mq-deadline" in r["lines"]


def test_udev_match_is_not_an_assignment():
    r = found('udev|/etc/udev/rules.d/1.rules|ATTR{queue/read_ahead_kb}=="8", ATTR{queue/read_ahead_kb}="64"\n')
    assert part(r, "cassandra_data") == {"cassandra_data_readahead_kb": 64}


def test_udev_vendor_rule_not_carried():
    r = found('udev|/usr/lib/udev/rules.d/60-block-scheduler.rules|ATTR{queue/scheduler}="bfq"\n')
    assert part(r, "cassandra_data") == {}
    assert any("scheduler bfq" in line for line in r["lines"])


def test_old_rule_on_the_disk_under_a_volume_carried():
    # the earlier role tuned the disk of an LVM volume set by hand: its value is carried
    lv = blk("vg1-cassandra", "dm-2", "253:2", "lvm", 4096, True, "", "/data")
    rule = 'udev|/etc/udev/rules.d/61-cassandra-data-disk.rules|KERNEL=="sdb", ATTR{queue/read_ahead_kb}="8"\n'
    r = found(rule + disk_records([blk("sdb", "sdb", "8:16", "disk", 8, True, "mq-deadline", children=[lv])],
                                  [("/data/c/data", "253:2", "/data", "xfs")], lvs=["253|2|vg1|cassandra|auto"]), own=True)
    assert r["vars"].get("cassandra_data_readahead_kb") == 8


def test_dir_not_created_yet():
    root = json.dumps({"filesystems": [{"maj:min": "253:0", "source": "/dev/x", "fstype": "xfs", "target": "/"}]})
    r = found(disk_records([], []) + "mnt|data|/data/x|#missing " + root + "\n" + "mnt|data|/data/y|#missing \n")
    assert "disks: /data/x: not created yet (it would be on /, an OS filesystem): tuned by a run after it is" in r["lines"]
    assert "disks: /data/y: not tuned, not found (not created yet?)" in r["lines"]


def test_own_node_on_lvm_keeps_its_devices_as_found():
    # set up by the role before it tuned LVM: running the roles after the import must change nothing
    lv = blk("vg1-cassandra", "dm-2", "253:2", "lvm", 4096, True, "", "/data")
    disks = disk_records([blk("sdb", "sdb", "8:16", "disk", 4096, True, "mq-deadline", children=[lv])],
                         [("/data/c/data", "253:2", "/data", "xfs")], lvs=["253|2|vg1|cassandra|auto"])
    r = found(disks, own=True)
    # nothing keeps a read-ahead there, LVM's is auto: the role sets none, as on the node
    assert part(r, "cassandra_data") == {"cassandra_data_readahead_kb": "kernel", "cassandra_data_lvm_readahead": "auto"}
    assert r["keep"] == {}
    assert part(found(disks), "cassandra_data") == part(r, "cassandra_data")  # the same on a node set up another way
    # an udev rule of the admin's with another value: the devices left as they are on this node (host_vars), nodes
    # added later tuned
    rule = 'udev|/etc/udev/rules.d/90-ra.rules|KERNEL=="sdb", ATTR{queue/read_ahead_kb}="8"\n'
    r = found(disks + rule, own=True)
    assert r["keep"] == {"cassandra_data_disk_tuning": False} and "cassandra_data_disk_tuning" not in r["vars"]
    assert any(n.startswith("cassandra_data_disk_tuning: false on this node (host_vars), its data devices left as they are"
                            " (cassandra_linux would change") for n in r["carried"])
    # tuned by this version already: nothing to keep
    lv = blk("vg1-cassandra", "dm-2", "253:2", "lvm", 4, True, "", "/data")
    done = disk_records([blk("sdb", "sdb", "8:16", "disk", 4, True, "mq-deadline", children=[lv])],
                        [("/data/c/data", "253:2", "/data", "xfs")], lvs=["253|2|vg1|cassandra|8"])
    done += 'udev|/etc/udev/rules.d/64-cassandra-data-disk.rules|ENV{ID_SERIAL}=="x", ATTR{queue/read_ahead_kb}="4"\n'
    assert found(done, own=True)["keep"] == {}
    # ... but for tuned's profiles
    tuned = ("tuned|virtual-guest|active\n"
             "tuned_conf|/usr/lib/tuned/virtual-guest/tuned.conf|[main]\n"
             "tuned_conf|/usr/lib/tuned/virtual-guest/tuned.conf|include=throughput-performance\n"
             "tuned_conf|/usr/lib/tuned/throughput-performance/tuned.conf|[disk]\n"
             "tuned_conf|/usr/lib/tuned/throughput-performance/tuned.conf|readahead=>4096\n")
    r = found(done + tuned, own=True)
    assert r["keep"] == {"cassandra_data_disk_tuning": False} and any("tuned profiles" in n for n in r["carried"])
    # tuned running the role's profile already, or not running: nothing the role would do
    assert found(done + tuned.replace("virtual-guest|active", "virtual-guest cassandra-disks|active"), own=True)["keep"] == {}
    assert found(done + tuned.replace("|active", "|inactive"), own=True)["keep"] == {}


def test_own_node_with_the_old_rule_and_data_on_the_os_filesystem():
    # the old role tuned the root disk; the role now removes its rule: kept as it is after an import
    root = blk("vda", "vda", "252:0", "disk", 4, True, "mq-deadline", children=[
        blk("vda1", "vda1", "252:1", "part", 4, True, "mq-deadline", "/")])
    rule = 'udev|/etc/udev/rules.d/61-cassandra-data-disk.rules|ENV{ID_PATH}=="pci-0000:00:05.0", ATTR{queue/read_ahead_kb}="4"\n'
    r = found(rule + disk_records([root], [("/var/lib/cassandra/data", "252:1", "/", "ext4")]), own=True)
    # its value kept: the rule replaced by the role's (no effect: the disk has it already)
    assert r["keep"] == {} and "cassandra_data_readahead_kb" not in r["vars"]
    # the disk with another value now: left as it is on this node
    r = found(rule + disk_records([dict(root, ra=128)], [("/var/lib/cassandra/data", "252:1", "/", "ext4")]),
              own=True)
    assert r["keep"] == {"cassandra_data_disk_tuning": False}


def test_own_node_os_volume_needs_the_udev_rule():
    # an LVM volume of the OS: never reloaded, kept by the udev rule, which is missing here
    root = blk("rhel-root", "dm-0", "253:0", "lvm", 4, True, "", "/")
    r = found(disk_records([blk("sda", "sda", "8:0", "disk", 128, True, "mq-deadline", children=[root])],
                           [("/var/lib/cassandra/data", "253:0", "/", "xfs")], lvs=["253|0|rhel|root|8"]), own=True)
    # no rule keeps a read-ahead: the kernel's (the role sets none there)
    assert r["keep"] == {} and r["vars"]["cassandra_data_readahead_kb"] == "kernel"


def test_commitlog_alone_on_the_os_filesystem_is_no_change():
    root = blk("vda", "vda", "252:0", "disk", 128, True, "mq-deadline", children=[
        blk("vda1", "vda1", "252:1", "part", 128, True, "mq-deadline", "/")])
    fs = json.dumps({"filesystems": [{"maj:min": "252:1", "source": "/dev/vda1", "fstype": "ext4", "target": "/"}]})
    r = found(disk_records([root], []) + "mnt|commitlog|/var/lib/cassandra/commitlog|%s\n" % fs, own=True)
    assert "disks: /var/lib/cassandra/commitlog: not tuned, the commitlog alone on /, an OS filesystem" in r["lines"]
    assert r["keep"] == {}


def test_disk_not_a_block_device():
    r = found(disk_records([], [("/var/lib/cassandra/data", "0:52", "/", "overlay")]))
    assert "disks: /var/lib/cassandra/data: not tuned, overlay on /dev/x, not a block device" in r["lines"]


def test_disks_lvm_stack():
    # RHEL's tuned sets 4 MB on the disk, LVM's auto read-ahead takes it for the volume
    lv = blk("vg1-cassandra", "dm-2", "253:2", "lvm", 4096, True, "", "/data")
    r = found(disk_records([blk("sdb", "sdb", "8:16", "disk", 4096, True, "mq-deadline", children=[lv])],
                           [("/data/c/data", "253:2", "/data", "xfs")], lvs=["253|2|vg1|cassandra|auto"]))
    assert u"disks: /data/c/data \u2192 vg1-cassandra (dm-2) \u2192 sdb" in r["lines"]
    assert ("device vg1-cassandra (dm-2) (/data/c/data, rotational): read_ahead_kb 4096 (cassandra_linux: 4),"
            " scheduler none, LVM read-ahead auto (cassandra_linux: 8)") in r["lines"]
    assert ("disk sdb (under vg1-cassandra (dm-2), rotational): read_ahead_kb 4096 (cassandra_linux: 4),"
            " scheduler mq-deadline") in r["lines"]


# --- THP, tuned, swap ---

def test_thp():
    text = ("thp|enabled|always madvise [never]\nthp|defrag|always defer defer+madvise [madvise] never\n"
            "cmdline|BOOT_IMAGE=/vmlinuz ro transparent_hugepage=never quiet\n"
            "grub|/etc/default/grub|GRUB_CMDLINE_LINUX=\"rhgb transparent_hugepage=never\"\n"
            "thp_unit|/etc/systemd/system/disable-thp.service|enabled\n")
    lines = found(text)["lines"]
    assert "THP now: enabled never, defrag madvise (cassandra_linux: never never)" in lines
    assert "THP set by kernel command line transparent_hugepage=never" in lines
    assert "THP set by /etc/default/grub transparent_hugepage=never (next boots)" in lines
    assert "THP set by /etc/systemd/system/disable-thp.service (enabled)" in lines
    assert "THP now: enabled never, defrag never (same as cassandra_linux)" in found(
        "thp|enabled|always madvise [never]\nthp|defrag|always [never]\n")["lines"]


def test_tuned():
    text = "tuned|db-site|active\n" + file_records("tuned_conf", "/etc/tuned/db-site/tuned.conf", "tuned.conf")
    lines = found(text)["lines"]
    assert "tuned: profile db-site (tuned active)" in lines
    assert "tuned /etc/tuned/db-site/tuned.conf [sysctl]: vm.swappiness=5" in lines
    assert "tuned /etc/tuned/db-site/tuned.conf [vm]: transparent_hugepages=never" in lines
    assert "tuned /etc/tuned/db-site/tuned.conf [disk]: readahead=>4096" in lines
    assert not any("dirty_ratio" in line for line in lines)  # not a key of the role
    assert any(line.startswith("tuned applies these when it starts") for line in lines)
    assert not any(line.startswith("tuned applies") for line in found(text.replace("|active", "|inactive"))["lines"])


def test_swap():
    assert found("")["lines"].count("swap: none active, none in /etc/fstab (same as cassandra_linux)") == 1
    lines = found("swap_fstab|/dev/mapper/vg-swap none swap defaults 0 0\nswap_on|/dev/dm-1 partition 2097148K\n")["lines"]
    assert "swap active: /dev/dm-1 partition 2097148K (cassandra_linux: none)" in lines
    assert "swap in /etc/fstab: /dev/mapper/vg-swap none swap defaults 0 0 (cassandra_linux removes it)" in lines


# --- time sync, firewall ---

def test_timesync_ntpd_turns_it_off():
    r = found("timesync|ntpd|active|enabled\ntimesync_server|/etc/ntp.conf|server ntp1.example.net iburst\n")
    assert part(r, "cassandra_linux_timesync") == {"cassandra_linux_timesync": False}
    assert "time sync: ntpd active (at boot: enabled)" in r["lines"]
    assert "time sync /etc/ntp.conf: server ntp1.example.net iburst" in r["lines"]
    assert part(found("timesync|ntpd|active|enabled\n", timesync=False), "cassandra_linux_timesync") == {}  # already off


def test_own_node_without_time_sync_not_given_chrony():
    """A node cassandra_linux set up (it stays managed) with no time sync running: the role run
    again must not install and start chrony there."""
    assert found("", own=True)["vars"]["cassandra_linux_timesync"] is False
    assert found("")["vars"]["cassandra_linux_timesync"] is False  # the same on a node set up another way
    assert "cassandra_linux_timesync" not in found("timesync|chronyd|active|enabled\n", own=True)["vars"]
    # running, not started at boot (or another than the one the role picks): the role would change it
    stopped = ("timesync|chronyd|active|disabled\ntimesync_own|chronyd|active|disabled\n"
               "timesync_own|chrony|inactive|\ntimesync_own|systemd-timesyncd|inactive|\n")
    assert found(stopped, os_family="RedHat")["vars"]["cassandra_linux_timesync"] is False
    assert "cassandra_linux_timesync" not in found(stopped.replace("disabled", "enabled"), os_family="RedHat")["vars"]


def test_timesync_chrony_kept():
    r = found("timesync|chronyd|active|enabled\ntimesync_server|/etc/chrony.conf|server 10.0.0.5 iburst\n")
    # its servers, for the nodes added later (the role writes them as they are: no change)
    assert part(r, "cassandra_linux_timesync") == {"cassandra_linux_timesync_servers": ["server 10.0.0.5 iburst"]}
    # some in a chrony.d file: not carried, said
    r = found("timesync|chronyd|active|enabled\ntimesync_server|/etc/chrony.conf|server 10.0.0.5 iburst\n"
              "timesync_server|/etc/chrony.d/site.conf|pool pool.example.net iburst\n")
    assert part(r, "cassandra_linux_timesync") == {}
    assert any(c.startswith("NOT carried: the time servers in /etc/chrony.d/site.conf") for c in r["carried"])
    # Debian's path
    r = found("timesync|chrony|active|enabled\nchrony_installed|\ntimesync_server|/etc/chrony/chrony.conf|pool 2.debian.pool.ntp.org iburst\n",
              os_family="Debian")
    assert r["vars"]["cassandra_linux_timesync_servers"] == ["pool 2.debian.pool.ntp.org iburst"]
    assert "time sync: none active (cassandra_linux starts chrony, or systemd-timesyncd on Debian/Ubuntu)" in found("")["lines"]


def test_firewall():
    text = ("fw|firewalld|active|default zone public\nfw_rule|zone public|ports 9042/tcp 17000/tcp\n"
            "fw_rule|zone public|service cassandra-gossip: 7000/tcp\n")
    lines = found(text)["lines"]
    assert "firewall: firewalld active, default zone public" in lines
    assert "firewall zone public: ports 9042/tcp 17000/tcp" in lines
    assert lines[-1].startswith("firewall: Cassandra ports open: 7000/tcp, 9042/tcp; not seen open: 7199/tcp")
    assert found("")["lines"][-1] == "firewall: none active (firewalld, ufw), no iptables/nftables rule for the Cassandra ports"
    lines = found("fw|iptables|INPUT policy DROP|\nfw_rule|iptables|-A INPUT -p tcp -m multiport --dports 7000,9042 -j ACCEPT\n")["lines"]
    assert "firewall: iptables INPUT policy DROP" in lines
    assert lines[-1].startswith("firewall: Cassandra ports open: 7000/tcp, 9042/tcp; not seen open: 7199/tcp")


def test_nothing_found_is_nothing_set():
    # nothing read sets the role's settings: the role sets none (time sync: none runs, none started)
    r = found("")
    assert r["vars"] == {"cassandra_linux_sysctl": {}, "cassandra_linux_limits": {}, "cassandra_linux_timesync": False,
                         "cassandra_firewall_enabled": False}


# --- the report ---

def node(name, os_found, **vars):
    return {"name": name, "dc": "dc1", "rack": "r1", "read": True, "vars": dict({"cassandra_cluster_name": "c"}, **vars),
            "hand_edits": [], "normalized": [], "notes": [], "os": os_found,
            "keep": {"cassandra_linux_manage": False}}


def test_report_section_and_round_trip():
    """Lines every node has once, the others under their nodes; the carried variables in group_vars."""
    common = SITE_SYSCTL + SITE_LIMITS + "groups|cassandra\n"
    a = found(common + "swap_on|/dev/sda2 partition 1024K\n")
    b = found(common)
    layout = cassandra_inventory_layout([node("n1", a, **a["vars"]), node("n2", b, **b["vars"])], "c")
    report = layout["report"].split("\n")
    start = report.index("OS TUNING FOUND ON THE NODES (read only, compared with what cassandra_linux and"
                         " cassandra_service set):")
    section = report[start:]
    assert section[1] == "  On every node read:"
    assert "    sysctl /etc/sysctl.d/99-cassandra.conf: vm.swappiness = 10 (cassandra_linux: 1)" in section
    assert section.index("  On n2:") < section.index("    swap: none active, none in /etc/fstab (same as cassandra_linux)")
    assert "  On n1:" in section and "    swap active: /dev/sda2 partition 1024K (cassandra_linux: none)" in section
    assert section.count("    sysctl /etc/sysctl.d/99-cassandra.conf: vm.swappiness = 10 (cassandra_linux: 1)") == 1
    carried = section.index("OS TUNING CARRIED INTO THE VARIABLES, for the nodes added later (the nodes above with"
                            " cassandra_linux_manage false are left as they are):")
    assert section[carried + 1] == "  On every node read:"
    gv = layout["group_vars"]["c"]
    assert gv["cassandra_linux_sysctl"]["vm.swappiness"] == 10
    assert gv["cassandra_linux_sysctl_file"] == "/etc/sysctl.d/99-cassandra.conf"
    assert gv["cassandra_linux_limits"] == {}  # (none written: the nodes' own files give them)
    assert layout["host_vars"]["n1"]["cassandra_linux_manage"] is False
    assert "cassandra_linux_swap_off: (role default) on n2; false on n1" in layout["differences"]


def test_report_os_differences_between_nodes():
    a = found(file_records("sysctl", "/etc/sysctl.d/99-db.conf", text="vm.swappiness = 5\n"))
    b = found(file_records("sysctl", "/etc/sysctl.d/99-db.conf", text="vm.swappiness = 7\n"))
    layout = cassandra_inventory_layout([node("n1", a, **a["vars"]), node("n2", b, **b["vars"])], "c")
    assert "cassandra_linux_sysctl" in layout["host_vars"]["n1"]
    assert layout["group_vars"]["c"]["cassandra_linux_sysctl_file"] == "/etc/sysctl.d/99-db.conf"
    assert "cassandra_linux_sysctl:" in layout["differences"]


def test_report_without_os_data():
    layout = cassandra_inventory_layout([node("n1", {})], "c")
    assert "OS TUNING" not in layout["report"]


def test_package_file_under_etc_not_carried():
    """Ubuntu's procps ships /etc/sysctl.d/10-map-count.conf: a distro default, not the admin's."""
    text = file_records("sysctl", "/etc/sysctl.d/10-map-count.conf", text="vm.max_map_count=1048576\n")
    r = found(text + "owner|/etc/sysctl.d/10-map-count.conf|procps\n")
    assert part(r, "cassandra_linux_sysctl") == {"cassandra_linux_sysctl": {}}
    assert ("sysctl /etc/sysctl.d/10-map-count.conf (package procps): vm.max_map_count = 1048576"
            " (cassandra_linux: 1048575)") in r["lines"]
    # /etc/sysctl.conf is a package's too, but the admin's file
    r = found(file_records("sysctl", "/etc/sysctl.conf", text="vm.swappiness = 5\n") + "owner|/etc/sysctl.conf|procps\n"
              + "sysctl_link|/etc/sysctl.d/99-sysctl.conf\n")
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 5
    assert r["vars"]["cassandra_linux_sysctl_file"] == "/etc/sysctl.conf"


# --- nodes the roles set up: running the roles again changes nothing (audit) ---

def test_own_node_sysctl_carries_only_its_own_file():
    own = file_records("sysctl", "/etc/sysctl.d/60-cassandra.conf", text="vm.swappiness = 1\nvm.max_map_count = 1048575\n")
    r = found(own + SITE_SYSCTL, own=True)
    # exactly its file: net.core.somaxconn of the site file would be added to 60-cassandra.conf
    assert sysctl_vars(r) == {"cassandra_linux_sysctl": {"vm.swappiness": 1, "vm.max_map_count": 1048575}}
    assert any(line.startswith("sysctl /etc/sysctl.d/99-cassandra.conf: vm.swappiness = 10") for line in r["lines"])


def test_own_node_without_its_sysctl_file_keeps_what_it_has():
    """Own (its limits file) but nothing in 60-cassandra.conf: the role must not write its defaults
    there; the role's keys an admin file sets are carried in that file, the others not at all."""
    r = found(file_records("sysctl", "/etc/sysctl.d/99-db.conf", text="vm.swappiness = 5\nnet.core.somaxconn = 4096\n"),
              own=True)
    # (that file's keys, all of them: the role writes them as they are)
    assert sysctl_vars(r) == {"cassandra_linux_sysctl": {"vm.swappiness": 5, "net.core.somaxconn": 4096},
                              "cassandra_linux_sysctl_file": "/etc/sysctl.d/99-db.conf"}
    # an older role's /etc/sysctl.conf (read at boot through its link)
    conf = file_records("sysctl", "/etc/sysctl.conf", text="vm.max_map_count = 262144\nvm.swappiness = 10\n")
    r = found(conf + "sysctl_link|/etc/sysctl.d/99-sysctl.conf\n", own=True)
    assert r["vars"]["cassandra_linux_sysctl"] == {"vm.max_map_count": 262144, "vm.swappiness": 10}
    assert r["vars"]["cassandra_linux_sysctl_file"] == "/etc/sysctl.conf"
    # nothing anywhere: nothing written (not the role's defaults)
    assert found("", own=True)["vars"]["cassandra_linux_sysctl"] == {}


def test_own_node_without_its_limits_file_keeps_the_limits_in_effect():
    other = file_records("limits", "/etc/security/limits.d/90-cassandra.conf", text="cassandra - nofile 200000\n")
    assert found(other, own=True)["vars"]["cassandra_linux_limits"] == {"nofile": 200000}


def test_own_node_limits_only_its_own_file():
    own = file_records("limits", "/etc/security/limits.d/cassandra.conf",
                       text="cassandra - memlock unlimited\ncassandra - nofile 200000\n")
    other = file_records("limits", "/etc/security/limits.d/zz-local.conf", text="cassandra - nofile 65536\ncassandra - rtprio 99\n")
    r = found(own + other, own=True)
    # exactly its file, in its order: the role writes the file whole from the dict
    assert list(r["vars"]["cassandra_linux_limits"].items()) == [("memlock", "unlimited"), ("nofile", 200000)]
    role_file = "".join("cassandra - %s %s\n" % kv for kv in LINUX["cassandra_linux_limits"].items())
    assert "cassandra_linux_limits" not in found(file_records("limits", "/etc/security/limits.d/cassandra.conf", text=role_file) + other,
                                                 own=True)["vars"]
    # same items, another order: a change for the role
    swapped = "".join("cassandra - %s %s\n" % kv for kv in reversed(list(LINUX["cassandra_linux_limits"].items())))
    assert "cassandra_linux_limits" in found(file_records("limits", "/etc/security/limits.d/cassandra.conf", text=swapped),
                                             own=True)["vars"]
    # only "-" lines are the role's
    soft = file_records("limits", "/etc/security/limits.d/cassandra.conf", text="cassandra soft nofile 5\n")
    assert found(soft, own=True)["vars"]["cassandra_linux_limits"] == {}  # nothing of the role's: nothing written


def test_limits_order_kept_in_the_vars_file():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _vars_yaml
    text = _vars_yaml({"cassandra_linux_limits": {"nofile": 1, "as": "unlimited"}, "cassandra_jmx_users": {"b": 1, "a": 2}})
    assert text.index("nofile") < text.index("as:")
    assert text.index("a: 2") < text.index("b: 1")  # the others still sorted


def test_own_unit_ignores_drop_ins_and_resets():
    text = ("unit|/etc/systemd/system/cassandra.service|LimitNOFILE=1048576\n"
            "unit|/etc/systemd/system/cassandra.service.d/override.conf|LimitNOFILE=500000\n")
    assert part(found(text, own_unit=True), "cassandra_service") == {}
    assert part(found(text, own_unit=False), "cassandra_service") == {"cassandra_service_limit_nofile": 500000}
    reset = text + "unit|/etc/systemd/system/cassandra.service.d/zz.conf|LimitNOFILE=\n"
    assert part(found(reset), "cassandra_service") == {}  # back to systemd's default: nothing carried


# --- sysctl.conf, masks (audit) ---

def test_sysctl_conf_at_its_link_place():
    conf = file_records("sysctl", "/etc/sysctl.conf", text="vm.swappiness = 5\n")
    zz = file_records("sysctl", "/etc/sysctl.d/99-zz.conf", text="vm.swappiness = 7\n")
    r = found(conf + zz + "sysctl_link|/etc/sysctl.d/99-sysctl.conf\n")
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 7  # 99-zz after 99-sysctl
    # no link: sysctl --system reads it last, systemd-sysctl not at all: not a file the role writes in, nothing
    # carried (the role would move it)
    r = found(conf + zz)
    assert r["vars"]["cassandra_linux_sysctl"] == {}


def test_sysctl_conf_without_link_never_the_file():
    r = found(file_records("sysctl", "/etc/sysctl.conf", text="vm.swappiness = 5\n"))
    assert r["vars"]["cassandra_linux_sysctl"] == {}
    assert "cassandra_linux_sysctl_file" not in r["vars"]
    assert any(c.startswith("NOT carried: vm.swappiness, set in other files") for c in r["carried"])
    assert any("systemd-sysctl does not read it at boot" in line for line in r["lines"])


def test_masked_files():
    vendor = file_records("sysctl", "/usr/lib/sysctl.d/50-x.conf", text="vm.swappiness = 30\n")
    admin = file_records("sysctl", "/etc/sysctl.d/10-a.conf", text="vm.swappiness = 5\n")
    r = found(vendor + admin + "masked|/etc/sysctl.d/50-x.conf\n")
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 5
    assert not any("50-x.conf" in line for line in r["lines"])
    rule = 'udev|/usr/lib/udev/rules.d/60-ra.rules|ATTR{queue/read_ahead_kb}="512"\n'
    assert not any("60-ra" in line for line in found(rule + "masked|/etc/udev/rules.d/60-ra.rules\n")["lines"])


def test_own_node_sysctl_file_with_fewer_keys():
    """Its file lacks a role key: carried without it (the role would add it)."""
    own = file_records("sysctl", "/etc/sysctl.d/60-cassandra.conf", text="vm.swappiness = 1\n")
    r = found(own, own=True)
    assert r["vars"]["cassandra_linux_sysctl"] == {"vm.swappiness": 1}
    full = "".join("%s = %s\n" % kv for kv in LINUX["cassandra_linux_sysctl"].items())
    assert sysctl_vars(found(file_records("sysctl", "/etc/sysctl.d/60-cassandra.conf", text=full), own=True)) == {}


def test_vendor_readahead_rule_not_carried():
    r = found('udev|/usr/lib/udev/rules.d/60-ra.rules|ATTR{queue/read_ahead_kb}="128"\n')
    assert part(r, "cassandra_data") == {}
    r = found('udev|/etc/udev/rules.d/60-ra.rules|ATTR{queue/read_ahead_kb}="128"\nowner|/etc/udev/rules.d/60-ra.rules|x\n')
    assert part(r, "cassandra_data") == {}


def test_wildcard_both_kinds_not_carried():
    r = found(file_records("limits", "/etc/security/limits.d/90-all.conf", text="* - nofile 65536\n"), own=True)
    assert part(r, "cassandra_linux_limits") == {"cassandra_linux_limits": {}}


def test_masked_name_does_not_hide_sysctl_conf_link():
    conf = file_records("sysctl", "/etc/sysctl.conf", text="vm.swappiness = 5\n")
    r = found(conf + "sysctl_link|/etc/sysctl.d/99-sysctl.conf\nmasked|/usr/lib/sysctl.d/99-sysctl.conf\n")
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 5


def test_running_limits_compared_with_the_unit():
    text = "unit|/etc/systemd/system/cassandra.service|LimitNOFILE=500000\nproc_limit|Max open files|500000|500000\n"
    assert not any(line.startswith("running Cassandra") for line in found(text)["lines"])
    lines = found(text.replace("|500000|500000", "|1024|4096"))["lines"]
    assert "running Cassandra: nofile soft 1024, hard 4096 (its unit: 500000)" in lines


def test_data_dir_not_found():
    assert "disks: /data/x: not tuned, not found (not created yet?)" in found(disk_records([], [("/data/x", "", "", "")]))["lines"]


def test_limits_order_differs_between_nodes():
    """Same limits in another order: a difference (the role writes the file in the dict's order)."""
    a = found(file_records("limits", "/etc/security/limits.d/cassandra.conf", text="cassandra - memlock unlimited\ncassandra - nofile 5\n"),
              own=True)
    b = found(file_records("limits", "/etc/security/limits.d/cassandra.conf", text="cassandra - nofile 5\ncassandra - memlock unlimited\n"),
              own=True)
    layout = cassandra_inventory_layout([node("n1", a, **a["vars"]), node("n2", b, **b["vars"])], "c")
    assert "cassandra_linux_limits" not in layout["group_vars"]["c"]
    assert list(layout["host_vars"]["n2"]["cassandra_linux_limits"]) == ["nofile", "memlock"]
    assert "cassandra_linux_limits:" in layout["differences"]


def test_rpm_limits_file_labelled_and_zero_padded_kept():
    rpm = file_records("limits", "/etc/security/limits.d/cassandra.conf", text="cassandra - nofile 0100\n")
    r = found(rpm + "owner|/etc/security/limits.d/cassandra.conf|cassandra\n", own=True)
    assert r["vars"]["cassandra_linux_limits"]["nofile"] == "0100"
    assert r["lines"][0].startswith("limits /etc/security/limits.d/cassandra.conf (package cassandra): cassandra nofile")


def test_running_limits_unit_suffix_not_compared():
    text = "unit|/etc/systemd/system/cassandra.service|LimitMEMLOCK=8M\nproc_limit|Max locked memory|8388608|8388608\n"
    assert not any(line.startswith("running Cassandra") for line in found(text)["lines"])


def test_limits_overridden_lines_shown():
    """The Apache RPM ships limits.d/cassandra.conf, read after a site 95-*.conf: the site lines are shown."""
    site = file_records("limits", "/etc/security/limits.d/95-local.conf", text="cassandra - nofile 500000\n")
    rpm = file_records("limits", "/etc/security/limits.d/cassandra.conf", text="cassandra - nofile 100000\n")
    r = found(site + rpm + "owner|/etc/security/limits.d/cassandra.conf|cassandra\n")
    assert ("limits /etc/security/limits.d/95-local.conf: cassandra - nofile = 500000, overridden by"
            " /etc/security/limits.d/cassandra.conf (package cassandra)") in r["lines"]
    # (the package's file: not rewritten by the role, none written)
    assert r["vars"]["cassandra_linux_limits"] == {}


def test_own_node_fallback_carries_only_the_target_files_keys():
    """Nothing in the role's sysctl file: only the keys of the file the role would write in are carried
    (a key of another file would be added there, and a /etc/sysctl.conf line removed)."""
    text = (file_records("sysctl", "/etc/sysctl.d/99-a.conf", text="vm.swappiness = 5\nvm.max_map_count = 262144\n")
            + file_records("sysctl", "/etc/sysctl.d/50-b.conf", text="net.core.rmem_max = 1000\n"))
    r = found(text, own=True)
    assert sysctl_vars(r) == {"cassandra_linux_sysctl": {"vm.swappiness": 5, "vm.max_map_count": 262144},
                              "cassandra_linux_sysctl_file": "/etc/sysctl.d/99-a.conf"}
    assert any(n.startswith("NOT carried: net.core.rmem_max, set in other files") for n in r["carried"])


def test_own_node_file_overridden_is_a_change_to_decide():
    own = file_records("sysctl", "/etc/sysctl.d/60-cassandra.conf", text="vm.swappiness = 1\n")
    later = file_records("sysctl", "/etc/sysctl.d/99-zz.conf", text="vm.swappiness = 10\n")
    r = found(own + later, own=True)
    assert any(n.startswith("CHANGE on these nodes if the roles run: vm.swappiness = 10 in /etc/sysctl.d/99-zz.conf")
               for n in r["carried"])
    assert not any(n.startswith("CHANGE") for n in found(own, own=True)["carried"])
    same = file_records("sysctl", "/etc/sysctl.d/99-zz.conf", text="vm.swappiness = 1\n")
    assert not any(n.startswith("CHANGE") for n in found(own + same, own=True)["carried"])  # same value: nothing changes
    conf = file_records("sysctl", "/etc/sysctl.conf", text="vm.swappiness = 1\n")
    assert any(n.startswith("CHANGE") for n in found(own + conf, own=True)["carried"])  # the role removes the line


def test_own_node_readahead_from_its_own_rule():
    for own in ("64-cassandra-data-disk.rules", "61-cassandra-data-disk.rules"):  # 61-: written by earlier versions
        rules = ('udev|/etc/udev/rules.d/%s|KERNEL=="sdb", ATTR{queue/read_ahead_kb}="8"\n'
                 'udev|/etc/udev/rules.d/70-other.rules|KERNEL=="sdc", ATTR{queue/read_ahead_kb}="128"\n' % own)
        assert found(rules, own=True)["vars"].get("cassandra_data_readahead_kb") == 8
        assert "cassandra_data_readahead_kb" not in found(rules)["vars"]  # not the role's node: several values


# --- what cassandra_linux would change on a node (the import's self-check) ---

RHEL = {"os_family": "RedHat"}
DEBIAN = {"os_family": "Debian"}
THP_SUM = hashlib.sha1(thp_unit({"enabled": "never", "defrag": "never"}).encode()).hexdigest()


def linux(**changes):
    """cassandra_linux's variables as the self-check hands them over: its defaults."""
    v = {"sysctl": LINUX["cassandra_linux_sysctl"], "sysctl_file": LINUX["cassandra_linux_sysctl_file"],
         "limits": LINUX["cassandra_linux_limits"], "timesync": True, "offline": False, "apply_live": "auto",
         "container_types": LINUX["cassandra_linux_container_types"], "disk_tuning": True, "readahead_kb": 4,
         "block_device": "", "block_devices": [], "tuned_manage": True, "tune_os_volumes": True}
    v.update(changes)
    return v


def chosen(values, value):
    """A sysfs choice file: always [madvise] never."""
    return " ".join("[%s]" % v if v == value else v for v in values)


def role_built(thp="never", defrag="never", limits=None):
    """The records of a node cassandra_linux set up (RedHat, chronyd), everything as it sets it."""
    out = "".join("sysctl|/etc/sysctl.d/60-cassandra.conf|%s = %s\nsysctl_live|%s|%s\n" % (k, v, k, v)
                  for k, v in LINUX["cassandra_linux_sysctl"].items())
    out += "".join("limits|/etc/security/limits.d/cassandra.conf|cassandra - %s %s\n" % kv
                   for kv in (limits or LINUX["cassandra_linux_limits"]).items())
    out += "limits|/etc/security/limits.d/90-nproc.conf|* - nproc 32768\n"
    out += "thp|enabled|%s\nthp|defrag|%s\n" % (chosen(["always", "madvise", "never"], thp),
                                                chosen(["always", "defer", "defer+madvise", "madvise", "never"], defrag))
    out += "thp_own|sum|%s\nthp_own|boot|enabled\n" % THP_SUM
    out += "chrony_installed|\ntimesync_own|chronyd|active|enabled\ntimesync_own|chrony|inactive|\n"
    out += "timesync_own|systemd-timesyncd|inactive|\n"
    return out


def changes(text, facts=None, own=True, **variables):
    return cassandra_linux_changes(text, linux(**variables), facts or RHEL, own)


def test_role_built_node_as_the_role_sets_it_changes_nothing():
    assert changes(role_built()) == []


def test_thp_on_a_role_built_node_is_a_change():
    # the false pass of rc9: THP set back to madvise by hand on a node the roles set up
    assert changes(role_built(thp="madvise", defrag="madvise")) == [
        {"what": "transparent_hugepage enabled (live)", "now": "madvise", "would": "never", "effect": "change"},
        {"what": "transparent_hugepage defrag (live)", "now": "madvise", "would": "never", "effect": "change"}]
    # in a container nothing is applied live (the host's /sys)
    assert changes(role_built(thp="madvise"), dict(RHEL, virtualization_type="podman")) == []


def test_thp_unit_written_where_thp_is_off_has_no_effect():
    no_unit = role_built().replace("thp_own|sum|%s\nthp_own|boot|enabled" % THP_SUM, "thp_own|boot|")
    assert [(c["what"], c["effect"]) for c in changes(no_unit)] == [
        ("/etc/systemd/system/disable-thp.service", "no effect"), ("disable-thp.service at boot", "no effect")]
    # THP on: the unit changes it at the next boot (and now)
    madvise = no_unit.replace("always madvise [never]", "always [madvise] never")
    assert [c["effect"] for c in changes(madvise)] == ["change", "change", "change"]


def test_swap_is_a_change():
    swap = role_built() + "swap_on|/dev/dm-1 partition 2097148K\nswap_fstab|/dev/mapper/rhel-swap none swap defaults 0 0\n"
    assert changes(swap) == [
        {"what": "swap (live)", "now": "on: /dev/dm-1", "would": "off (swapoff -a)", "effect": "change"},
        {"what": "swap in /etc/fstab", "now": "/dev/mapper/rhel-swap none swap defaults 0 0", "would": "removed",
         "effect": "change"}]


def test_nproc_file_added_with_no_effect_on_the_cassandra_user():
    no_nproc = role_built().replace("limits|/etc/security/limits.d/90-nproc.conf|* - nproc 32768\n", "")
    assert changes(no_nproc) == [{"what": "/etc/security/limits.d/90-nproc.conf", "now": "no line * - nproc 32768",
                                  "would": "* - nproc 32768 added", "effect": "no effect"}]
    assert not [c for c in changes(no_nproc, DEBIAN) if "nproc" in c["what"]]  # RedHat only


def test_limits_file_rewritten_with_other_values_is_a_change():
    other = role_built(limits={"memlock": "unlimited", "nofile": 100000, "nproc": 32768, "as": "unlimited"})
    assert changes(other) == [{
        "what": "limits of the cassandra user: nofile (its logins)",
        "now": "100000 (/etc/security/limits.d/cassandra.conf)",
        "would": "1048576 (/etc/security/limits.d/cassandra.conf)", "effect": "change"}]
    # the same lines without the role's mark: rewritten, no effect
    assert changes(role_built(), own=False) == [{
        "what": "/etc/security/limits.d/cassandra.conf",
        "now": "cassandra - memlock unlimited; cassandra - nofile 1048576; cassandra - nproc 32768; cassandra - as unlimited",
        "would": "cassandra - memlock unlimited; cassandra - nofile 1048576; cassandra - nproc 32768; cassandra - as"
                 " unlimited", "effect": "no effect"}]


def test_limits_block_of_earlier_versions_removed():
    block = "limits_block_found|\nlimits_block|cassandra - nofile 100000\nlimits|/etc/security/limits.conf|cassandra - nofile 100000\n"
    # limits.conf is read first: the role's file wins already, no effect
    assert [(c["what"], c["effect"]) for c in changes(role_built() + block)] == [
        ("the block earlier versions wrote in /etc/security/limits.conf", "no effect")]
    # a key only the block sets: the cassandra user loses it
    block = block.replace("nofile 100000", "rss 1000")
    assert changes(role_built() + block) == [{"what": "limits of the cassandra user: rss (its logins)",
                                              "now": "1000 (/etc/security/limits.conf)", "would": "not set",
                                              "effect": "change"}]


def test_sysctl_live_and_file_changes():
    swappiness = role_built().replace("sysctl_live|vm.swappiness|1", "sysctl_live|vm.swappiness|60")
    assert changes(swappiness) == [
        {"what": "sysctl vm.swappiness (live)", "now": "60", "would": "1", "effect": "change"}]
    # a key missing in the file, the node has the value live: written, no effect
    missing = role_built().replace("sysctl|/etc/sysctl.d/60-cassandra.conf|vm.swappiness = 1\n", "")
    assert changes(missing) == [{"what": "sysctl vm.swappiness (/etc/sysctl.d/60-cassandra.conf)", "now": "not set",
                                 "would": "1", "effect": "no effect"}]
    # a role key in /etc/sysctl.conf: removed (the role's file then decides at boot)
    conf = role_built() + "sysctl|/etc/sysctl.conf|vm.swappiness = 10\n"
    assert changes(conf) == [{"what": "sysctl vm.swappiness (/etc/sysctl.conf)", "now": "10", "would": "removed",
                              "effect": "no effect"}]
    # its file target: the file of the inventory
    assert changes(role_built(), sysctl={"vm.swappiness": 1}) == []
    assert changes(role_built(), sysctl={"vm.swappiness": 5})[0] == {
        "what": "sysctl vm.swappiness (live)", "now": "1", "would": "5", "effect": "change"}


def test_time_sync_changes():
    stopped = role_built().replace("timesync_own|chronyd|active|enabled", "timesync_own|chronyd|inactive|disabled")
    assert changes(stopped) == [
        {"what": "time sync chronyd", "now": "inactive", "would": "started", "effect": "change"},
        {"what": "time sync chronyd at boot", "now": "disabled", "would": "enabled", "effect": "change"}]
    assert changes(stopped, timesync=False) == []
    # Debian without chrony: systemd-timesyncd, installed if missing; offline: nothing downloaded, a warning only
    debian = role_built().replace("chrony_installed|\n", "")
    assert changes(debian, DEBIAN) == [
        {"what": "time sync systemd-timesyncd", "now": "inactive, not installed", "would": "installed and started",
         "effect": "change"},
        {"what": "time sync systemd-timesyncd at boot", "now": "not found", "would": "enabled", "effect": "change"}]
    assert changes(debian, DEBIAN, offline=True) == []
    assert changes(role_built().replace("timesync_own|chrony|inactive|", "timesync_own|chrony|active|enabled"), DEBIAN) == []


SSD_TUNED = disk_records([blk("sdb", "sdb", "8:16", "disk", 4, False, "none", "/data")],
                         [("/data/c/data", "8:16", "/data", "xfs")])


def test_disk_changes():
    rule = 'udev|/etc/udev/rules.d/64-cassandra-data-disk.rules|ENV{ID_SERIAL}=="x", ATTR{queue/read_ahead_kb}="4"\n'
    assert changes(role_built() + SSD_TUNED + rule) == []
    # no rule: written, the devices keep what they have now
    assert changes(role_built() + SSD_TUNED) == [{"what": "/etc/udev/rules.d/64-cassandra-data-disk.rules",
                                                  "now": "absent", "would": "written", "effect": "no effect"}]
    # a read-ahead and a scheduler set by hand: changes
    by_hand = SSD_TUNED.replace('"ra": 4', '"ra": 128').replace('"sched": "none"', '"sched": "mq-deadline"')
    assert [(c["what"], c["now"], c["would"], c["effect"]) for c in changes(role_built() + by_hand + rule)] == [
        ("read_ahead_kb sdb (live)", "128", "4", "change"), ("IO scheduler sdb (live)", "mq-deadline", "none", "change")]
    # the inventory's read-ahead: its rule rewritten (no effect while the disks have it)
    assert changes(role_built() + SSD_TUNED.replace('"ra": 4', '"ra": 8') + rule, readahead_kb=8) == [
        {"what": "/etc/udev/rules.d/64-cassandra-data-disk.rules", "now": "read_ahead_kb 4", "would": "read_ahead_kb 8",
         "effect": "no effect"}]
    assert changes(role_built() + by_hand, disk_tuning=False) == []
    assert changes(role_built() + SSD_TUNED + rule, block_devices=["/dev/sdb"]) == [{
        "what": "data devices", "now": "set by cassandra_data_block_device(s)",
        "would": "tuned (the import does not work out which devices)", "effect": "change"}]


def test_lvm_read_ahead_auto_is_a_change():
    lv = blk("vg1-cassandra", "dm-2", "253:2", "lvm", 4, True, "", "/data")
    disks = disk_records([blk("sdb", "sdb", "8:16", "disk", 4, True, "mq-deadline", children=[lv])],
                         [("/data/c/data", "253:2", "/data", "xfs")], lvs=["253|2|vg1|cassandra|auto"])
    assert [(c["what"], c["now"], c["would"]) for c in changes(role_built() + disks) if c["effect"] == "change"] == [
        ("LVM read-ahead vg1/cassandra (sectors) (lvchange, Cassandra stopped)", "auto", "8"),
        # (meanwhile the udev rule keeps it)
        ("/etc/udev/rules.d/64-cassandra-data-disk.rules", "absent", "written")]


# --- rc10: the import carries what the node has, so that cassandra_linux changes nothing there ---

def owner_like():
    """A RHEL 9 node set up by hand (built from scratch for the test): the package from a file, its limits file,
    an admin's sysctl file, THP always/madvise from the kernel, chronyd with the site's servers, the data on LVM
    (read-ahead auto) over an SSD running mq-deadline, no swap, no udev rule."""
    out = file_records("sysctl", "/etc/sysctl.d/90_cassandra.conf",
                       text="vm.max_map_count = 1048575\nvm.swappiness = 1\nnet.core.somaxconn = 4096\n")
    out += file_records("sysctl", "/etc/sysctl.d/99-image.conf", text="kernel.panic = 10\n")
    live = (("vm.max_map_count", 1048575), ("vm.swappiness", 1), ("net.core.somaxconn", 4096), ("vm.zone_reclaim_mode", 0))
    out += "".join("sysctl_live|%s|%s\n" % kv for kv in live)
    out += file_records("limits", "/etc/security/limits.d/cassandra.conf",
                        text="cassandra - memlock unlimited\ncassandra - nofile 100000\ncassandra - nproc 32768\n")
    out += "owner|/etc/security/limits.d/cassandra.conf|cassandra\n"
    out += file_records("limits", "/etc/security/limits.d/92-cassandra.conf", text="cassandra - nofile 1048576\n")
    out += "groups|cassandra\n"
    out += "thp|enabled|[always] madvise never\nthp|defrag|always defer defer+madvise [madvise] never\n"
    out += "cmdline|BOOT_IMAGE=/vmlinuz root=/dev/mapper/rhel-root ro quiet\nthp_own|boot|\n"
    out += "timesync|chronyd|active|enabled\nchrony_installed|\ntimesync_own|chronyd|active|enabled\n"
    out += "timesync_own|chrony|active|alias\ntimesync_own|systemd-timesyncd|inactive|\n"
    out += "timesync_server|/etc/chrony.conf|server ntp1.example.org iburst\n"
    out += "timesync_server|/etc/chrony.conf|server ntp2.example.org iburst prefer\n"
    lv = blk("datavg-data", "dm-3", "253:3", "lvm", 128, False, "", "/data")
    out += disk_records([blk("sdb", "sdb", "8:16", "disk", 128, False, "mq-deadline", children=[lv])],
                        [("/data/cassandra/data", "253:3", "/data", "xfs")], lvs=["253|3|datavg|data|auto"])
    return out


def would_be(found_vars):
    """cassandra_linux's variables after the import: its defaults, then what the import wrote."""
    v = linux()
    names = {"cassandra_linux_sysctl": "sysctl", "cassandra_linux_sysctl_file": "sysctl_file",
             "cassandra_linux_limits": "limits", "cassandra_linux_nproc_all": "nproc_all",
             "cassandra_linux_swap_off": "swap_off", "cassandra_linux_thp_enabled": "thp_enabled",
             "cassandra_linux_thp_defrag": "thp_defrag", "cassandra_linux_timesync": "timesync",
             "cassandra_linux_timesync_servers": "timesync_servers", "cassandra_data_readahead_kb": "readahead_kb",
             "cassandra_data_scheduler": "scheduler", "cassandra_data_lvm_readahead": "lvm_readahead",
             "cassandra_data_disk_tuning": "disk_tuning"}
    v.update((names[k], val) for k, val in found_vars.items() if k in names)
    return v


def test_owner_like_node_is_a_no_op_with_the_roles():
    r = found(owner_like(), os_family="RedHat")
    assert part(r, "cassandra_linux", "cassandra_data") == {
        "cassandra_linux_sysctl": {"vm.max_map_count": 1048575, "vm.swappiness": 1, "net.core.somaxconn": 4096},
        "cassandra_linux_sysctl_file": "/etc/sysctl.d/90_cassandra.conf",
        "cassandra_linux_limits": {}, "cassandra_linux_nproc_all": False,
        "cassandra_linux_thp_enabled": "kernel", "cassandra_linux_thp_defrag": "kernel",
        "cassandra_linux_timesync_servers": ["server ntp1.example.org iburst", "server ntp2.example.org iburst prefer"],
        "cassandra_data_readahead_kb": "kernel", "cassandra_data_scheduler": "keep",
        "cassandra_data_lvm_readahead": "auto"}
    assert r["keep"] == {}
    # cassandra_linux_manage true: nothing the role would change, not even with no effect
    assert cassandra_linux_changes(owner_like(), would_be(r["vars"]), RHEL, False) == []
    # with the stock defaults instead: far from a no-op
    assert len(cassandra_linux_changes(owner_like(), linux(), RHEL, False)) > 5


def test_thp_carried():
    # nothing sets it: the kernel's
    r = found("thp|enabled|[always] madvise never\nthp|defrag|always [madvise] never\n")
    assert part(r, "cassandra_linux_thp") == {"cassandra_linux_thp_enabled": "kernel", "cassandra_linux_thp_defrag": "kernel"}
    # something sets it at boot (a unit, the kernel command line, tuned): its values, which the role's unit writes
    unit = "thp_unit|/etc/systemd/system/thp.service|enabled\n"
    r = found("thp|enabled|always [madvise] never\nthp|defrag|always [madvise] never\n" + unit)
    assert part(r, "cassandra_linux_thp") == {"cassandra_linux_thp_enabled": "madvise", "cassandra_linux_thp_defrag": "madvise"}
    tuned = "tuned_conf|/etc/tuned/db/tuned.conf|[vm]\ntuned_conf|/etc/tuned/db/tuned.conf|transparent_hugepages=madvise\n"
    assert part(found("thp|enabled|always [madvise] never\nthp|defrag|always madvise [never]\n" + tuned),
                "cassandra_linux_thp") == {"cassandra_linux_thp_enabled": "madvise"}
    # the role's unit, THP off: the defaults, nothing written
    r = found("thp|enabled|always madvise [never]\nthp|defrag|always madvise [never]\n"
              "thp_unit|/etc/systemd/system/disable-thp.service|enabled\n", own=True)
    assert part(r, "cassandra_linux_thp") == {}


def test_swap_carried():
    r = found("swap_on|/dev/dm-1 partition 2097148K\n")
    assert r["vars"]["cassandra_linux_swap_off"] is False
    assert "cassandra_linux_swap_off" not in found("")["vars"]


def test_nproc_all_carried_on_redhat():
    assert found("", os_family="RedHat")["vars"]["cassandra_linux_nproc_all"] is False
    there = "limits|/etc/security/limits.d/90-nproc.conf|* - nproc 32768\n"
    assert "cassandra_linux_nproc_all" not in found(there, os_family="RedHat")["vars"]
    assert "cassandra_linux_nproc_all" not in found("", os_family="Debian")["vars"]


def test_scheduler_and_lvm_carried():
    ssd = SDB1_SSD.replace('"sched": "none"', '"sched": "mq-deadline"')
    r = found(ssd)
    assert part(r, "cassandra_data") == {"cassandra_data_readahead_kb": "kernel", "cassandra_data_scheduler": "keep"}
    assert part(found(SDB1_SSD), "cassandra_data") == {"cassandra_data_readahead_kb": "kernel"}


# --- the model with the variables of rc10 ---

def test_thp_unit_template_writes_the_unit_of_earlier_versions():
    # never/never: the very file earlier versions copied (no rewrite on the nodes they set up)
    assert hashlib.sha1(thp_unit({"enabled": "never", "defrag": "never"}).encode()).hexdigest() == \
        "0eb0320872bcba95824374ce4a88b902a9827fea"
    assert "echo 'madvise' > /sys/kernel/mm/transparent_hugepage/defrag\"" in thp_unit({"defrag": "madvise"})
    assert "enabled" not in thp_unit({"defrag": "madvise"}).split("ExecStart")[1]


def test_model_thp_values():
    # the role's unit with madvise where the node has never: the unit rewritten, THP changed live
    assert [(c["what"], c["effect"]) for c in changes(role_built(), thp_enabled="madvise")] == [
        ("/etc/systemd/system/disable-thp.service", "change"), ("transparent_hugepage enabled (live)", "change")]
    # keep: nothing, whatever the node has
    assert changes(role_built(thp="always"), thp_enabled="keep", thp_defrag="keep") == []
    # kernel on both: its unit removed (what THP is at the next boot: the kernel's, not known here)
    assert changes(role_built(), thp_enabled="kernel", thp_defrag="kernel") == [{
        "what": "/etc/systemd/system/disable-thp.service", "now": "enabled",
        "would": "removed (THP left to the kernel at boot)", "effect": "change"}]
    no_unit = role_built().replace("thp_own|sum|%s\nthp_own|boot|enabled\n" % THP_SUM, "thp_own|boot|\n")
    assert changes(no_unit, thp_enabled="kernel", thp_defrag="kernel") == []


def test_model_swap_limits_nproc_switches():
    swap = role_built() + "swap_on|/dev/dm-1 partition 2097148K\nswap_fstab|/dev/x none swap defaults 0 0\n"
    assert changes(swap, swap_off=False) == []
    other = role_built(limits={"nofile": 100000})
    assert changes(other, limits={}) == []
    # another file of the role's: written there (the cassandra user's limits the same: no effect)
    assert [(c["what"], c["effect"]) for c in changes(role_built(), limits_file="/etc/security/limits.d/92-c.conf")] \
        == [("/etc/security/limits.d/92-c.conf", "no effect")]
    no_nproc = role_built().replace("limits|/etc/security/limits.d/90-nproc.conf|* - nproc 32768\n", "")
    assert changes(no_nproc, nproc_all=False) == []


def test_model_disk_variables():
    by_hand = SSD_TUNED.replace('"ra": 4', '"ra": 128').replace('"sched": "none"', '"sched": "mq-deadline"')
    # the kernel's read-ahead, the scheduler kept: nothing to set on them
    assert changes(role_built() + by_hand, readahead_kb="kernel", scheduler="keep") == []
    # the scheduler kept, the read-ahead set
    assert [c["what"] for c in changes(role_built() + by_hand, scheduler="keep") if c["effect"] == "change"] == [
        "read_ahead_kb sdb (live)", "/etc/udev/rules.d/64-cassandra-data-disk.rules"]
    # the role's rule there while nothing is to be kept: removed (no effect on the devices now)
    rule = 'udev|/etc/udev/rules.d/64-cassandra-data-disk.rules|ENV{ID_SERIAL}=="x", ATTR{queue/read_ahead_kb}="4"\n'
    assert changes(role_built() + by_hand + rule, readahead_kb="kernel", scheduler="keep", lvm_readahead="auto") == [
        {"what": "/etc/udev/rules.d/64-cassandra-data-disk.rules", "now": "present", "would": "removed",
         "effect": "no effect"}]
    lv = blk("vg1-cassandra", "dm-2", "253:2", "lvm", 128, True, "", "/data")
    disks = disk_records([blk("sdb", "sdb", "8:16", "disk", 128, True, "mq-deadline", children=[lv])],
                         [("/data/c/data", "253:2", "/data", "xfs")], lvs=["253|2|vg1|cassandra|256"])
    # LVM's own read-ahead asked for, the volume has 256 sectors: lvchange
    assert [(c["what"], c["would"]) for c in changes(role_built() + disks, readahead_kb="kernel", lvm_readahead="auto")] \
        == [("LVM read-ahead vg1/cassandra (sectors) (lvchange, Cassandra stopped)", "auto"),
            # (its scheduler line: the rule says none for an SSD it is put on)
            ("/etc/udev/rules.d/64-cassandra-data-disk.rules", "written")]


def test_model_chrony_servers():
    servers = "timesync_server|/etc/chrony.conf|pool 2.rhel.pool.ntp.org iburst\n"
    assert changes(role_built() + servers, timesync_servers=["pool 2.rhel.pool.ntp.org   iburst"]) == []
    assert changes(role_built() + servers, timesync_servers=["server ntp1.example.org iburst"]) == [
        {"what": "chrony server line", "now": "pool 2.rhel.pool.ntp.org iburst", "would": "commented out",
         "effect": "change"},
        {"what": "chrony server line", "now": "absent", "would": "server ntp1.example.org iburst", "effect": "change"}]
    assert changes(role_built() + servers) == []  # none listed: chrony.conf left as it is


# --- the firewall (rc10) ---

FIREWALLD = ("fw|firewalld|active|default zone public\nfw_rule|zone public|ports 7000/tcp 9042/tcp 7199/tcp\n"
             "fw_rule|zone public|service ssh: 22/tcp\n")


def test_firewall_carried():
    r = found(FIREWALLD)
    assert part(r, "open_ports", "cassandra_firewall") == {"open_ports": ["7000/tcp", "9042/tcp", "7199/tcp"]}
    assert not [c for c in r["carried"] if c.startswith("NOT carried")]  # (ssh: not a Cassandra port)
    # a rich rule on a Cassandra port: not carried, said
    rich = FIREWALLD + 'fw_rule|zone public|rich rule rule family="ipv4" source address="10.0.9.0/24" port port="7199" protocol="tcp" accept\n'
    assert any(c.startswith("NOT carried: zone public rich rule") for c in found(rich)["carried"])
    ufw = "fw|ufw|active|\nfw_rule|ufw|9042/tcp                   ALLOW       Anywhere\nfw_rule|ufw|9042/tcp (v6)              ALLOW       Anywhere (v6)\n"
    assert found(ufw)["vars"]["open_ports"] == ["9042/tcp"]
    # iptables only (no firewalld nor ufw): none the role runs, its rules not carried
    ipt = "fw|iptables|INPUT policy DROP|\nfw_rule|iptables|-A INPUT -p tcp --dport 9042 -j ACCEPT\n"
    r = found(ipt)
    assert r["vars"]["cassandra_firewall_enabled"] is False
    assert len([c for c in r["carried"] if c.startswith("NOT carried")]) == 2


def fw(**changes):
    v = {"manage": True, "enabled": True, "open_ports": ["7000/tcp", "9042/tcp", "7199/tcp"], "port_sources": {}}
    v.update(changes)
    return v


CASSANDRA_PORTS = ["7000/tcp", "9042/tcp", "7199/tcp"]


def test_firewall_model():
    assert cassandra_firewall_changes(FIREWALLD, fw(), "RedHat", CASSANDRA_PORTS) == []
    assert cassandra_firewall_changes(FIREWALLD, fw(open_ports=["7000/tcp", "9042/tcp", "7001/tcp"]), "RedHat",
                                      CASSANDRA_PORTS) == [
        {"what": "port 7001/tcp", "now": "closed", "would": "open", "effect": "change"}]
    assert cassandra_firewall_changes("", fw(), "RedHat", CASSANDRA_PORTS) == [
        {"what": "firewall firewalld", "now": "none", "would": "installed and started", "effect": "change"}]
    assert cassandra_firewall_changes("", fw(enabled=False), "RedHat", CASSANDRA_PORTS) == []
    assert cassandra_firewall_changes(FIREWALLD, fw(enabled=False), "RedHat", CASSANDRA_PORTS) == [
        {"what": "firewall firewalld", "now": "active", "would": "stopped, disabled", "effect": "change"}]
    assert cassandra_firewall_changes(FIREWALLD, fw(manage=False), "RedHat", CASSANDRA_PORTS) == []
    ipt = "fw|iptables|INPUT policy DROP|\n"
    assert [c["what"] for c in cassandra_firewall_changes(ipt, fw(enabled=False), "RedHat", CASSANDRA_PORTS)] == [
        "firewall rule (not carried)"]
