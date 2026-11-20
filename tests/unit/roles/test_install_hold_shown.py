from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# (audit r5) the plan of a newer release whose repository the run writes skips the package install (it cannot look
# in that repository yet): the Debian hold of the pinned packages is shown there all the same, not applied unplanned
# by the release window.

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import load, render

TASKS = load("roles", "cassandra_install", "tasks", "cassandra.yml")


def test_the_hold_is_compared_where_the_install_of_another_release_is_skipped():
    top = dict((t.get("name"), t) for t in TASKS)
    install = top["Install Cassandra Packages"]
    assert "Hold the pinned packages (Debian)" not in [t.get("name") for t in install["block"]]
    hold = top["Hold the pinned packages (Debian)"]
    variables = {"cassandra_offline": False, "ansible_facts": {"os_family": "Debian"}, "cassandra_package_version": "5.0.9",
                 "cassandra_package_hold": True, "_cassandra_install_check": True, "item": "cassandra",
                 "_cassandra_install_before": {"cassandra": [{"version": "5.0.8"}]}, "_cassandra_install_upgrade": True,
                 "_cassandra_repository_items": [{"item": "uris"}], "cassandra_install_method": "repository"}
    assert all(render("{{ %s }}" % w, variables) for w in hold["when"])
    # the install skipped there
    assert not all(render("{{ %s }}" % w, variables) for w in install["when"])
    assert not all(render("{{ %s }}" % w, dict(variables, cassandra_offline=True)) for w in hold["when"])
    assert not all(render("{{ %s }}" % w, dict(variables, _cassandra_install_before={})) for w in hold["when"])
    names = [t.get("name") for t in TASKS]
    assert names.index("Install Cassandra Packages") < names.index("Hold the pinned packages (Debian)")
