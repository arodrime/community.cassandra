from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config and the role's header line: a file cassandra_config leaves as it is (the same settings, e.g. taken
# over by import_cluster) gets the header alone, shown apart in the plan, no restart for it; a file kept by hand
# (cassandra_config_keep_files, import_keep_hand_edits) is not touched. And Medusa (cassandra_medusa_enabled): compared
# in the plan, applied at the node's turn, no restart for it, nothing on a node where it is false. Rendered by Ansible.

import base64
import hashlib
import os

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import HEADER_ADDED, MEDUSA_LIVE
from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    FIND, NOTE, ROLE, TOP, find, load, render, with_task_vars)
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

YAML_HEADER = "# Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file."
CONF = "/etc/cassandra"
MARK = "Managed by Ansible (community.cassandra.cassandra_config)"
MEDUSA = [{"item": "Medusa", "before": "0.29.0", "after": "0.30.1"},
          {"item": "medusa.ini [storage] bucket_name", "before": "old", "after": "new"}]


def b64(text):
    return base64.b64encode(text.encode()).decode()


def headers(kept, live, keep_files=(), new=None):
    """The role's _cassandra_config_header for the files left as they are (kept) whose live text is live {name}."""
    new = new or dict((f, YAML_HEADER + "\n" + text) for f, text in live.items())
    variables = {"_cassandra_config_kept": list(kept), "cassandra_config_keep_files": list(keep_files),
                 "_cassandra_config_compared": dict((f, b64(t)) for f, t in live.items()),
                 "_cassandra_config_new": new, "cassandra_config_storage_dir": "", "cassandra_jvm": {},
                 "cassandra_conf_dir": CONF, "_cassandra_config_header_mark": MARK,
                 "_cassandra_config_live_sha1": dict((f, hashlib.sha1(t.encode()).hexdigest()) for f, t in live.items())}
    task = find(ROLE, "List the files left as they are that get the role's header")
    return render(task["ansible.builtin.set_fact"]["_cassandra_config_header"], variables)


def test_a_file_left_as_it_is_gets_the_header_not_one_kept_by_hand():
    live = {"cassandra.yaml": "num_tokens: 16\n", "logback.xml": "<configuration/>\n"}
    found = headers(["cassandra.yaml", "logback.xml"], live, keep_files=["logback.xml"])
    assert found == [{"file": "cassandra.yaml", "path": CONF + "/cassandra.yaml", "header": YAML_HEADER, "mark": MARK,
                      "sha1": hashlib.sha1(b"num_tokens: 16\n").hexdigest()}]
    # it has one already: nothing (idempotent)
    assert headers(["cassandra.yaml"], {"cassandra.yaml": YAML_HEADER + "\nnum_tokens: 16  # x\n"},
                   new={"cassandra.yaml": YAML_HEADER + "\nnum_tokens: 16\n"}) == []
    # cassandra_config_normalize (nothing compared: every file rewritten from the template), a new node: nothing
    assert headers([], {}) == []


def needs(changes=(), header=(), medusa=None, enabled=True, running=True):
    """(todo, then, kept_running, steps, medusa) of a running node imported, with no record of its start."""
    variables = {"_cassandra_config_changes": list(changes), "_cassandra_config_new": {}, "cassandra_conf_dir": CONF,
                 "_cassandra_config_header": [{"file": f} for f in header],
                 "_cassandra_medusa_items": MEDUSA if medusa is None else medusa, "cassandra_medusa_enabled": enabled,
                 "cassandra_pending_snapshot": {"failed": True}, "_cassandra_config_perm_changes": [],
                 "_cassandra_config_dir_notes": [], "cassandra_config_restart_pending": False,
                 "cassandra_config_newer_files": [], "cassandra_config_unknown_files": [], "_cassandra_config_kept": [],
                 "cassandra_config_keep_files": [], "_cassandra_preflight_running": running,
                 "cassandra_apply_config_unit": {"skipped": True},
                 "cassandra_jvm": {"pid": "42", "start": "100.00"} if running else {}, "inventory_hostname": "n1",
                 "_cassandra_apply_config_as_started": [], "_cassandra_apply_config_conf_switch": False}
    variables = with_task_vars(NOTE, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    keys = ("cassandra_apply_config_todo", "cassandra_apply_config_then", "cassandra_apply_config_kept_running",
            "cassandra_apply_config_steps", "cassandra_apply_config_medusa")
    out = [render(facts[k], variables) for k in keys]
    return (out[0] in (True, "True"),) + tuple(out[1:])


def test_after_an_import_the_header_alone_is_no_restart():
    # import-then-no-op: the files the import took over have the same settings, they only get the header
    assert needs(header=["cassandra.yaml", "logback.xml"], enabled=False) == (True, "none", [HEADER_ADDED], [], [])
    # nothing at all: up to date
    assert needs(enabled=False)[:2] == (False, "none")


def test_the_header_never_spares_a_restart_another_file_needs():
    todo, then, kept, steps, dummy = needs(changes=["jvm-server.options"], header=["cassandra.yaml"], enabled=False)
    assert (todo, then, kept) == (True, "restart", [])
    assert steps == ["write the config", "drain and restart Cassandra"]


def test_medusa_is_compared_and_applied_with_no_restart():
    assert needs() == (True, "none", [MEDUSA_LIVE], [], MEDUSA)
    # with a restart for the config: set up first, at the node's turn
    assert needs(changes=["cassandra.yaml"])[3] == ["write the config", "set up Medusa", "drain and restart Cassandra"]
    # Cassandra stopped on purpose: Medusa set up, Cassandra left stopped
    assert needs(running=False)[:2] == (True, "write")


def test_nothing_of_medusa_where_it_is_not_enabled():
    # (items left by an earlier play, a variable of another run: never taken)
    assert needs(enabled=False) == (False, "none", [], [], [])
    assert needs(medusa=[]) == (False, "none", [], [], [])


def test_medusa_is_compared_in_check_mode_on_its_nodes_only():
    compare = find(FIND["tasks"], "Compare Medusa")
    role = compare["ansible.builtin.include_role"]
    assert role["name"] == "community.cassandra.cassandra_medusa" and role["apply"] == {"check_mode": True}
    assert compare["when"] == "cassandra_medusa_enabled | default(false) | bool"
    assert compare["vars"]["_cassandra_medusa_compare"] is True and compare["vars"]["cassandra_change_report_dir"] == ""
    names = [t.get("name") for t in FIND["tasks"]]
    assert names.index("Compare Medusa") < names.index("Note what this node needs")
    # in the view: Medusa's changes beside the config's
    side = find(FIND["tasks"], "Put the settings that differ side by side")
    assert "'medusa': v.cassandra_apply_config_medusa" in side["vars"]["_nodes"]


def test_medusa_is_set_up_at_the_node_turn_never_under_check():
    action = load("roles", "cassandra_operation", "tasks", "action_apply_config.yml")
    names = [t.get("name") for t in action]
    setup = action[names.index("Set up Medusa")]
    assert setup["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_medusa"}
    assert setup["when"] == ["not ansible_check_mode", "cassandra_apply_config_medusa | default([]) | length > 0"]
    assert names.index("Write the config") < names.index("Set up Medusa") < names.index("Drain and restart the node")


def test_the_medusa_role_knows_the_compare_is_check_mode():
    # (check mode applied by apply_config: ansible_check_mode false) no link to a Medusa not installed yet
    role = load_tasks(os.path.join(TOP, "roles", "cassandra_medusa", "tasks", "main.yml"))
    link = find(role, "Link medusa into {{ cassandra_medusa_link_dir }}")
    when = link["when"][-1]
    for check, compare, installed, runs in ((False, True, "", False), (False, True, "0.30.1", True),
                                            (True, False, "", False), (False, False, "", True)):
        variables = {"ansible_check_mode": check, "_cassandra_medusa_compare": compare, "_cassandra_medusa_installed": installed}
        assert render("{{ %s }}" % when, variables) is runs
    items = find(role, "List the changes")["ansible.builtin.set_fact"]["_cassandra_medusa_items"]
    report = find(role, "Report changes")
    assert "_cassandra_medusa_ini_changes" in items
    assert report["vars"]["cassandra_change_report_items"] == "{{ _cassandra_medusa_items }}"


def test_the_header_is_recorded_as_the_running_cassandra_s_file_by_file():
    header = load("roles", "cassandra_config", "tasks", "header.yml")
    names = [t["name"] for t in header]
    assert names[0] == "Record the config the running Cassandra started with"  # before anything is written
    record = header[names.index("Record them with their header as the files the running Cassandra runs with")]
    request = record["vars"]["_cassandra_service_record_request"]
    item = {"key": CONF + "/cassandra.yaml", "value": {"status": "added", "before": "a" * 40, "after": "b" * 40}}
    variables = {"item": item, "cassandra_jvm": {"pid": "42", "start": "100.00"}}
    # only when the record has the file as it was (the running Cassandra started with it), as written
    assert render(request["before"], variables) == {CONF + "/cassandra.yaml": "a" * 40}
    assert render(request["files"], variables) == {CONF + "/cassandra.yaml": "b" * 40}
    assert "cassandra_jvm.pid is defined" in record["when"]
    added = {"stdout": '{"/etc/cassandra/cassandra.yaml": {"status": "added"}, "/etc/cassandra/logback.xml": {"status": "present"}}'}
    assert len(render(record["loop"], {"cassandra_config_header_added": added})) == 1


def test_the_header_command_skipped_under_check_is_read_as_nothing_added():
    # (--check: the command is skipped, its stdout empty; apply_config --check stopped on from_json of '')
    header = load("roles", "cassandra_config", "tasks", "header.yml")
    names = [t["name"] for t in header]
    say = header[names.index("Say the files the header was not added to")]
    record = header[names.index("Record them with their header as the files the running Cassandra runs with")]
    skipped = {"cassandra_config_header_added": {"skipped": True, "changed": False, "rc": 0, "stdout": ""}}
    assert render(say["vars"]["_result"], skipped) == {}
    assert render(record["loop"], skipped) == []


def test_the_header_is_added_after_the_seed_and_not_to_the_jmx_files_kept_by_hand():
    apply = load("roles", "cassandra_config", "tasks", "apply.yml")
    seed = apply[0]
    assert "_cassandra_config_header | default([])" in seed["when"][1]
    access = load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "access.yml"))
    find_jmx = find(access, "Find the JMX files left as they are without the role's header")
    variables = {"cassandra_config_jmx_same": {"stdout": '["password", "access"]'},
                 "cassandra_config_keep_files": ["jmxremote.password"], "_cassandra_config_header_mark": MARK,
                 "_cassandra_jmx_password_content": YAML_HEADER + "\nops s3cret\n",
                 "_cassandra_jmx_access_content": YAML_HEADER + "\nops readwrite\n",
                 "cassandra_jmx_remote_password_file": CONF + "/jmxremote.password",
                 "cassandra_jmx_remote_access_file": CONF + "/jmxremote.access"}
    assert render(find_jmx["vars"]["_files"], variables) == [
        {"file": "jmxremote.access", "header": YAML_HEADER, "mark": MARK, "path": CONF + "/jmxremote.access"}]


@pytest.mark.parametrize("template, first", [
    ("roles/cassandra_config/templates/jmxremote.access.j2", YAML_HEADER),
    ("roles/cassandra_config/templates/jmxremote.password.j2", YAML_HEADER),
    ("roles/cassandra_linux/templates/disable-thp.service.j2",
     "# Managed by Ansible (community.cassandra.cassandra_linux): change the role variables, not this file."),
])
def test_every_template_starts_with_the_header(template, first):
    with open(os.path.join(TOP, template), encoding="utf-8") as f:
        assert f.readline().rstrip("\n") == first


def test_the_tuned_profile_has_the_header_from_its_lines():
    # (cassandra-disks.tuned.conf.j2 writes the lines cassandra_disk_tuned_profile gives: the header first)
    from ansible_collections.community.cassandra.tests.unit.plugins.filter.test_cassandra_disks import (
        LV, RHEL_TUNED, SDB, TUNED_STATE, TUNED_UDEV)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_disks import cassandra_disk_tuned_profile
    lines = cassandra_disk_tuned_profile(RHEL_TUNED, [LV, SDB], TUNED_STATE, TUNED_UDEV, 4)["lines"]
    assert lines[0] == "# Managed by Ansible (community.cassandra.cassandra_linux): change the role variables, not this file."


def test_the_files_that_get_the_header_are_said_by_one_rule():
    # (confirm.yml's config files and access.yml's JMX files: one list of changes, the same words)
    items = load("roles", "cassandra_config", "vars", "main.yml")["_cassandra_config_header_items"]
    header = [{"file": "cassandra.yaml", "path": CONF + "/cassandra.yaml"},
              {"file": "jmxremote.access", "path": CONF + "/jmxremote.access"}]
    assert render(items, {"_cassandra_config_header": header}) == [
        {"item": h["path"], "before": "no header", "after": "header added", "header": True} for h in header]
    confirm = load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "confirm.yml"))
    access = load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "access.yml"))
    assert "_cassandra_config_header_items" in find(confirm, "Record the changes for the report")[
        "ansible.builtin.set_fact"]["_cassandra_config_items"]
    said = find(access, "Say them among the changes")["ansible.builtin.set_fact"]["_cassandra_config_items"]
    assert render(said, {"_cassandra_config_items": [{"item": "x", "before": "a", "after": "b"}] + render(
        items, {"_cassandra_config_header": header[:1]}), "_cassandra_config_header_items": render(
            items, {"_cassandra_config_header": header})}) == [{"item": "x", "before": "a", "after": "b"}] + render(
        items, {"_cassandra_config_header": header})
