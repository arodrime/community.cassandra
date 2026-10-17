from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Under --check, a cassandra-<series> repository that cassandra_repository
# removes (install method packages, e.g. one left on a plain directory of
# files) is still there: the dnf calls after it leave it out, else they fail.

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

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")


def load(role, name):
    with open(os.path.join(ROLES, role, "tasks", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def tasks(items):
    todo = list(items)
    while todo:
        t = todo.pop(0)
        yield t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])


def task(items, name):
    return next(t for t in tasks(items) if t.get("name") == name)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def removed(results):
    t = task(load("cassandra_repository", "no_repository.yml"), "Under --check, the repositories dnf leaves out")
    return render(t["ansible.builtin.set_fact"]["_cassandra_repository_check_removed"],
                  _cassandra_repository_removed_yum={"results": results})


def test_check_mode_lists_only_the_repositories_the_real_run_removes():
    results = [{"item": "50x", "changed": True}, {"item": "311x", "changed": False}, {"item": "40x", "changed": True}]
    assert removed(results) == ["cassandra-50x", "cassandra-40x"]
    assert removed([{"item": "50x", "changed": False}]) == []
    t = task(load("cassandra_repository", "no_repository.yml"), "Under --check, the repositories dnf leaves out")
    assert "ansible_check_mode" in t["when"]


def test_every_install_task_is_under_the_disablerepo_defaults():
    main = load("cassandra_install", "main.yml")
    assert len(main) == 1 and main[0]["block"][0]["ansible.builtin.import_tasks"] == "install.yml"
    for module in ("ansible.builtin.dnf", "ansible.legacy.dnf"):
        assert main[0]["module_defaults"][module]["disablerepo"] == "{{ _cassandra_repository_check_removed | default([]) }}"
        assert render(main[0]["module_defaults"][module]["disablerepo"]) == []


def test_timesync_package_under_the_disablerepo_defaults():
    t = task(load("cassandra_linux", "timesync.yml"), "Install time sync package")
    for module in ("ansible.builtin.dnf", "ansible.legacy.dnf"):
        assert t["module_defaults"][module]["disablerepo"] == "{{ _cassandra_repository_check_removed | default([]) }}"


def test_jemalloc_query_leaves_them_out():
    argv = task(load("cassandra_install", "install.yml"), "Look up jemalloc in the enabled repos (RedHat)")["ansible.builtin.command"]["argv"]
    assert render(argv) == ["dnf", "-q", "repoquery", "jemalloc"]
    assert render(argv, _cassandra_repository_check_removed=["cassandra-50x"]) == [
        "dnf", "-q", "repoquery", "--disablerepo=cassandra-50x", "jemalloc"]


def test_signing_keys_downloaded_and_read_under_check():
    # --check on a blank host: the keys are still downloaded and checked (a temp file, removed after),
    # their fingerprints read unless gpg is not really installed yet
    keys = load("cassandra_repository", "repository.yml")
    for name in ("Download the signing keys", "Read the fingerprints of the downloaded keys",
                 "Look for gpg (--check)", "Remove the downloaded file"):
        assert task(keys, name)["check_mode"] is False, name
    probe = "_cassandra_repository_gpg_check.rc | default(0) == 0"
    assert task(keys, "Read the fingerprints of the downloaded keys")["when"] == probe
    assert task(keys, "Check the downloaded keys are known ones")["when"] == probe
    assert render("{{ %s }}" % probe, _cassandra_repository_gpg_check={"skipped": True}) is True
    assert render("{{ %s }}" % probe, _cassandra_repository_gpg_check={"rc": 2}) is False


def test_mirror_credentials_not_sent_to_another_host():
    keys = load("cassandra_repository", "repository.yml")
    download = task(keys, "Download the signing keys")
    on_mirror = download["vars"]["_on_mirror"]
    creds = dict(cassandra_install_username="reader", cassandra_install_url="https://mirror.example.com/cassandra/")
    assert render(on_mirror, cassandra_repository_key_url="https://downloads.apache.org/cassandra/KEYS", **creds) is False
    assert render(on_mirror, cassandra_repository_key_url="https://mirror.example.com/keys/KEYS", **creds) is True
    assert render(on_mirror, cassandra_repository_key_url="file:///tmp/KEYS", **creds) is False
    # the mirror's host, but not the mirror: in clear, or another service
    assert render(on_mirror, cassandra_repository_key_url="http://mirror.example.com/keys/KEYS", **creds) is False
    assert render(on_mirror, cassandra_repository_key_url="https://mirror.example.com:8443/keys/KEYS", **creds) is False
    assert render(on_mirror, cassandra_repository_key_url="https://mirror.example.com/keys/KEYS",
                  cassandra_install_username="", cassandra_install_url="https://mirror.example.com/") is False


def test_dsbulk_gets_the_mirror_credentials_on_the_mirror_only():
    dsbulk = load("cassandra_install", "dsbulk.yml")
    on_mirror = task(dsbulk, "Install dsbulk")["vars"]["_on_mirror"]
    creds = dict(cassandra_install_username="reader", cassandra_install_url="https://mirror.example.com/cassandra/")
    assert render(on_mirror, cassandra_dsbulk_url="https://mirror.example.com/dsbulk/dsbulk.tar.gz", **creds) is True
    for url in ("https://github.com/datastax/dsbulk/releases/download/1.11.0/dsbulk-1.11.0.tar.gz",
                "http://mirror.example.com/dsbulk/dsbulk.tar.gz", "https://mirror.example.com:8443/dsbulk/dsbulk.tar.gz",
                "file:///tmp/dsbulk.tar.gz"):
        assert render(on_mirror, cassandra_dsbulk_url=url, **creds) is False, url
