import os

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')


def test_cassandra_installed_from_the_files(host):
    for name in ("cassandra", "cassandra-tools"):
        assert host.package(name).is_installed
        assert host.package(name).version == "5.0.7"
    assert "5.0.7" in host.run("cassandra -v").stdout


def test_no_repository_left(host):
    assert not host.run("ls /etc/yum.repos.d/cassandra-* /etc/apt/sources.list.d/cassandra-* 2>/dev/null").stdout.strip()


def debian(host):
    return host.ansible("setup")["ansible_facts"]["ansible_os_family"] == "Debian"


def test_package_manager_works(host):
    if debian(host):
        assert host.run("apt-get update").rc == 0
    else:
        assert host.run("dnf -q makecache").rc == 0


def test_no_java_package_and_no_download_left(host):
    if debian(host):
        assert not host.run("dpkg -l | awk '$1 == \"ii\" && $2 ~ /^openjdk-/'").stdout.strip()
        assert host.package("cassandra").is_installed
    else:
        assert not host.run("rpm -qa | grep -iE '^(java|jre)-'").stdout.strip()
    assert not host.file("/var/tmp/cassandra-packages").exists
