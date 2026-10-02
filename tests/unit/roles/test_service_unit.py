from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service: the systemd unit it writes, and the expressions of its tasks.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service")

with open(os.path.join(ROLE, "templates", "cassandra.service.j2"), encoding="utf-8") as f:
    UNIT = f.read()
with open(os.path.join(ROLE, "defaults", "main.yml"), encoding="utf-8") as f:
    DEFAULTS = yaml.safe_load(f)
with open(os.path.join(ROLE, "tasks", "main.yml"), encoding="utf-8") as f:
    TASKS = yaml.safe_load(f)


def task(name):
    todo = list(TASKS)
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", [])
    raise KeyError(name)


def render(template, escape_backslashes=True, **overrides):
    variables = dict(DEFAULTS, **overrides)
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template),
                                                                      escape_backslashes=escape_backslashes)


def unit(**overrides):
    # a .j2 file: Jinja unescapes its string literals itself
    return render(UNIT, escape_backslashes=False, **overrides).splitlines()


def drain_line(lines):
    return [line for line in lines if line.startswith("ExecStop=")]


def test_defaults():
    lines = unit()
    for expected in ["User=cassandra", "Group=cassandra", "ExecStart=/usr/sbin/cassandra -f", "Restart=no",
                     "TimeoutStopSec=180", "LimitNOFILE=1048576", "LimitNPROC=32768", "LimitMEMLOCK=infinity",
                     "LimitAS=infinity", "TasksMax=infinity", "SuccessExitStatus=143",
                     "WorkingDirectory=-/var/lib/cassandra"]:
        assert expected in lines
    # a start limit would also block manual restarts
    assert [line for line in lines if line.startswith(("StartLimit", "RestartSec"))] == []
    assert drain_line(lines) == ["ExecStop=-/usr/bin/timeout -k 10 120 /usr/bin/nodetool -p 7199 drain"]


def test_runs_as_the_service_account():
    lines = unit(cassandra_user="dbsvc", cassandra_group="dbgrp")
    assert "User=dbsvc" in lines
    assert "Group=dbgrp" in lines
    assert [line for line in lines if line.startswith(("User=", "Group="))] == ["User=dbsvc", "Group=dbgrp"]


@pytest.mark.parametrize("variables, expected", [
    ({}, "/usr/bin/nodetool -p 7199 drain"),
    ({"cassandra_jmx_port": 7299}, "/usr/bin/nodetool -p 7299 drain"),
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/ops.pw"},
     "/usr/bin/nodetool -p 7199 -u ops -pwf /etc/cassandra/ops.pw drain"),
    # an inline password is never written to the world-readable unit
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password": "s3cret"}, "/usr/bin/nodetool -p 7199 drain"),
    ({"cassandra_jmx_password_file": "/etc/cassandra/ops.pw"}, "/usr/bin/nodetool -p 7199 drain"),
])
def test_drain_on_stop(variables, expected):
    lines = unit(**variables)
    assert drain_line(lines) == ["ExecStop=-/usr/bin/timeout -k 10 120 " + expected]
    assert "s3cret" not in "\n".join(lines)


def test_no_drain():
    assert drain_line(unit(cassandra_service_drain_on_stop=False)) == []


def test_restart_on_failure_is_capped():
    lines = unit(cassandra_service_restart="on-failure", cassandra_service_start_limit_burst=5,
                 cassandra_service_start_limit_interval=600)
    for expected in ["Restart=on-failure", "RestartSec=30", "StartLimitBurst=5", "StartLimitIntervalSec=600"]:
        assert expected in lines
    # StartLimit* belong to [Unit]
    assert lines.index("StartLimitBurst=5") < lines.index("[Service]")


def test_environment():
    lines = unit(cassandra_service_environment={"JAVA_HOME": "/usr/lib/jvm/java-17", "MAX_HEAP_SIZE": "8G"})
    assert [line for line in lines if line.startswith("Environment=")] == [
        'Environment="JAVA_HOME=/usr/lib/jvm/java-17"', 'Environment="MAX_HEAP_SIZE=8G"']
    # nothing left of the template's comment, no blank line in [Service]
    service = lines[lines.index("[Service]"):lines.index("[Install]") - 1]
    assert "" not in service
    assert not any("{#" in line for line in lines)


@pytest.mark.parametrize("value, written", [
    # % starts a systemd specifier (%p = unit prefix): doubled to reach the JVM as is
    ("-XX:ErrorFile=/var/log/cassandra/hs_err_%p.log", "-XX:ErrorFile=/var/log/cassandra/hs_err_%%p.log"),
    ('-Dname="a b"', '-Dname=\\"a b\\"'),
    ("C:\\x", "C:\\\\x"),
    # a YAML block scalar ends with a newline: kept in the quotes, not on a line of its own
    ("-Xfoo\n-Xbar\n", "-Xfoo\\n-Xbar\\n"),
])
def test_environment_escaped_for_systemd(value, written):
    assert [line for line in unit(cassandra_service_environment={"JVM_EXTRA_OPTS": value})
            if line.startswith("Environment=")] == ['Environment="JVM_EXTRA_OPTS=%s"' % written]


@pytest.mark.parametrize("drain, stop, ok", [
    (120, 180, True),
    (169, 180, True),
    (170, 180, False),
    (300, 180, False),
])
def test_drain_must_end_before_the_stop_timeout(drain, stop, ok):
    that = task("Leave time for the drain within the stop timeout")["ansible.builtin.assert"]["that"]
    assert render("{{ %s }}" % that, cassandra_service_drain_timeout=drain,
                  cassandra_service_timeout_stop=stop) is ok


WAIT = task("Wait for the node to be joined (Mode NORMAL)")


@pytest.mark.parametrize("variables, argv, hidden", [
    ({}, ["nodetool", "-p", "7199", "netstats"], False),
    ({"cassandra_jmx_port": 7299}, ["nodetool", "-p", "7299", "netstats"], False),
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/ops.pw"},
     ["nodetool", "-p", "7199", "-u", "ops", "-pwf", "/etc/cassandra/ops.pw", "netstats"], False),
    # the file wins over an inline password, which is then not on the command line
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/ops.pw",
      "cassandra_jmx_password": "s3cret"},
     ["nodetool", "-p", "7199", "-u", "ops", "-pwf", "/etc/cassandra/ops.pw", "netstats"], False),
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password": "s3cret"},
     ["nodetool", "-p", "7199", "-u", "ops", "-pw", "s3cret", "netstats"], True),
    # no user, no credentials (as the unit's drain)
    ({"cassandra_jmx_password_file": "/etc/cassandra/ops.pw"}, ["nodetool", "-p", "7199", "netstats"], False),
    ({"cassandra_jmx_password": "s3cret"}, ["nodetool", "-p", "7199", "netstats"], False),
])
def test_wait_command(variables, argv, hidden):
    assert render(WAIT["ansible.builtin.command"]["argv"], **variables) == argv
    assert render(WAIT["no_log"], **variables) is hidden


NETSTATS_NORMAL = "Mode: NORMAL\nNot sending any streams.\nRead Repair Statistics:\n"


@pytest.mark.parametrize("stdout, joined", [
    (NETSTATS_NORMAL, True),
    ("Mode: JOINING\nBootstrap 1f3c...\n", False),
    ("Mode: STARTING\n", False),
    ("Mode: NORMALISH\n", False),
    ("", False),  # nodetool failed: JMX not up yet
])
def test_wait_until_mode_normal(stdout, joined):
    result = {"stdout": stdout}
    assert render("{{ %s }}" % WAIT["until"], cassandra_service_netstats=result) is joined
    assert render("{{ %s }}" % WAIT["failed_when"], cassandra_service_netstats=result) is not joined


@pytest.mark.parametrize("state, restart_on_change, unit_changed, expected", [
    ("started", False, False, "started"),
    ("started", False, True, "started"),  # a changed unit waits for the next restart
    ("started", True, True, "restarted"),
    ("started", True, False, "started"),
    ("restarted", False, False, "restarted"),
])
def test_start_or_restart(state, restart_on_change, unit_changed, expected):
    start = task("Start or restart cassandra")["ansible.builtin.systemd_service"]["state"]
    assert render(start, cassandra_service_state=state, cassandra_service_restart_on_change=restart_on_change,
                  cassandra_service_unit={"changed": unit_changed}) == expected


@pytest.mark.parametrize("check_mode, status, act", [
    (False, {}, True),
    (True, {"LoadState": "loaded"}, True),
    (True, {"LoadState": "not-found"}, False),  # --check before the package is installed
    (True, {}, False),
])
def test_act_on_the_unit(check_mode, status, act):
    expr = task("Tell whether there is a unit to act on")["ansible.builtin.set_fact"]["_cassandra_service_act"]
    assert render(expr, ansible_check_mode=check_mode, cassandra_service_before={"status": status}) is act


def conditions(name):
    when = task(name)["when"]
    return when if isinstance(when, list) else [when]


@pytest.mark.parametrize("active, main_pid, looked", [
    ("active", 0, True),  # active (exited): the init script's, maybe with no JVM
    ("active", 4321, False),  # the native unit's JVM
    ("inactive", 0, False),
])
def test_jvm_looked_for_only_under_an_init_script_unit(active, main_pid, looked):
    variables = dict(_cassandra_service_act=True,
                     cassandra_service_unit_state={"status": {"ActiveState": active, "MainPID": str(main_pid)}})
    assert all(render("{{ %s }}" % c, **variables) for c in conditions("Look for a running Cassandra JVM")) is looked
    # read only: also under --check, which then shows the reset
    assert task("Look for a running Cassandra JVM")["check_mode"] is False


@pytest.mark.parametrize("jvm, state, reset, warned", [
    ({"rc": 1}, "started", True, False),  # no JVM: reset, then started
    ({"rc": 0}, "started", False, True),  # a JVM under the init script: left running, said so
    ({"rc": 0}, "stopped", False, False),  # it is about to be stopped: no takeover message
    ({"skipped": True, "changed": False}, "started", False, False),  # the native unit runs it
])
def test_reset_or_warn(jvm, state, reset, warned):
    variables = dict(cassandra_service_jvm=jvm, cassandra_service_state=state)
    assert all(render("{{ %s }}" % c, **variables)
               for c in conditions("Reset a unit left active with no Cassandra running")) is reset
    assert all(render("{{ %s }}" % c, **variables)
               for c in conditions("Warn that a Cassandra started by the init script keeps running")) is warned
