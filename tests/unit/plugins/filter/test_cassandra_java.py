from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible.errors import AnsibleFilterError
from ansible_collections.community.cassandra.plugins.filter.cassandra_java import (
    cassandra_java_alternative, cassandra_java_check, cassandra_java_major, cassandra_java_release_major, cassandra_java_supported,
    cassandra_java_tarball)

MIRROR = {
    "11": {"url": "https://mirror.example.com/java/jdk-11.tar.gz", "checksum": "sha256:11"},
    "17": {"url": "https://mirror.example.com/java/jdk-17.tar.gz", "checksum": "sha256:17",
           "username": "java", "password": "secret"},
}


def test_supported_table():
    assert cassandra_java_supported("40x") == ["8", "11"]
    assert cassandra_java_supported("41x") == ["8", "11"]
    assert cassandra_java_supported("50x") == ["11", "17"]
    assert cassandra_java_supported("60x") == []


@pytest.mark.parametrize("version, series", [("8", "40x"), ("11", "41x"), (11, "50x"), ("17", "50x"), ("1.8", "40x")])
def test_supported_java_passes(version, series):
    assert cassandra_java_check(version, series) == ""


@pytest.mark.parametrize("version, series", [("21", "50x"), ("8", "50x"), ("17", "41x"), ("25", "50x")])
def test_unsupported_java_refused(version, series):
    why = cassandra_java_check(version, series)
    assert "runs on Java" in why and str(version) in why and "cassandra_java_allow_unsupported" in why
    assert cassandra_java_check(version, series, True) == ""


def test_unknown_series_refused_even_when_allowed():
    assert "Unknown Cassandra series 60x" in cassandra_java_check("17", "60x", True)


def test_tarball_entry_by_major():
    assert cassandra_java_tarball(MIRROR, "17") == MIRROR["17"]
    assert cassandra_java_tarball(MIRROR, 11) == MIRROR["11"]
    # keys written as numbers in YAML (17: rather than "17":)
    assert cassandra_java_tarball({17: MIRROR["17"]}, "17") == MIRROR["17"]


def test_no_entry():
    assert cassandra_java_tarball(MIRROR, "21") == {}
    assert cassandra_java_tarball({}, "17") == {}
    assert cassandra_java_tarball(None, "17") == {}
    assert cassandra_java_tarball(MIRROR, "") == {}


def test_entry_without_checksum_gets_an_empty_one():
    assert cassandra_java_tarball({"17": {"url": "/srv/jdk-17.tar.gz"}}, "17") == {"url": "/srv/jdk-17.tar.gz", "checksum": ""}


@pytest.mark.parametrize("tarballs, message", [
    (["https://mirror/jdk.tar.gz"], "must be a dict"),
    ({"17": "https://mirror/jdk.tar.gz"}, "must be a dict with a url"),
    ({"17": {"checksum": "sha256:17"}}, "must be a dict with a url"),
    ({"17": {"url": "https://mirror/jdk.tar.gz", "sha256": "17"}}, "unknown key(s) sha256"),
    ({"17": {"url": "a"}, 17: {"url": "b"}}, "several entries for Java 17"),
    ({"17": {"url": "https://mirror/jdk.tar.gz", "username": None}}, "username must be a string"),
])
def test_bad_entries_refused(tarballs, message):
    with pytest.raises(AnsibleFilterError) as error:
        cassandra_java_tarball(tarballs, "17")
    assert message in str(error.value)


@pytest.mark.parametrize("content, major", [
    ('IMPLEMENTOR="Eclipse Adoptium"\nJAVA_VERSION="17.0.12"\nJAVA_VERSION_DATE="2024-07-16"\n', "17"),
    ('JAVA_VERSION="11.0.24"\n', "11"),
    ('JAVA_VERSION="1.8.0_422"\nOS_NAME="Linux"\n', "8"),
    ('JAVA_VERSION="21"\n', "21"),
    ('JAVA_RUNTIME_VERSION="17.0.12+7"\nJAVA_VERSION_DATE="2024-07-16"\n', ""),
    ("", ""),
    (None, ""),
])
def test_release_major(content, major):
    assert cassandra_java_release_major(content) == major


@pytest.mark.parametrize("version, major", [("17", "17"), (17, "17"), ("1.8", "8"), ("17.0.12", "17"), ("11.0.24+8", "11")])
def test_major(version, major):
    assert cassandra_java_major(version) == major


# the java to make the default: the package's own among the alternatives, else one whose path names the version
RHEL_ALTERNATIVES = [
    "java - status is auto.",
    " link currently points to /usr/lib/jvm/java-11-openjdk-11.0.25.0.9-2.el9.x86_64/bin/java",
    "/usr/lib/jvm/java-11-openjdk-11.0.25.0.9-2.el9.x86_64/bin/java - family java-11-openjdk.x86_64 priority 11002509",
    " slave jjs: /usr/lib/jvm/java-11-openjdk-11.0.25.0.9-2.el9.x86_64/bin/jjs",
    "/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.432.b06-2.el9.x86_64/jre/bin/java - family java-1.8.0-openjdk.x86_64 priority 1800432",
    "/usr/lib/jvm/zulu17/bin/java - family zulu17 priority 2171140",
    "Current `best' version is /usr/lib/jvm/zulu17/bin/java.",
]
DEBIAN_ALTERNATIVES = [
    "java - auto mode",
    "  link best version is /usr/lib/jvm/temurin-17-jdk-amd64/bin/java",
    "/usr/lib/jvm/java-11-openjdk-amd64/bin/java - priority 1111",
    "  slave java.1.gz: /usr/lib/jvm/java-11-openjdk-amd64/man/man1/java.1.gz",
    "/usr/lib/jvm/temurin-17-jdk-amd64/bin/java - priority 1",
]


@pytest.mark.parametrize("alternatives, files, version, expected", [
    # the package's own java: a path naming no version
    (RHEL_ALTERNATIVES, ["/usr/lib/jvm/zulu17", "/usr/lib/jvm/zulu17/bin/java", "/usr/lib/jvm/zulu17/bin/javac"], "17",
     "/usr/lib/jvm/zulu17/bin/java"),
    (DEBIAN_ALTERNATIVES, ["/usr/lib/jvm/temurin-17-jdk-amd64/bin/java", "/usr/lib/jvm/temurin-17-jdk-amd64/bin/javac"], "17",
     "/usr/lib/jvm/temurin-17-jdk-amd64/bin/java"),
    # OpenJDK: its own java too, and by its path with no file list (Java 8's alternative is its jre/bin/java)
    (RHEL_ALTERNATIVES, ["/usr/lib/jvm/java-11-openjdk-11.0.25.0.9-2.el9.x86_64/bin/java"], "11",
     "/usr/lib/jvm/java-11-openjdk-11.0.25.0.9-2.el9.x86_64/bin/java"),
    (RHEL_ALTERNATIVES, [], "11", "/usr/lib/jvm/java-11-openjdk-11.0.25.0.9-2.el9.x86_64/bin/java"),
    (RHEL_ALTERNATIVES, [], "8", "/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.432.b06-2.el9.x86_64/jre/bin/java"),
    (DEBIAN_ALTERNATIVES, [], "11", "/usr/lib/jvm/java-11-openjdk-amd64/bin/java"),
    # a package java not registered as an alternative, a version no path names: nothing to set
    (DEBIAN_ALTERNATIVES, ["/opt/jdk-17/bin/java"], "17", ""),
    (RHEL_ALTERNATIVES, [], "17", ""),
    ([], [], "17", ""),
])
def test_java_alternative(alternatives, files, version, expected):
    assert cassandra_java_alternative(alternatives, files, version) == expected
