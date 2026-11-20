from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install, another release of an installed node (upgrade, apply_config): the release looked for in the
# repository first; missing, a stop with the package manager's words and the way out, before any change (else dnf
# or apt failed later, under --check at "Install Cassandra Packages", its reason dropped).

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    find, load, render, with_task_vars)

TASKS = load("roles", "cassandra_install", "tasks", "cassandra.yml")
LOOK = find(TASKS, "Look for the release in the repository")
STOP = find(TASKS, "Stop when the release is not in the repository")
BASE = {"_cassandra_install_upgrade": True, "_cassandra_install_check": False, "cassandra_package_version": "4.1.12", "cassandra_offline": False,
        "cassandra_install_method": "repository", "_cassandra_install_before": {"cassandra": [{"version": "4.1.11"}]},
        "ansible_facts": {"os_family": "RedHat"}, "inventory_hostname": "n1", "_cassandra_java_home": ""}


@pytest.mark.parametrize("changes, looked", [
    ({}, True),
    ({"_cassandra_install_before": {"cassandra": [{"version": "4.1.12"}]}}, False),  # installed already
    ({"_cassandra_install_before": {"cassandra": [{"version": "4.1.12-1"}]}}, False),
    ({"_cassandra_install_upgrade": False}, False),  # not the version's change: refused elsewhere
    ({"_cassandra_install_before": {}}, False),  # a new node
    ({"cassandra_offline": True}, False),
    ({"cassandra_install_method": "packages"}, False),
    # the plan, this run changing the repository: not judged on the one there now (the window reads the new one)
    ({"_cassandra_install_check": True, "_cassandra_repository_items": [{"item": "baseurl"}]}, False),
    ({"_cassandra_install_check": True, "_cassandra_repository_items": []}, True),
])
def test_the_release_is_looked_for_on_a_change_of_release(changes, looked):
    variables = dict(BASE, **changes)
    assert all(render("{{ %s }}" % w, variables) for w in LOOK["when"]) is looked
    assert LOOK["check_mode"] is False and LOOK["failed_when"] is False  # (in the plan, under --check)


def _said(**variables):
    return render(STOP["ansible.builtin.fail"]["msg"], with_task_vars(STOP, dict(BASE, **variables))).split("\n")


def test_a_missing_release_stops_with_the_package_manager_s_words():
    # one fact per line; only what is sure: the package unchanged (the window, the upgrade stopped Cassandra and may
    # have written the repository before: never "nothing was changed")
    assert _said(cassandra_install_release={"rc": 1, "stdout": "Error: No matching Packages to list",
                                            "stdout_lines": ["Error: No matching Packages to list"]}) == [
        "n1: dnf did not list Cassandra 4.1.12: Error: No matching Packages to list.",
        "Its Cassandra package was not changed.",
        "Way out: set cassandra_package_version to a release it lists (dnf --showduplicates list cassandra there), or"
        " add that release to the repository, then run again."]
    assert "cassandra_install_release.rc | default(0) != 0" in STOP["when"]
    names = [t.get("name") for t in TASKS]
    assert names.index("Stop when the release is not in the repository") < names.index("Install Cassandra Packages")
    # the repository file written by this run (package_update: cassandra_repository first): said
    said = _said(cassandra_install_release={"rc": 1, "stdout_lines": []}, _cassandra_repository_items=[{"item": "baseurl"}])
    assert said[:2] == ["n1: dnf did not list Cassandra 4.1.12: nothing listed.",
                        "Its Cassandra package was not changed; its repository file was written by this run."]


def test_a_repository_dnf_cannot_read_is_not_said_missing_the_release():
    out = ("Error: Failed to download metadata for repo 'cassandra-41x': Cannot download repomd.xml: Curl error (7):"
           " Couldn't connect to server")
    said = _said(cassandra_install_release={"rc": 1, "stdout": out, "stdout_lines": [out]})
    assert said == ["n1: dnf did not list Cassandra 4.1.12 (it could not read a repository): " + out + ".",
                    "Its Cassandra package was not changed.",
                    "Way out: make that repository answer on the node, then run again."]


def test_the_check_honours_the_excludes_the_install_honours():
    # (versionlock or excludepkgs on cassandra: the package module refuses the release; dnf download, with
    # cassandra_java_home, ignores them)
    shell = " ".join(render(LOOK["ansible.builtin.shell"], dict(BASE, _own_repo=False, cassandra_version="41x")).split())
    assert "--disableexcludes" not in shell
    shell = render(LOOK["ansible.builtin.shell"], dict(BASE, _own_repo=False, cassandra_version="41x",
                                                       _cassandra_java_home="/opt/x/jdk"))
    assert "--disableexcludes=all" in shell


def test_dnf_reads_the_role_s_repository_alone():
    shell = render(LOOK["ansible.builtin.shell"], dict(BASE, _own_repo=True, cassandra_version="41x"))
    assert "--disablerepo='*' --enablerepo=cassandra-41x cassandra-4.1.12" in " ".join(shell.split())
    shell = render(LOOK["ansible.builtin.shell"], dict(BASE, _own_repo=False, cassandra_version="41x"))
    assert "--enablerepo" not in shell
