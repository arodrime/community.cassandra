from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config: a config file deleted or edited by hand on a running node, and
# written back with the content the running Cassandra started with (the record
# of its start, config_snapshot.yml) does not restart it: Cassandra runs with
# exactly that. Never when the record is not that JVM's (another pid or start:
# a start outside the roles), for cassandra.yaml, when another file changes or
# a restart is pending for another one, nor when that JVM restarted between the
# plan and the write. Rendered by Ansible.

import base64
import hashlib
import json

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    FIND, find, render, with_task_vars)

AS_STARTED = find(FIND["tasks"], "Note the files written back as the running Cassandra started with them")
NOTE = find(FIND["tasks"], "Note what this node needs")
CONF = "/etc/cassandra/conf"
OPTIONS = "jvm17-server.options"
STARTED_WITH = "-Xss256k\n-XX:+UseG1GC\n"


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def needs(changes=(OPTIONS,), new=None, record=None, recorded=("42", 100.0), start=100.0, pending=False, newer=(),
          running=True, conf=CONF, target=None, full=False, kept=(), keep_files=()):
    """(then, notes) of a running node whose files in changes are written with new {name: text}; recorded:
    the JVM (pid, start) the record was written for (None: an older record, without it)."""
    new = {OPTIONS: STARTED_WITH, "cassandra.yaml": "num_tokens: 16\n"} if new is None else new
    record = {conf + "/" + OPTIONS: sha1(STARTED_WITH), conf + "/cassandra.yaml": sha1("num_tokens: 8\n")} \
        if record is None else record
    if recorded is not None:
        record = dict(record, _jvm={"pid": recorded[0], "start": recorded[1]})
    variables = {"_cassandra_config_changes": list(changes), "_cassandra_config_new": new,
                 "cassandra_conf_dir": CONF,  # (public from the compare step; the role default of cassandra_service is not seen here)
                 "cassandra_pending_snapshot": {"content": base64.b64encode(json.dumps(record).encode()).decode()},
                 "cassandra_rpm_conf_alternative": "/etc/cassandra/conf.ansible",
                 "cassandra_config_conf_target": {"stat": {"lnk_source": target}} if target else {"skipped": True},
                 "_cassandra_config_perm_changes": [], "_cassandra_config_dir_notes": [],
                 "cassandra_config_restart_pending": pending, "cassandra_config_newer_files": list(newer),
                 "_cassandra_config_kept": list(kept), "cassandra_config_keep_files": list(keep_files),
                 "_cassandra_preflight_running": running, "cassandra_apply_config_unit": {"skipped": True},
                 "cassandra_jvm": {"pid": "42", "start": "%.2f" % start} if start is not None else {}}
    variables = with_task_vars(AS_STARTED, variables)
    variables["_cassandra_apply_config_as_started"] = render(
        AS_STARTED["ansible.builtin.set_fact"]["_cassandra_apply_config_as_started"], variables)
    variables = with_task_vars(NOTE, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    said = (render(facts["cassandra_apply_config_then"], variables), render(facts["cassandra_apply_config_notes"], variables))
    return said + (render(facts["cassandra_apply_config_as_started"], variables),) if full else said


def test_a_file_written_back_as_the_running_cassandra_started_with_it_is_no_restart():
    assert needs() == ("none", [OPTIONS + " written back as the running Cassandra started with it: no restart for it"])
    # deleted by hand: absent now, so not pending either; edited by hand: pending, but written back
    assert needs(pending=True, newer=[OPTIONS])[0] == "none"


@pytest.mark.parametrize("case", [
    dict(new={OPTIONS: "-Xss512k\n"}),  # another content: a real change
    dict(changes=[OPTIONS, "cassandra.yaml"]),  # another file changes too
    dict(pending=True, newer=["cassandra-env.sh"]),  # a restart pending for another file
    dict(pending=True, newer=[OPTIONS, "cassandra.service"]),  # its unit changed since the start
    dict(recorded=("41", 100.0)),  # the record of another JVM: Cassandra restarted outside the roles since
    dict(recorded=("42", 60.0)),  # the same pid, another start (a reboot)
    dict(recorded=None),  # a record that does not say for which JVM (written by an earlier version)
    dict(start=None),  # the JVM's start unknown
    dict(changes=["cassandra.yaml"], new={"cassandra.yaml": "num_tokens: 8\n"}),  # settings change live (seeds...)
    dict(target="/etc/cassandra/default.conf"),  # the conf directory itself switches (rpm alternative)
    dict(record={}),  # no record of that file (imported, seeded without it)
    dict(conf="/etc/cassandra"),  # a record of another directory
])
def test_restarted_whenever_the_running_cassandra_may_not_have_that_content(case):
    assert needs(**case)[0] == "restart"


def test_the_files_written_back_are_noted_before_the_decision():
    names = [t.get("name") for t in FIND["tasks"]]
    assert names.index("Look for a restart still pending") < names.index(AS_STARTED["name"]) \
        < names.index("Note what this node needs")


def test_the_jvm_is_checked_again_before_the_write():
    # restarted between the plan and the write (the question may wait): refused, nothing written on it
    from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import load
    action = load("roles", "cassandra_operation", "tasks", "action_apply_config.yml")
    names = [t["name"] for t in action]
    check = action[names.index("Check the JVM a file is written back for still runs")]
    assert names.index("Check the JVM a file is written back for still runs") < names.index("Write the config")
    assert check["block"][0]["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_config", "tasks_from": "jvm_started.yml"}
    plan = needs(full=True)[2]
    assert plan == {"files": [OPTIONS], "pid": "42", "start": "100.00", "sha1": {OPTIONS: sha1(STARTED_WITH)}}
    stop = check["block"][1]
    for jvm, ok in (({"pid": "42", "start": "100.00"}, True), ({"pid": "43", "start": "300.00"}, False),
                    ({"pid": "42", "start": "300.00"}, False), ({}, False)):
        variables = dict(stop["vars"], cassandra_apply_config_as_started=plan, cassandra_jvm=jvm)
        assert all(render("{{ %s }}" % c, variables) for c in stop["ansible.builtin.assert"]["that"]) is ok
    # restarted anyway for another file: nothing to check (the check is for the nodes left running)
    assert needs(changes=[OPTIONS, "cassandra.yaml"])[0] == "restart"
    assert "cassandra_apply_config_then | default('restart') == 'none'" in check["when"]


def test_the_files_written_back_are_checked_after_the_write():
    from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import load
    action = load("roles", "cassandra_operation", "tasks", "action_apply_config.yml")
    names = [t["name"] for t in action]
    assert names.index("Write the config") < names.index("Check the files written back")
    check = action[names.index("Check the files written back")]
    # restarted during the write?
    read = next(t for t in check["block"] if t["name"] == "Read the running Cassandra once written")
    assert read["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_config", "tasks_from": "jvm_started.yml"}
    stop = next(t for t in check["block"] if t["name"] == "Stop if one differs from what Cassandra started with")
    plan = needs(full=True)[2]
    same = {"pid": "42", "start": "100.00"}
    for written, jvm, ok in ((sha1(STARTED_WITH), same, True), (sha1("-Xss512k\n"), same, False), (None, same, False),
                             (sha1(STARTED_WITH), {"pid": "43", "start": "300.00"}, False)):
        results = {"results": [{"item": OPTIONS, "stat": {"exists": written is not None, "checksum": written}}
                               if written else {"item": OPTIONS, "stat": {"exists": False}}]}
        variables = dict(stop["vars"], cassandra_apply_config_as_started=plan, cassandra_apply_config_written_back=results,
                         inventory_hostname="n3", cassandra_jvm=jvm, ansible_inventory_sources=["/srv/inv/prod.yml"],
                         cassandra_hosts="prod")
        assert all(render("{{ %s }}" % c, variables) for c in stop["ansible.builtin.assert"]["that"]) is ok
        if not ok:  # the way out: a command rolling_restart takes (--limit, not cassandra_target_nodes), this inventory
            msg = render(stop["ansible.builtin.assert"]["fail_msg"], variables)
            assert "(the plan said no restart): restart it so that it runs with the inventory's config: ansible-playbook" in msg
            assert "community.cassandra.rolling_restart" in msg and "--limit n3" in msg and "/srv/inv/prod.yml" in msg, msg
            assert "cassandra_hosts=prod" in msg and "cassandra_target_nodes" not in msg, msg
    # stopped during the write: started again, then apply_config again
    results = {"results": [{"item": OPTIONS, "stat": {"exists": True, "checksum": sha1(STARTED_WITH)}}]}
    variables = dict(stop["vars"], cassandra_apply_config_as_started=plan, cassandra_apply_config_written_back=results,
                     inventory_hostname="n3", cassandra_jvm={})
    assert render(stop["ansible.builtin.assert"]["fail_msg"], variables) == (
        "Cassandra restarted or stopped while %s was written back (the plan said no restart): start Cassandra on it"
        " (systemctl start cassandra), then run apply_config again" % OPTIONS)


def test_a_node_not_running_reads_its_files_when_it_starts():
    # stopped on purpose: written, left stopped (no JVM, nothing written back "as it started")
    then, notes = needs(running=False, start=None)
    assert then == "write" and notes == []


def test_the_play_level_tasks_use_no_role_private_default():
    # cassandra_service's defaults are seen inside its includes only (not public here): a play-level task
    # reading one fails on every node ('_cassandra_service_conf_dir' is undefined, seen in the e2e)
    for t in FIND["tasks"]:
        if any(k in t for k in ("ansible.builtin.include_role", "ansible.builtin.include_tasks", "ansible.builtin.import_role")):
            continue
        assert "_cassandra_service_" not in str(t), t.get("name")


def test_a_file_left_as_it_is_with_the_same_settings_is_no_restart():
    # a comment added by hand to the options file: cassandra_config leaves the file (same settings), so nothing
    # for Cassandra to pick up; a file kept on the operator's word (cassandra_config_keep_files) still is
    same = needs(changes=(), pending=True, newer=[OPTIONS], kept=[OPTIONS])
    assert same == ("none", [OPTIONS + " changed since the running Cassandra started, same settings: no restart for it"])
    kept = needs(changes=(), pending=True, newer=[OPTIONS], kept=[OPTIONS], keep_files=[OPTIONS])
    assert kept[0] == "restart" and kept[1][0].startswith("restart pending (" + OPTIONS)
