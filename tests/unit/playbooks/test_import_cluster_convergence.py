from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's CONVERGENCE section, its tasks run for real by ansible-playbook on what the import found (90
# nodes): its score and first actions at the end of the run, before where the full report is; the whole section in
# report.txt (report.check.txt under --check), before NOT READ; no secret shown.

import json
import os
import subprocess
import sys

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout_over
from ansible_collections.community.cassandra.tests.unit.plugins.filter.test_cassandra_import_convergence import (
    SECRETS, fake_node)

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
with open(os.path.join(TOP, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    WRITE = next(p for p in yaml.safe_load(f) if p["name"] == "Write the inventory")
TASKS = [t for t in WRITE["tasks"] if t["name"] in ("Write report.txt", "Show the summary")]


def playbook(tmp_path, self_check_ok=True):
    # (over a file of yours, as the playbook lays it out: a value of yours the nodes do not all have)
    layout = cassandra_inventory_layout_over([fake_node(i) for i in range(90)], "Big", [
        {"path": "group_vars/all/tuning.yml", "content": "cassandra_concurrent_reads: 32\n"}])
    (tmp_path / "layout.json").write_text(json.dumps(layout))
    (tmp_path / "reports" / "big").mkdir(parents=True)
    report_dir = str(tmp_path / "reports" / "big")
    variables = dict((k, WRITE["vars"][k]) for k in ("_report", "_convergence", "_convergence_screen", "_newline"))
    variables.update(_self_check_ok=self_check_ok, _changes={"changed": [], "removed": [], "existed": []})
    plays = [{"name": "Write the inventory", "hosts": "localhost", "connection": "local", "gather_facts": False,
              "vars": variables,
              "tasks": [{"name": "Read what the import found", "ansible.builtin.set_fact": {
                  "_layout": "{{ lookup('ansible.builtin.file', 'layout.json') | from_json }}", "_self_check": {},
                  "_report_args": {
                      "written": [str(tmp_path / "inventories" / "big.yml"), str(tmp_path / "inventories")],
                      "report_file": report_dir + "{{ '/report.check.txt' if ansible_check_mode else '/report.txt' }}",
                      "secrets_clear": [], "check": "{{ ansible_check_mode }}", "inventory": "", "hosts": "",
                      "cwd": str(tmp_path), "allow_unread": True, "todo": []}}}] + TASKS}]
    (tmp_path / "summary.yml").write_text(yaml.safe_dump(plays, sort_keys=False))


def run(tmp_path, *extra, **kwargs):
    playbook(tmp_path, **kwargs)
    environ = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_LOCALHOST_WARNING="0",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
                   ANSIBLE_PYTHON_INTERPRETER=sys.executable, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
                   ANSIBLE_NOCOLOR="1")
    for name in ("ANSIBLE_CONFIG", "ANSIBLE_INVENTORY", "CASSANDRA_CLUSTER", "COLUMNS", "ANSIBLE_FORCE_COLOR"):
        environ.pop(name, None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "summary.yml"] + list(extra)
    result = subprocess.run(argv, env=environ, cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=300, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


@pytest.mark.parametrize("check", [False, True])
def test_the_section_on_the_screen_and_in_the_report(tmp_path, check):
    rc, out = run(tmp_path, *(["--check"] if check else []))
    assert rc == 0, out
    lines = out.split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith("CONVERGENCE  "))
    assert lines[at].startswith("CONVERGENCE  4/11 settings common to all nodes, 7 differ, 3 files kept by hand"), out
    assert "  Next (5 of 7, the safest first, then the fewest nodes):" in lines[at + 1:at + 3]
    assert max(len(line) for line in lines[at:]) <= 79  # (a log's width)
    end = next(i for i, line in enumerate(lines) if line.startswith("full report"))
    assert end > at and "section CONVERGENCE" in " ".join(lines[at:end])
    name = "report.check.txt" if check else "report.txt"
    assert lines[end] == "full report%s: reports/big/%s" % (" (--check)" if check else "", name)
    report = (tmp_path / "reports" / "big" / name).read_text()
    assert "\nCONVERGENCE — towards one group, every setting in group_vars/big/\n" in report
    assert report.index("\nCONVERGENCE ") < report.index("\nNOT READ ")
    assert "  SETTINGS THAT DIFFER — the safest change first, then the fewest nodes" in report and "  KEPT BY HAND " in report
    assert not (tmp_path / "reports" / "big" / ("report.txt" if check else "report.check.txt")).exists()
    for secret in SECRETS:
        assert secret not in out and secret not in report
    assert "TASK [" not in out and "fatal:" not in out


def test_the_score_only_on_a_failed_self_check(tmp_path):
    rc, out = run(tmp_path, self_check_ok=False)
    assert rc == 0, out
    lines = out.split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith("CONVERGENCE  "))
    assert "The self-check first" in " ".join(lines[at:at + 4]) and "Next (" not in out
    assert "The self-check first (SELF-CHECK above)" in (tmp_path / "reports" / "big" / "report.txt").read_text()


def test_the_tasks_pass_the_section_on():
    show = next(t for t in WRITE["tasks"] if t["name"] == "Show the summary")["ansible.builtin.debug"]["msg"]
    assert "convergence=_convergence_screen)" in show and "convergence=_convergence)" in WRITE["vars"]["_report"]
    assert "screen=true" in WRITE["vars"]["_convergence_screen"] and "screen" not in WRITE["vars"]["_convergence"]
    for name in ("_convergence", "_convergence_screen"):
        assert "self_check_ok=_self_check_ok | bool" in WRITE["vars"][name]
