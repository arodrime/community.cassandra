from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node's "Clean up, by batches": Ansible templates its loop before its
# when, so the loop renders for every cleanup choice, the default (none) and
# --check too, where the when then skips the task. The task as add_node.yml
# has it, with its play's _mode, run by ansible-playbook on two local hosts.

import os
import subprocess

import pytest
import yaml

from ansible_collections.community.cassandra.tests.unit.helpers import LOCAL, run_playbook

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
with open(os.path.join(TOP, "playbooks", "add_node.yml"), encoding="utf-8") as f:
    PLAY = [p for p in yaml.safe_load(f) if p.get("name") == "Clean up the nodes that handed data over"][0]
TASK = [t for t in PLAY["tasks"] if t.get("name") == "Clean up, by batches"][0]


def run(tmp_path, cleanup, check):
    play = {"hosts": "all", "gather_facts": False, "tasks": [TASK],
            "vars": {"_mode": PLAY["vars"]["_mode"], "_targets": ["n1", "n2"]}}
    (tmp_path / "playbook.yml").write_text(yaml.safe_dump([play]))
    (tmp_path / "inventory").write_text("n1 _cassandra_preflight='{\"cassandra_dc\": \"dc1\", \"cassandra_rack\": \"r1\"}'\n"
                                        "n2 _cassandra_preflight='{\"cassandra_dc\": \"dc1\", \"cassandra_rack\": \"r2\"}'\n")
    args = ["-i", "inventory"] + LOCAL + ["-e", "cassandra_add_node_cleanup=" + cleanup] + (["--check"] if check else [])
    return run_playbook(args + ["playbook.yml"], dict(ANSIBLE_HOST_KEY_CHECKING="0"), ["ANSIBLE_CONFIG", "ANSIBLE_INVENTORY"],
                        cwd=tmp_path, stdin=subprocess.DEVNULL)


@pytest.mark.playbook
@pytest.mark.parametrize("cleanup, check", [("none", False), ("none", True), ("rack", True)])
def test_the_loop_renders_and_the_task_is_skipped(tmp_path, cleanup, check):
    rc, out = run(tmp_path, cleanup, check)
    assert rc == 0, out
    assert "skipping: [n1]" in out and "fatal" not in out and "ok=0" in out and "failed=0" in out
