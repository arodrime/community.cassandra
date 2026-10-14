from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node on nodes emptied by reset_node, then one with its cassandra.yaml
# deleted and one with a setting changed by hand: they are new nodes for
# cassandra_config (no second question after add_node's, no "already
# initialized"), and the answer to cassandra_config's own question, when
# it asks, holds for every node of the batch.

import os
import re
import select
import sys
import time

import pytest
import yaml

from ansible_collections.community.cassandra.tests.unit.roles.test_config_preview import (
    initialized_facts, render, task, task_vars)

pty = pytest.importorskip("pty")

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
DATA = "/data/cassandra/data"
STALE = "/var/lib/cassandra/data"  # the package's default: not one of the inventory's data dirs
# a node emptied by reset_node: its data dirs empty, a system keyspace left in the package's default dir
RESET = dict(cassandra_data_file_directories=[DATA], isdir=lambda d: d == STALE + "/system")
EDITED = "cluster_name: 'my_cluster'\nnum_tokens: 8\ndata_file_directories:\n  - /data/cassandra/data\n" \
         "commitlog_directory: /data/cassandra/commitlog\nconcurrent_reads: 64\n"


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_reset_node_without_cassandra_yaml_is_new():
    facts = initialized_facts(None, _cassandra_config_new_node=True, **RESET)
    assert facts["_cassandra_config_initialized"] is False
    assert facts["_cassandra_config_initialized_why"] == ""
    assert facts["_cassandra_config_left"] == [STALE]  # said, left as it is
    assert facts["_cassandra_config_reset"] == ""


def test_reset_node_with_a_setting_changed_by_hand_is_new():
    facts = initialized_facts(EDITED, _cassandra_config_new_node=True, **RESET)
    assert facts["_cassandra_config_initialized"] is False and facts["_cassandra_config_left"] == [STALE]


def test_outside_an_operation_that_checked_the_node_the_stale_dir_still_counts():
    # apply_config on a node whose inventory does not match it yet: the guard stays
    facts = initialized_facts(None, **RESET)
    assert facts["_cassandra_config_initialized"] is True
    assert facts["_cassandra_config_initialized_why"] == STALE + "/system"
    assert facts["_cassandra_config_left"] == []


@pytest.mark.parametrize("new", [False, True])
def test_a_missing_cassandra_yaml_never_makes_a_node_initialized(new):
    facts = initialized_facts(None, isdir=lambda d: False, _cassandra_config_new_node=new)
    assert facts["_cassandra_config_initialized"] is False and facts["_cassandra_config_initialized_why"] == ""


@pytest.mark.parametrize("live, readable, isdir", [
    ("data_file_directories: [/old/data]\n", False, lambda d: False),  # the live file can't be read
    ("data_file_directories: ['/x{{ 1 }}', /old]\n", True, lambda d: False),  # a path with "{"
    ("data_file_directories: [/old/data]\n", True, lambda d: d == "/old/data/system"),  # a former config's dir
])
def test_new_node_reasons_outside_its_data_dirs_left_out(live, readable, isdir):
    assert initialized_facts(live, readable=readable, isdir=isdir)["_cassandra_config_initialized"] is True
    facts = initialized_facts(live, readable=readable, isdir=isdir, _cassandra_config_new_node=True)
    assert facts["_cassandra_config_initialized"] is False


@pytest.mark.parametrize("found, services, why", [
    ([DATA], {}, DATA + "/system"),  # its own data dir: a reset that did not happen (--check)
    (["/ssd/system"], {}, "/ssd/system/system"),  # local_system_data_file_directory
    ([], {"cassandra.service": {"state": "running"}}, "Cassandra is running"),
])
def test_a_new_node_with_data_in_its_own_dirs_or_running_is_still_initialized(found, services, why):
    facts = initialized_facts(None, isdir=lambda d: d[:-len("/system")] in found, services=services,
                              cassandra_data_file_directories=[DATA],
                              cassandra_extra_settings={"local_system_data_file_directory": "/ssd/system"},
                              _cassandra_config_new_node=True)
    assert facts["_cassandra_config_initialized"] is True and facts["_cassandra_config_initialized_why"] == why


def test_a_stat_error_outside_its_data_dirs_does_not_count_for_a_new_node():
    def isdir(d):
        if d == "/x/system":
            raise OSError(13, "Permission denied", d)
        return False
    live = "data_file_directories: [/x]\n"
    assert initialized_facts(live, isdir=isdir)["_cassandra_config_initialized"] is True
    assert initialized_facts(live, isdir=isdir, _cassandra_config_new_node=True)["_cassandra_config_initialized"] is False
    # in its own data dir: still not checked
    assert initialized_facts(live, isdir=isdir, cassandra_data_file_directories=["/x"],
                             _cassandra_config_new_node=True)["_cassandra_config_initialized"] is True


def prepare_vars(state, confirm=None, replace=False, planned=True, operation_confirm=None):
    """What action_add_prepare.yml passes to cassandra_config for a node in that state (planned: add_node worked
    out its reset, cassandra_add_node_reset not false)."""
    tasks = dict((t["name"], t) for t in load("roles", "cassandra_service", "tasks", "action_add_prepare.yml"))
    tell = tasks["Tell cassandra_config whether the node is new"]
    v = {} if state is None else {"cassandra_new_node_state": state}
    if planned and state is not None:
        v["_cassandra_node_reset_plan"] = {"problems": [], "delete": []}
    if operation_confirm is not None:
        v["cassandra_operation_confirm"] = operation_confirm
    if replace:
        v["_cassandra_add_checked_new"] = True
    if confirm is not None:
        v["cassandra_config_confirm"] = confirm
    v = task_vars(tell, **v)
    facts = dict((k, render(t, **v)) for k, t in tell["ansible.builtin.set_fact"].items())
    include = tasks["Write the config"]
    assert include["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_config"}
    return dict((k, render(t, **facts)) for k, t in include["vars"].items() if k != "cassandra_seeds")


@pytest.mark.parametrize("state, replace, confirm, given, new", [
    ("new", False, False, None, True), ("new", False, False, True, True),  # add_node's question covered it
    (None, True, False, None, True),  # replace_node: checked blank, or reset first
    (None, False, "auto", None, False),  # add_datacenter: not checked, cassandra_config's own guard stays
    ("joined", False, "auto", None, False), ("joined", False, True, True, False), ("joining", False, "auto", None, False),
])
def test_add_prepare_asks_nothing_more_for_a_new_node(state, replace, confirm, given, new):
    # _cassandra_config_no_question: what an -e cassandra_config_confirm=true (which beats the include's
    # cassandra_config_confirm) can't override
    assert prepare_vars(state, given, replace) == {"cassandra_config_confirm": confirm, "_cassandra_config_new_node": new,
                                                   "_cassandra_config_no_question": confirm is False}


def test_add_node_reset_false_keeps_the_guard_of_cassandra_config():
    # -e cassandra_add_node_reset=false: no reset plan, the live cassandra.yaml's dirs not looked at by add_node
    assert prepare_vars("new", planned=False) == {"cassandra_config_confirm": "auto", "_cassandra_config_new_node": False,
                                                  "_cassandra_config_no_question": False}


def test_no_second_question_whatever_e_cassandra_config_confirm_says():
    # add_node -e cassandra_add_node_reset=true -e cassandra_config_confirm=true: the extra var reaches
    # cassandra_config as true, the add's own question covered the new nodes' config
    template = task("List the files to change and whether to ask first")["ansible.builtin.set_fact"]["_cassandra_config_ask"]
    preview = {"results": [{"stdout": "diff", "cassandra_config_file": "cassandra.yaml"}]}
    passed = prepare_vars("new", confirm=True)
    assert passed["_cassandra_config_no_question"] is True
    assert render(template, cassandra_config_confirm=True, _cassandra_config_initialized=True, _kept=[],
                  cassandra_config_preview=preview, _cassandra_config_no_question=True) is False
    assert render(template, cassandra_config_confirm=True, _cassandra_config_initialized=True, _kept=[],
                  cassandra_config_preview=preview) is True  # a run of the role on its own: asked
    # the operations whose own question shows the config changes say so too; update_java's and
    # add_datacenter's show none: -e cassandra_config_confirm=true still asks there
    for path, said in ((("roles", "cassandra_service", "tasks", "action_apply_config.yml"), [True]),
                       (("roles", "cassandra_service", "tasks", "action_upgrade.yml"), [True]),
                       (("roles", "cassandra_service", "tasks", "action_update_java.yml"), [None])):
        writes = [t for t in load(*path) if (t.get("ansible.builtin.include_role") or {}).get("name")
                  == "community.cassandra.cassandra_config"]
        assert [t["vars"].get("_cassandra_config_no_question") for t in writes] == said, path
    assert "_cassandra_config_no_question" not in open(os.path.join(TOP, "playbooks", "add_datacenter.yml")).read()


def test_the_question_comes_after_the_diffs_it_is_about():
    # "diffs above": printed by the ops callback too (marked), each node's under its name, when it asks
    shown = task("Show the config changes")
    asked = task("Show the config changes to confirm")
    assert "cassandra_output" not in (shown.get("vars") or {}) and asked["vars"]["cassandra_output"] is True
    preview = {"results": [{"stdout": "-a\n+b", "stdout_lines": ["-a", "+b"], "cassandra_config_file": "cassandra.yaml"},
                           {"stdout": "", "stdout_lines": [], "cassandra_config_file": "logback.xml"}]}
    variables = dict(inventory_hostname="n4", cassandra_config_preview=preview, _cassandra_config_kept=[])
    assert render(shown["ansible.builtin.debug"]["msg"], **variables) == ["-a", "+b"]
    assert render(asked["ansible.builtin.debug"]["msg"], **variables) == ["n4:", "-a", "+b"]
    for ask, check, says in ((True, False, (False, True)), (False, False, (True, False)), (True, True, (True, False))):
        variables = dict(_cassandra_config_changes=["cassandra.yaml"], _cassandra_config_ask=ask, ansible_check_mode=check)
        assert tuple(all(render("{{ %s }}" % c, **variables) for c in t["when"]) for t in (shown, asked)) == says


def test_replace_node_says_its_replacement_was_checked():
    plays = load("playbooks", "replace_node.yml")
    replace = [t for p in plays for t in p.get("tasks", []) if t.get("name") == "Replace"][0]
    assert replace["vars"]["_cassandra_add_checked_new"] is True
    names = [t.get("name") for p in plays for t in p.get("tasks", []) for t in [t] + t.get("block", [])]
    assert names.index("The new host is blank") < names.index("Replace")


@pytest.mark.parametrize("initialized", [False, True])
def test_a_new_node_with_changes_is_not_asked(initialized):
    # whatever the role finds, a node add_node checked new and confirmed is not asked again
    template = task("List the files to change and whether to ask first")["ansible.builtin.set_fact"]["_cassandra_config_ask"]
    passed = prepare_vars("new")
    assert render(template, cassandra_config_confirm=passed["cassandra_config_confirm"], _cassandra_config_initialized=initialized,
                  _kept=[], cassandra_config_preview={"results": [{"stdout": "diff", "cassandra_config_file": "cassandra.yaml"}]}) is False


def test_create_cluster_marks_the_nodes_it_emptied_new():
    plays = load("playbooks", "create_cluster.yml")
    tasks = [t for p in plays for t in p.get("tasks", []) for t in [t] + t.get("block", [])]
    forget = [t for t in tasks if t.get("name") == "Forget the cluster that was there"][0]
    assert forget["ansible.builtin.set_fact"]["_cassandra_create_cluster_emptied"] is True
    assert forget["when"] == "not ansible_check_mode"
    # for its cassandra_config only (not a fact a later include in the same run would read)
    role = [r for p in plays for r in p.get("roles", []) if isinstance(r, dict) and r.get("role") == "community.cassandra.cassandra_config"]
    assert [r["vars"] for r in role] == [{"_cassandra_config_new_node": "{{ _cassandra_create_cluster_emptied | default(false) }}"}]
    assert render(role[0]["vars"]["_cassandra_config_new_node"]) is False
    assert render(role[0]["vars"]["_cassandra_config_new_node"], _cassandra_create_cluster_emptied=True) is True


def includes(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from includes(t.get(key))


def test_every_operation_that_asked_writes_the_config_without_a_second_question():
    """Each include of the whole cassandra_config role (not its defaults) in the playbooks and roles sets
    cassandra_config_confirm: the operation's own question (or a --check preview) covers the change."""
    found = []
    for top in ("playbooks", os.path.join("roles", "cassandra_service", "tasks")):
        for name in sorted(os.listdir(os.path.join(TOP, top))):
            if not name.endswith(".yml"):
                continue
            plays = load(top, name) or []
            tasks = [t for p in plays for k in ("pre_tasks", "tasks", "post_tasks") for t in p.get(k, [])] \
                if top == "playbooks" else plays
            for t in includes(tasks):
                inc = t.get("ansible.builtin.include_role") or {}
                if inc.get("name") == "community.cassandra.cassandra_config" and "tasks_from" not in inc:
                    found.append((name, "cassandra_config_confirm" in t.get("vars", {})))
    assert ("action_add_prepare.yml", True) in found and ("apply_config.yml", True) in found
    assert [f for f in found if not f[1]] == []


# cassandra_config's own question, when it asks (apply_config... nodes already in the ring):
# one prompt for the batch, its answer for each of its nodes

PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: What the role found on each node (read from the others through hostvars)
      ansible.builtin.set_fact:
        _cassandra_config_ask: true
        _cassandra_config_changes: [cassandra.yaml]
        _cassandra_config_initialized: true
        _cassandra_config_initialized_why: /var/lib/cassandra/data/system

    - name: Write the config
      ansible.builtin.include_tasks: confirm.yml

    - name: Go on
      ansible.builtin.debug:
        msg: went on
"""


def confirm_tasks():
    """The role's two tasks, as they are in tasks/main.yml."""
    ask, check = task("Confirm config changes"), task("Stop unless the changes were confirmed")
    assert ask["register"] == "cassandra_config_answer"
    return [ask, check]


def run_in_terminal(tmp_path, answer):
    (tmp_path / "confirm.yml").write_text(yaml.safe_dump(confirm_tasks()))
    (tmp_path / "playbook.yml").write_text(PLAYBOOK)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0",
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", "node1,node2,", "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable, str(tmp_path / "playbook.yml")]
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.chdir(str(tmp_path))
        os.execve(argv[0], argv, env)
    output, deadline, sent = b"", time.time() + 120, False
    while time.time() < deadline:
        ready, dummy, dummy = select.select([fd], [], [], 1)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:  # the run ended
                break
            if not data:
                break
            output += data
        if not sent and b"Type 'yes' to apply" in output:
            time.sleep(1)  # pause drops what was typed before it reads
            os.write(fd, answer.encode() + b"\r")
            sent = True
    rc = os.waitpid(pid, 0)[1]
    rc = os.waitstatus_to_exitcode(rc) if hasattr(os, "waitstatus_to_exitcode") else rc >> 8
    return rc, output.decode(errors="replace")


def test_the_answer_holds_for_every_node_of_the_batch(tmp_path):
    rc, output = run_in_terminal(tmp_path, "yes")
    assert rc == 0, output
    assert output.count("Type 'yes' to apply") == 1  # one question for the batch
    assert re.search(r"node1: cassandra.yaml\r?\n\s*node2: cassandra.yaml", output), output
    assert len(re.findall(r'"msg": "went on"', output)) == 2, output
    assert "not confirmed" not in output


def test_no_stops_every_node_of_the_batch(tmp_path):
    rc, output = run_in_terminal(tmp_path, "no")
    assert rc != 0
    assert sorted(set(re.findall(r"Config changes not confirmed on (node\d)", output))) == ["node1", "node2"], output
    assert "went on" not in output


def test_no_question_at_all_with_cassandra_operation_confirm_false():
    # a node add_node could not check new (-e cassandra_add_node_reset=false), add_datacenter's: no question either
    assert prepare_vars("new", planned=False, operation_confirm=False)["cassandra_config_confirm"] is False
    assert prepare_vars(None, operation_confirm="false")["cassandra_config_confirm"] is False
    assert prepare_vars(None, operation_confirm=True)["cassandra_config_confirm"] == "auto"


def test_add_node_prepares_only_the_nodes_not_in_the_ring_yet():
    plays = load("playbooks", "add_node.yml")
    play = [p for p in plays if p.get("name") == "Prepare the new nodes, all at once"][0]
    block = [t for t in play["tasks"] if "block" in t][0]
    prepare = [t for t in block["block"] if t.get("name") == "Prepare this node"][0]
    assert prepare["when"] == "cassandra_new_node_state == 'new'"
