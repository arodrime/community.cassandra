from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config: a config file deleted or edited by hand on a running node, and
# written back with the content the running Cassandra started with (the record
# of its start, config_snapshot.yml) does not restart it: Cassandra runs with
# exactly that (cassandra.yaml too: its seeds reloaded, the one setting Cassandra
# re-reads from it). Never when the record is not that JVM's (another pid or start:
# a start outside the roles), when another file changes or
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
          running=True, conf=CONF, target=None, full=False, kept=(), keep_files=(), **extra):
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
    variables = dict(variables, inventory_hostname="n1", **extra)
    variables = with_task_vars(AS_STARTED, variables)
    variables["_cassandra_apply_config_as_started"] = render(
        AS_STARTED["ansible.builtin.set_fact"]["_cassandra_apply_config_as_started"], variables)
    variables = with_task_vars(NOTE, variables)
    facts = NOTE["ansible.builtin.set_fact"]
    said = (render(facts["cassandra_apply_config_then"], variables), render(facts["cassandra_apply_config_notes"], variables))
    return said + (render(facts["cassandra_apply_config_as_started"], variables),) if full else said


def test_a_file_written_back_as_the_running_cassandra_started_with_it_is_no_restart():
    assert needs() == ("none", [OPTIONS + " written back as the running Cassandra started with it: no restart for it"])
    # cassandra.yaml too (edited back by hand, the mode or a setting): no restart, its seeds reloaded at the write
    assert needs(changes=["cassandra.yaml"], new={"cassandra.yaml": "num_tokens: 8\n"}) == (
        "none", ["cassandra.yaml written back as the running Cassandra started with it: no restart for it"])
    from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import load
    action = load("roles", "cassandra_operation", "tasks", "action_apply_config.yml")
    write = next(t for t in action if t.get("name") == "Write the config")
    reload_seeds = write["vars"]["cassandra_config_reload_seeds"]
    assert render(reload_seeds, {"cassandra_apply_config_as_started": {"files": ["cassandra.yaml"]}}) is True
    assert render(reload_seeds, {"cassandra_apply_config_as_started": {"files": [OPTIONS]}}) is False
    assert render(reload_seeds, {"cassandra_apply_config_as_started": {}}) is False
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
    dict(changes=["cassandra.yaml"], new={"cassandra.yaml": "num_tokens: 4\n"}),  # cassandra.yaml, another content
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
                     inventory_hostname="n3", cassandra_jvm={}, ansible_inventory_sources=["/srv/inv/prod.yml"],
                     cassandra_hosts="prod", _cassandra_preflight={"cassandra_dc": "dc1", "cassandra_rack": "r2"})
    # start_rack on it alone, which checks the start (by hand, its storage_compatibility_mode would go unchecked)
    msg = render(stop["ansible.builtin.assert"]["fail_msg"], variables)
    assert msg.startswith(
        "Cassandra restarted or stopped while %s was written back (the plan said no restart): start Cassandra on it"
        " with start_rack (which checks the start first): ansible-playbook -i " % OPTIONS), msg
    assert msg.endswith("community.cassandra.start_rack -e cassandra_hosts=prod --limit n3 -e cassandra_target_dc=dc1"
                        " -e cassandra_target_rack=r2; then run apply_config again"), msg
    assert "systemctl" not in msg


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


C4 = "storage_compatibility_mode: CASSANDRA_4\n"
UPGRADING = "storage_compatibility_mode: UPGRADING\n"


def _but(case, **changed):
    """The case with some arguments changed."""
    out = dict(case)
    out.update(changed)
    return out


def test_a_file_with_the_same_settings_is_no_restart_only_when_cassandra_started_with_them():
    # UPGRADING written ahead of the node (an apply_config cut before its restart, the role run alone): the file has
    # the inventory's settings, but the running Cassandra started with CASSANDRA_4 (the record of its start):
    # written since its start, not running yet: a restart, never "same settings: no restart"
    ahead = dict(changes=(), pending=True, newer=["cassandra.yaml"], kept=["cassandra.yaml"],
                 new={"cassandra.yaml": UPGRADING}, record={CONF + "/cassandra.yaml": sha1(C4)})
    assert needs(**ahead) == ("restart", ["restart pending (cassandra.yaml changed since the running Cassandra started)"])
    # it started with the file as cassandra_config renders it, a comment added by hand since: no restart
    started = _but(ahead, record={CONF + "/cassandra.yaml": sha1(UPGRADING)})
    assert needs(**started) == (
        "none", ["cassandra.yaml changed since the running Cassandra started, same settings: no restart for it"])
    # not known what it started with: the record of another JVM, none for that file, another directory
    for case in (dict(recorded=("41", 100.0)), dict(record={}),
                 dict(record={"/etc/cassandra/cassandra.yaml": sha1(UPGRADING)})):
        assert needs(**_but(started, **case))[0] == "restart", case
    # the options file started with as written another way (same settings), a comment added since: not known to
    # be the same settings as the inventory's (only its checksum is recorded): a restart
    assert needs(changes=(), pending=True, newer=[OPTIONS], kept=[OPTIONS],
                 record={CONF + "/" + OPTIONS: sha1("-Xss256k\n-XX:+UseG1GC # by hand\n")})[0] == "restart"


def _mode_read(yaml_mode, logged=None, running="yes", more=()):
    lines = ["file=%s/cassandra.yaml" % CONF, "yaml_read=yes", "yaml=" + yaml_mode, "running=" + running] + list(more)
    return {"stdout_lines": lines + (["logged=" + logged] if logged else [])}


def test_a_storage_mode_written_ahead_of_the_node_is_restarted_without_a_record():
    # no record of its start (imported, restarted by hand): nothing pending by the files, but the mode it logged at
    # its start (CASSANDRA_4) is before its cassandra.yaml's (UPGRADING): a restart, said
    ahead = dict(changes=(), recorded=None, record={}, cassandra_version="50x",
                 cassandra_storage_mode_read=_mode_read("UPGRADING", logged="CASSANDRA_4"))
    assert needs(**ahead) == (
        "restart", ["restart pending (its cassandra.yaml has storage_compatibility_mode UPGRADING, Cassandra runs"
                    " CASSANDRA_4)"])
    # given by hand (-e cassandra_storage_mode_running) when nothing was read (its file changed since its start)
    changed = _mode_read("UPGRADING", more=("pending=yes", "changed=time"))
    assert needs(**_but(ahead, cassandra_storage_mode_read=changed, cassandra_storage_mode_running="n1:CASSANDRA_4")) == (
        "restart", ["restart pending (its cassandra.yaml has storage_compatibility_mode UPGRADING, Cassandra runs"
                    " CASSANDRA_4 as given with -e)"])
    # not against its file, unchanged since its start (the guard refuses that -e): the file tells
    assert needs(**_but(ahead, cassandra_storage_mode_read=_mode_read("UPGRADING"),
                        cassandra_storage_mode_running="n1:CASSANDRA_4")) == ("none", [])
    # it runs its file's mode, or a later one (the guards' case), or 4.x, or stopped: nothing for it
    for case in (dict(cassandra_storage_mode_read=_mode_read("UPGRADING", logged="UPGRADING")),
                 dict(cassandra_storage_mode_read=_mode_read("UPGRADING", logged="NONE")),
                 dict(cassandra_storage_mode_read=_mode_read("UPGRADING")),
                 dict(cassandra_version="41x")):
        assert needs(**_but(ahead, **case)) == ("none", []), case
    # the note of the files written after its start with no record leaves out cassandra.yaml then (it is restarted)
    say = find(FIND["tasks"], "Say which files changed since Cassandra started, with no record of the ones it started with")
    base = {"inventory_hostname": "n1", "cassandra_version": "50x", "cassandra_config_unknown_files": ["cassandra.yaml"]}
    for read, said in ((_mode_read("UPGRADING", logged="CASSANDRA_4"), []),
                       (_mode_read("UPGRADING", logged="UPGRADING"), ["cassandra.yaml"])):
        variables = with_task_vars(say, dict(base, cassandra_storage_mode_read=read))
        assert variables["_unknown"] == said


def test_the_way_out_of_the_jump_guard_and_apply_config_converge():
    # the jump guard's way out (On n1, run rolling_restart --limit n1: it restarts it in the UPGRADING its
    # cassandra.yaml has) passes the guards of rolling_restart; apply_config with UPGRADING in the inventory restarts
    # it too; once restarted (UPGRADING logged, the record of that start), apply_config has nothing more to do
    from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import (
        cassandra_storage_mode_back, cassandra_storage_mode_jump, cassandra_storage_mode_unsure)
    host = {"inventory_hostname": "n1", "cassandra_storage_mode_read": _mode_read("UPGRADING", logged="CASSANDRA_4")}
    assert [f(host) for f in (cassandra_storage_mode_back, cassandra_storage_mode_jump, cassandra_storage_mode_unsure)] \
        == ["", "", {}]
    ahead = dict(changes=(), pending=True, newer=["cassandra.yaml"], kept=["cassandra.yaml"],
                 new={"cassandra.yaml": UPGRADING}, record={CONF + "/cassandra.yaml": sha1(C4)}, cassandra_version="50x",
                 cassandra_storage_mode_read=_mode_read("UPGRADING", logged="CASSANDRA_4"))
    assert needs(**ahead)[0] == "restart"
    done = _but(ahead, pending=False, newer=[], record={CONF + "/cassandra.yaml": sha1(UPGRADING)},
                cassandra_storage_mode_read=_mode_read("UPGRADING", logged="UPGRADING"))
    assert needs(**done) == ("none", [])


def test_a_restart_into_none_from_a_file_at_none_already_is_the_point_of_no_return():
    # its cassandra.yaml at NONE already (no diff), Cassandra runs UPGRADING (logged): apply_config restarts it into
    # NONE; the plan says it is the point of no return, as for a file it writes with NONE
    import os
    import yaml
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "apply_config.yml"),
              encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    task = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Put the settings that differ side by side")
    template = task["ansible.builtin.set_fact"]["cassandra_apply_config_no_return"]

    def no_return(given="", then="restart", **reads):
        hostvars = dict((h, {"inventory_hostname": h, "cassandra_storage_mode_read": r, "cassandra_apply_config_then": then,
                             "cassandra_storage_mode_running": given}) for h, r in reads.items())
        moved = render(task["vars"]["_moved"], {"hostvars": hostvars, "ansible_play_hosts": list(hostvars)})
        return render(template, {"_nodes": [], "hostvars": hostvars, "ansible_play_hosts": list(hostvars),
                                 "_moved": moved})
    assert no_return(n1=_mode_read("NONE", logged="UPGRADING"), n2=_mode_read("NONE", logged="NONE"),
                     n3=_mode_read("UPGRADING", logged="CASSANDRA_4")) == ["n1"]
    # given with -e when nothing else tells it (its file changed since its start)
    changed = _mode_read("NONE", more=("pending=yes", "changed=time"))
    assert no_return(n1=changed) == []
    assert no_return(n1=changed, given="n1:UPGRADING") == ["n1"]
    # stopped: the mode it last ran (logged at its last start); started by the run only
    stopped = _mode_read("NONE", running="no", more=("last_logged=UPGRADING",))
    assert no_return(n1=stopped, then="start") == ["n1"]
    assert no_return(n1=stopped, then="write") == []
