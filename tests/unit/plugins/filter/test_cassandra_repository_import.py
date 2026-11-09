from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: cassandra_repository takes over a node's Cassandra repository
# files only when it would write them as they are, with their URL, credentials
# and key path; else they are left as they are (cassandra_repository_manage: false).

import os

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_repository_import import (
    DEB_KEY, DEB_URL, RPM_KEY, RPM_URL, YUM_KEYS, cassandra_repository_import)

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

KEYS = "k" * 40  # sha1 of the role's KEYS
REPO = "/etc/yum.repos.d/cassandra-50x.repo"
YUM = ("[cassandra-50x]\nbaseurl = https://mirror.example/rpm/50x/\ngpgcheck = 1\n"
       "gpgkey = file:///etc/pki/rpm-gpg/apache-cassandra.asc\nname = Official Cassandra 50x yum repo\n"
       "password = tok\nusername = svc\n")


def yum(text=YUM, mode="0600", key=KEYS, files=None, from_file=False, key_mode="0644"):
    stats = {REPO: {"mode": mode, "checksum": "x"}, "/etc/pki/rpm-gpg/apache-cassandra.asc": {"checksum": key, "mode": key_mode}}
    return cassandra_repository_import(files or {REPO: text}, stats, "50x", "RedHat", KEYS, {}, from_file)


def test_role_written_mirror_taken_over():
    # the series at the end of the URL: as cassandra_version, so the next upgrade takes the next series' repository
    assert yum() == {"manage": True, "why": "", "vars": {
        "cassandra_install_url": "https://mirror.example/rpm/{cassandra_version}/", "cassandra_install_username": "svc",
        "cassandra_install_password": "tok"}}


@pytest.mark.parametrize("url, written", [
    ("http://mirror/repo/50x", "http://mirror/repo/{cassandra_version}"),  # no trailing slash: kept so
    ("http://mirror/repo/50x/os/", "http://mirror/repo/50x/os/"),  # not at the end: as it is
    ("http://mirror/repo/cassandra-50x/", "http://mirror/repo/cassandra-50x/"),  # part of a name: as it is
    ("http://mirror/50x/repo/", "http://mirror/50x/repo/"),
])
def test_series_in_the_url(url, written):
    out = yum(YUM.replace("https://mirror.example/rpm/50x/", url))
    assert out["vars"]["cassandra_install_url"] == written
    # written by the import as a template (not !unsafe), rendered with the series it was read with: the node's URL
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _vars_yaml
    text = _vars_yaml({"cassandra_install_url": written})
    assert "!unsafe" not in text
    value = yaml.safe_load(text)["cassandra_install_url"]
    templar = Templar(loader=DataLoader(), variables={"cassandra_version": "50x"})
    assert templar.template(trust_as_template(value)) == url


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


def test_apt_credentials_of_the_role_for_any_mirror_url():
    # the machine as the role writes it: http:// for http, the host as written, the port, no user@
    for uri, machine in (("http://mirror.example/deb", "http://mirror.example"),
                         ("https://mirror.example:8443/deb", "mirror.example:8443"),
                         ("https://olduser@Mirror.example/deb", "Mirror.example"),
                         ("https://[fd00::1]:8443/deb", "fd00::1:8443")):
        out = apt({SRC: DEB.replace("https://mirror.example/deb", uri), AUTH: MARKED.replace("mirror.example", machine)})
        assert out["manage"] is True, uri
        assert out["vars"]["cassandra_install_username"] == "svc", uri
    # the role would rewrite one for the whole netloc
    out = apt({SRC: DEB.replace("https://mirror.example/deb", "https://u@mirror.example/deb"),
               AUTH: MARKED.replace("mirror.example", "u@mirror.example")})
    assert out["manage"] is False


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
    url = trust_as_template(defaults["cassandra_install_url"])
    assert Templar(loader=DataLoader(), variables={"ansible_facts": {"os_family": "Debian"}}).template(url) == DEB_URL
    assert Templar(loader=DataLoader(), variables={"ansible_facts": {"os_family": "RedHat"}, "cassandra_version": "41x"}
                   ).template(url) == RPM_URL % "41x"
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


def test_apt_series_in_the_url():
    # apt names the series in Suites; a mirror that also has it at the end of its URL gets cassandra_version there
    out = apt({SRC: DEB.replace("https://mirror.example/deb\n", "https://mirror.example/deb/50x/\n")})
    assert out["manage"] is True and out["vars"]["cassandra_install_url"] == "https://mirror.example/deb/{cassandra_version}/"
    # the import's self-check does not compare it with the node (the template is the user's way to write it)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import NOT_READ
    assert "cassandra_install_url" in NOT_READ


def test_a_url_with_a_template_read_from_a_node_stays_unsafe():
    # a "{{" read from a node is never templated: only the import's own series token becomes one
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _vars_yaml
    assert "!unsafe" in _vars_yaml({"cassandra_install_url": "http://m/{{ lookup('pipe', 'id') }}/"})
