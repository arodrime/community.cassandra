from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node's refusal of new nodes it may not add (a former member of this
# cluster, a node of another one, a node already in the ring...): a
# controlled stop, one block for all of them under the ops callback, then
# what to do. Its tasks, as add_node.yml has them, run by ansible-playbook on
# two local hosts with the facts the checks before them set: the whole
# output compared.

import json
import os
import re
import subprocess
import sys

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
with open(os.path.join(TOP, "playbooks", "add_node.yml"), encoding="utf-8") as f:
    PLAY = [p for p in yaml.safe_load(f) if p.get("name") == "Check the new nodes"][0]
TASKS = dict((t.get("name"), t) for t in PLAY["tasks"])

FORMER = {
    "_cassandra_node_reset_plan": {"problems": ["it holds this cluster's keyspaces (shop) ... -e cassandra_add_node_reset=true resets it"]},
    "_cassandra_node_reset_check": {
        "problems": ["it holds this cluster's keyspaces (shop) ... -e cassandra_add_node_reset=true resets it"],
        "holds": "holds 12.0 GiB of cluster 'my_cluster' (keyspaces shop), not in the ring, stopped",
        "why": "a former member of this cluster: the writes only it holds would be lost if wiped", "former": True},
    "_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": "rack_b"},
    "cassandra_new_node_state": "new",
}
OTHER = {
    "_cassandra_node_reset_plan": {"problems": ["its data is of cluster 'billing', neither 'my_cluster' nor the package's"
                                                " stock 'Test Cluster'", "/data: can not be read (without root?)"]},
    "_cassandra_node_reset_check": {
        "problems": ["its data is of cluster 'billing', neither 'my_cluster' nor the package's stock 'Test Cluster'"],
        "holds": "holds 3.0 GiB of cluster 'billing', not in the ring, stopped",
        "why": "its data is of cluster 'billing', neither 'my_cluster' nor the package's stock 'Test Cluster'",
        "way": "a node of another cluster: take it out of cassandra_target_nodes, or, if nothing it holds is needed,"
               " empty its directories by hand (reset_node refuses another cluster's node)",
        "former": False},
    "_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": "rack_c"},
    "cassandra_new_node_state": "new",
}
PLAYBOOK = """
- hosts: all
  gather_facts: false
  any_errors_fatal: true
  vars:
    _new: [node7, node8]
    _p0: {cassandra_cluster_name: my_cluster}
  tasks:
    - name: The facts the checks before set
      ansible.builtin.set_fact:
        "{{ item.key }}": "{{ item.value }}"
      loop: "{{ facts[inventory_hostname] | dict2items }}"

    - name: The refusal
      ansible.builtin.include_tasks: refusal.yml

    - name: Go on
      ansible.builtin.debug:
        msg: went on
      vars:
        cassandra_output: true
"""


def run(tmp_path, task, facts):
    (tmp_path / "refusal.yml").write_text(yaml.safe_dump([TASKS[task]]))
    (tmp_path / "playbook.yml").write_text(PLAYBOOK)
    (tmp_path / "inventory").write_text("[my_cluster]\nnode7\nnode8\n")
    (tmp_path / "facts.json").write_text(json.dumps({"facts": facts}))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_FORCE_COLOR="0", PWD=str(tmp_path), COLUMNS="80")
    env.pop("ANSIBLE_CONFIG", None)
    env.pop("ANSIBLE_INVENTORY", None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", "inventory", "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable, "-e", "@facts.json",
            "-e", "cassandra_target_nodes=node7,node8", "playbook.yml"]
    proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          check=False, cwd=str(tmp_path), timeout=300)
    out = proc.stdout.decode("utf-8", "replace")
    # each rule as 4 of its characters (its width is the terminal's)
    return proc.returncode, [re.sub(r"^([-=])\1{3,}$", lambda m: m.group(1) * 4, line) for line in out.splitlines()]


def test_former_member_and_node_of_another_cluster_one_block(tmp_path):
    rc, lines = run(tmp_path, "Refuse the new nodes add_node may not reset", {"node7": FORMER, "node8": OTHER})
    assert rc != 0
    assert lines == [
        "====",
        "",
        "REFUSED  add_node  my_cluster  nothing was changed",
        "",
        "----",
        "",
        "node7 (dc1/rack_b): holds 12.0 GiB of cluster 'my_cluster' (keyspaces shop), not in the ring, stopped"
        " — a former member of this cluster: the writes only it holds would be lost if wiped",
        "node8 (dc1/rack_c): holds 3.0 GiB of cluster 'billing', not in the ring, stopped"
        " — its data is of cluster 'billing', neither 'my_cluster' nor the package's stock 'Test Cluster';"
        " reset refused: /data: can not be read (without root?)",
        "",
        "----",
        "",
        "TO DO",
        "  1. if what node7 holds is not needed (snapshot or copy it first if in doubt), empty it:",
        "     ansible-playbook -i inventory community.cassandra.reset_node -e cassandra_target_nodes=node7",
        "",
        "  2. then run add_node again without node8:",
        "     ansible-playbook -i inventory community.cassandra.add_node -e cassandra_target_nodes=node7",
        "",
        "  3. or both in one run (add_node empties it first):",
        "     ansible-playbook -i inventory community.cassandra.add_node -e cassandra_target_nodes=node7"
        " -e cassandra_add_node_reset=true",
        "",
        "  4. node8: a node of another cluster: take it out of cassandra_target_nodes, or, if nothing it holds is needed,"
        " empty its directories by hand (reset_node refuses another cluster's node); then run add_node again:",
        "     ansible-playbook -i inventory community.cassandra.add_node -e cassandra_target_nodes=node7,node8",
    ]


def test_two_former_members(tmp_path):
    rc, lines = run(tmp_path, "Refuse the new nodes add_node may not reset", {"node7": FORMER, "node8": dict(
        FORMER, _cassandra_preflight={"cassandra_dc": "dc1", "cassandra_rack": "rack_c"})})
    assert rc != 0
    assert "TASK [" not in "\n".join(lines) and "fatal" not in "\n".join(lines)
    assert lines[lines.index("TO DO"):] == [
        "TO DO",
        "  1. if what node7, node8 hold is not needed (snapshot or copy it first if in doubt), empty them:",
        "     ansible-playbook -i inventory community.cassandra.reset_node -e cassandra_target_nodes=node7,node8",
        "",
        "  2. then run add_node again:",
        "     ansible-playbook -i inventory community.cassandra.add_node -e cassandra_target_nodes=node7,node8",
        "",
        "  3. or both in one run (add_node empties them first):",
        "     ansible-playbook -i inventory community.cassandra.add_node -e cassandra_target_nodes=node7,node8"
        " -e cassandra_add_node_reset=true",
    ]


def test_nothing_refused_goes_on(tmp_path):
    fine = dict(FORMER, _cassandra_node_reset_plan={"problems": []}, _cassandra_node_reset_check={"problems": []})
    rc, lines = run(tmp_path, "Refuse the new nodes add_node may not reset", {"node7": fine, "node8": fine})
    assert rc == 0, lines
    assert lines == ["went on", "went on"]


def test_a_node_already_in_the_ring_one_block(tmp_path):
    down = {"cassandra_new_node_state": "replace", "_cassandra_preflight": {"cassandra_dc": "dc1", "cassandra_rack": "rack_b"},
            "cassandra_new_node_address": "10.0.0.7",
            "_cassandra_new_node_refusal": "a dead node with this address is in the ring (DN, host ID 1e96). Adding this node"
                                           " would be a replace, not an add: run replace_node."}
    rc, lines = run(tmp_path, "Refuse the nodes that are not new", {"node7": down, "node8": {"cassandra_new_node_state": "new"}})
    assert rc != 0
    assert lines == [
        "====",
        "",
        "REFUSED  add_node  my_cluster  nothing was changed",
        "",
        "----",
        "",
        "node7 (10.0.0.7, dc1/rack_b): a dead node with this address is in the ring (DN, host ID 1e96). Adding this node would be a"
        " replace, not an add: run replace_node.",
        "",
        "----",
        "",
        "TO DO",
        "  1. node7: do what its line above says, then run add_node again if still needed:",
        "     ansible-playbook -i inventory community.cassandra.add_node -e cassandra_target_nodes=node7,node8",
        "",
        "  2. or add the other nodes now without node7:",
        "     ansible-playbook -i inventory community.cassandra.add_node -e cassandra_target_nodes=node8",
    ]


def test_the_checks_leave_the_refusal_to_add_node():
    """new_node_state.yml and reset_node_plan.yml keep their refusal for add_node's block (not per host), and fail
    on their own for the other playbooks (topology records it for its plan, reset_node and replace_node refuse)."""
    names = [t.get("name") for t in PLAY["tasks"]]
    check = [t for t in PLAY["tasks"] if t.get("name") == "Check this node"][0]
    state = [t for t in check["block"] if t.get("name") == "Find which ones already joined (an interrupted run)"][0]
    assert state["vars"]["_cassandra_new_node_refused_by_caller"] is True
    assert names.index("Check this node") + 1 == names.index("Refuse the nodes that are not new")
    plan = TASKS["Work out the reset of the new nodes (cassandra_add_node_reset)"]
    assert plan["vars"]["_cassandra_node_reset_refused_by_caller"] is True
    assert names.index("Work out the reset of the new nodes (cassandra_add_node_reset)") + 1 == names.index(
        "Refuse the new nodes add_node may not reset")
    assert names.index("Refuse the new nodes add_node may not reset") < names.index("Show the plan and confirm the add")
    roles = os.path.join(TOP, "roles", "cassandra_operation", "tasks")
    with open(os.path.join(roles, "reset_node_plan.yml"), encoding="utf-8") as f:
        refuse = [t for t in yaml.safe_load(f) if t.get("name") == "Refuse the reset of {{ inventory_hostname }}"][0]
    assert "not _cassandra_node_reset_refused_by_caller | default(false) | bool" in refuse["when"]
    with open(os.path.join(roles, "new_node_state.yml"), encoding="utf-8") as f:
        block = yaml.safe_load(f)[0]["block"]
    refuse = [t for t in block if t.get("name") == "Refuse a node that is not simply new or joined"][0]
    assert "not _cassandra_new_node_refused_by_caller | default(false) | bool" in refuse["when"]
    assert refuse["ansible.builtin.fail"]["msg"] == "{{ inventory_hostname }} ({{ _address }}): {{ _cassandra_new_node_refusal }}"


def test_a_former_member_with_a_directory_problem_gets_no_reset_suggested(tmp_path):
    both = dict(FORMER, _cassandra_node_reset_plan={"problems": FORMER["_cassandra_node_reset_plan"]["problems"]
                                                    + ["/data: can not be read (without root?)"]})
    rc, lines = run(tmp_path, "Refuse the new nodes add_node may not reset", {"node7": both, "node8": dict(
        FORMER, _cassandra_node_reset_plan={"problems": []}, _cassandra_node_reset_check={"problems": []})})
    assert rc != 0
    text = "\n".join(lines)
    assert "reset refused: /data: can not be read (without root?)" in text
    assert "cassandra_add_node_reset=true" not in text and "reset_node" not in text


def test_a_stopped_add_says_how_to_go_on():
    """add_node keeps no progress file: the same command again finds where each node stands."""
    add = [p for p in yaml.safe_load(open(os.path.join(TOP, "playbooks", "add_node.yml"), encoding="utf-8"))
           if p.get("name") == "Add the new nodes, one at a time"][0]
    hint = [t for t in add["tasks"] if t.get("name") == "Add this node"][0]["vars"]["cassandra_service_resume_hint"]
    assert "rolling_resume" not in hint and "run {{ _cassandra_screen_asked_by | default('') or 'add_node' }} again" in hint
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "node_operation.yml"), encoding="utf-8") as f:
        text = f.read()
    # the progress file named only when there is one
    assert "(progress: ' ~ cassandra_progress_file ~ ')') if cassandra_progress_file is defined else ''" in text
    assert "cassandra_progress_file | default('none')" not in text
