from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config: a config file deleted or edited by hand on a running node, and
# written back with the content the running Cassandra started with (the record
# of its start, config_snapshot.yml) does not restart it: Cassandra runs with
# exactly that. Never when the record is not that JVM's (written before it
# started: a start outside the roles), nor when another file changes or a
# restart is pending for another one. Rendered by Ansible.

import base64
import hashlib
import json

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    FIND, find, render, with_task_vars)

AS_STARTED = find(FIND["tasks"], "Note the files written back as the running Cassandra started with them")
READ_START = find(FIND["tasks"], "Read when the running Cassandra started (files written back as it started with them)")
NOTE = find(FIND["tasks"], "Note what this node needs")
CONF = "/etc/cassandra/conf"
OPTIONS = "jvm17-server.options"
STARTED_WITH = "-Xss256k\n-XX:+UseG1GC\n"


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def needs(changes=(OPTIONS,), new=None, record=None, written=200.0, start=100.0, pending=False, newer=(),
          running=True, conf=CONF):
    """(then, notes) of a running node whose files in changes are written with new {name: text}."""
    new = {OPTIONS: STARTED_WITH, "cassandra.yaml": "num_tokens: 16\n"} if new is None else new
    record = {conf + "/" + OPTIONS: sha1(STARTED_WITH), conf + "/cassandra.yaml": sha1("num_tokens: 8\n")} \
        if record is None else record
    variables = {"_cassandra_config_changes": list(changes), "_cassandra_config_new": new,
                 "_cassandra_service_conf_dir": CONF,
                 "cassandra_pending_snapshot": {"content": base64.b64encode(json.dumps(record).encode()).decode(),
                                                "mtime": written},
                 "_cassandra_config_perm_changes": [], "_cassandra_config_dir_notes": [],
                 "cassandra_config_restart_pending": pending, "cassandra_config_newer_files": list(newer),
                 "_cassandra_preflight_running": running, "cassandra_apply_config_unit": {"skipped": True},
                 "cassandra_jvm": {"pid": "42", "start": str(start)} if start is not None else {}}
    variables = with_task_vars(AS_STARTED, variables)
    variables["_cassandra_apply_config_as_started"] = render(
        AS_STARTED["ansible.builtin.set_fact"]["_cassandra_apply_config_as_started"], variables)
    variables = with_task_vars(NOTE, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    return render(facts["cassandra_apply_config_then"], variables), render(facts["cassandra_apply_config_notes"], variables)


def test_a_file_written_back_as_the_running_cassandra_started_with_it_is_no_restart():
    assert needs() == ("none", [OPTIONS + " written back as the running Cassandra started with it: no restart for it"])
    # deleted by hand: absent now, so not pending either; edited by hand: pending, but written back
    assert needs(pending=True, newer=[OPTIONS])[0] == "none"


@pytest.mark.parametrize("case", [
    dict(new={OPTIONS: "-Xss512k\n"}),  # another content: a real change
    dict(changes=[OPTIONS, "cassandra.yaml"]),  # another file changes too
    dict(pending=True, newer=["cassandra-env.sh"]),  # a restart pending for another file
    dict(pending=True, newer=[OPTIONS, "cassandra.service"]),  # its unit changed since the start
    dict(written=50.0),  # the record is older than the JVM: Cassandra started outside the roles since
    dict(start=None),  # the JVM's start unknown
    dict(record={}),  # no record of that file (imported, seeded without it)
    dict(conf="/etc/cassandra"),  # a record of another directory
])
def test_restarted_whenever_the_running_cassandra_may_not_have_that_content(case):
    assert needs(**case)[0] == "restart"


def test_the_start_is_read_only_when_a_file_is_written_back():
    assert READ_START["ansible.builtin.include_role"]["tasks_from"] == "jvm_started.yml"
    assert READ_START["when"] == ["_cassandra_apply_config_as_started | length > 0", "cassandra_jvm.start is not defined"]
    names = [t.get("name") for t in FIND["tasks"]]
    assert names.index("Look for a restart still pending") < names.index(AS_STARTED["name"]) \
        < names.index(READ_START["name"]) < names.index("Note what this node needs")


def test_a_node_not_running_reads_its_files_when_it_starts():
    # stopped on purpose: written, left stopped (no JVM, nothing written back "as it started")
    then, notes = needs(running=False, start=None)
    assert then == "write" and notes == []
