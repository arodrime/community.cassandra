import os

import pytest

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')


def test_cassandra_available(host):
    cmd = host.run("cassandra -h")
    assert cmd.rc == 0


def test_nodetool_available(host):
    cmd = host.run("nodetool help")
    assert cmd.rc == 0


def test_cqlsh_available(host):
    cmd = host.run("cqlsh --version")

    assert cmd.rc == 0
    assert "cqlsh" in cmd.stdout


@pytest.mark.parametrize("tool", ["sstablemetadata", "sstabledump", "sstablesplit", "sstableofflinerelevel"])
def test_cassandra_tools_available(host, tool):
    assert host.exists(tool)


def test_jemalloc_found_by_ldconfig(host):
    # Optional on RedHat-likes: only there when a repo (EPEL, Amazon) has it
    if host.system_info.distribution not in ("ubuntu", "debian") and not host.run("dnf -q repoquery jemalloc").stdout:
        pytest.skip("jemalloc not in any enabled repo")

    assert "libjemalloc.so" in host.run("ldconfig -p").stdout


def test_java_17_only(host):
    cmd = host.run("java -version")

    assert cmd.rc == 0
    assert 'version "17.' in cmd.stderr
    assert host.run("ls -d /usr/lib/jvm/*11*").rc != 0


def test_policy_rc_d_removed(host):
    if host.system_info.distribution in ("ubuntu", "debian"):
        assert not host.file("/usr/sbin/policy-rc.d").exists


def test_cassandra_not_started_by_package(host):
    assert host.run("pgrep -f [C]assandraDaemon").rc != 0


def test_cqlsh_python_override_on_50x(host):
    wrapper = host.file("/usr/local/bin/cqlsh")
    if host.system_info.distribution == "ubuntu" and host.system_info.release == "26.04":
        # python3 is 3.14, past 5.0's cqlsh (3.13 at most): python3.11 from deadsnakes
        assert 'CQLSH_PYTHON:-/usr/bin/python3.11}' in wrapper.content_string
        assert host.run("/usr/bin/python3.11 -V").rc == 0
    else:
        # 5.0's cqlsh supports the system python3: no wrapper installed
        assert not wrapper.exists


def test_sudo_rs_on_ubuntu_2604(host):
    if host.system_info.distribution != "ubuntu" or host.system_info.release != "26.04":
        pytest.skip("Ubuntu 26.04 only")
    assert "sudo-rs" in host.run("sudo --version").stdout


def test_pinned_packages_held_on_debian(host):
    if host.system_info.distribution not in ("debian", "ubuntu"):
        return
    held = host.run("apt-mark showhold").stdout.split()
    assert "cassandra" in held
