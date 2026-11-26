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
    # (rounds 7, 8) the error as observed, then the file's path on lines of its own, cut after a /, in 79 columns
    said = output.split("TASK [Say the diffs were not written]")[1].split("TASK [")[0]
    lines = [line.strip().rstrip(",")[1:-1] for line in said.split("\n")
             if line.strip().startswith('"') and not line.strip().startswith('"msg"')]
    assert lines[0].startswith("WARNING  the files' diffs of this run were not written: ")
    at = next(i for i, line in enumerate(lines) if line.startswith("  the file:"))
    assert "".join(line.strip() for line in lines[at:-1])[len("the file:"):].strip() == str(path), lines
    assert all(len(line) <= 79 for line in lines[at:]), lines


@pytest.mark.playbook
def test_a_diff_file_whose_place_is_a_dir_is_said(tmp_path):
    # (audit round 7) the dir there, the file's place a directory: the write fails, said with its error, the view
    # names no file
    path = tmp_path / "reports" / "prod" / "apply_config-2026-11-25-120000.diff"
    path.mkdir(parents=True)
    rc, output = run(tmp_path, path)
    assert rc == 0, output
    assert "WARNING  the files' diffs of this run were not written" in output, output
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


def test_the_diff_file_is_written_before_the_question():
    # (audit rounds 6, 7) the plan names a file that is there: written before the question ("what this run writes
    # once you answer yes": true answered or not, the report's NODES say what was applied), nothing rewritten after;
    # the report's plan reads the view after the write (the one naming no file when it failed)
    names = [t.get("name") for t in FIND["tasks"]]
    write = names.index("Write the files' diffs next to the report of the run")
    plan, question = names.index("Keep the plan for the report of the run"), names.index("Show the plan and confirm the run")
    assert write < plan < question
    assert "Note the diffs as written" not in names
