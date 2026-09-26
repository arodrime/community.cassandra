import os

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')


def test_hosts_file(host):
    f = host.file('/etc/hosts')

    assert f.exists
    assert f.user == 'root'
    assert f.group == 'root'


def sysctl_conf(host):
    return host.file("/etc/sysctl.d/60-cassandra.conf").content_string


def test_swappiness_persisted(host):
    assert "vm.swappiness=1" in sysctl_conf(host)


def test_max_map_count_persisted(host):
    assert "vm.max_map_count=1048575" in sysctl_conf(host)


def test_tcp_settings_persisted(host):
    conf = sysctl_conf(host)

    assert "net.ipv4.tcp_keepalive_time=60" in conf
    assert "net.core.rmem_max=16777216" in conf


def test_time_sync_package_installed(host):
    # Legacy NTP packages (existing test expected these)
    legacy_ntp = all(host.package(p).is_installed for p in ["ntp", "ntpdate", "ntp-doc"])

    # Modern replacements
    chrony = host.package("chrony").is_installed
    timesyncd = host.package("systemd-timesyncd").is_installed

    assert legacy_ntp or chrony or timesyncd, "No supported time-sync package installed (ntp/chrony/systemd-timesyncd)"

# TOD Re-enable when Ubuntu 24.04 fixed
# def test_time_sync_service(host):
#    # Accept any common service name provided by the supported packages:
#    candidates = ["ntpd", "ntp", "chronyd", "chrony", "systemd-timesyncd"]
#
#    def svc_up(svc_name):
#        s = host.service(svc_name)
#        # service() may return an object for non-existent units with False flags;
#        # we require both running and enabled to consider it healthy.
#        return getattr(s, "is_running", False) and getattr(s, "is_enabled", False)
#
#    assert any(svc_up(s) for s in candidates), "No supported time-sync service is running+enabled"


def test_limit_file(host):

    f = host.file("/etc/security/limits.d/cassandra.conf")

    assert f.exists
    assert "cassandra - nofile 1048576" in f.content_string
    assert "ANSIBLE MANAGED BLOCK" not in host.file("/etc/security/limits.conf").content_string
    assert "vm.swappiness" not in host.file("/etc/sysctl.conf").content_string

    # Extra check for RH based system
    if host.system_info.distribution == "redhat" \
            or host.system_info.distribution == "centos":
        f = host.file("/etc/security/limits.d/90-nproc.conf")

        assert f.exists
        assert "nproc" in f.content_string


# /sys/kernel/mm is the host's in a container: THP is only persisted there,
# never applied, so check the unit rather than the live value.
def test_thp_service_installed_not_enabled_in_container(host):
    assert host.file("/etc/systemd/system/disable-thp.service").exists
    assert not host.service("disable-thp").is_enabled


def test_data_disk_readahead(host):
    f = host.file("/tmp/fake-sys/block/sdz/queue/read_ahead_kb")

    assert f.content_string.strip() == "4"


def test_data_disk_scheduler(host):
    f = host.file("/tmp/fake-sys/block/sdz/queue/scheduler")

    assert f.content_string.strip() == "none"


def test_data_disk_udev_rule(host):
    f = host.file("/etc/udev/rules.d/60-cassandra-data-disk.rules")

    assert f.exists
    assert f.mode == 0o644
    assert 'KERNEL=="sdz"' in f.content_string
    assert 'ATTR{queue/read_ahead_kb}="4"' in f.content_string
    assert 'ATTR{queue/scheduler}="none"' in f.content_string


def test_jbod_disks_tuned(host):
    assert host.file("/tmp/fake-sys/block/sdy/queue/read_ahead_kb").content_string.strip() == "4"
    rules = host.file("/etc/udev/rules.d/60-cassandra-data-disk.rules").content_string
    assert 'KERNEL=="sdz"' in rules and 'KERNEL=="sdy"' in rules
