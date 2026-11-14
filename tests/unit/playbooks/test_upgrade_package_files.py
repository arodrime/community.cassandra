from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# upgrade: with cassandra_install_method packages, the target files are checked
# before any node stops; skipped checks (the repository method) are no failure.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "upgrade.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)

ASSERT = next(t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == "The target package files are there")


def render(name, hostvars):
    variables = dict(ASSERT["vars"], hostvars=hostvars, ansible_play_hosts_all=list(hostvars))
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(ASSERT["vars"][name]))


def results(*items):
    return {"cassandra_upgrade_files": {"results": list(items)}}


SKIPPED = {"skipped": True, "item": "cassandra"}


def test_skipped_checks_are_no_failure():
    hostvars = {"n1": dict(results(SKIPPED, SKIPPED), inventory_hostname="n1"),
                "n2": dict(results({"status": 206}, {"status": 200}), inventory_hostname="n2")}
    assert render("_missing", hostvars) == []


def test_missing_files_and_unreachable_mirror():
    hostvars = {"n1": dict(results({"status": 206}, {"status": 404}), inventory_hostname="n1"),
                "n2": dict(results({"status": -1}, {"status": -1}), inventory_hostname="n2"),
                "n3": {"inventory_hostname": "n3"}}
    assert render("_missing", hostvars) == ["n1", "n2"]
    assert sorted(render("_codes", hostvars)) == [-1, 404]


def test_preflight_shows_the_target_config_with_the_former_series_installed():
    # the role runs with check_mode applied, which ansible_check_mode does not tell: the series check is told apart
    todo = [t for play in PLAYS for t in play.get("tasks", [])]
    show = None
    while todo:
        t = todo.pop(0)
        show = t if t.get("name") == "Show the target config (nothing is written)" else show
        todo += t.get("block", [])
    assert show["ansible.builtin.include_role"]["apply"]["check_mode"] is True
    assert show["vars"]["_cassandra_config_target_preview"] is True
    todo = load_tasks(os.path.join(os.path.dirname(PLAYBOOK), "..", "roles", "cassandra_config", "tasks", "main.yml"))
    while todo:
        t = todo.pop(0)
        if t.get("name") == "Refuse the templates of another series than the one installed":
            assert "not _cassandra_config_target_preview | default(false) | bool" in t["when"]
            return
        todo += t.get("block", [])
    raise AssertionError("series check not found")


AVAILABLE = next(t for play in PLAYS for t in play.get("tasks", [])
                 if t.get("name") == "The target packages are available")
LOOK = next(t for play in PLAYS for t in play.get("tasks", [])
            if t.get("name") == "Look for the target packages in the repositories the roles don't manage")


def template(text, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(text))


def looked(**node):
    base = {"_phase": "rolling", "cassandra_install_method": "repository", "cassandra_repository_manage": True}
    base.update(node)
    return all(template("{{ %s }}" % w, **base) for w in LOOK["when"])


def test_target_looked_for_in_repositories_the_roles_do_not_manage():
    assert looked(cassandra_install_method="system")
    assert looked(cassandra_repository_manage=False)
    assert not looked()  # the roles add the target series' repository themselves
    assert not looked(cassandra_install_method="system", cassandra_offline=True)
    assert not looked(cassandra_install_method="packages", cassandra_repository_manage=False)


def available_message(family):
    hostvars = {"n1": {"inventory_hostname": "n1", "cassandra_upgrade_available": {"rc": 1}},
                "n2": {"inventory_hostname": "n2", "cassandra_upgrade_available": {"rc": 0}},
                "n3": {"inventory_hostname": "n3", "cassandra_upgrade_available": {"skipped": True}}}
    variables = dict(hostvars=hostvars, ansible_play_hosts_all=list(hostvars), cassandra_package_version="5.0.6",
                     ansible_facts={"os_family": family})
    variables["_missing"] = template(AVAILABLE["vars"]["_missing"], **variables)
    variables["_lines"] = [template(line, **variables) for line in AVAILABLE["vars"]["_lines"]]
    assert variables["_missing"] == ["n1"]
    return template(AVAILABLE["ansible.builtin.assert"]["fail_msg"],
                    _nl="\n", **{k: v for k, v in variables.items() if k != "_nl"}).split("\n")


def test_target_not_offered_stops_one_fact_per_line_with_the_way_out():
    lines = available_message("RedHat")
    assert lines[0] == ("n1: their repositories offer no cassandra 5.0.6 (repositories the roles do not manage:"
                        " cassandra_repository_manage false, or cassandra_install_method system). Nothing was done.")
    assert lines[1].startswith("Way out: add the target series to them (your mirror or Satellite); or let the roles")
    # apt answers from its cached lists: refresh them
    assert "then apt-get update on them (apt reads its cached lists)" in available_message("Debian")[1]
