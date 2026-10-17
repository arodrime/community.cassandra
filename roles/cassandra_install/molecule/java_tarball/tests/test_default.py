import os

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')

JAVA_HOME = "/opt/cassandra-java/OpenJDK17U-jre_x64_linux_hotspot_17.0.20.1_1"


def test_java_is_the_tarball(host):
    assert host.file(JAVA_HOME + "/bin/java").exists
    assert host.file("/usr/bin/java").linked_to == JAVA_HOME + "/bin/java"
    assert '"17.' in host.run("java -version").stderr


def test_no_java_package(host):
    if host.system_info.distribution in ("ubuntu", "debian"):
        assert not host.run("dpkg -l | awk '$1 == \"ii\" && $2 ~ /^openjdk-/'").stdout.strip()
        assert host.package("cassandra-java-tarball").is_installed
    else:
        assert not host.run("rpm -qa | grep -iE '^(java|jre)-'").stdout.strip()


def test_cassandra_installed_and_runs_with_it(host):
    assert host.package("cassandra").is_installed
    assert host.package("cassandra-tools").is_installed
    assert host.run("cassandra -v").rc == 0
