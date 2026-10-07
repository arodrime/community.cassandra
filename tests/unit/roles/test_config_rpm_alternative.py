from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config's RPM conf dir alternative, its tasks run by ansible-playbook
# on a local host: check mode (--check, or check_mode applied to the role by
# apply_config's compare, where ansible_check_mode stays false) seeds no
# directory, so it selects none: the alternatives module fails on a missing
# path. A real run seeds it, then selects it. The module is replaced by a fail
# task, which stops the run whenever the selection would run.

import os
import subprocess
import sys

import pytest
import yaml

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks", "main.yml")


def alternative_tasks():
    with open(TASKS, encoding="utf-8") as f:
        todo = list(yaml.safe_load(f))
    while todo:
        t = todo.pop(0)
        if t.get("name") == "Set up the RPM conf dir alternative":
            return t["block"]
        todo += t.get("block", [])
    raise KeyError("Set up the RPM conf dir alternative")


def run(tmp_path, how):
    """Runs the block's tasks with the conf dir in use under tmp_path: (rc, output)."""
    tasks = alternative_tasks()
    select = [t for t in tasks if t["name"] == "Select the alternative conf dir"][0]
    del select["community.general.alternatives"]
    select["ansible.builtin.fail"] = {"msg": "SELECTED {{ cassandra_rpm_conf_alternative }}"}
    (tmp_path / "alternative.yml").write_text(yaml.safe_dump(tasks))
    in_use = tmp_path / "default.conf"
    in_use.mkdir()
    (in_use / "cassandra.yaml").write_text("cluster_name: my_cluster\n")
    include = {"ansible.builtin.include_tasks": {"file": str(tmp_path / "alternative.yml")}}
    if how == "apply":
        include["ansible.builtin.include_tasks"]["apply"] = {"check_mode": True}
    play = [{"hosts": "localhost", "gather_facts": False,
             "vars": {"cassandra_rpm_conf_alternative": str(tmp_path / "ansible.conf"),
                      "cassandra_rpm_conf_alternative_priority": 100,
                      "cassandra_config_conf_target": {"stat": {"exists": True, "lnk_source": str(in_use)}}},
             "tasks": [include]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    env = dict(os.environ, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", "localhost,", "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable, str(tmp_path / "play.yml")]
    if how == "check":
        argv.append("--check")
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=300, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


@pytest.mark.parametrize("how", ["apply", "check"])
def test_check_mode_seeds_and_selects_nothing(tmp_path, how):
    rc, out = run(tmp_path, how)
    assert rc == 0, out
    assert "SELECTED" not in out
    assert not (tmp_path / "ansible.conf").exists()


def test_a_real_run_seeds_then_selects(tmp_path):
    rc, out = run(tmp_path, "real")
    assert rc != 0 and "SELECTED %s" % (tmp_path / "ansible.conf") in out, out
    assert (tmp_path / "ansible.conf" / "cassandra.yaml").read_text() == "cluster_name: my_cluster\n"


def test_the_selection_waits_for_the_seed():
    names = [t["name"] for t in alternative_tasks()]
    assert names == ["Seed the alternative conf dir from the one in use", "Check for the alternative conf dir",
                     "Select the alternative conf dir"]
    select = alternative_tasks()[2]
    assert select["when"] == "cassandra_rpm_conf_alternative_stat.stat.exists"
