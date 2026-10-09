from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A package installed already is not installed again: no package manager
# call, which reads the metadata of every repository and fails when a mirror
# is down though nothing is to install (add_node on nodes with python3.11
# already there). A package given with its version is never skipped.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")


def tasks(role, name):
    with open(os.path.join(ROLES, role, "tasks", name), encoding="utf-8") as f:
        todo = list(yaml.safe_load(f))
    while todo:
        task = todo.pop(0)
        todo = list(task.get("block", [])) + list(task.get("rescue", [])) + list(task.get("always", [])) + todo
        yield task


def task(role, file, name):
    """The task (not a block of that name)."""
    return next(t for t in tasks(role, file) if t.get("name") == name and "block" not in t)


def runs(role, file, name, **variables):
    found = task(role, file, name)
    conditions = found.get("when", [])
    conditions = conditions if isinstance(conditions, list) else [conditions]
    variables.update(dict((k, v) for k, v in (found.get("vars") or {}).items() if k not in variables))
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(templar.template(trust_as_template("{{ %s }}" % c)) is True or
               str(templar.template(trust_as_template("{{ %s }}" % c))) == "True" for c in conditions)


INSTALL = {"cassandra_install_java": True, "_cassandra_java_home": "", "cassandra_offline": False,
           "cassandra_cqlsh_python_manage": True, "cassandra_install_jemalloc": True, "cassandra_check_mode": False,
           "ansible_check_mode": False, "cassandra_cqlsh_python_repo_uri": "", "cassandra_install_method": "repository",
           "ansible_facts": {"os_family": "RedHat", "distribution": "RedHat"}, "cassandra_package_version": ""}
EL8 = {"python3.11": [{"version": "3.11.13"}], "java-11-openjdk-headless": [{"version": "11.0.25"}],
       "cassandra": [{"version": "4.1.5"}], "cassandra-tools": [{"version": "4.1.5"}], "jemalloc": [{"version": "5.2"}],
       "tar": [{}], "gzip": [{}], "procps-ng": [{}], "python3": [{}], "shadow-utils": [{}]}


@pytest.mark.parametrize("name, extra", [
    ("Install python3.11", {}),
    ("Install Java", {"cassandra_java_package": "java-11-openjdk-headless"}),
    ("Install Cassandra Packages", {"_cassandra_install_names": ["cassandra", "cassandra-tools"],
                                    "_cassandra_install_upgrade": False}),
    ("Look up jemalloc in the enabled repos (RedHat)", {}),
    ("Install jemalloc", {"cassandra_jemalloc_query": {"stdout": "jemalloc-5.2"}}),
])
def test_installed_packages_need_no_package_manager_call(name, extra):
    variables = dict(INSTALL, **extra)
    assert runs("cassandra_install", "install.yml", name, _cassandra_install_before={}, **dict(variables))
    assert not runs("cassandra_install", "install.yml", name, _cassandra_install_before=EL8, **dict(variables))


def test_a_version_given_is_never_skipped():
    variables = dict(INSTALL, _cassandra_install_names=["cassandra-4.1.6", "cassandra-tools-4.1.6"],
                     _cassandra_install_upgrade=False)
    assert runs("cassandra_install", "install.yml", "Install Cassandra Packages", _cassandra_install_before=EL8, **variables)
    variables = dict(INSTALL, cassandra_java_package="java-11-openjdk-headless-11.0.26")
    assert runs("cassandra_install", "install.yml", "Install Java", _cassandra_install_before=EL8, **variables)


def test_the_other_install_tasks():
    for role, file, name, extra in (
            ("cassandra_install", "java_tarball.yml", "Install tar", {}),
            ("cassandra_install", "dsbulk.yml", "Install tar and gzip (unarchive needs them)", {}),
            ("cassandra_install", "packages.yml", "Install what Cassandra needs besides Java (RedHat)", {}),
            ("cassandra_install", "install.yml", "Install what Cassandra needs besides Java", {})):
        assert runs(role, file, name, _cassandra_install_before={}, **dict(INSTALL, **extra)), name
        assert not runs(role, file, name, _cassandra_install_before=EL8, **dict(INSTALL, **extra)), name
    linux = {"cassandra_linux_timesync": True, "cassandra_offline": False, "_cassandra_timesync_pkg": "chrony"}
    assert runs("cassandra_linux", "os.yml", "Install time sync package", ansible_facts={"packages": {}}, **linux)
    assert not runs("cassandra_linux", "os.yml", "Install time sync package", ansible_facts={"packages": {"chrony": []}},
                    **linux)
    firewall = {"cassandra_offline": False, "firewall_packages": ["python-firewall", "firewalld"]}
    assert runs("cassandra_firewall", "firewall.yml", "Ensure firewall package is installed", ansible_facts={"packages": {}},
                **firewall)
    assert runs("cassandra_firewall", "firewall.yml", "Ensure firewall package is installed",
                ansible_facts={"packages": {"firewalld": []}}, **firewall)  # its Python bindings missing
    assert not runs("cassandra_firewall", "firewall.yml", "Ensure firewall package is installed",
                    ansible_facts={"packages": {"firewalld": [], "python3-firewall": []}}, **firewall)
    assert not runs("cassandra_firewall", "firewall.yml", "Ensure firewall package is installed",
                    ansible_facts={"packages": {"ufw": []}}, cassandra_offline=False, firewall_packages=["ufw"])
    medusa = {"cassandra_offline": False, "_cassandra_medusa_python_pending": False,
              "_cassandra_medusa_packages": ["python3.11", "python3.11-pip"]}
    assert runs("cassandra_medusa", "main.yml", "Install them", ansible_facts={"packages": {"python3.11": []}}, **medusa)
    assert not runs("cassandra_medusa", "main.yml", "Install them",
                    ansible_facts={"packages": {"python3.11": [], "python3.11-pip": []}}, **medusa)
