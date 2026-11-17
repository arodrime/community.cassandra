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


@pytest.mark.parametrize("start, manage, env, written, removed", [
    ("package_script", True, {"JAVA_HOME": "/usr/lib/jvm/jre-17"}, True, False),
    ("package_script", True, {}, False, True),  # none set: the roles' drop-in removed, none written
    ("unit", True, {"JAVA_HOME": "/x"}, False, False),  # the role's unit carries it itself
    ("package_script", False, {"JAVA_HOME": "/x"}, False, False),  # the node's own unit kept: nothing
])
def test_the_drop_in_only_for_the_init_script_with_an_environment(start, manage, env, written, removed):
    variables = {"cassandra_service_start": start, "cassandra_service_unit_manage": manage,
                 "cassandra_service_environment": env}

    def runs(task):
        whens = BLOCK["when"] + ([task["when"]] if isinstance(task.get("when"), str) else task.get("when", []))
        return all(render("{{ %s }}" % w, **variables) in (True, "True") for w in whens)
    assert runs(TASKS["Write the drop-in with its environment"]) is written
    assert runs(TASKS["Remove the drop-in when no environment is set"]) is removed
    assert TASKS["Write the drop-in with its environment"]["ansible.builtin.template"]["dest"] == \
        "{{ _cassandra_service_environment_dropin }}"
    assert load("vars", "main.yml")["_cassandra_service_environment_dropin"] == \
        "/etc/systemd/system/cassandra.service.d/99-ansible-environment.conf"


def test_the_drop_in_sets_the_environment_only():
    with open(os.path.join(ROLE, "templates", "cassandra-environment.conf.j2"), encoding="utf-8") as f:
        text = render(f.read(), cassandra_service_environment={"JAVA_HOME": "/usr/lib/jvm/jre-17", "LOCAL_JMX": "no"})
    lines = [line for line in text.split("\n") if line and not line.startswith("#")]
    assert lines == ["[Service]", 'Environment="JAVA_HOME=/usr/lib/jvm/jre-17"', 'Environment="LOCAL_JMX=no"']
    assert text.startswith("# Managed by Ansible (community.cassandra.cassandra_service)")
