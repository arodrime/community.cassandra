from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config's .diff file (2026-11-25): written under --check too, next to the run's report; the first run of an
# inventory has no reports dir yet: made first, else the plan named a file that was not there. Run for real.

import os
import stat
import sys

import pytest
import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import run_playbook
from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import FIND, find

WRITE = find(FIND["tasks"], "Write the files' diffs next to the report of the run")


def run(tmp_path, path):
    playbook = tmp_path / "play.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "vars": {"cassandra_apply_config_diff_text": "== jvm-server.options  on node1\n-a\n+b",
                 "cassandra_apply_config_diff_path": str(path),
                 "cassandra_apply_config_diff": ["WILL CHANGE", "  jvm-server.options  rewritten",
                                                 "real diffs: reports/prod/" + path.name],
                 "_cassandra_apply_config_diff_unnamed": ["WILL CHANGE", "  jvm-server.options  rewritten",
                                                          "      (24 more lines not shown)"]},
        "tasks": [WRITE, {"name": "Show the view", "ansible.builtin.debug": {
            "msg": "VIEW {{ cassandra_apply_config_diff | to_json }}"}}]}]))
    args = ["-c", "local", "-i", "localhost,", "-e", "ansible_python_interpreter=" + sys.executable, "--check",
            playbook]
    return run_playbook(args, dict(ANSIBLE_LOCALHOST_WARNING="0"), cwd=tmp_path)


@pytest.mark.playbook
def test_the_diff_file_is_written_when_the_reports_dir_is_not_there_yet(tmp_path):
    path = tmp_path / "reports" / "prod" / "apply_config-check-2026-11-25-120000.diff"
    rc, output = run(tmp_path, path)
    assert rc == 0, output
    assert path.read_text() == "== jvm-server.options  on node1\n-a\n+b\n"
    assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o600


@pytest.mark.playbook
def test_a_diff_file_not_written_is_said(tmp_path):
    (tmp_path / "reports").write_text("a file, not a dir\n")
    path = tmp_path / "reports" / "prod" / "apply_config-2026-11-25-120000.diff"
    rc, output = run(tmp_path, path)
    assert rc == 0, output
    assert "WARNING  the files' diffs of this run were not written" in output, output
    assert "Not a directory" in output, output  # (the error observed, audit round 6)
    assert not path.exists()
    # (audit rounds 2 and 3) the view names no file that is not there: the one rendered naming none
    assert 'VIEW [\\"WILL CHANGE\\", \\"  jvm-server.options  rewritten\\", \\"      (24 more lines not shown)\\"]' \
        in output, output


@pytest.mark.playbook
def test_a_diff_file_not_written_gives_every_node_the_view_naming_none(tmp_path):
    # (audit round 5) the run is run_once on the play's nodes: the view naming no file reaches each of them (the
    # recap and the report read it from any)
    (tmp_path / "reports").write_text("a file, not a dir\n")
    path = tmp_path / "reports" / "prod" / "apply_config-2026-11-25-120000.diff"
    playbook = tmp_path / "play.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "all", "gather_facts": False,
        "vars": {"cassandra_apply_config_diff_text": "== x\n-a\n+b", "cassandra_apply_config_diff_path": str(path),
                 "cassandra_apply_config_diff": ["WILL CHANGE", "real diffs: " + str(path)],
                 "_cassandra_apply_config_diff_unnamed": ["WILL CHANGE", "      (1 more line not shown)"]},
        "tasks": [WRITE, {"name": "Show the view", "ansible.builtin.debug": {
            "msg": "VIEW {{ inventory_hostname }} {{ cassandra_apply_config_diff | join('|') }}"}}]}]))
    args = ["-c", "local", "-i", "node1,node2,node3,", "-e", "ansible_python_interpreter=" + sys.executable, playbook]
    rc, output = run_playbook(args, dict(ANSIBLE_LOCALHOST_WARNING="0"), cwd=tmp_path)
    assert rc == 0, output
    for name in ("node1", "node2", "node3"):
        assert "VIEW %s WILL CHANGE|      (1 more line not shown)" % name in output, output


@pytest.mark.playbook
def test_the_diff_file_is_written_before_the_question_and_said_written_once_confirmed(tmp_path):
    # (audit round 6) the plan names a file that is there: written before the question, its head "what this run
    # writes if you answer yes" (a run declined keeps it so); once confirmed, "what this run wrote"; the report's plan
    # reads the view after the write (the one naming no file when it failed)
    names = [t.get("name") for t in FIND["tasks"]]
    write = names.index("Write the files' diffs next to the report of the run")
    plan, question = names.index("Keep the plan for the report of the run"), names.index("Show the plan and confirm the run")
    assert write < plan < question
    assert names[-1] == "Note the diffs as written"
    path = tmp_path / "apply_config-2026-11-26-120000.diff"
    head = "apply_config c: the files' diffs (what this run writes if you answer yes), secrets masked"
    path.write_text(head + "\n\n== x\n-a\n+b\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "vars": {"cassandra_apply_config_diff_text": head + "\n\n== x\n-a\n+b", "cassandra_apply_config_diff_path":
                 str(path), "_cassandra_apply_config_diff_written": {"checksum": "x"}},
        "tasks": [find(FIND["tasks"], "Note the diffs as written")]}]))
    args = ["-c", "local", "-i", "localhost,", "-e", "ansible_python_interpreter=" + sys.executable, playbook]
    rc, output = run_playbook(args, dict(ANSIBLE_LOCALHOST_WARNING="0"), cwd=tmp_path)
    assert rc == 0, output
    assert path.read_text() == "apply_config c: the files' diffs (what this run wrote), secrets masked\n\n== x\n-a\n+b\n"
