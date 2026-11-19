from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# What --check hid and now shows (the check-mode audit, test_check_mode_audit.py): the THP defrag the unit
# writes, a unit left active with no Cassandra (stopped and started again), the cqlshlib links.

import os

from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks
from ansible_collections.community.cassandra.tests.unit.helpers import render

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")


def task(role, name, path):
    todo = load_tasks(os.path.join(ROLES, role, "tasks", path))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo[0:0] = t.get("block", [])
    raise KeyError(name)


def when(t, **variables):
    return all(render("{{ %s }}" % c, **variables) in (True, "True") for c in t["when"])


def test_thp_in_the_change_list():
    snapshot = task("cassandra_linux", "Read the current OS settings", "snapshot.yml")
    assert "/sys/kernel/mm/transparent_hugepage/defrag" in snapshot["ansible.builtin.shell"]
    t = task("cassandra_linux", "Add transparent_hugepage to the change list", "report.yml")
    before = {"thp|enabled": "always", "thp|defrag": "madvise"}
    wanted = {"enabled": "never", "defrag": "never"}

    def items(**variables):
        out = []
        for item in t["loop"]:
            v = dict(dict(_cassandra_linux_live=True, _cassandra_linux_before=before, _cassandra_linux_thp_set=wanted,
                          _cassandra_linux_items=[]), item=item, **variables)
            if when(t, **v):
                out += render(t["ansible.builtin.set_fact"]["_cassandra_linux_items"], **v)
        return out
    assert items() == [{"item": "transparent_hugepage", "before": "always", "after": "never"},
                       {"item": "transparent_hugepage defrag", "before": "madvise", "after": "never"}]
    # the values the inventory gives (kernel or keep: not set), not live, no THP: none
    assert items(_cassandra_linux_thp_set={"defrag": "madvise"}) == []
    assert items(_cassandra_linux_thp_set={"enabled": "madvise"}) == [
        {"item": "transparent_hugepage", "before": "always", "after": "madvise"}]
    assert items(_cassandra_linux_live=False) == []
    assert items(_cassandra_linux_before={"thp|enabled": "", "thp|defrag": ""}) == []


def test_unit_left_active_with_no_cassandra_shown_under_check():
    look = task("cassandra_service", "Look for a running Cassandra JVM", "main.yml")
    assert look["check_mode"] is False and look["changed_when"] is False  # a read: run under --check too
    items = task("cassandra_service", "Report", "main.yml")["vars"]
    common = {"cassandra_service_unit": {"changed": False}, "cassandra_service_enabled": True,
              "_cassandra_service_before": {"active": "active", "enabled": "enabled", "unit": "SysV /etc/init.d/cassandra"},
              "ansible_check_mode": True, "_would": "would be "}
    for rc, state, expected in ((1, "started", [{"item": "cassandra", "before": "active, no Cassandra running",
                                                 "after": "would be started again"}]),
                                (0, "started", []), (1, "stopped", [])):
        want = "inactive" if state == "stopped" else "active"
        got = render(items["cassandra_change_report_items"], cassandra_service_jvm={"rc": rc},
                     cassandra_service_state=state, _want_active=want, **common)
        assert [i for i in got if i["before"] == "active, no Cassandra running"] == expected


def test_cqlshlib_links_in_the_change_list():
    link = task("cassandra_install", "Link cqlshlib into them (RedHat)", "cqlsh_links.yml")
    assert link["register"] == "cassandra_cqlshlib_links"
    t = task("cassandra_install", "Add the cqlshlib links to the change list", "report.yml")
    results = {"results": [{"changed": True, "dest": "/usr/lib/python3.9/site-packages/cqlshlib"}, {"changed": False}]}
    loop = render(t["loop"], cassandra_cqlshlib_links=results)
    assert len(loop) == 1
    items = render(t["ansible.builtin.set_fact"]["_cassandra_install_items"], item=loop[0])
    assert items == [{"item": "/usr/lib/python3.9/site-packages/cqlshlib", "before": "absent or another",
                      "after": "link to /usr/lib/python3.6/site-packages/cqlshlib"}]
