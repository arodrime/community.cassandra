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
    assert not path.exists()
    # (audit rounds 2 and 3) the view names no file that is not there: the one rendered naming none
    assert 'VIEW [\\"WILL CHANGE\\", \\"  jvm-server.options  rewritten\\", \\"      (24 more lines not shown)\\"]' \
        in output, output
