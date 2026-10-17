from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install's defaults pick the Java tarball of cassandra_java_version
# in cassandra_java_tarballs; an explicit cassandra_java_tarball wins.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template


load_collection_filters()

DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_install", "defaults", "main.yml")
MIRROR = {
    "11": {"url": "https://mirror.example.com/java/jdk-11.tar.gz", "checksum": "sha256:11"},
    "17": {"url": "https://mirror.example.com/java/jdk-17.tar.gz", "checksum": "sha256:17"},
    "21": {"url": "https://other.example.com/jdk-21.tar.gz", "checksum": "sha256:21", "username": "u21", "password": "p21"},
}


def resolve(**inventory):
    with open(DEFAULTS) as f:
        variables = yaml.safe_load(f)
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    variables.update({
        "ansible_facts": {"os_family": "Debian", "distribution": "Ubuntu"},
        "cassandra_install_username": "mirror-user", "cassandra_install_password": "mirror-pass",
        "cassandra_install_url": "https://mirror.example.com/cassandra/",
    })
    variables.update(inventory)
    templar = Templar(loader=DataLoader(), variables=variables)
    names = ["cassandra_java_tarball", "cassandra_java_tarball_checksum",
             "cassandra_java_tarball_username", "cassandra_java_tarball_password"]
    return dict((n, templar.template(trust_as_template("{{ %s }}" % n))) for n in names)


def test_cluster_picks_its_major():
    assert resolve(cassandra_java_tarballs=MIRROR, cassandra_java_version="17") == {
        "cassandra_java_tarball": "https://mirror.example.com/java/jdk-17.tar.gz",
        "cassandra_java_tarball_checksum": "sha256:17",
        # same host as the mirror: the mirror's credentials
        "cassandra_java_tarball_username": "mirror-user", "cassandra_java_tarball_password": "mirror-pass"}


def test_entry_credentials_win_over_the_mirror():
    mirror = {"17": dict(MIRROR["17"], username="java", password="jpass")}
    resolved = resolve(cassandra_java_tarballs=mirror, cassandra_java_version="17")
    assert (resolved["cassandra_java_tarball_username"], resolved["cassandra_java_tarball_password"]) == ("java", "jpass")


def test_entry_elsewhere_gets_its_own_credentials_only():
    resolved = resolve(cassandra_java_tarballs=MIRROR, cassandra_java_version="21")
    assert resolved["cassandra_java_tarball"] == "https://other.example.com/jdk-21.tar.gz"
    assert (resolved["cassandra_java_tarball_username"], resolved["cassandra_java_tarball_password"]) == ("u21", "p21")


def test_entry_elsewhere_without_credentials_gets_none():
    mirror = {"17": {"url": "https://other.example.com/jdk-17.tar.gz"}}
    resolved = resolve(cassandra_java_tarballs=mirror, cassandra_java_version="17")
    assert (resolved["cassandra_java_tarball_username"], resolved["cassandra_java_tarball_password"]) == ("", "")


def test_explicit_tarball_wins_without_the_entry_checksum():
    resolved = resolve(cassandra_java_tarballs=MIRROR, cassandra_java_version="17",
                       cassandra_java_tarball="https://elsewhere.example.com/jdk-17.0.1.tar.gz")
    assert resolved["cassandra_java_tarball"] == "https://elsewhere.example.com/jdk-17.0.1.tar.gz"
    assert resolved["cassandra_java_tarball_checksum"] == ""
    assert resolved["cassandra_java_tarball_username"] == ""


def test_no_entry_no_tarball():
    assert resolve(cassandra_java_tarballs=MIRROR, cassandra_java_version="8")["cassandra_java_tarball"] == ""
    assert resolve(cassandra_java_version="17")["cassandra_java_tarball"] == ""


def test_install_java_false_takes_no_tarball():
    resolved = resolve(cassandra_java_tarballs=MIRROR, cassandra_java_version="17", cassandra_install_java=False)
    assert resolved["cassandra_java_tarball"] == ""


def test_entry_with_a_user_only_gets_no_mirror_password():
    mirror = {"17": {"url": "https://mirror.example.com/java/jdk-17.tar.gz", "username": "java"}}
    resolved = resolve(cassandra_java_tarballs=mirror, cassandra_java_version="17")
    assert (resolved["cassandra_java_tarball_username"], resolved["cassandra_java_tarball_password"]) == ("java", "")
