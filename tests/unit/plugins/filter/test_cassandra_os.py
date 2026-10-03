from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_os import (
    _limits,
    _records,
    _sysctl_line,
    _sysctl_order,
    cassandra_os_import,
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


def found(text, **changes):
    return cassandra_os_import(text, wanted(**changes))


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

def test_sysctl_infra_file_carried_into_its_file():
    r = found(VENDOR_SYSCTL + SITE_SYSCTL + SYSCTL_CONF + "sysctl_live|vm.swappiness|60\nsysctl_live|vm.max_map_count|1048575\n")
    sysctl = r["vars"]["cassandra_linux_sysctl"]
    assert sysctl["vm.swappiness"] == 10  # the infra file, read after the vendor's 50-default.conf
    assert sysctl["net.core.somaxconn"] == 4096  # a key of a Cassandra file, the role has none
    assert sysctl["net.ipv4.tcp_keepalive_time"] == 300
    assert sysctl["net.core.rmem_max"] == 33554432  # /etc/sysctl.conf, read last
    assert sysctl["vm.zone_reclaim_mode"] == 0  # role default kept
    assert "kernel.sysrq" not in sysctl  # not Cassandra's
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
    assert r["vars"] == {}
    assert "sysctl /usr/lib/sysctl.d/50-default.conf: vm.swappiness = 30 (cassandra_linux: 1)" in r["lines"]


def test_sysctl_untuned_live_value():
    r = found("sysctl_live|vm.max_map_count|65530\nsysctl_live|vm.swappiness|1\n")
    assert r["lines"][0] == "sysctl live: vm.max_map_count = 65530 (no file sets it; cassandra_linux: 1048575)"
    assert not any("vm.swappiness" in line for line in r["lines"])
    assert r["vars"] == {}


def test_sysctl_same_values_still_name_the_file():
    """Same values as the role, in another file: the role writes into that file (not a second one it overrides)."""
    r = found(file_records("sysctl", "/etc/sysctl.d/99-db.conf", text="vm.max_map_count = 1048575\nvm.swappiness = 1\n"))
    assert r["vars"] == {"cassandra_linux_sysctl_file": "/etc/sysctl.d/99-db.conf"}


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

def test_limits_user_entries_carried():
    r = found(NPROC + SITE_LIMITS + "groups|cassandra cassandra_admins\n")
    limits = r["vars"]["cassandra_linux_limits"]
    assert limits["nofile"] == 500000
    assert limits["nproc"] == 65536  # the user's entry wins over "*"
    assert limits["memlock"] == "unlimited"  # from its group
    assert limits["as"] == "unlimited"  # soft and hard differ: the role default kept
    assert any("NOT carried: the cassandra user's as" in c for c in r["carried"])
    assert ("limits /etc/security/limits.d/95-cassandra-limits.conf: cassandra nofile = 500000"
            " (cassandra_linux: 1048576)") in r["lines"]
    assert ("limits /etc/security/limits.d/95-cassandra-limits.conf: @cassandra_admins memlock = unlimited"
            " (same as cassandra_linux)") in r["lines"]


def test_limits_group_not_member_ignored():
    r = found(SITE_LIMITS + "groups|cassandra\n")
    assert "memlock" not in [line.split()[3] for line in r["lines"] if line.startswith("limits")]
    assert r["vars"]["cassandra_linux_limits"]["memlock"] == "unlimited"  # role default


def test_limits_wildcard_shown_not_carried():
    """A distro's "*" entry applies to cassandra but is not Cassandra tuning."""
    r = found(NPROC)
    assert r["vars"] == {}
    assert "limits /etc/security/limits.d/20-nproc.conf: * soft nproc = 4096 (cassandra_linux: 32768)" in r["lines"]


def test_limits_later_file_same_rank_wins_and_limits_conf_first():
    recs = _records(file_records("limits", "/etc/security/limits.d/10-a.conf", text="cassandra - nofile 1000\n")
                    + file_records("limits", "/etc/security/limits.d/90-b.conf", text="cassandra - nofile 2000\n")
                    + file_records("limits", "/etc/security/limits.conf", text="cassandra - nofile 3000\n"))
    dummy, out, dummy2 = _limits(recs, wanted(), False)
    assert out["cassandra_linux_limits"]["nofile"] == 2000


def test_limits_user_beats_later_group_and_wildcard():
    recs = _records(file_records("limits", "/etc/security/limits.d/10-a.conf", text="cassandra - nofile 1000\n")
                    + file_records("limits", "/etc/security/limits.d/90-b.conf", text="@cassandra - nofile 2000\n* - nofile 3000\n")
                    + "groups|cassandra\n")
    dummy, out, dummy2 = _limits(recs, wanted(), False)
    assert out["cassandra_linux_limits"]["nofile"] == 1000


def test_limits_same_as_role_nothing_carried():
    r = found(file_records("limits", "/etc/security/limits.d/cassandra.conf",
                           text="cassandra - memlock unlimited\ncassandra - nofile 1048576\n"))
    assert "cassandra_linux_limits" not in r["vars"]


# --- the unit ---

def test_unit_limits_last_wins_and_carried():
    text = ("unit|/etc/systemd/system/cassandra.service|LimitNOFILE=100000\n"
            "unit|/etc/systemd/system/cassandra.service|LimitMEMLOCK=infinity\n"
            "unit|/etc/systemd/system/cassandra.service.d/limits.conf|LimitNOFILE=200000\n"
            "proc_limit|Max open files|200000|200000\nproc_limit|Max locked memory|unlimited|unlimited\n")
    r = found(text)
    assert r["vars"] == {"cassandra_service_limit_nofile": 200000}
    assert "unit /etc/systemd/system/cassandra.service.d/limits.conf: LimitNOFILE=200000 (cassandra_service: 1048576)" in r["lines"]
    assert "unit /etc/systemd/system/cassandra.service: LimitMEMLOCK=infinity (same as cassandra_service)" in r["lines"]
    assert not any(line.startswith("running Cassandra: nofile") for line in r["lines"])  # as its unit says
    assert not any("running Cassandra: memlock" in line for line in r["lines"])


# --- disks ---

def test_udev_readahead_carried():
    r = found(UDEV.splitlines()[0] + "\n" + "disk|/data/c|/dev/sdb1|part|sdb|8|none|0\n")
    assert r["vars"] == {"cassandra_data_readahead_kb": 8}
    assert any(line.startswith("udev /etc/udev/rules.d/99-readahead.rules: read_ahead_kb 8 (cassandra_linux: 4),"
                               " scheduler none") for line in r["lines"])
    assert "disk sdb (/data/c, SSD): read_ahead_kb 8 (cassandra_linux: 4), scheduler none (same as cassandra_linux)" in r["lines"]


def test_udev_several_readaheads_not_carried():
    r = found(UDEV)  # 8, and --setra 16 sectors = 8 KB... same
    assert r["vars"] == {"cassandra_data_readahead_kb": 8}
    r = found(UDEV + "udev|/etc/udev/rules.d/70-os.rules|ACTION==\"add\", KERNEL==\"sda\", ATTR{queue/read_ahead_kb}=\"128\"\n")
    assert r["vars"] == {}
    assert any("several read-aheads (8, 128)" in c for c in r["carried"])


def test_udev_rule_not_the_live_value_not_carried():
    r = found(UDEV.splitlines()[0] + "\ndisk|/data/c|/dev/sdb|disk|sdb|128|mq-deadline|1\n")
    assert r["vars"] == {}
    assert any("NOT carried: read_ahead_kb 8" in c and "128" in c for c in r["carried"])
    assert "disk sdb (/data/c, rotational): read_ahead_kb 128 (cassandra_linux: 4), scheduler mq-deadline" in r["lines"]


def test_udev_match_is_not_an_assignment():
    r = found('udev|/etc/udev/rules.d/1.rules|ATTR{queue/read_ahead_kb}=="8", ATTR{queue/read_ahead_kb}="64"\n')
    assert r["vars"] == {"cassandra_data_readahead_kb": 64}


def test_udev_vendor_rule_not_carried():
    r = found('udev|/usr/lib/udev/rules.d/60-block-scheduler.rules|ATTR{queue/scheduler}="bfq"\n')
    assert r["vars"] == {}
    assert any("scheduler bfq" in line for line in r["lines"])


def test_disk_not_plain():
    r = found("disk|/var/lib/cassandra/data|overlay||||||\n")
    assert "disk of /var/lib/cassandra/data: overlay (?), not a plain disk: its read-ahead is not read" in r["lines"]


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
    assert r["vars"] == {"cassandra_linux_timesync": False}
    assert "time sync: ntpd active (at boot: enabled)" in r["lines"]
    assert "time sync /etc/ntp.conf: server ntp1.example.net iburst" in r["lines"]
    assert found("timesync|ntpd|active|enabled\n", timesync=False)["vars"] == {}  # already off


def test_own_node_without_time_sync_not_given_chrony():
    """A node cassandra_linux set up (it stays managed) with no time sync running: the role run
    again must not install and start chrony there."""
    assert found("", own=True)["vars"]["cassandra_linux_timesync"] is False
    assert "cassandra_linux_timesync" not in found("")["vars"]  # not managed: left as it is anyway
    assert "cassandra_linux_timesync" not in found("timesync|chronyd|active|enabled\n", own=True)["vars"]


def test_timesync_chrony_kept():
    r = found("timesync|chronyd|active|enabled\ntimesync_server|/etc/chrony.conf|server 10.0.0.5 iburst\n")
    assert r["vars"] == {}
    assert any(c.startswith("time sync servers: cassandra_linux does not write them") for c in r["carried"])
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


def test_nothing_found_is_all_defaults():
    r = found("")
    assert r["vars"] == {} and r["carried"] == []


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
    assert gv["cassandra_linux_limits"]["nofile"] == 500000
    assert layout["host_vars"]["n1"]["cassandra_linux_manage"] is False
    assert "  none" in layout["differences"]


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
    assert r["vars"] == {}
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
    # exactly its file: net.core.somaxconn of the infra file would be added to 60-cassandra.conf
    assert sysctl_vars(r) == {"cassandra_linux_sysctl": {"vm.swappiness": 1, "vm.max_map_count": 1048575}}
    assert any(line.startswith("sysctl /etc/sysctl.d/99-cassandra.conf: vm.swappiness = 10") for line in r["lines"])


def test_own_node_without_its_sysctl_file_keeps_what_it_has():
    """Own (its limits file) but nothing in 60-cassandra.conf: the role must not write its defaults
    there; the role's keys an admin file sets are carried in that file, the others not at all."""
    r = found(file_records("sysctl", "/etc/sysctl.d/99-db.conf", text="vm.swappiness = 5\nnet.core.somaxconn = 4096\n"),
              own=True)
    assert sysctl_vars(r) == {"cassandra_linux_sysctl": {"vm.swappiness": 5}, "cassandra_linux_sysctl_file": "/etc/sysctl.d/99-db.conf"}
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
    other = file_records("limits", "/etc/security/limits.d/zz-infra.conf", text="cassandra - nofile 65536\ncassandra - rtprio 99\n")
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
    assert found(text, own_unit=True)["vars"] == {}
    assert found(text, own_unit=False)["vars"] == {"cassandra_service_limit_nofile": 500000}
    reset = text + "unit|/etc/systemd/system/cassandra.service.d/zz.conf|LimitNOFILE=\n"
    assert found(reset)["vars"] == {}  # back to systemd's default: nothing carried


# --- sysctl.conf, masks (audit) ---

def test_sysctl_conf_at_its_link_place():
    conf = file_records("sysctl", "/etc/sysctl.conf", text="vm.swappiness = 5\n")
    zz = file_records("sysctl", "/etc/sysctl.d/99-zz.conf", text="vm.swappiness = 7\n")
    r = found(conf + zz + "sysctl_link|/etc/sysctl.d/99-sysctl.conf\n")
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 7  # 99-zz after 99-sysctl
    r = found(conf + zz)  # no link: sysctl --system reads it last
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 5


def test_sysctl_conf_without_link_never_the_file():
    r = found(file_records("sysctl", "/etc/sysctl.conf", text="vm.swappiness = 5\n"))
    assert r["vars"]["cassandra_linux_sysctl"]["vm.swappiness"] == 5
    assert "cassandra_linux_sysctl_file" not in r["vars"]
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
    assert r["vars"] == {}
    r = found('udev|/etc/udev/rules.d/60-ra.rules|ATTR{queue/read_ahead_kb}="128"\nowner|/etc/udev/rules.d/60-ra.rules|x\n')
    assert r["vars"] == {}


def test_wildcard_both_kinds_not_carried():
    r = found(file_records("limits", "/etc/security/limits.d/90-all.conf", text="* - nofile 65536\n"))
    assert r["vars"] == {}


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
    assert "disk of /data/x: not found (no such directory yet?)" in found("disk|/data/x||||||\n")["lines"]


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
    r = found(rpm + "owner|/etc/security/limits.d/cassandra.conf|cassandra\n")
    assert r["vars"]["cassandra_linux_limits"]["nofile"] == "0100"
    assert r["lines"][0].startswith("limits /etc/security/limits.d/cassandra.conf (package cassandra): cassandra nofile")


def test_running_limits_unit_suffix_not_compared():
    text = "unit|/etc/systemd/system/cassandra.service|LimitMEMLOCK=8M\nproc_limit|Max locked memory|8388608|8388608\n"
    assert not any(line.startswith("running Cassandra") for line in found(text)["lines"])


def test_limits_overridden_lines_shown():
    """The Apache RPM ships limits.d/cassandra.conf, read after an infra 95-*.conf: the infra lines are shown."""
    infra = file_records("limits", "/etc/security/limits.d/95-infra.conf", text="cassandra - nofile 500000\n")
    rpm = file_records("limits", "/etc/security/limits.d/cassandra.conf", text="cassandra - nofile 100000\n")
    r = found(infra + rpm + "owner|/etc/security/limits.d/cassandra.conf|cassandra\n")
    assert ("limits /etc/security/limits.d/95-infra.conf: cassandra - nofile = 500000, overridden by"
            " /etc/security/limits.d/cassandra.conf (package cassandra)") in r["lines"]
    assert r["vars"]["cassandra_linux_limits"]["nofile"] == 100000


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
    rules = ('udev|/etc/udev/rules.d/61-cassandra-data-disk.rules|KERNEL=="sdb", ATTR{queue/read_ahead_kb}="8"\n'
             'udev|/etc/udev/rules.d/70-other.rules|KERNEL=="sdc", ATTR{queue/read_ahead_kb}="128"\n')
    assert found(rules, own=True)["vars"].get("cassandra_data_readahead_kb") == 8
    assert "cassandra_data_readahead_kb" not in found(rules)["vars"]  # not the role's node: several values
