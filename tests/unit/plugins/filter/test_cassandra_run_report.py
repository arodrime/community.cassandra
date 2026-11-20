from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The report of a run of a changing operation (apply_config): where it goes, its name, its text, the reports kept;
# run_report.yml run for real on the controller; apply_config and node_operation.yml feeding it.

import datetime
import os
import subprocess
import sys
import time

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_run_report import (
    cassandra_reports_dir, cassandra_run_report, cassandra_run_report_name, cassandra_run_report_old,
    cassandra_run_report_times)

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
BUILD = "community.cassandra 3.0.0-rc1, commit 1a2b3c4d5e6f"


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def epoch(text):
    return int(time.mktime(datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timetuple()))


def test_the_reports_go_next_to_the_inventory_dir(tmp_path):
    inventories = tmp_path / "inventories"
    inventories.mkdir()
    (inventories / "prod.yml").write_text("all: {}\n")
    # the inventory dir, or a file in it: reports/<cluster group> next to it (import_cluster's report.txt's dir)
    assert cassandra_reports_dir([str(inventories)], "prod") == str(tmp_path / "reports" / "prod")
    assert cassandra_reports_dir([str(inventories / "prod.yml")], "prod") == str(tmp_path / "reports" / "prod")
    # relative to the dir the playbook runs from; none: there
    assert cassandra_reports_dir(["inventories"], "prod", cwd=str(tmp_path)) == str(tmp_path / "reports" / "prod")
    assert cassandra_reports_dir([], "prod", cwd=str(tmp_path)) == str(tmp_path / "reports" / "prod")
    # the one given (cassandra_run_report_dir), relative to the current dir too
    assert cassandra_reports_dir([str(inventories)], "prod", "/opt/zorglub/reports") == "/opt/zorglub/reports"
    assert cassandra_reports_dir([str(inventories)], "prod", "out/", str(tmp_path)) == str(tmp_path / "out")


def test_the_name_says_the_operation_check_and_second():
    started = epoch("2026-11-19 23:58:41")
    assert cassandra_run_report_name("apply_config", False, started) == "apply_config-2026-11-19-235841.txt"
    assert cassandra_run_report_name("apply_config", True, started) == "apply_config-check-2026-11-19-235841.txt"
    assert cassandra_run_report_name("apply_config", True, str(started)) == "apply_config-check-2026-11-19-235841.txt"
    assert cassandra_run_report_name("apply_config").startswith("apply_config-%s" % time.strftime("%Y-%m-%d"))


def test_the_text_has_the_head_then_the_sections_in_full():
    started = epoch("2026-11-19 23:58:41")
    long = "WARNING  node2: " + "a long warning " * 20
    text = cassandra_run_report([{"title": "DIFFERENCES FOUND", "lines": ["  cassandra.yaml", "    x  1 -> 2"]},
                                 {"title": "CHECKS", "lines": []},  # (nothing: left out)
                                 {"title": "RECAP", "lines": ["DONE  apply_config  prod  1 applied", long]}],
                                "apply_config", "prod", started=started, ended=started + 222, build=BUILD)
    assert text.split("\n") == [
        "apply_config  prod  DONE",
        "  made by:  " + BUILD,
        "  started:  2026-11-19 23:58:41",
        "  ended:    2026-11-20 00:02:23 (3m42s)",
        "  run:      a real run",
        "",
        "DIFFERENCES FOUND",
        "  cassandra.yaml",
        "    x  1 -> 2",
        "",
        "RECAP",
        "DONE  apply_config  prod  1 applied",
        long,  # (whole: not fitted to a terminal)
        ""]
    check = cassandra_run_report([], "apply_config", "prod", check=True, started=started, build=BUILD).split("\n")
    assert check[0] == "apply_config  prod  CHECK" and check[-2] == "  run:      --check, nothing was changed"
    assert "ended" not in "".join(check)
    stopped = cassandra_run_report([{"title": "", "lines": ["STOPPED  apply_config  prod  at node2"]}], "apply_config",
                                   "prod", status="STOPPED", started="", build=BUILD).split("\n")
    assert stopped[0] == "apply_config  prod  STOPPED" and stopped[2] == "  started:  not known"
    assert stopped[-3:] == ["", "STOPPED  apply_config  prod  at node2", ""]
    # the collection's own label by default
    assert "  made by:  community.cassandra " in cassandra_run_report([], "apply_config")


def test_the_report_masks_a_secret_a_section_lets_through():
    text = cassandra_run_report([{"title": "STOPPED", "lines": [
        "node1: nodetool -u cassandra -pw zorglub1 status failed", "  keystore_password: zorglub2",
        "  cassandra_jmx_password_file: /opt/x/jmxremote.password"], "mask": True}], "apply_config", "prod", build=BUILD)
    assert "zorglub" not in text
    assert "-pw ****" in text and "keystore_password: ****" in text and "/opt/x/jmxremote.password" in text


def test_lines_built_masked_are_not_masked_again():
    # (drift's sections, the plan: their values masked already; masked again, the way out of a line naming
    # secrets.yml, or a "token:" text, was eaten)
    lines = ["cassandra_server_keystore_password   **** (value 2) on db2, db3; **** on db1",
             "  -> already in group_vars/prod/secrets.yml: remove cassandra_server_keystore_password from host_vars/db1.yml",
             "db1  token: abc def"]
    text = cassandra_run_report([{"title": "BETWEEN NODES", "lines": lines}], "drift", "prod", build=BUILD)
    assert text.split("\n")[-4:-1] == lines
    # a raw section (a stop's text): masked, a YAML file's name not taken for a secret's
    text = cassandra_run_report([{"title": "", "lines": lines[1:2] + ["  ssl.pwd: zorglub"], "mask": True}], "x", build=BUILD)
    assert lines[1] in text and "zorglub" not in text


def test_the_times_say_when_each_turn_started():
    starts = {"node1": epoch("2026-11-19 23:58:41"), "node10": ""}
    assert cassandra_run_report_times([{"name": "node1", "result": "apply_config done in 42s"},
                                       {"name": "node10", "result": "not done (the run stopped before it)"},
                                       {"name": "node3", "result": "up to date"}], starts) == [
        "node1   23:58:41  apply_config done in 42s",
        "node10  -         not done (the run stopped before it)",
        "node3   -         up to date"]
    assert cassandra_run_report_times([]) == []


def test_the_oldest_reports_past_the_kept_ones():
    files = [{"path": "/r/apply_config-2026-11-%02d-1200.txt" % d, "mtime": 1000 + d} for d in range(1, 24)]
    files += [{"path": "/r/apply_config-check-2026-11-24-1200.txt", "mtime": 1024},
              {"path": "/r/rolling_restart-2026-11-01-1200.txt", "mtime": 1},  # another operation's
              {"path": "/r/apply_config-notes.txt", "mtime": 1}]  # not a report of the run
    old = cassandra_run_report_old(files, "apply_config")
    assert old == ["/r/apply_config-2026-11-%02d-1200.txt" % d for d in range(1, 5)]  # (24 reports, 20 kept)
    # (the reports named to the minute by an earlier version: kept and removed the same way)
    assert cassandra_run_report_old([{"path": "/r/apply_config-2026-11-25-120000.txt", "mtime": 2000}] + files,
                                    "apply_config")[:5] == old + ["/r/apply_config-2026-11-05-1200.txt"]
    assert cassandra_run_report_old(files, "apply_config", keep=30) == []
    assert cassandra_run_report_old(files, "rolling_restart", keep=0) == ["/r/rolling_restart-2026-11-01-1200.txt"]


def run_report(tmp_path, check=False, report_dir=None, keep=None):
    """run_report.yml run for real, on localhost, the inventory in tmp_path/inventories -> (rc, output)."""
    inventories = tmp_path / "inventories"
    inventories.mkdir(exist_ok=True)
    (inventories / "prod.yml").write_text("prod:\n  hosts:\n    localhost:\n      ansible_connection: local\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "prod", "gather_facts": False,
        "vars": {"cassandra_cluster_name": "Orders"},
        "tasks": [{"ansible.builtin.include_role": {"name": "community.cassandra.cassandra_operation",
                                                    "tasks_from": "run_report.yml"},
                   "vars": {"_cassandra_run_report_operation": "apply_config",
                            "_cassandra_run_report_started": epoch("2026-11-19 23:58:41"),
                            "_cassandra_run_report_sections": [{"title": "RECAP", "lines": ["DONE  apply_config"]}]}}]}]))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0", PWD=str(tmp_path))
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(inventories),
            "-e", "ansible_python_interpreter=" + sys.executable, "-e", "cassandra_hosts=prod", str(playbook)]
    argv += ["--check"] if check else []
    argv += ["-e", "cassandra_run_report_dir=" + report_dir] if report_dir else []
    argv += ["-e", "cassandra_run_report_keep=%d" % keep] if keep is not None else []
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=300, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


def test_run_report_writes_the_report_and_keeps_the_last_ones(tmp_path):
    reports = tmp_path / "reports" / "prod"
    reports.mkdir(parents=True)
    for d in range(1, 21):  # 20 of an earlier month, and one of another operation
        (reports / ("apply_config-2026-10-%02d-1200.txt" % d)).write_text("old\n")
        os.utime(str(reports / ("apply_config-2026-10-%02d-1200.txt" % d)), (1000 + d, 1000 + d))
    (reports / "rolling_restart-2026-10-01-1200.txt").write_text("other\n")
    rc, output = run_report(tmp_path)
    assert rc == 0, output
    report = reports / "apply_config-2026-11-19-235841.txt"
    assert report.is_file(), output
    text = report.read_text()
    assert text.startswith("apply_config  Orders  DONE\n  made by:  community.cassandra ") and "\nRECAP\nDONE  apply_config\n" in text
    assert "Report: %s" % report in output
    assert report.stat().st_mode & 0o777 == 0o600  # (it may name a node's secrets' places: its owner only)
    # 20 kept: the oldest one removed, the other operation's left
    assert not (reports / "apply_config-2026-10-01-1200.txt").exists()
    assert len(list(reports.glob("apply_config-*.txt"))) == 20 and (reports / "rolling_restart-2026-10-01-1200.txt").exists()
    # --check: its own name, written too (on the controller)
    rc, output = run_report(tmp_path, check=True, keep=2)
    assert rc == 0, output
    assert (reports / "apply_config-check-2026-11-19-235841.txt").read_text().startswith("apply_config  Orders  CHECK\n")
    assert sorted(p.name for p in reports.glob("apply_config-*.txt")) == [
        "apply_config-2026-11-19-235841.txt", "apply_config-check-2026-11-19-235841.txt"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_report_not_written_is_said_and_the_run_goes_on(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        rc, output = run_report(tmp_path, report_dir=str(locked / "reports"))
    finally:
        locked.chmod(0o700)
    assert rc == 0, output
    assert "WARNING  the report of this run was not written (" in output and str(locked / "reports") in output
    assert "set cassandra_run_report_dir to a dir you can write." in " ".join(output.split())


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def find(tasks, name):
    for t in tasks:
        if t.get("name") == name:
            return t
        for key in ("block", "rescue", "always"):
            found = find(t.get(key) or [], name)
            if found:
                return found
    return None


def test_apply_config_writes_its_report_after_its_recap():
    summary = next(p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Summary")
    names = [t["name"] for t in summary["tasks"]]
    assert names[-2:] == ["Print the changes", "Write the report of the run"]
    t = summary["tasks"][-1]
    assert t["ansible.builtin.include_role"]["tasks_from"] == "run_report.yml" and t["run_once"] is True
    # the recap's variables (the play's), not an operator message itself
    assert "cassandra_output" not in t["vars"] and "_nodes" in summary["vars"] and "_nodes" not in t["vars"]
    hostvars = {"node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
                          "cassandra_op_done": True, "cassandra_op_result": "apply_config done in 42s",
                          "_cassandra_op_start": epoch("2026-11-19 23:58:41"), "_cassandra_apply_config_start": 1000,
                          "cassandra_apply_config_notes": ["WARNING  node1's data dirs belong to root"]},
                "node2": {"cassandra_apply_config_todo": False, "cassandra_op_result": "nothing to apply"},
                "localhost": {"_cassandra_run_report_plan": [{"title": "WILL DO", "lines": ["node1  will apply"]}]}}
    variables = dict(trust_as_template_dict(dict(summary["vars"], **t["vars"])), hostvars=hostvars, groups={"prod": ["node1", "node2"]},
                     cassandra_hosts="prod", cassandra_cluster_name="prod", ansible_play_hosts_all=["node1", "node2"],
                     ansible_inventory_sources=[], ansible_check_mode=False)
    sections = render("{{ _cassandra_run_report_sections }}", **variables)
    assert [s["title"] for s in sections] == ["WILL DO", "RECAP", "NODES"]
    recap = sections[1]["lines"]
    assert recap[0].startswith("DONE  apply_config  prod  1 restarted, 1 up to date")
    # every warning in full, none "said above" (the plan's screen is not in the file)
    assert "WARNING  node1: node1's data dirs belong to root" in recap and not [x for x in recap if "above" in x]
    assert sections[2]["lines"] == ["node1  23:58:41  apply_config done in 42s", "node2  -         nothing to apply"]
    assert render("{{ _cassandra_run_report_status }}", **variables) == "DONE"
    # --check: no plan kept (none shown), its recap holds the differences and what a real run would do
    hostvars["localhost"] = {}
    variables["ansible_check_mode"] = True
    sections = render("{{ _cassandra_run_report_sections }}", **variables)
    assert [s["title"] for s in sections] == ["RECAP"] and render("{{ _cassandra_run_report_status }}", **variables) == "CHECK"


def trust_as_template_dict(values):
    return dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in values.items())


def test_apply_config_keeps_its_plan_for_the_report():
    play = next(p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Find what each node needs")
    names = [t["name"] for t in play["tasks"]]
    assert names.index("Keep the plan for the report of the run") == names.index("Show the plan and confirm the run") - 1
    t = play["tasks"][names.index("Keep the plan for the report of the run")]
    assert t["delegate_to"] == "localhost" and t["delegate_facts"] is True and t["run_once"] is True
    facts = t["ansible.builtin.set_fact"]
    assert facts["_cassandra_run_report_operation"] == "apply_config"
    hostvars = {"node1": {"cassandra_apply_config_todo": True, "cassandra_apply_config_then": "restart",
                          "_cassandra_notes": ["WARNING  cassandra_heap_size: 31G"]}}
    variables = dict(trust_as_template_dict(t["vars"]), hostvars=hostvars, ansible_play_hosts=["node1"],
                     ansible_play_hosts_all=["node1"], _todo=["node1"], cassandra_apply_config_diff=["  x  1 -> 2"],
                     ansible_check_mode=False)
    plan = render(facts["_cassandra_run_report_plan"], **variables)
    assert [s["title"] for s in plan] == ["DIFFERENCES FOUND", "WILL DO", "CHECKS"]
    assert plan[0]["lines"] == ["  x  1 -> 2"] and plan[1]["lines"][0] == "node1  write + restart"
    assert plan[2]["lines"] == ["WARNING  cassandra_heap_size: 31G"]
    assert render(facts["_cassandra_run_report_plan"], **dict(variables, ansible_check_mode=True)) == []
    assert render(facts["_cassandra_run_report_plan"], **dict(variables, _todo=[])) == []


def test_a_stopped_run_writes_its_report_with_the_stop_as_said():
    tasks = load("roles", "cassandra_operation", "tasks", "node_operation.yml")
    rescue = next(t for t in tasks if t.get("rescue"))["rescue"]
    names = [t["name"] for t in rescue]
    assert names.index("Write the report of the run") == names.index("Stop here") - 1
    report, stop = rescue[names.index("Write the report of the run")], rescue[names.index("Stop here")]
    # the same text (one anchor), only for an operation that keeps a report
    assert report["vars"]["_cassandra_stop_said"] == stop["ansible.builtin.fail"]["msg"]
    assert report["when"] == "hostvars['localhost']._cassandra_run_report_operation is defined"
    hostvars = {"node1": {"cassandra_op_result": "apply_config done in 42s", "_cassandra_op_start": epoch("2026-11-19 23:58:41")},
                "node2": {"cassandra_op_result": "apply_config FAILED after 3s: cluster health check failed"},
                "localhost": {"_cassandra_run_report_operation": "apply_config", "_cassandra_run_report_started": 1000,
                              "_cassandra_run_report_plan": [{"title": "WILL DO", "lines": ["node1, node2  will apply"]}]}}
    variables = dict(trust_as_template_dict(report["vars"]), hostvars=hostvars, ansible_play_hosts_all=["node1", "node2", "node3"])
    variables["_cassandra_stop_said"] = ["STOPPED  apply_config  prod  at node2", "  cluster health check failed"]
    sections = render("{{ _cassandra_run_report_sections }}", **variables)
    assert sections == [
        {"title": "WILL DO", "lines": ["node1, node2  will apply"]},
        {"title": "", "lines": ["STOPPED  apply_config  prod  at node2", "  cluster health check failed"], "mask": True},
        {"title": "NODES", "lines": ["node1  23:58:41  apply_config done in 42s",
                                     "node2  -         apply_config FAILED after 3s: cluster health check failed",
                                     "node3  -         not reached"]}]
    assert render("{{ _cassandra_run_report_status }}", **variables) == "STOPPED"
    assert render("{{ _cassandra_run_report_operation }}", **variables) == "apply_config"


def test_report_of_an_operation_that_changes_nothing():
    # drift: neither a real run nor --check, said as it is
    text = cassandra_run_report([{"title": "", "lines": ["FROM STOCK x"]}], "drift", "prod", status="at stock",
                                started=1763590000, ended=1763590003, build="b", run="read only, nothing was changed")
    assert text.splitlines()[0] == "drift  prod  at stock"
    assert "  run:      read only, nothing was changed" in text.splitlines()
    assert text.endswith("\n\nFROM STOCK x\n")
