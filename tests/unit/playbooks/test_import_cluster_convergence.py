from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's report, its tasks run for real by ansible-playbook on what the import found (90 nodes): the end of
# the run, the CONVERGENCE section under FOR INFO in report.txt (report.check.txt under --check), the shareable copy
# and its key when asked; no secret shown.

import json
import os
import re
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
TASKS = [t for t in WRITE["tasks"] if t["name"] in (
    "Write report.txt and report.details.txt", "Make the key of the shareable copy readable by its owner only",
    "Write the shareable copy of the report", "Show the summary")]


def playbook(tmp_path, self_check_ok=True):
    # (over a file of yours, as the playbook lays it out: a value of yours the nodes do not all have)
    layout = cassandra_inventory_layout_over([fake_node(i) for i in range(90)], "Big", [
        {"path": "group_vars/all/tuning.yml", "content": "cassandra_concurrent_reads: 32\n"}])
    (tmp_path / "layout.json").write_text(json.dumps(layout))
    (tmp_path / "reports" / "big").mkdir(parents=True)
    report_dir = str(tmp_path / "reports" / "big")
    variables = dict((k, WRITE["vars"][k]) for k in ("_report", "_report_details", "_convergence", "_newline"))
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
def test_the_report_and_the_end_of_the_run(tmp_path, check):
    rc, out = run(tmp_path, *(["--check"] if check else []))
    assert rc == 0, out
    lines = out.split("\n")
    head = next(i for i, line in enumerate(lines) if line.startswith("IMPORT big"))
    assert lines[head].startswith("IMPORT big%s  90 nodes (89 read)  " % (" (--check, inventory not written)" if check else ""))
    assert max(len(line) for line in lines[head:]) <= 79  # (a log's width)
    name = "report.check.txt" if check else "report.txt"
    end = next(i for i, line in enumerate(lines) if line.startswith("report: "))
    assert lines[end] == "report: reports/big/%s" % name
    assert "Convergence" not in " ".join(lines[head:end])  # (FOR INFO: in the report only)
    assert (tmp_path / "reports" / "big" / name).read_text().startswith("# Written by")
    report = (tmp_path / "reports" / "big" / name.replace(".txt", ".details.txt")).read_text()  # (its details)
    convergence = report.index("\n  Convergence: how close the nodes are to sharing every setting\n")
    assert report.index("\n==== FOR INFO =") < convergence < report.index("\n  Not read from the nodes")
    assert "    SETTINGS THAT DIFFER — the safest change first, then the fewest nodes" in report
    assert "    KEPT BY HAND " in report
    assert not (tmp_path / "reports" / "big" / ("report.txt" if check else "report.check.txt")).exists()
    assert not list((tmp_path / "reports" / "big").glob("*shareable*"))  # (only when asked)
    for secret in SECRETS:
        assert secret not in out and secret not in report
    assert "TASK [" not in out and "fatal:" not in out


def test_the_convergence_on_a_failed_self_check(tmp_path):
    rc, out = run(tmp_path, self_check_ok=False)
    assert rc == 0, out
    assert "The self-check first (SELF-CHECK above)" in (tmp_path / "reports" / "big" / "report.details.txt").read_text()


@pytest.mark.parametrize("check", [False, True])
def test_the_shareable_copy(tmp_path, check):
    rc, out = run(tmp_path, "-e", "import_cluster_report_shareable=true", *(["--check"] if check else []))
    assert rc == 0, out
    stem = tmp_path / "reports" / "big" / ("report.check" if check else "report")
    copy, key = stem.with_name(stem.name + ".shareable.txt"), stem.with_name(stem.name + ".shareable.map")
    assert oct(copy.stat().st_mode & 0o777) == "0o644"
    if check:  # (the key of a copy not written: none)
        assert not key.exists()
    else:
        assert oct(key.stat().st_mode & 0o777) == "0o600" and "n1 = cass001" in key.read_text()
    text = copy.read_text()
    assert text.startswith("SHAREABLE COPY of an import_cluster report")
    assert "\nIMPORT c1%s  90 nodes (89 read)  " % (" (--check, inventory not written)" if check else "") in text
    assert not re.search(r"cass\d{3}|10\.0\.\d+\.\d+|\b[Bb]ig\b|\bbig_", text)
    assert "cass001" not in text
    assert "a copy to share (names, addresses and secrets replaced; check it before sharing): reports/big/%s.shareable.txt" % stem.name in out


def test_the_tasks_pass_the_section_on():
    assert "convergence=_convergence)" in WRITE["vars"]["_report"]
    assert "self_check_ok=_self_check_ok | bool" in WRITE["vars"]["_convergence"]
    assert "_convergence_screen" not in WRITE["vars"]
    shareable = next(t for t in WRITE["tasks"] if t["name"] == "Write the shareable copy of the report")
    assert [i["mode"] for i in shareable["loop"]] == ["0644", "0600"]
    assert "import_cluster_report_shareable | default(false) | bool" in shareable["when"]
    # the key made 0600 before anything is written in it (no window where it is readable by others)
    names = [t["name"] for t in WRITE["tasks"]]
    touch = WRITE["tasks"][names.index("Make the key of the shareable copy readable by its owner only")]
    assert names.index(touch["name"]) < names.index(shareable["name"])
    assert touch["ansible.builtin.file"]["mode"] == "0600" and touch["ansible.builtin.file"]["state"] == "touch"
