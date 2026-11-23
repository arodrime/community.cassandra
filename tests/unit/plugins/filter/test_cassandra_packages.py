from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The installed packages of a few names, read with rpm -q or dpkg-query -W (not package_facts, which puts every
# package of the node in its facts, copied again for each task): given as package_facts gives them.

import shutil
import subprocess

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_packages import (
    cassandra_package_query, cassandra_installed_packages)


def test_the_command_reads_the_names_given():
    rpm = cassandra_package_query(["cassandra", ["cassandra-tools", ""], "cassandra"], "RedHat")
    assert rpm[:3] == ["rpm", "-q", "--qf"] and rpm[4:] == ["cassandra", "cassandra-tools"]
    deb = cassandra_package_query("libjemalloc2", "Debian")
    assert deb[:2] == ["dpkg-query", "-W"] and deb[3:] == ["libjemalloc2"]


def test_rpm_as_package_facts_gives_them():
    out = ("installed\tcassandra\t5.0.9\t1\t(none)\tnoarch\n"
           "package jemalloc is not installed\n"
           "installed\tkernel-core\t5.14.0\t503.el9\t(none)\tx86_64\n"
           "installed\tkernel-core\t5.14.0\t570.el9\t(none)\tx86_64\n"
           "installed\tjava-17-openjdk-headless\t17.0.13.0.11\t4.el9\t1\tx86_64\n")
    assert cassandra_installed_packages({"rc": 1, "stdout": out, "stderr": ""}) == {
        "cassandra": [{"name": "cassandra", "version": "5.0.9", "release": "1", "epoch": None, "arch": "noarch"}],
        "kernel-core": [{"name": "kernel-core", "version": "5.14.0", "release": "503.el9", "epoch": None, "arch": "x86_64"},
                        {"name": "kernel-core", "version": "5.14.0", "release": "570.el9", "epoch": None, "arch": "x86_64"}],
        "java-17-openjdk-headless": [{"name": "java-17-openjdk-headless", "version": "17.0.13.0.11", "release": "4.el9",
                                      "epoch": "1", "arch": "x86_64"}]}


def test_dpkg_the_installed_ones_only():
    out = ("installed\tcassandra\t5.0.9\t\t\tall\n"
           "config-files\tjemalloc\t3.6.0-11\t\t\tamd64\n"  # removed, its config files left: not installed
           "half-configured\tlibjemalloc2\t5.3.0-2\t\t\tamd64\n")
    err = "dpkg-query: no packages found matching python3.11\n"
    assert cassandra_installed_packages({"rc": 1, "stdout": out, "stderr": err}) == {
        "cassandra": [{"name": "cassandra", "version": "5.0.9", "arch": "all"}],
        "libjemalloc2": [{"name": "libjemalloc2", "version": "5.3.0-2", "arch": "amd64"}]}
    assert cassandra_installed_packages({"rc": 1, "stdout": "", "stderr": err}) == {}  # none installed


def test_not_read():
    assert cassandra_installed_packages({"skipped": True, "changed": False}) == {}
    assert cassandra_installed_packages({}) == {}
    assert cassandra_installed_packages("installed\ttar\t1.34\t\t\tamd64") == {
        "tar": [{"name": "tar", "version": "1.34", "arch": "amd64"}]}
    for failed in ({"rc": 2, "stdout": "", "stderr": "", "msg": "[Errno 2] No such file or directory: b'rpm'"},
                   {"rc": 1, "stdout": "", "stderr": "error: cannot open Packages database in /var/lib/rpm"}):
        with pytest.raises(AnsibleFilterError, match="the installed packages could not be read: .*(rpm|database)"):
            cassandra_installed_packages(failed)


@pytest.mark.skipif(not shutil.which("dpkg-query"), reason="no dpkg-query here")
def test_the_real_dpkg_query():
    argv = cassandra_package_query(["dpkg", "zzfake-not-a-package"], "Debian")
    done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False)
    found = cassandra_installed_packages({"rc": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
    assert list(found) == ["dpkg"] and found["dpkg"][0]["version"]
