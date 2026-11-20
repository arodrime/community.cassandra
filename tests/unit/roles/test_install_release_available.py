from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install, another release of an installed node (upgrade, apply_config): the release looked for in the
# repository first; missing, a stop with the package manager's words and the way out, before any change (else dnf
# or apt failed later, under --check at "Install Cassandra Packages", its reason dropped).

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import find, load, render

TASKS = load("roles", "cassandra_install", "tasks", "cassandra.yml")
LOOK = find(TASKS, "Look for the release in the repository")
STOP = find(TASKS, "Stop when the release is not in the repository")
BASE = {"_cassandra_install_upgrade": True, "cassandra_package_version": "4.1.12", "cassandra_offline": False,
        "cassandra_install_method": "repository", "_cassandra_install_before": {"cassandra": [{"version": "4.1.11"}]},
        "ansible_facts": {"os_family": "RedHat"}, "inventory_hostname": "n1"}


@pytest.mark.parametrize("changes, looked", [
    ({}, True),
    ({"_cassandra_install_before": {"cassandra": [{"version": "4.1.12"}]}}, False),  # installed already
    ({"_cassandra_install_before": {"cassandra": [{"version": "4.1.12-1"}]}}, False),
    ({"_cassandra_install_upgrade": False}, False),  # not the version's change: refused elsewhere
    ({"_cassandra_install_before": {}}, False),  # a new node
    ({"cassandra_offline": True}, False),
    ({"cassandra_install_method": "packages"}, False),
])
def test_the_release_is_looked_for_on_a_change_of_release(changes, looked):
    variables = dict(BASE, **changes)
    assert all(render("{{ %s }}" % w, variables) for w in LOOK["when"]) is looked
    assert LOOK["check_mode"] is False and LOOK["failed_when"] is False  # (in the plan, under --check)


def test_a_missing_release_stops_with_the_package_manager_s_words():
    said = " ".join(render(STOP["ansible.builtin.fail"]["msg"], dict(BASE, cassandra_install_release={
        "rc": 1, "stdout_lines": ["Error: No matching Packages to list"]})).split())
    assert said == ("n1: Cassandra 4.1.12 is not in the repository the node reads (Error: No matching Packages to"
                    " list). Nothing was changed. Set cassandra_package_version to a release it has (dnf"
                    " --showduplicates list cassandra lists them there), or add that release to the repository, then"
                    " run again.")
    assert "cassandra_install_release.rc | default(0) != 0" in STOP["when"]
    names = [t.get("name") for t in TASKS]
    assert names.index("Stop when the release is not in the repository") < names.index("Install Cassandra Packages")


def test_dnf_reads_the_role_s_repository_alone():
    shell = render(LOOK["ansible.builtin.shell"], dict(BASE, _own_repo=True, cassandra_version="41x"))
    assert "--disablerepo='*' --enablerepo=cassandra-41x cassandra-4.1.12" in " ".join(shell.split())
    shell = render(LOOK["ansible.builtin.shell"], dict(BASE, _own_repo=False, cassandra_version="41x"))
    assert "--enablerepo" not in shell
