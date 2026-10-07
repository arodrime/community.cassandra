from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: cassandra_repository takes over a node's Cassandra repository
# files only when it would write them as they are, with their URL, credentials
# and key path; else they are left as they are (cassandra_repository_manage: false).

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_repository_import import (
    DEB_KEY, DEB_URL, RPM_KEY, RPM_URL, YUM_KEYS, cassandra_repository_import)

KEYS = "k" * 40  # sha1 of the role's KEYS
REPO = "/etc/yum.repos.d/cassandra-50x.repo"
YUM = ("[cassandra-50x]\nbaseurl = https://mirror.example/rpm/50x/\ngpgcheck = 1\n"
       "gpgkey = file:///etc/pki/rpm-gpg/apache-cassandra.asc\nname = Official Cassandra 50x yum repo\n"
       "password = tok\nusername = svc\n")


def yum(text=YUM, mode="0600", key=KEYS, files=None, from_file=False, key_mode="0644"):
    stats = {REPO: {"mode": mode, "checksum": "x"}, "/etc/pki/rpm-gpg/apache-cassandra.asc": {"checksum": key, "mode": key_mode}}
    return cassandra_repository_import(files or {REPO: text}, stats, "50x", "RedHat", KEYS, {}, from_file)


def test_role_written_mirror_taken_over():
    assert yum() == {"manage": True, "why": "", "vars": {
        "cassandra_install_url": "https://mirror.example/rpm/50x/", "cassandra_install_username": "svc",
        "cassandra_install_password": "tok"}}


def test_apache_defaults_need_no_variable():
    text = ("[cassandra-50x]\nbaseurl = https://redhat.cassandra.apache.org/50x/\ngpgcheck = 1\n"
            "gpgkey = file:///etc/pki/rpm-gpg/apache-cassandra.asc\nname = Official Cassandra 50x yum repo\n")
    assert yum(text, mode="0644") == {"manage": True, "vars": {}, "why": ""}


@pytest.mark.parametrize("change", [
    lambda t: t + "sslverify = 0\n",  # a key the role does not write
    lambda t: t.replace("Official Cassandra 50x yum repo", "Cassandra mirror"),  # written by hand
    lambda t: t.replace("gpgcheck = 1", "gpgcheck = 0"),
    lambda t: t.replace("password = tok\n", ""),
    lambda t: t + "[other]\nbaseurl = x\n",
])
def test_other_files_left_as_they_are(change):
    out = yum(change(YUM))
    assert out["manage"] is False and out["vars"] == {} and out["why"]


def test_mode_and_keys():
    assert yum(mode="0644")["manage"] is False  # the role writes a file with a password 0600
    assert yum(key="other")["manage"] is False  # its key file would be replaced by the role's
    assert yum(key_mode="0600")["manage"] is False  # the role's copy is 0644


def test_other_series_or_package_from_a_file():
    assert yum(files={REPO: YUM, "/etc/yum.repos.d/cassandra-41x.repo": YUM})["manage"] is False  # the role removes it
    assert yum(from_file=True)["manage"] is False  # the role removes the repository for package files


SRC = "/etc/apt/sources.list.d/cassandra-50x.sources"
AUTH = "/etc/apt/auth.conf.d/cassandra.conf"
# deb822_repository's own output: fields sorted by option name
DEB = ("Components: main\nX-Repolib-Name: cassandra-50x\nSigned-By: /etc/apt/keyrings/apache-cassandra.asc\nSuites: 50x\n"
       "Types: deb\nURIs: https://mirror.example/deb\n")
MARKED = "# Managed by Ansible (community.cassandra.cassandra_repository)\nmachine mirror.example login svc password tok\n"
DEBIAN_PACKAGES = ["apt-transport-https", "curl", "gnupg", "python3-debian"]


def apt(files, packages=tuple(DEBIAN_PACKAGES), auth_mode="0600", auth_uid=0):
    stats = {SRC: {"mode": "0644"}, AUTH: {"mode": auth_mode, "uid": auth_uid, "gid": 0},
             "/etc/apt/keyrings/apache-cassandra.asc": {"checksum": KEYS, "mode": "0644"}}
    return cassandra_repository_import(files, stats, "50x", "Debian", KEYS, dict((p, []) for p in packages), False,
                                       DEBIAN_PACKAGES)


def test_apt_mirror_with_the_role_credentials():
    assert apt({SRC: DEB, AUTH: MARKED})["vars"] == {
        "cassandra_install_url": "https://mirror.example/deb", "cassandra_install_username": "svc",
        "cassandra_install_password": "tok"}


def test_apt_credentials_not_the_roles_left_as_they_are():
    # the role would remove or rewrite an auth.conf it did not write
    out = apt({SRC: DEB, AUTH: "machine mirror.example login svc password tok\n"})
    assert out["manage"] is False and AUTH in out["why"]


def test_apt_file_not_the_modules_own_output():
    # deb822_repository rewrites a file that is not its output byte for byte
    assert apt({SRC: "X-Repolib-Name: cassandra-50x\n" + DEB.replace("X-Repolib-Name: cassandra-50x\n", "")})["manage"] is False
    assert apt({SRC: "# mirror\n" + DEB})["manage"] is False
    assert apt({SRC: DEB.replace("/deb\n", "/deb/\n")})["vars"]["cassandra_install_url"] == "https://mirror.example/deb/"


def test_apt_credentials_written_another_way():
    assert apt({SRC: DEB, AUTH: MARKED.replace("login svc", "login   svc")})["manage"] is False
    assert apt({SRC: DEB, AUTH: MARKED}, auth_uid=1000)["manage"] is False  # the role writes it root:root


def test_apt_credentials_for_another_host():
    out = apt({SRC: DEB, AUTH: MARKED.replace("mirror.example", "other.example")})
    assert out["manage"] is False and "other.example" in out["why"]


def test_apt_others():
    assert apt({SRC: DEB, "/etc/apt/sources.list.d/cassandra-50x.list": "deb x 50x main\n"})["manage"] is False
    assert apt({SRC: DEB}, packages=["curl"])["manage"] is False  # the role would install the others
    assert apt({SRC: DEB.replace("Suites: 50x", "Suites: 41x")})["manage"] is False
    assert apt({SRC: DEB})["vars"] == {"cassandra_install_url": "https://mirror.example/deb"}


def test_no_files():
    assert cassandra_repository_import({}, {}, "50x", "RedHat", KEYS)["manage"] is True


def test_what_the_role_writes():
    # the filter's idea of the role's files follows the role
    role = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_repository")
    with open(os.path.join(role, "defaults", "main.yml")) as fh:
        defaults = yaml.safe_load(fh)
    assert defaults["cassandra_repository_deb_url"] == DEB_URL
    assert defaults["cassandra_repository_rpm_url"] == RPM_URL.replace("%s", "{{ cassandra_version }}")
    assert (defaults["cassandra_rpm_key_path"], defaults["cassandra_apt_keyring_path"]) == (RPM_KEY, DEB_KEY)
    with open(os.path.join(role, "tasks", "repository.yml")) as fh:
        tasks = yaml.safe_load(fh)
    yum = next(t["ansible.builtin.yum_repository"] for t in tasks if "ansible.builtin.yum_repository" in t
               and t["ansible.builtin.yum_repository"].get("baseurl"))
    assert yum["description"] == "Official Cassandra {{ cassandra_version }} yum repo"
    assert set(k.replace("description", "name") for k in yum if k not in ("mode",)) == YUM_KEYS
    deb = next(t["ansible.builtin.deb822_repository"] for t in tasks if "ansible.builtin.deb822_repository" in t)
    assert sorted(deb) == ["components", "name", "signed_by", "suites", "types", "uris"]  # DEB_FILE's fields
    with open(os.path.join(role, "tasks", "repository.yml")) as fh:
        assert "# Managed by Ansible (community.cassandra.cassandra_repository)\n      machine" in fh.read()
