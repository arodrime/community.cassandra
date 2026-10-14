from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's stop on a failed self-check, its task run for real by ansible-playbook on what the self-check
# found: a short block (the verdict, what differs grouped by what makes it pass, how, where the details are),
# plain text under the ops callback (red on its verdict only) and the default one, no secret shown.

import json
import os
import re
import subprocess
import sys

import pytest
import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
COLLECTIONS = os.path.abspath(os.path.join(TOP, "..", "..", ".."))
with open(os.path.join(TOP, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    WRITE = next(p for p in yaml.safe_load(f) if p["name"] == "Write the inventory")
STOP = next(t for t in WRITE["tasks"] if t["name"] == "Stop on a failed self-check")

SECRET = "hunter2"
DATA = {
    "layout": {"cluster_group": "my_cluster",
               "nodes": [{"name": "node1", "read": True, "hand_edits": ["cassandra-env.sh, line 311:",
                                                                        "  + export KEYSTORE_PASSWORD=****"]},
                         {"name": "node2", "read": True, "hand_edits": ["jvm17-server.options, line 40:",
                                                                        "  + -XX:+UseFoo"]},
                         {"name": "node3", "read": True, "hand_edits": []},
                         {"name": "node4", "read": False, "reason": "unreachable", "hand_edits": []}],
               "theirs_win": [{"node": "node3", "key": "cassandra_concurrent_reads",
                               "path": "host_vars/node3/local.yml"}],
               "yours": [{"node": "node3", "key": "cassandra_concurrent_reads", "value": 64, "yours": 16,
                          "path": "host_vars/node3/local.yml"}]},
    "self_check": {
        "node1": {"differences": ["cassandra-env.sh: node has export KEYSTORE_PASSWORD=****, import would write nothing",
                                  "cassandra.yaml: server_encryption_options.keystore_password: node has '%s',"
                                  " import would write 'cassandra'" % SECRET]},
        "node2": {"differences": ["jvm17-server.options: UseFoo: node has '-XX:+UseFoo', import would write nothing"]},
        "node3": {"differences": ["cassandra.yaml: concurrent_reads: node has '64', import would write '16'"]}}}


def playbook(tmp_path):
    (tmp_path / "data.json").write_text(json.dumps(DATA))
    read = "{{ (lookup('ansible.builtin.file', 'data.json') | from_json).%s }}"
    plays = [{"name": "Write the inventory", "hosts": "localhost", "connection": "local", "gather_facts": False,
              "vars": {"_self_check_ok": False, "_newline": WRITE["vars"]["_newline"]},
              "tasks": [{"name": "Read what the import found", "ansible.builtin.set_fact": {
                  "_layout": read % "layout", "_self_check": read % "self_check",
                  "_report_args": {"report_file": str(tmp_path / "reports" / "my_cluster" / "report.txt"),
                                   "allow_unread": False, "check": "{{ ansible_check_mode }}", "inventory": "",
                                   "hosts": "", "cwd": str(tmp_path)}}}, STOP]}]
    (tmp_path / "stop.yml").write_text(yaml.safe_dump(plays, sort_keys=False))


def run(tmp_path, *extra, **env):
    playbook(tmp_path)
    environ = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_LOCALHOST_WARNING="0",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
                   ANSIBLE_PYTHON_INTERPRETER=sys.executable, **env)
    for name in ("ANSIBLE_CONFIG", "ANSIBLE_INVENTORY", "CASSANDRA_CLUSTER", "COLUMNS"):
        environ.pop(name, None)
    if "ANSIBLE_NOCOLOR" in env:
        environ.pop("ANSIBLE_FORCE_COLOR", None)  # (ansible-test --color sets it, which wins)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "stop.yml"] + list(extra)
    result = subprocess.run(argv, env=environ, cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=300, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


def block(out):
    """The stop's lines under the ops callback: from its verdict."""
    lines = out.rstrip("\n").split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith("SELF-CHECK FAILED"))
    return [line for line in lines[at:] if not re.match(r"^\s*(PLAY RECAP|localhost\s+:)", line)]


@pytest.mark.parametrize("check", [False, True])
def test_the_stop_under_the_ops_callback(tmp_path, check):
    rc, out = run(tmp_path, *(["--check"] if check else []), ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
                  ANSIBLE_NOCOLOR="1")
    assert rc == 2, out
    lines = block(out)
    text = "\n".join(lines)
    assert lines[:2] == ["SELF-CHECK FAILED  my_cluster  the inventory would change 4 settings on 3",
                         "  nodes; 1 node not read %s" % ("(--check: the inventory not written)" if check
                                                          else "(written for inspection only, marked NOT VALID)")]
    for line in ("  Hand edits no variable covers (cassandra_config would revert them):",
                 "    - jvm17-server.options, on node2:", "        -XX:+UseFoo (not in the inventory)",
                 "  Your inventory files override the import (Ansible will apply your value):",
                 "  Not read, so not checked: node4 (unreachable)", "HOW TO MAKE IT PASS",
                 "Details: reports/my_cluster/report.txt, section SELF-CHECK"):
        assert line in lines, out
    assert "-e import_cluster_keep_hand_edits=true" in text and "-e import_cluster_allow_unread=true" in text
    assert "-e import_cluster_strict=false" in text
    assert "ansible-playbook community.cassandra.apply_config --check --diff" in text
    # blank lines between the groups, no rule, no task dump, no secret
    assert lines.count("") >= 3 and not [line for line in lines if re.match(r"^[-=]{4,}$", line)]
    assert "fatal:" not in out and "TASK [" not in out and SECRET not in out
    assert max(len(line) for line in lines if "apply_config --check --diff" not in line) <= 79


def test_the_stop_in_colour_red_on_its_verdict_only(tmp_path):
    rc, out = run(tmp_path, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops", ANSIBLE_FORCE_COLOR="1")
    assert rc == 2, out
    red = [line for line in out.split("\n") if "\x1b[0;31m" in line]
    assert [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in red] == [
        "SELF-CHECK FAILED  my_cluster  the inventory would change 4 settings on 3",
        "  nodes; 1 node not read (written for inspection only, marked NOT VALID)"]
    assert "\x1b" not in [line for line in out.split("\n") if "HOW TO MAKE IT PASS" in line][0]
    assert SECRET not in out


@pytest.mark.parametrize("result_format", ["yaml", "json"])
def test_the_stop_under_the_default_callback(tmp_path, result_format):
    # one text: plain lines under the yaml result format; ansible-core 2.19+ prints it as is before the result
    rc, out = run(tmp_path, ANSIBLE_STDOUT_CALLBACK="ansible.builtin.default", ANSIBLE_NOCOLOR="1",
                  ANSIBLE_CALLBACK_RESULT_FORMAT=result_format)
    assert rc == 2, out
    assert SECRET not in out
    plain = re.search(r"^HOW TO MAKE IT PASS$", out, re.M) or re.search(r"^ +HOW TO MAKE IT PASS$", out, re.M)
    if result_format == "yaml":
        assert plain and re.search(r"^ *msg: \|-?\n *SELF-CHECK FAILED  my_cluster  ", out, re.M), out
    else:
        assert '"msg": "SELF-CHECK FAILED  my_cluster  the inventory would change 4 settings' in out
        assert "\\nHOW TO MAKE IT PASS\\n" in out


def test_the_stop_is_one_text_of_the_filter():
    msg = STOP["ansible.builtin.fail"]["msg"]
    assert "cassandra_import_self_check_advice(" in msg and msg.strip().endswith("| join(_newline) }}")
    for given in ("report_file=_report_args.report_file", "error=_self_check_error | default('')",
                  "allow_unread=_report_args.allow_unread", "check=_report_args.check",
                  "inventory=_report_args.inventory", "hosts=_report_args.hosts", "cwd=_report_args.cwd"):
        assert given in msg
    assert STOP["vars"]["cassandra_output"] is True and STOP["run_once"] is True
    assert "import_cluster_strict | default(true) | bool" in STOP["when"]
