from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install's defaults pick the Java tarball of cassandra_java_version
# in cassandra_java_tarballs; an explicit cassandra_java_tarball wins.

import os
import re

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


def templar_with(**inventory):
    with open(DEFAULTS) as f:
        variables = yaml.safe_load(f)
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    variables.update({
        "ansible_facts": {"os_family": "Debian", "distribution": "Ubuntu"},
        "cassandra_install_username": "mirror-user", "cassandra_install_password": "mirror-pass",
        "cassandra_install_url": "https://mirror.example.com/cassandra/",
    })
    variables.update(inventory)
    return Templar(loader=DataLoader(), variables=variables)


def resolve(**inventory):
    templar = templar_with(**inventory)
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


def test_tarball_over_http_or_another_port_gets_no_mirror_credentials():
    for url in ("http://mirror.example.com/java/jdk-17.tar.gz", "https://mirror.example.com:8443/java/jdk-17.tar.gz"):
        resolved = resolve(cassandra_java_tarball=url)
        assert (resolved["cassandra_java_tarball_username"], resolved["cassandra_java_tarball_password"]) == ("", ""), url


def test_tarball_url_without_a_file_name_is_refused():
    with open(os.path.join(os.path.dirname(DEFAULTS), "..", "tasks", "java_check.yml")) as f:
        check = next(t for t in yaml.safe_load(f) if t["name"] == "The tarball has a file name")
    that = trust_as_template("{{ %s }}" % check["ansible.builtin.assert"]["that"])
    for url in ("https://mirror.example.com/java/", "https://mirror.example.com/java/..", "https://mirror.example.com/.",
                "https://mirror.example.com/java/.tar.gz", "https://mirror.example.com/java/..tgz", "/tmp/.tar"):
        assert templar_with(cassandra_java_tarball=url).template(that) is False, url
    assert templar_with(cassandra_java_tarball="https://mirror.example.com/java/jdk-17.tar.gz").template(that) is True
    assert templar_with(cassandra_java_tarball="").template(that) is True


def test_an_existing_java_directory_is_emptied_only_when_this_role_made_it():
    # in java_check.yml: install, update_java, upgrade and the new node checks refuse before anything changes
    with open(os.path.join(os.path.dirname(DEFAULTS), "..", "tasks", "java_check.yml")) as f:
        tasks = yaml.safe_load(f)
    check = next(t for t in tasks if t["name"] == "The Java directory is new, empty or this role's")
    # for any Java directory, cassandra_java_home or one under cassandra_java_tarball_dir
    assert check["when"] == "cassandra_java_tarball != ''"
    for name in ("Look for the Java directory", "Look for the unpacked Java", "Look for files in the Java directory",
                 "Look for an unpack left half done"):
        assert not re.search(r"\bcassandra_java_home !=", str(next(t for t in tasks if t["name"] == name).get("when"))), name
    that = trust_as_template("{{ %s }}" % check["ansible.builtin.assert"]["that"])

    def allowed(exists=True, isdir=True, java=None, started=False, done=False, files=None):
        variables = dict(cassandra_java_check_dir={"stat": {"exists": exists, "isdir": isdir} if exists else {"exists": False}},
                         cassandra_java_check_unpacking={"stat": {"exists": started}},
                         cassandra_java_check_unpacked={"stat": {"exists": done}})
        if java is not None:  # stat only with cassandra_java_home
            variables["cassandra_java_check_java"] = {"stat": {"exists": java}}
        if files is not None:  # find only on a directory
            variables["cassandra_java_check_files"] = {"matched": files}
        return templar_with(**variables).template(that)
    assert allowed(exists=False) is True  # a new directory
    assert allowed(files=0) is True  # empty: a run stopped right after making it
    assert allowed(files=3) is False  # e.g. /opt, or someone else's Java under cassandra_java_tarball_dir
    assert allowed(java=False, files=3) is False  # cassandra_java_home without bin/java
    assert allowed(java=True, files=3) is True  # cassandra_java_home with a Java: kept, not unpacked over
    assert allowed(started=True, files=3) is True  # this role's half-done unpack: redone
    assert allowed(done=True, files=3) is True  # unpacked: nothing to do
    assert allowed(isdir=False) is False  # a file in its place
    assert allowed(isdir=False, java=True) is True  # cassandra_java_home a link to a Java (stat does not follow it)
    look = next(t for t in tasks if t["name"] == "Look for files in the Java directory")
    assert look["ansible.builtin.find"]["hidden"] is True  # a directory of dotfiles is not empty
    assert look["check_mode"] is False  # read under --check too, else the guard passes
