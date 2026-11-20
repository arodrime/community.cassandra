from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A Cassandra package left half done (two rpm packages, a dpkg run interrupted): every restart or start of that node
# refused before any change, not only a release update (audit r5: a config restart ran Cassandra on two packages'
# jars, or a silent upgrade on dpkg's half-configured files). apply_config, rolling_restart, update_java. Rendered by
# Ansible from the playbooks and the task files.

from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    find, load, render)

APPLY = load("playbooks", "apply_config.yml")
REFUSE = "Refuse to restart or start a node whose Cassandra package is left half done"


def _tasks(plays):
    return [t for p in plays for t in p.get("tasks") or []]


def _refusal(task, hostvars):
    variables = dict(task["vars"], hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    return render(task["vars"]["_refusal"], variables)


def test_apply_config_refuses_a_restart_or_a_start_on_a_package_left_half_done():
    tasks = _tasks(APPLY)
    task = find(tasks, REFUSE)
    two = "installed 5.0.8, installed 5.0.9"
    hostvars = {
        # a config restart, the inventory's release the one it runs (no release update: refused before as well)
        "n1": {"cassandra_apply_config_unfinished": two, "cassandra_apply_config_then": "restart"},
        # its unit failed, its config written then started
        "n2": {"cassandra_apply_config_unfinished": "half-configured 5.0.9", "cassandra_apply_config_then": "start"},
        # not restarted nor started: its config written, nothing refused
        "n3": {"cassandra_apply_config_unfinished": "unpacked 5.0.9", "cassandra_apply_config_then": "none"},
        "n4": {"cassandra_apply_config_unfinished": "unpacked 5.0.9", "cassandra_apply_config_then": "write"},
        # left stopped by the window of an interrupted run (its mark): a resume starts it
        "n5": {"cassandra_apply_config_unfinished": "half-installed 5.0.9", "cassandra_apply_config_then": "write",
               "cassandra_apply_config_marked": True},
        "n6": {"cassandra_apply_config_unfinished": "", "cassandra_apply_config_then": "restart"},
    }
    lines = _refusal(task, hostvars)
    assert [x.split(":")[0] for x in lines if x.startswith("n")] == ["n1", "n2", "n5"]
    assert lines[0].endswith("its restart would run Cassandra from that state.")
    assert lines[1].endswith("its start would run Cassandra from that state.")
    assert lines[-1] == "then run the same command again."
    assert _refusal(task, dict((k, v) for k, v in hostvars.items() if k in ("n3", "n4", "n6"))) == []
    # --check too (it stops where the real run stops); drift's compare: nothing stopped
    assert task["when"] == ["_refusal | length > 0",
                            "not _cassandra_apply_config_compare_only | default(false) | bool"]
    assert task["run_once"] is True
    # before any change: after the plan knows the restarts, before the run is confirmed
    names = [t.get("name") for t in tasks]
    assert names.index("Note what this node needs") < names.index(REFUSE) < names.index("Show the plan and confirm the run")
    # the fact it reads, with the version change (a node not running too: no version read, its package read)
    note = find(tasks, "Note the version change")
    assert note["ansible.builtin.set_fact"]["cassandra_apply_config_unfinished"] == "{{ _unfinished }}"


def test_rolling_restart_and_update_java_refuse_it_before_their_question():
    for playbook, hosts in (("rolling_restart", "{{ ansible_play_hosts }}"),
                            ("update_java", "{{ groups['cassandra_update_java_True'] | default([]) }}")):
        tasks = _tasks(load("playbooks", playbook + ".yml"))
        names = [t.get("name") for t in tasks]
        refuse = "Refuse to restart a node whose Cassandra package is left half done"
        assert names.index(refuse) < names.index("Show the plan and confirm the run"), playbook
        task = tasks[names.index(refuse)]
        assert task["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_operation",
                                                        "tasks_from": "package_unfinished.yml"}
        assert task["vars"] == {"_cassandra_package_unfinished_hosts": hosts, "_cassandra_package_unfinished_run": playbook}
    shared = load("roles", "cassandra_operation", "tasks", "package_unfinished.yml")
    read, stop = shared
    assert read["check_mode"] is False and read["failed_when"] is False
    assert render(read["ansible.builtin.command"]["argv"], {"ansible_facts": {"os_family": "RedHat"}}) == [
        "rpm", "-q", "--qf", "installed %{VERSION}\\n", "cassandra"]
    assert render(read["ansible.builtin.command"]["argv"], {"ansible_facts": {"os_family": "Debian"}}) == [
        "dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", "cassandra"]
    assert stop["run_once"] is True and stop["when"] == "_refusal | length > 0"
    hostvars = {"n1": {"cassandra_package_read": {"stdout_lines": ["installed 4.1.11", "installed 4.1.12"]}},
                "n2": {"cassandra_package_read": {"stdout_lines": ["triggers-pending 4.1.12"]}},
                "n3": {"cassandra_package_read": {"skipped": True}}}
    variables = dict(stop["vars"], hostvars=hostvars, ansible_play_hosts=sorted(hostvars),
                     _cassandra_package_unfinished_hosts=["n1", "n2", "n3"], _cassandra_package_unfinished_run="rolling_restart",
                     cassandra_cluster_name="zorglub")
    said = render(stop["ansible.builtin.fail"]["msg"], dict(variables, **{k: render(v, variables) if isinstance(v, str)
                                                                          else v for k, v in stop["vars"].items()}))
    lines = said.split("\n")
    assert lines[0] == "REFUSED  rolling_restart  zorglub  nothing was changed"
    assert lines[1].startswith("  n1: rpm -q cassandra lists 2 packages (4.1.11, 4.1.12)")
    assert "n2" not in said and "n3" not in said
    # a node the run does not restart (update_java: on that Java already): not judged
    variables["_cassandra_package_unfinished_hosts"] = ["n2"]
    assert render(stop["vars"]["_refusal"], variables) == []
