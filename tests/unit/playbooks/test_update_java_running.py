from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# update_java: a node already running cassandra_java_version is left alone, its
# Java read from the running JVM found by its process (not the unit's main PID,
# nor a package path that may not name the version).

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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "update_java.yml")
with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
TASKS = dict((t["name"], t) for p in PLAYS for t in p.get("tasks", []))
NOTE = TASKS["Note what this node needs"]


def spec(version):
    if version is None:
        return {"stderr": "Error: could not find libjava.so\n", "rc": 1}
    return {"stderr": "Property settings:\n    java.home = /x\n    java.specification.version = %s\n    java.vm.vendor = Azul\n"
                      "openjdk version \"%s\"\n" % (version, version)}


def todo(exe, version, wanted="17", java_home="", resolved=""):
    variables = {"cassandra_jvm": {"exe": exe} if exe else {}, "cassandra_update_java_spec": spec(version) if exe else {},
                 "cassandra_java_version": wanted, "_cassandra_java_home": java_home,
                 "cassandra_update_java_home": {"stdout": resolved}}
    for k, v in NOTE["vars"].items():
        variables[k] = trust_as_template(v)
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(NOTE["ansible.builtin.set_fact"]["cassandra_update_java_todo"]))


@pytest.mark.parametrize("exe, version, wanted, expected", [
    ("/usr/lib/jvm/zulu17/bin/java", "17", "17", False),  # the path does not name java-17
    ("/usr/lib/jvm/java-11-openjdk-11.0.25/bin/java", "11", "17", True),
    ("/usr/lib/jvm/java-1.8.0-openjdk/jre/bin/java", "1.8", "8", False),
    ("", "", "17", True),  # not running
    # its JDK removed by an update since it started (java cannot run): restarted onto the installed Java
    ("/usr/lib/jvm/java-17-openjdk-17.0.9/bin/java", None, "17", True),
])
def test_package_java(exe, version, wanted, expected):
    assert todo(exe, version, wanted) is expected


def test_java_home():
    assert todo("/srv/java/jdk-17.0.20+1-jre/bin/java", "17", java_home="/srv/java/jre17",
                resolved="/srv/java/jdk-17.0.20+1-jre") is False
    assert todo("/opt/other/jdk-17/bin/java", "17", java_home="/srv/java/jre17", resolved="/srv/java/jdk-17.0.20+1-jre") is True


def test_running_java_found_by_its_process():
    find = TASKS["Find the running Cassandra and its java"]["ansible.builtin.include_role"]
    assert find["tasks_from"] == "jvm_started.yml"
    assert "systemd_service" not in str(PLAYS[1])
