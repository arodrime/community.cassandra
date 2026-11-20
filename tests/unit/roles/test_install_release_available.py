from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install, another release of an installed node (upgrade, apply_config): the release looked for in the
# repository first; missing, a stop with the package manager's words and the way out, before any change (else dnf
# or apt failed later, under --check at "Install Cassandra Packages", its reason dropped).

import subprocess

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    find, load, render, with_task_vars)

TASKS = load("roles", "cassandra_install", "tasks", "release_check.yml")
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
    # (cassandra.yml runs them before the install)
    names = [t.get("name") for t in load("roles", "cassandra_install", "tasks", "cassandra.yml")]
    assert names.index("Check the release is in the repository") < names.index("Install Cassandra Packages")
    assert find(load("roles", "cassandra_install", "tasks", "cassandra.yml"), "Check the release is in the repository")[
        "ansible.builtin.import_tasks"] == "release_check.yml"
    # the repository file written by this run (package_update: cassandra_repository first): said
    said = _said(cassandra_install_release={"rc": 1, "stdout_lines": []}, _cassandra_repository_items=[{"item": "baseurl"}])
    assert said[:2] == ["n1: dnf did not list Cassandra 4.1.12: nothing listed.",
                        "Its Cassandra package was not changed; its repository file was written by this run."]


def test_apply_config_s_plan_refuses_a_missing_release_in_the_block_of_its_stops():
    # e2e: apply_config --check said dnf's words as a bare "fatal" per node, its other stops a REFUSED block
    plan = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Find what each node needs"][0]
    compare = find(plan["tasks"], "Compare the repository, the packages and the firewall")
    head = render(compare["vars"]["_cassandra_install_refused_head"], {"hostvars": {"localhost": {
        "_cassandra_cluster_shown": "zorglub"}}})
    assert head == "apply_config  zorglub"
    listed = {"rc": 1, "stdout": "Error: No matching Packages to list", "stdout_lines": ["Error: No matching Packages to list"]}
    said = _said(cassandra_install_release=listed, _cassandra_install_refused_head=head, _cassandra_install_check=True)
    assert said[:2] == ["REFUSED  apply_config  zorglub  nothing was changed",
                        "  n1: dnf did not list Cassandra 4.1.12: Error: No matching Packages to list."]
    assert " ".join(x.strip() for x in said[2:]) == (
        "Way out: set cassandra_package_version to a release it lists (dnf --showduplicates list cassandra there), or"
        " add that release to the repository, then run again.")
    assert all(len(x) <= 79 for x in said)
    # its repository written by this run (not the plan's: the window's), or no head given: the plain lines
    assert _said(cassandra_install_release=listed, _cassandra_install_refused_head=head,
                 _cassandra_repository_items=[{"item": "baseurl"}])[0].startswith("n1: dnf did not list")
    assert _said(cassandra_install_release=listed)[0].startswith("n1: dnf did not list")


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
    def shell(**more):
        variables = with_task_vars(LOOK, dict(BASE, cassandra_repository_manage=False, cassandra_version="41x", **more))
        return " ".join(render(LOOK["ansible.builtin.shell"], variables).split())

    # (the list as the install sees it first; the exclusions lifted only to tell an excluded release apart)
    assert shell().startswith("dnf -q --showduplicates list available cassandra-4.1.12 2>&1 ||")
    assert shell(_cassandra_java_home="/opt/x/jdk") == \
        "dnf -q --showduplicates list available --disableexcludes=all cassandra-4.1.12 2>&1"


def test_dnf_reads_the_role_s_repository_alone():
    variables = dict(BASE, cassandra_repository_manage=True, cassandra_version="41x")
    shell = render(LOOK["ansible.builtin.shell"], with_task_vars(LOOK, variables))
    assert "--disablerepo='*' --enablerepo=cassandra-41x cassandra-4.1.12" in " ".join(shell.split())
    variables = dict(BASE, cassandra_repository_manage=False, cassandra_version="41x")
    shell = render(LOOK["ansible.builtin.shell"], with_task_vars(LOOK, variables))
    assert "--enablerepo" not in shell


def _listed(tmp_path, excluded, **variables):
    """The look-up's shell run against a dnf that has cassandra-4.1.12, behind an exclusion or not."""
    variables = with_task_vars(LOOK, dict(BASE, cassandra_version="41x", cassandra_repository_manage=True, **variables))
    shell = render(LOOK["ansible.builtin.shell"], variables)
    dnf = tmp_path / "dnf"
    dnf.write_text('#!/bin/sh\ncase "$*" in *--disableexcludes=all*) echo "cassandra.noarch 4.1.12-1 cassandra-41x";'
                   ' exit 0;; esac\n%s\n' % ('echo "Error: No matching Packages to list"; exit 1' if excluded else
                                             'echo "cassandra.noarch 4.1.12-1 cassandra-41x"'))
    dnf.chmod(0o755)
    return subprocess.run(["sh", "-c", shell], capture_output=True, text=True, check=False, env={"PATH": "%s:/usr/bin:/bin" % tmp_path})


def test_a_release_the_node_excludes_is_said_excluded(tmp_path):
    # (versionlock or excludepkgs: the install refuses it; "add it to the repository" would send the operator the
    # wrong way)
    run = _listed(tmp_path, excluded=True)
    assert run.returncode == 1 and run.stdout.splitlines() == [
        "Error: No matching Packages to list",
        "cassandra-4.1.12 is in the repository, excluded on this node (dnf excludepkgs or versionlock)"]
    said = _said(cassandra_install_release={"rc": 1, "stdout": run.stdout, "stdout_lines": run.stdout.splitlines()})
    assert said[2] == ("Way out: lift the exclusion of cassandra on the node (excludepkgs in dnf.conf or the repository"
                       " file, or dnf versionlock delete cassandra), then run again.")
    assert _listed(tmp_path, excluded=False).returncode == 0
