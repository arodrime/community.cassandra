from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service with the package's init script (cassandra_service_start: package_script): its environment
# (cassandra_service_environment) in a drop-in of the unit systemd generates for the script, which systemd applies as
# to any unit (checked on EL 9, systemd 252: the JVM the script starts gets JAVA_HOME, MAX_HEAP_SIZE). None set: no
# drop-in (a node without one stays as it is, byte for byte); the role's own unit: never.

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

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
ROLE = os.path.join(TOP, "roles", "cassandra_service")


def load(*path):
    with open(os.path.join(ROLE, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


BLOCK = next(t for t in load("tasks", "unit.yml") if t.get("name") == "Write the environment of the package's init script")
TASKS = dict((t["name"], t) for t in BLOCK["block"])


def _list(when):
    return when if isinstance(when, list) else [when] if when else []


@pytest.mark.parametrize("start, manage, env, written, removed", [
    ("package_script", True, {"JAVA_HOME": "/usr/lib/jvm/jre-17"}, True, False),
    ("package_script", True, {}, False, True),  # none set: the roles' drop-in removed, none written
    # the role's unit carries it itself: the roles' drop-in removed (left from package_script, it would override the
    # unit's environment; pre-push audit, rc13)
    ("unit", True, {"JAVA_HOME": "/x"}, False, True),
    ("package_script", False, {"JAVA_HOME": "/x"}, False, False),  # the node's own unit kept: nothing
])
def test_the_drop_in_only_for_the_init_script_with_an_environment(start, manage, env, written, removed):
    variables = {"cassandra_service_start": start, "cassandra_service_unit_manage": manage,
                 "cassandra_service_environment": env}
    variables["_cassandra_service_dropin_wanted"] = render(load("vars", "main.yml")["_cassandra_service_dropin_wanted"],
                                                           **variables)

    def runs(task):
        whens = _list(BLOCK["when"]) + _list(task.get("when"))
        return all(render("{{ %s }}" % w, **variables) in (True, "True") for w in whens)
    assert runs(TASKS["Write the drop-in with its environment"]) is written
    assert runs(TASKS["Remove the drop-in when no environment is set, or with the role's unit"]) is removed
    assert TASKS["Write the drop-in with its environment"]["ansible.builtin.template"]["dest"] == \
        "{{ _cassandra_service_environment_dropin }}"


def test_the_drop_in_comes_after_the_nodes_own():
    # (pre-push audit, rc13) systemd reads the drop-ins by their names' order, the last value of a variable wins:
    # after the one systemctl edit writes (override.conf) and the numbered ones, so the inventory's values hold
    name = os.path.basename(load("vars", "main.yml")["_cassandra_service_environment_dropin"])
    assert name == "zz-ansible-environment.conf"
    assert sorted([name, "override.conf", "99-java.conf", "zulu.conf"])[-1] == name


def test_a_change_of_the_drop_in_restarts_as_one_of_the_unit():
    # (pre-push audit, rc13) cassandra_service_restart_on_change: the drop-in written or removed too
    unit = dict((t.get("name"), t) for t in load("tasks", "unit.yml"))
    changed = unit["Note whether the unit or its environment changed"]["ansible.builtin.set_fact"]
    for registered in ("cassandra_service_unit", "cassandra_service_dropin", "cassandra_service_dropin_removed"):
        assert "%s is changed" % registered in changed["_cassandra_service_unit_changed"]
    with open(os.path.join(ROLE, "tasks", "main.yml"), encoding="utf-8") as f:
        main = f.read()
    with open(os.path.join(ROLE, "tasks", "wait_normal.yml"), encoding="utf-8") as f:
        wait = f.read()
    assert "cassandra_service_unit is changed" not in main.replace("if cassandra_service_unit is changed else", "")
    assert main.count("_cassandra_service_unit_changed") == 2 and "_cassandra_service_unit_changed" in wait


def test_the_drop_in_sets_the_environment_only():
    with open(os.path.join(ROLE, "templates", "cassandra-environment.conf.j2"), encoding="utf-8") as f:
        text = render(f.read(), cassandra_service_environment={"JAVA_HOME": "/usr/lib/jvm/jre-17", "LOCAL_JMX": "no"})
    lines = [line for line in text.split("\n") if line and not line.startswith("#")]
    assert lines == ["[Service]", 'Environment="JAVA_HOME=/usr/lib/jvm/jre-17"', 'Environment="LOCAL_JMX=no"']
    assert text.startswith("# Managed by Ansible (community.cassandra.cassandra_service)")


def test_the_drop_in_quotes_each_value_as_systemd_reads_it():
    # (pre-push audit, rc13) % starts a systemd specifier, \ and " end or escape the quoted value
    with open(os.path.join(ROLE, "templates", "cassandra-environment.conf.j2"), encoding="utf-8") as f:
        text = render(f.read(), cassandra_service_environment={"JVM_EXTRA_OPTS": '-Dx="a b" -Dp=50% -Dw=C:\\x'})
    assert 'Environment="JVM_EXTRA_OPTS=-Dx=\\"a b\\" -Dp=50%% -Dw=C:\\\\x"' in text.split("\n")
