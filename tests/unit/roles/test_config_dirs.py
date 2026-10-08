from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config refuses Cassandra directories on a mount point of /etc/fstab
# that is not mounted (they would be on the root filesystem), and reloads the
# seed list of a running node.

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

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks", "main.yml")


def task(name):
    with open(TASKS, encoding="utf-8") as f:
        todo = list(yaml.safe_load(f))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def on_unmounted(dirs, unmounted):
    variables = {
        "cassandra_config_dirs": {"results": [{"item": d, "stat": {"exists": exists}} for d, exists in dirs]},
        "cassandra_config_unmounted": {"stdout_lines": unmounted},
    }
    template = task("Refuse directories on a disk that is not mounted")["vars"]["_on_unmounted"]
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


@pytest.mark.parametrize("dirs, unmounted, refused", [
    ([("/data/cassandra/data", False)], ["/data"], ["/data/cassandra/data (/data)"]),
    ([("/data", False)], ["/data"], ["/data (/data)"]),
    # there already: the empty mount point, or dirs left below it on the root filesystem
    ([("/data", True)], ["/data"], ["/data (/data)"]),
    ([("/var/lib/cassandra/data", True), ("/var/lib/cassandra/hints", True)], ["/var/lib/cassandra"],
     ["/var/lib/cassandra/data (/var/lib/cassandra)", "/var/lib/cassandra/hints (/var/lib/cassandra)"]),
    ([("/data/cassandra/data", False)], ["/data2", "/dat"], []),  # not a prefix of a path component
    ([("/data/cassandra/data", False), ("/var/lib/cassandra/hints", True)], [], []),
])
def test_refused_on_unmounted_disk(dirs, unmounted, refused):
    assert on_unmounted(dirs, unmounted) == refused


def test_seed_reload_logs_in_to_jmx():
    # remote JMX with authentication: nodetool reloadseeds needs the login too
    reload = task("Reload the seed list on the running node")["community.cassandra.cassandra_reload"]
    assert {"username", "password_file", "password"} <= set(reload)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_dirs_whose_owner_and_mode_are_checked():
    # data, commit log, saved caches, hints (each path once, the first kind wins); not the log dir
    stat = task("Read the owner and mode of Cassandra's directories")
    assert stat["ansible.builtin.stat"]["follow"] is True
    assert render(stat["vars"]["_dirs"], cassandra_data_file_directories=["/d1", "/d2"], cassandra_commitlog_dir="/cl",
                  cassandra_saved_caches_dir="/d1", cassandra_hints_dir="", cassandra_log_dir="/log") == [
        ["data dir", "/d1"], ["data dir", "/d2"], ["commitlog dir", "/cl"]]


def test_dir_owner_and_mode_set_on_the_top_level_only():
    fix = task("Set the owner and mode of the existing ones")
    assert fix["loop"] == "{{ _cassandra_config_dir_changes }}"
    assert "recurse" not in fix["ansible.builtin.file"] and fix["ansible.builtin.file"]["state"] == "directory"
    # in the report (apply_config's recap), and shown with the files' ones
    assert "_cassandra_config_dir_changes" in task("Record the changes for the report")["ansible.builtin.set_fact"]["_cassandra_config_items"]
    assert "_cassandra_config_dir_changes" in task("Show the owner, group and mode changes")["ansible.builtin.debug"]["msg"]


def test_dirs_another_account_owns_in_a_data_dir_sampled():
    look = task("Look for keyspace and table dirs another account owns")
    argv = look["ansible.builtin.command"]["argv"]
    assert "-maxdepth 2" in argv[2] and "head -n 4" in argv[2]  # no walk of the sstables
    assert "find -H " in argv[2] and "-name lost+found -prune" in argv[2]  # a link to a disk; a mount point's lost+found
    assert look["check_mode"] is False and look["changed_when"] is False
    results = [{"item": ["data dir", "/d1"], "stat": {"isdir": True}}, {"item": ["commitlog dir", "/cl"], "stat": {"isdir": True}},
               {"item": ["data dir", "/d2"], "stat": {"exists": False}}]
    assert render(look["loop"], cassandra_config_dir_stat={"results": results}) == [results[0]]
    notes = task("Note the dirs another account owns")["ansible.builtin.set_fact"]["_cassandra_config_dir_notes"]
    found = {"results": [{"item": {"item": ["data dir", "/d1"]}, "stdout_lines": ["/d1/ks1", "/d1/ks1/t1-1", "/d1/ks2", "/d1/ks3"]},
                         {"item": {"item": ["data dir", "/d3"]}, "stdout_lines": ["/d3/a", "/d3/b", "/d3/c"]},
                         {"item": {"item": ["data dir", "/d2"]}, "stdout_lines": []}, {"skipped": True}]}
    assert render(notes, cassandra_config_dir_foreign=found, _cassandra_config_perm_settings={"user": "cassandra"}) == [
        "WARNING data dir /d1: not owned by cassandra: /d1/ks1, /d1/ks1/t1-1, /d1/ks2, ... (left as they are: chown -R them"
        " if Cassandra cannot read them)",
        "WARNING data dir /d3: not owned by cassandra: /d3/a, /d3/b, /d3/c (left as they are: chown -R them"
        " if Cassandra cannot read them)"]


def test_dirs_the_account_can_use_through_its_other_groups_are_left():
    # the filter's candidates tried as the account: rc 0 (another group, an ACL) left as is; no runuser: kept
    try_task = task("Try the directories as the account Cassandra runs as")
    assert try_task["ansible.builtin.command"]["argv"][:2] == ["runuser", "-u"]
    assert try_task["check_mode"] is False and try_task["failed_when"] is False
    a, b, c = ({"path": p} for p in ("/a", "/b", "/c"))
    results = [{"item": a, "rc": 0}, {"item": b, "rc": 1}, {"item": c, "failed": True, "msg": "no runuser"}]
    changes = task("List the owner, group and mode changes of the directories")["ansible.builtin.set_fact"]["_cassandra_config_dir_changes"]
    assert render(changes, cassandra_config_dir_try={"results": results}) == [b, c]
    assert render(changes, cassandra_config_dir_try={"results": []}) == []
