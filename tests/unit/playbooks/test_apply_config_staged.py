from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config, a node restarted for its config files alone: the files staged next to the live ones on every such
# node at once (cassandra_config stage.yml), put in place at its turn by a rename (staged.yml), the ones left
# removed at the end (staged_clean.yml). The node-side script and command run here on a temporary conf directory;
# which nodes are staged is rendered from the playbook.

import hashlib
import json
import os
import re
import subprocess
import sys

import pytest
import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import render_as_given

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _tasks(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _move(conf, staged, backup=False):
    task = next(t for t in _tasks("roles", "cassandra_config", "tasks", "staged.yml") if t["name"] == "Put the staged config files in place")
    script = task["vars"]["_move"]
    return subprocess.run([sys.executable, "-c", script, str(conf), json.dumps(staged), str(backup)],
                          capture_output=True, text=True, check=False)


def _stage(conf, name, text):
    (conf / (".%s.ansible-next" % name)).write_text(text)
    return hashlib.sha1(text.encode()).hexdigest()


def test_the_staged_files_are_put_in_place_by_a_rename(tmp_path):
    (tmp_path / "cassandra.yaml").write_text("old\n")
    staged = {"cassandra.yaml": _stage(tmp_path, "cassandra.yaml", "new\n"), "jvm-server.options": _stage(tmp_path, "jvm-server.options", "-Xss1m\n")}
    done = _move(tmp_path, staged)
    assert done.returncode == 0 and json.loads(done.stdout) == ["cassandra.yaml", "jvm-server.options"]
    assert (tmp_path / "cassandra.yaml").read_text() == "new\n" and (tmp_path / "jvm-server.options").read_text() == "-Xss1m\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cassandra.yaml", "jvm-server.options"]  # nothing left, no backup


def test_a_backup_is_kept_as_the_template_module_keeps_it(tmp_path):
    (tmp_path / "cassandra.yaml").write_text("old\n")
    done = _move(tmp_path, {"cassandra.yaml": _stage(tmp_path, "cassandra.yaml", "new\n")}, backup=True)
    assert done.returncode == 0
    backups = [p for p in tmp_path.iterdir() if p.name != "cassandra.yaml"]
    assert len(backups) == 1 and backups[0].read_text() == "old\n"
    # <file>.<pid>.<date>@<time>~ (ansible's backup_local)
    assert re.match(r"^cassandra\.yaml\.\d+\.\d{4}-\d{2}-\d{2}@\d{2}:\d{2}:\d{2}~$", backups[0].name)


@pytest.mark.parametrize("damage", ["other", "missing"])
def test_nothing_is_moved_unless_every_file_is_as_staged(tmp_path, damage):
    (tmp_path / "cassandra.yaml").write_text("old\n")
    staged = {"cassandra.yaml": _stage(tmp_path, "cassandra.yaml", "new\n"), "logback.xml": _stage(tmp_path, "logback.xml", "<x/>\n")}
    if damage == "other":
        (tmp_path / ".logback.xml.ansible-next").write_text("edited\n")
    else:
        (tmp_path / ".logback.xml.ansible-next").unlink()
    done = _move(tmp_path, staged)
    assert done.returncode != 0
    assert "nothing moved: logback.xml (%s)" % ("another content" if damage == "other" else "missing") in done.stderr
    assert (tmp_path / "cassandra.yaml").read_text() == "old\n" and (tmp_path / ".cassandra.yaml.ansible-next").exists()


def test_the_files_left_are_removed_through_the_conf_link(tmp_path):
    # RedHat: /etc/cassandra/conf is an alternatives link to the directory
    real = tmp_path / "default.conf"
    real.mkdir()
    (real / ".cassandra.yaml.ansible-next").write_text("x")
    (real / ".hidden").write_text("kept")
    (real / "cassandra.yaml").write_text("kept")
    (tmp_path / "conf").symlink_to(real)
    task = _tasks("roles", "cassandra_config", "tasks", "staged_clean.yml")[0]
    argv = [a.replace("{{ cassandra_conf_dir }}", str(tmp_path / "conf")) for a in task["ansible.builtin.command"]["argv"]]
    done = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert done.stdout.strip().endswith("/.cassandra.yaml.ansible-next")
    assert sorted(p.name for p in real.iterdir()) == [".hidden", "cassandra.yaml"]


def _note():
    with open(os.path.join(TOP, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
        play = next(p for p in yaml.safe_load(f) if p.get("name") == "Find what each node needs")
    return next(t for t in play["tasks"] if t.get("name") == "Note what this node needs")


def _stage_expr():
    note = _note()
    return note["ansible.builtin.set_fact"]["cassandra_apply_config_stage"], note["vars"]["_stage_files"]


def _staged(values):
    """cassandra_apply_config_stage over values, its vars rendered first (as Ansible does)"""
    note = _note()
    values = dict(values)
    for name in ("_stage_other_changes", "_stage_role_steps", "_stage_paths"):
        values[name] = render_as_given(note["vars"][name], values)
    return render_as_given(note["ansible.builtin.set_fact"]["cassandra_apply_config_stage"], values)


BASE = {"_todo": True, "_then": "restart", "_java": False, "_release": False, "_perms": [], "_dirs": [], "_jmx": [], "_users": [], "_exporter": [],
        "_header": [], "_medusa": [], "_extras": [], "_hand_started": False, "_stage_files": ["cassandra.yaml"],
        "_cassandra_config_links": [], "cassandra_config_dirs": {"results": [{"stat": {"exists": True}}]}}


@pytest.mark.parametrize("change", [
    {}, {"_todo": False}, {"_then": "none"}, {"_then": "start"}, {"_java": True}, {"_release": True}, {"_perms": ["x"]},
    {"_dirs": ["x"]}, {"_jmx": ["x"]}, {"_exporter": [{"kind": "config"}]}, {"_header": ["x"]}, {"_medusa": ["x"]},
    {"_extras": ["x"]}, {"_hand_started": True}, {"_cassandra_apply_config_conf_switch": True},
    {"_cassandra_config_links": ["cassandra.yaml"]}, {"cassandra_config_dirs": {"results": [{"stat": {"exists": False}}]}},
    {"cassandra_prometheus_exporter_enabled": True}, {"cassandra_axonops_enabled": True}])
def test_only_a_restart_for_the_config_files_alone_is_staged(change):
    assert _staged(dict(BASE, **change)) in ((True, "True") if not change else (False, "False"))


def test_the_files_staged_are_the_ones_the_template_task_writes():
    expr, files = _stage_expr()
    assert render_as_given(files, {"_cassandra_config_write": ["cassandra.yaml", "logback.xml"],
                                   "_cassandra_config_kept": ["logback.xml"]}) == ["cassandra.yaml"]
    template = next(t for t in _tasks("roles", "cassandra_config", "tasks", "apply.yml") if t.get("name") == "Template Cassandra config files")
    assert template["when"] == ["cassandra_config_file not in _cassandra_config_kept", "cassandra_config_file in _cassandra_config_write"]
