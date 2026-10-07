import hashlib
import os

import pytest

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')


def include_vars(host):
    ansible = host.ansible('include_vars',
                           'file="../../defaults/main.yml"',
                           False,
                           False)
    return ansible


def get_cassandra_version(host):
    return include_vars(host)['ansible_facts']['cassandra_version']


def get_cassandra_apt_keyring_path(host):
    return include_vars(host)['ansible_facts']['cassandra_apt_keyring_path']


@pytest.fixture(scope="module")
def os_family(host):
    return host.ansible("setup", "filter=ansible_os_family")["ansible_facts"]["ansible_os_family"]


@pytest.fixture
def redhat_only(os_family):
    if os_family != "RedHat":
        pytest.skip("RedHat family only")


@pytest.fixture
def debian_only(os_family):
    if os_family != "Debian":
        pytest.skip("Debian family only")


@pytest.mark.usefixtures("redhat_only")
def test_redhat_cassandra_repository_file(host):
    cassandra_version = get_cassandra_version(host)
    f = host.file("/etc/yum.repos.d/cassandra-{0}.repo".format(cassandra_version))
    assert f.exists
    assert f.user == 'root'
    assert f.group == 'root'
    assert f.mode == 0o644
    assert "gpgkey = file:///etc/pki/rpm-gpg/apache-cassandra.asc" in f.content_string


@pytest.mark.usefixtures("redhat_only")
def test_redhat_yum_search(host):
    cassandra_version = get_cassandra_version(host)
    cmd = host.run("yum search cassandra --disablerepo='*' \
                        --enablerepo='cassandra-{0}'".format(cassandra_version))

    assert cmd.rc == 0
    assert "cassandra" in cmd.stdout


def test_signing_keys_shipped_with_the_role(host, os_family):
    path = "/etc/pki/rpm-gpg/apache-cassandra.asc" if os_family == "RedHat" \
        else get_cassandra_apt_keyring_path(host)
    f = host.file(path)
    assert f.exists
    assert f.mode == 0o644
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "files", "KEYS"), "rb") as shipped:
        assert f.sha256sum == hashlib.sha256(shipped.read()).hexdigest()


@pytest.mark.usefixtures("debian_only")
def test_debian_cassandra_repository_file(host):
    cassandra_version = get_cassandra_version(host)
    keyring_path = get_cassandra_apt_keyring_path(host)
    assert not host.file("/etc/apt/sources.list.d/cassandra-{0}.list".format(cassandra_version)).exists
    f = host.file("/etc/apt/sources.list.d/cassandra-{0}.sources".format(cassandra_version))

    assert f.exists
    assert f.user == 'root'
    assert f.group == 'root'
    assert f.mode == 0o644
    assert "URIs: https://debian.cassandra.apache.org" in f.content_string
    assert "Suites: {0}".format(cassandra_version) in f.content_string
    assert "Signed-By: {0}".format(keyring_path) in f.content_string


@pytest.mark.usefixtures("debian_only")
def test_debian_apt_search(host):
    cmd = host.run("apt-cache policy cassandra")

    assert cmd.rc == 0
    assert "debian.cassandra.apache.org" in cmd.stdout


def test_only_the_current_series_repository(host, os_family):
    cassandra_version = get_cassandra_version(host)
    for series in ("311x", "40x", "41x", "50x"):
        if series == cassandra_version:
            continue
        if os_family == "RedHat":
            assert not host.file("/etc/yum.repos.d/cassandra-{0}.repo".format(series)).exists
        else:
            assert not host.file("/etc/apt/sources.list.d/cassandra-{0}.sources".format(series)).exists


def test_no_credentials_left(host, os_family):
    # the converge ends without credentials: the ones of its first run are gone
    if os_family == "Debian":
        assert not host.file("/etc/apt/auth.conf.d/cassandra.conf").exists
    else:
        f = host.file("/etc/yum.repos.d/cassandra-{0}.repo".format(get_cassandra_version(host)))
        assert f.mode == 0o644
        assert "password" not in f.content_string
