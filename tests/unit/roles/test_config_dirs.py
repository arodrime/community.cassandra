from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config refuses Cassandra directories on a mount point of /etc/fstab
# that is not mounted (they would be on the root filesystem), and reloads the
# seed list of a running node.

import os

import pytest

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.roles.config_reader import config_read
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks", "main.yml")
READ = "Read the node's config files and directories"
LOOK = "Look for a system keyspace, and into Cassandra's directories"
NOTE = "Note what was found in the directories"


def task(name):
    todo = load_tasks(TASKS)
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


def test_dirs_whose_owner_and_mode_are_checked(tmp_path):
    # data, commit log, saved caches, hints (each path once, the first kind wins); not the log dir
    read = task(READ)
    assert render(read["vars"]["_request"]["dir_stats"], cassandra_data_file_directories=["/d1", "/d2"], cassandra_commitlog_dir="/cl",
                  cassandra_saved_caches_dir="/d1", cassandra_hints_dir="", cassandra_log_dir="/log",
                  _cassandra_config_more_dirs=[]) == [
        ["data dir", "/d1"], ["data dir", "/d2"], ["commitlog dir", "/cl"]]
    # read as the stat module does with follow (a link to a disk: the disk's directory)
    (tmp_path / "disk").mkdir(mode=0o750)
    (tmp_path / "data").symlink_to(tmp_path / "disk")
    request = {"step": "node", "dirs": [], "yaml": str(tmp_path / "none.yaml"), "conf_target": "", "keep": [], "files": [],
               "dir_stats": [["data dir", str(tmp_path / "data")], ["hints dir", str(tmp_path / "gone")]]}
    found = config_read(request)["dir_stats"]
    assert [r["item"] for r in found] == request["dir_stats"]
    assert found[0]["stat"]["isdir"] is True and found[0]["stat"]["mode"] == "0750" and found[0]["stat"]["islnk"] is False
    assert found[1]["stat"] == {"exists": False}


def test_dir_owner_and_mode_set_on_the_top_level_only():
    fix = task("Set the owner and mode of the existing ones")
    assert fix["loop"] == "{{ _cassandra_config_dir_changes }}"
    assert "recurse" not in fix["ansible.builtin.file"] and fix["ansible.builtin.file"]["state"] == "directory"
    # in the report (apply_config's recap), and shown with the files' ones
    assert "_cassandra_config_dir_changes" in task("Record the changes for the report")["ansible.builtin.set_fact"]["_cassandra_config_items"]
    assert "_cassandra_config_dir_changes" in task("Show the owner, group and mode changes")["ansible.builtin.debug"]["msg"]


def test_dirs_another_account_owns_in_a_data_dir_sampled(tmp_path):
    look = task(LOOK)
    assert look["check_mode"] is False and look["changed_when"] is False
    results = [{"item": ["data dir", "/d1"], "stat": {"isdir": True}}, {"item": ["commitlog dir", "/cl"], "stat": {"isdir": True}},
               {"item": ["data dir", "/d2"], "stat": {"exists": False}}]
    assert render(look["vars"]["_foreign"], cassandra_config_dir_stat={"results": results}) == ["/d1"]
    assert render(task(NOTE)["vars"]["_stats"], cassandra_config_dir_stat={"results": results}) == [results[0]]
    # keyspace and table dirs only, a sample (no walk of the sstables), through a link to a disk; a mount point's lost+found
    for d in ("ks1/t1", "ks1/t2", "ks2/t3/snapshots/s", "lost+found/x", "ks3", "ks4"):
        (tmp_path / "disk" / d).mkdir(parents=True)
    (tmp_path / "data").symlink_to(tmp_path / "disk")
    sample = config_read({"step": "dirs", "system": [], "tries": [], "foreign": [str(tmp_path / "data")], "user": "root"})["foreign"]
    assert len(sample) == 1 and len(sample[0]) == 4 and not [p for p in sample[0] if "lost+found" in p or "snapshots" in p]
    assert config_read({"step": "dirs", "system": [], "tries": [], "foreign": [str(tmp_path / "data")],
                        "user": str(os.getuid())})["foreign"] == [[]]  # all the account's
    notes = task("Note the dirs another account owns")["ansible.builtin.set_fact"]["_cassandra_config_dir_notes"]
    found = {"results": [{"item": {"item": ["data dir", "/d1"]}, "stdout_lines": ["/d1/ks1", "/d1/ks1/t1-1", "/d1/ks2", "/d1/ks3"]},
                         {"item": {"item": ["data dir", "/d3"]}, "stdout_lines": ["/d3/a", "/d3/b", "/d3/c"]},
                         {"item": {"item": ["data dir", "/d2"]}, "stdout_lines": []}, {"skipped": True}]}
    assert render(notes, cassandra_config_dir_foreign=found, _cassandra_config_perm_settings={"user": "cassandra"}) == [
        "WARNING - data dir /d1: not owned by cassandra: /d1/ks1, /d1/ks1/t1-1, /d1/ks2, ... (left as they are: chown -R them"
        " if Cassandra cannot read them)",
        "WARNING - data dir /d3: not owned by cassandra: /d3/a, /d3/b, /d3/c (left as they are: chown -R them"
        " if Cassandra cannot read them)"]


def test_dirs_the_account_can_use_through_its_other_groups_are_left():
    # the filter's candidates tried as the account: rc 0 (another group, an ACL) left as is; no runuser: kept
    note = task(NOTE)["ansible.builtin.set_fact"]["cassandra_config_dir_try"]["results"]
    a, b, c = ({"path": p} for p in ("/a", "/b", "/c"))
    tried = [{"item": a, "rc": 0}, {"item": b, "rc": 1}, {"item": c, "rc": 2}]
    assert render(note, _tries=[a, b, c], _read={"tries": [0, 1, 2]}) == tried
    results = [{"item": a, "rc": 0}, {"item": b, "rc": 1}, {"item": c, "failed": True, "msg": "no runuser"}]
    changes = task("List the owner, group and mode changes of the directories")["ansible.builtin.set_fact"]["_cassandra_config_dir_changes"]
    assert render(changes, cassandra_config_dir_try={"results": results}) == [b, c]
    assert render(changes, cassandra_config_dir_try={"results": []}) == []


def test_a_failed_seed_reload_is_a_warning():
    # past failed_when: false the result is never "failed": the module's msg tells
    assert task("Reload the seed list on the running node")["failed_when"] is False
    say = task("Say the seed reload failed")
    when = " and ".join("(%s)" % w for w in say["when"])
    failed = {"changed": False, "failed": False, "rc": 0, "error": "Failed to reload the seed node list",
              "stdout": "Failed to reload the seed node list.",
              "msg": "nodetool reloadseeds did not execute successfully (rc 0): Failed to reload the seed node list. ..."}
    ok = {"changed": True, "msg": "nodetool reloadseeds executed successfully"}
    skipped = {"skipped": True, "changed": False}
    for result, expected in ((failed, True), (ok, False), (skipped, False)):
        assert render("{{ %s }}" % when, cassandra_config_seeds_reload=result) is expected, result
    text = render(say["ansible.builtin.debug"]["msg"], inventory_hostname="n1", cassandra_config_seeds_reload=failed,
                  _error=render(say["vars"]["_error"], cassandra_config_seeds_reload=failed))
    assert text.startswith("WARNING - n1: the new seed list is written in cassandra.yaml but nodetool reloadseeds failed:"
                           " Failed to reload the seed node list. The running node cannot load its cassandra.yaml again")
    assert "fix the file before any restart, or the node will not start" in text
    text = render(say["ansible.builtin.debug"]["msg"], inventory_hostname="n1", _error="error: Authentication failed",
                  cassandra_config_seeds_reload={"stdout": ""})
    assert text.endswith("Authentication failed. The list applies at the next restart of the node, or fix the cause and"
                         " reload the seeds (nodetool reloadseeds).")
