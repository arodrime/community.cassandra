from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config's RPM conf dir alternative, its tasks run by ansible-playbook
# on a local host: check mode (--check, or check_mode applied to the role by
# apply_config's compare, where ansible_check_mode stays false) seeds no
# directory, so it selects none: the alternatives module fails on a missing
# path. A real run seeds it, then selects it. The module is replaced by a fail
# task, which stops the run whenever the selection would run.

import os

import pytest
import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import LOCAL, run_playbook
from ansible_collections.community.cassandra.tests.unit.task_files import find_task, load_tasks

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks", "main.yml")


def alternative_tasks():
    return find_task(load_tasks(TASKS), "Set up the RPM conf dir alternative")["block"]


def run(tmp_path, how):
    """Runs the block's tasks with the conf dir in use under tmp_path: (rc, output)."""
    tasks = alternative_tasks()
    select = find_task(tasks, "Select the alternative conf dir")
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
    return run_playbook(["-i", "localhost,"] + LOCAL + [tmp_path / "play.yml"] + (["--check"] if how == "check" else []),
                        cwd=tmp_path)


@pytest.mark.playbook
@pytest.mark.parametrize("how", ["apply", "check"])
def test_check_mode_seeds_and_selects_nothing(tmp_path, how):
    rc, out = run(tmp_path, how)
    assert rc == 0, out
    assert "SELECTED" not in out
    assert not (tmp_path / "ansible.conf").exists()


@pytest.mark.playbook
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
