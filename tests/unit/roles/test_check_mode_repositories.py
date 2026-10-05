from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Under --check, a cassandra-<series> repository that cassandra_repository
# removes (install method packages, e.g. one left on a plain directory of
# files) is still there: the dnf calls after it leave it out, else they fail.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

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
    t = task(load("cassandra_linux", "os.yml"), "Install time sync package")
    assert t["module_defaults"]["ansible.builtin.dnf"]["disablerepo"] == "{{ _cassandra_repository_check_removed | default([]) }}"


def test_jemalloc_query_leaves_them_out():
    argv = task(load("cassandra_install", "install.yml"), "Look up jemalloc in the enabled repos (RedHat)")["ansible.builtin.command"]["argv"]
    assert render(argv) == ["dnf", "-q", "repoquery", "jemalloc"]
    assert render(argv, _cassandra_repository_check_removed=["cassandra-50x"]) == [
        "dnf", "-q", "repoquery", "--disablerepo=cassandra-50x", "jemalloc"]
