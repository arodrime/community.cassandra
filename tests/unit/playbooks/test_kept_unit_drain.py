from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A node's own unit kept as found (cassandra_service_unit_manage: false) may
# not drain on stop: stop_rack and the restarts drain it with nodetool first.
# The expressions are read from the playbooks and tasks, rendered by Ansible.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def true(conditions, **variables):
    conditions = conditions if isinstance(conditions, list) else [conditions]
    return all(render("{{ %s }}" % c, **variables) for c in conditions)


STOP_PLAY = next(p for p in load("playbooks", "stop_rack.yml") if p.get("name") == "Stop the rack")
DRAIN = next(t for t in STOP_PLAY["tasks"] if t.get("name") == "Drain the node")


def test_stop_rack_drains_before_stopping():
    names = [t["name"] for t in STOP_PLAY["tasks"]]
    assert names.index("Drain the node") < names.index("Stop Cassandra")
    assert "drained by its unit" not in open(os.path.join(TOP, "playbooks", "stop_rack.yml"), encoding="utf-8").read()


def drain_failure(err):
    """The result cassandra_drain fails with, nodetool answering rc 2 and err."""
    from ansible_collections.community.cassandra.plugins.modules import cassandra_drain

    class Fail(Exception):
        pass

    class Module(object):
        def __init__(self, **kwargs):
            self.params = {"host": "127.0.0.1", "port": 7199, "password": None, "password_file": None, "username": None,
                           "nodetool_path": None, "nodetool_flags": "", "debug": False, "cassandra_version": "4.1"}

        def run_command(self, cmd):
            return 2, "", err

        def fail_json(self, **kwargs):
            raise Fail(kwargs)

    cassandra_drain.AnsibleModule, saved = Module, cassandra_drain.AnsibleModule
    try:
        with pytest.raises(Fail) as exc:
            cassandra_drain.main()
    finally:
        cassandra_drain.AnsibleModule = saved
    return exc.value.args[0]


STOP_WARN = next(t for t in STOP_PLAY["tasks"] if t.get("name") == "Say the drain did not work")


def test_stop_rack_warning_keeps_the_error_only():
    """The node is stopped all the same: the warning gives nodetool's error, not the drain message's advice."""
    result = dict(drain_failure("error: Some failure\n-- StackTrace --\n"), failed=False)  # (failed_when: false)
    assert true(STOP_WARN["when"], cassandra_rack_drain=result)
    assert render(STOP_WARN["ansible.builtin.debug"]["msg"], inventory_hostname="n1", cassandra_rack_drain=result) == (
        "WARNING - n1 was not drained (error: Some failure): stopped all the same.")
    assert STOP_WARN["vars"]["cassandra_output"] is True  # shown under the ops callback


def test_stop_rack_warns_a_drain_without_answer():
    # JMX not answering: no rc, failed_when false makes it ok; still not drained
    result = {"failed": False, "changed": False, "msg": "Unable to reach JMX at 127.0.0.1:7199"}
    assert true(STOP_WARN["when"], cassandra_rack_drain=result)
    assert "(Unable to reach JMX at 127.0.0.1:7199): stopped" in render(
        STOP_WARN["ansible.builtin.debug"]["msg"], inventory_hostname="n1", cassandra_rack_drain=result)
    assert not true(STOP_WARN["when"], cassandra_rack_drain={"changed": True, "msg": "nodetool drain executed successfully"})


@pytest.mark.parametrize("operation_drain, unit_manage, drained", [
    (True, True, True),
    (False, False, True),   # a kept unit may not drain: drained anyway
    (False, True, False),   # left to the role's unit
])
def test_stop_rack_drain(operation_drain, unit_manage, drained):
    assert true(DRAIN["when"], cassandra_operation_drain=operation_drain, cassandra_service_unit_manage=unit_manage) is drained


def test_stop_rack_drain_never_stops_the_stop():
    # a node that does not answer (down, JMX): stopped all the same, not left drained and running
    assert DRAIN["failed_when"] is False and DRAIN["ignore_errors"] is True


@pytest.mark.parametrize("file", ["action_restart.yml", "action_reboot.yml"])
@pytest.mark.parametrize("operation_drain, unit_manage, drained", [
    (True, True, True), (False, False, True), (False, True, False)])
def test_restart_drain(file, operation_drain, unit_manage, drained):
    tasks = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", file))
    task = next(t for t in tasks if t["name"] == "Drain the node")
    assert true(task["when"], cassandra_operation_drain=operation_drain, cassandra_service_unit_manage=unit_manage) is drained


@pytest.mark.parametrize("host_vars, drained", [({}, False), ({"cassandra_service_unit_manage": False}, True)])
def test_rack_restart_drains_a_kept_unit(host_vars, drained):
    block = load("roles", "cassandra_operation", "tasks", "restart_batch.yml")[0]["block"]
    task = next(t for t in block if t["name"] == "Drain the rack's nodes")
    assert true(task["when"], cassandra_operation_drain=False, item="node1", hostvars={"node1": host_vars}) is drained


RACK = load("roles", "cassandra_operation", "tasks", "restart_batch.yml")[0]
RACK_DRAIN = next(t for t in RACK["block"] if t["name"] == "Drain the rack's nodes")
RACK_WARN = next(t for t in RACK["block"] if t["name"] == "Say the drains that did not work")
# the drain loop of a rack: n1 drained, n2 failed (nodetool's rc), n3 skipped (left to its unit), n4 no answer
DRAIN_LOOP = {"changed": True, "msg": "All items completed", "results": [
    {"item": "n1", "changed": True, "failed": False, "msg": "nodetool drain executed successfully"},
    {"item": "n2", "changed": False, "failed": False, "rc": 2, "error": "error: x",
     "msg": "nodetool drain did not execute successfully (rc 2): error: x. If its Mode..."},
    {"item": "n3", "changed": False, "skipped": True},
    {"item": "n4", "changed": False, "failed": False, "msg": "Unable to reach JMX at 10.0.0.4:7199"},
    # the timeout: Ansible's own result, no changed key, failed_when not applied
    {"item": "n5", "failed": True, "msg": "The cassandra_drain action failed to execute in the expected time frame (120)"
                                          " and was terminated"}]}
TIMEOUT_221 = {"item": "n6", "changed": False, "failed": True, "msg": "Task failed: Timed out after 120 second(s).",
               "timedout": {"frame": None, "period": 120}}  # ansible-core 2.18+


def test_rack_drain_never_stops_the_restart():
    # a node not drained is restarted all the same, never left drained and running
    assert RACK_DRAIN["failed_when"] is False and RACK_DRAIN["ignore_errors"] is True
    assert RACK_DRAIN["timeout"] == "{{ cassandra_service_drain_timeout | int }}"
    names = [t["name"] for t in RACK["block"]]
    assert names.index("Drain the rack's nodes") < names.index("Say the drains that did not work") < names.index("Restart them")


def test_rack_drain_failures_are_warned_each():
    variables = dict(RACK_WARN["vars"], cassandra_restart_drain=DRAIN_LOOP)
    items = render(RACK_WARN["loop"], **variables)
    assert items == ["n2", "n4", "n5"]
    assert [render(RACK_WARN["ansible.builtin.debug"]["msg"], item=i, **variables) for i in items] == [
        "WARNING - n2 was not drained (error: x): restarted all the same.",
        "WARNING - n4 was not drained (Unable to reach JMX at 10.0.0.4:7199): restarted all the same.",
        "WARNING - n5 was not drained (The cassandra_drain action failed to execute in the expected time frame (120)"
        " and was terminated): restarted all the same."]
    assert RACK_WARN["vars"]["cassandra_output"] is True


RACK_STOP = next(t for t in RACK["block"] if t["name"] == "Stop when no node of the rack answered")
JMX_DOWN = {"item": "n1", "failed": False, "msg": "Unable to reach JMX at 10.0.0.1:7199"}


@pytest.mark.parametrize("results, stops", [
    ([JMX_DOWN, dict(JMX_DOWN, item="n2")], True),  # none answered (a wrong JMX login): nothing drained, none restarted
    (DRAIN_LOOP["results"], False),  # some drained: all restarted
    ([JMX_DOWN, DRAIN_LOOP["results"][4]], False),  # a timeout: maybe drained in part, restarted
    ([TIMEOUT_221, dict(TIMEOUT_221, item="n7")], False),  # every drain timed out (2.18+): restarted
    ([{"item": "n8", "failed": True, "msg": "Task failed: Timed out after 120 second(s)."}], False),  # (its message only)
    ([JMX_DOWN, DRAIN_LOOP["results"][1]], False),  # nodetool answered with an error on n2
    ([DRAIN_LOOP["results"][2]], False),  # all left to their unit
])
def test_rack_stops_when_no_node_answered(results, stops):
    variables = dict(RACK_STOP["vars"], cassandra_restart_drain={"results": results})
    assert true(RACK_STOP["when"], **variables) is stops
    names = [t["name"] for t in RACK["block"]]
    # (no WARNING "restarted all the same" when it stops: the stop first)
    assert names.index(RACK_STOP["name"]) < names.index("Say the drains that did not work") < names.index("Restart them")


# the restart loop of a rack failing on n2
RESTART_LOOP = {"failed": True, "msg": "One or more items failed", "results": [
    {"item": "n1", "changed": True, "failed": False},
    {"item": "n2", "changed": False, "failed": True, "msg": "Unable to restart service cassandra"},
    {"item": "n3", "changed": True, "failed": False}]}


@pytest.mark.parametrize("node, failure, said", [
    ("n2", RESTART_LOOP, "restart FAILED: Unable to restart service cassandra"),
    ("n1", RESTART_LOOP, "restart not finished: the rack stopped at n2"),  # not counted as failed by the recap
    ("n1", {"failed": True, "msg": "cluster not healthy"}, "restart FAILED: cluster not healthy"),
    # the wait for Mode NORMAL out of retries: netstats' own result, no msg
    ("n3", {"failed": True, "msg": "One or more items failed",
            "results": [{"item": "n3", "failed": True, "attempts": 30, "mode": "JOINING"}]},
     "restart FAILED: not back in Mode NORMAL in time"),
])
def test_rack_failure_names_the_node_that_failed(node, failure, said):
    record = next(t for t in RACK["rescue"] if t["name"] == "Record the failure")
    assert render(record["ansible.builtin.set_fact"]["cassandra_op_result"], item=node, ansible_failed_result=failure) == said


@pytest.mark.parametrize("file, then", [("action_restart.yml", "restarted"), ("action_reboot.yml", "rebooted"),
                                        ("action_upgrade.yml", "flushed, then stopped")])
def test_the_actions_drain_the_same_way(file, then):
    imports = [t for t in load("roles", "cassandra_operation", "tasks", file) if t.get("name") == "Drain the node"]
    assert imports[0]["ansible.builtin.import_tasks"] == "drain.yml"
    assert imports[0]["vars"]["_cassandra_service_drain_then"] == then
    assert ("when" in imports[0]) is (file != "action_upgrade.yml")  # an upgrade always drains


@pytest.mark.parametrize("result, resumed, stops, warns", [
    ({"failed": False, "rc": 2, "error": "error: x", "msg": "nodetool drain did not execute successfully (rc 2)"}, False,
     False, "error: x"),  # nodetool answered with an error: warned, the node goes on
    ({"failed": False, "msg": "Unable to reach JMX at 127.0.0.1:7199"}, False, True, None),  # no answer: stops
    ({"failed": False, "msg": "Connection refused", "stderr": ""}, True, False, None),  # a resumed node, stopped already
    ({"failed": True, "msg": "The cassandra_drain action failed to execute in the expected time frame (120) and was"
                             " terminated"}, False, False, "The cassandra_drain action"),  # the timeout (2.16, 2.17)
    ({"failed": True, "changed": False, "msg": "Task failed: Timed out after 120 second(s).",
      "timedout": {"period": 120}}, False, False, "Task failed: Timed out"),  # the timeout (2.18+)
    ({"failed": False, "changed": True, "msg": "ok"}, False, False, None),
    ({"skipped": True, "changed": False}, False, False, None),  # --check, or left to the unit
])
def test_node_drain_failure(result, resumed, stops, warns):
    tasks = load("roles", "cassandra_operation", "tasks", "drain.yml")
    drain = next(t for t in tasks if t["name"] == "Drain the node")
    stop = next(t for t in tasks if t["name"] == "Stop when the node did not answer")
    warn = next(t for t in tasks if t["name"] == "Say the drain did not work")
    assert drain["failed_when"] is False and drain["ignore_errors"] is True
    assert drain["timeout"] == "{{ cassandra_service_drain_timeout | int }}"
    skipped = "skipped" in result
    variables = dict(cassandra_service_drain=result, _cassandra_resumed_node=resumed, inventory_hostname="n1",
                     _cassandra_service_drain_then="restarted")
    if skipped:
        variables["cassandra_service_drain"] = dict(result, skipped=True)
    assert true(stop["when"], **variables) is stops
    if not skipped:
        assert true(warn["when"], **variables) is (warns is not None or stops or resumed)
    if warns:
        assert warns in render(warn["ansible.builtin.debug"]["msg"], **variables)
    assert tasks.index(drain) + 1 == tasks.index(stop) and tasks.index(stop) + 1 == tasks.index(warn)
    assert warn["vars"]["cassandra_output"] is True


def test_service_stops_before_the_boot_setting():
    names = [t["name"] for t in load("roles", "cassandra_service", "tasks", "main.yml")]
    assert names.index("Stop cassandra") < names.index("Enable cassandra at boot")
