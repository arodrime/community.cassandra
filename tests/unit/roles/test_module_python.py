from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# python3.11, installed for cqlsh or Medusa, comes before /usr/bin/python3 in
# the interpreter discovery of ansible-core 2.17+: from the next run on, the
# modules keep running with /usr/bin/python3 (its python3-debian, Cassandra
# driver), unless ansible_python_interpreter is set.

import os
import warnings

import pytest
import yaml

from ansible import constants as C
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
SKIPPED = {"skipped": True, "changed": False}


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def runs(task, variables):
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(templar.template(trust_as_template("{{ %s }}" % w)) for w in task["when"])


def pin(discovered=None, python3="3.14", rc=0, interpreter=None, discovered_by_probe=None):
    """The interpreter set for the next tasks ('' when left to the discovery), and whether python3 was probed."""
    probe, choose = load("roles", "cassandra_install", "tasks", "python_interpreter.yml")
    facts = {} if discovered is None else {"discovered_interpreter_python": discovered}
    variables = {"ansible_facts": facts}
    if interpreter is not None:
        variables["ansible_python_interpreter"] = interpreter
    probed = runs(probe, variables)
    if discovered is None and discovered_by_probe is not None:
        facts["discovered_interpreter_python"] = discovered_by_probe  # the probe is the first module of the run
    variables["cassandra_module_python3_version"] = {"rc": rc, "stdout": python3 if rc == 0 else ""} \
        if probed else SKIPPED
    if runs(choose, variables):
        return choose["ansible.builtin.set_fact"]["ansible_python_interpreter"], probed
    return "", probed


@pytest.mark.parametrize("discovered, python3, expected", [
    ("/usr/bin/python3.11", "3.14", "/usr/bin/python3"),  # Ubuntu 26.04, ansible-core 2.17 to 2.19
    ("/usr/bin/python3.11", "3.9", "/usr/bin/python3"),   # RHEL 9 with Medusa's python3.11, 2.17+
    ("/usr/bin/python3.11", "3.13", "/usr/bin/python3"),  # Debian 13 with a python3.11, 2.17
    ("/usr/bin/python3.11", "3.11", ""),                  # Debian 12: python3.11 is the system's
    ("/usr/bin/python3.11", "3.6", ""),                   # RHEL 8: 3.6 runs no module of 2.17+
])
def test_python311_discovered(discovered, python3, expected):
    assert pin(discovered, python3) == (expected, True)


@pytest.mark.parametrize("discovered", [
    "/usr/bin/python3",                # first run, 2.16 (distribution map), or no python3.11
    "/usr/bin/python3.12",             # Ubuntu 24.04: its python3 comes first
    "/usr/bin/python3.14",             # Ubuntu 26.04, 2.20+
    "/usr/libexec/platform-python",    # RHEL 8, 2.16
])
def test_other_discovery_left_alone(discovered):
    assert pin(discovered) == ("", False)


def test_set_by_the_user():
    for interpreter in ["/usr/bin/python3.11", "/opt/py/bin/python", "auto"]:
        assert pin("/usr/bin/python3.11", interpreter=interpreter) == ("", False)


def test_no_module_ran_yet():
    # the probe triggers the discovery, its result decides
    assert pin(None, "3.14", discovered_by_probe="/usr/bin/python3.11") == ("/usr/bin/python3", True)
    assert pin(None, "3.14", discovered_by_probe="/usr/bin/python3") == ("", True)
    # an interpreter_python of ansible.cfg: no discovery, no fact
    assert pin(None, "3.14") == ("", True)


def test_interpreter_python_of_the_config(monkeypatch):
    # with a fact cache, a discovered_interpreter_python of an earlier run outlives the discovery
    for value, expected in (("/usr/bin/python3.11", ("", False)), ("auto_silent", ("/usr/bin/python3", True))):
        monkeypatch.setenv("ANSIBLE_PYTHON_INTERPRETER", value)
        monkeypatch.setattr(C, "INTERPRETER_PYTHON", value, raising=False)  # read at startup up to 2.18
        assert pin("/usr/bin/python3.11") == expected


def test_no_usr_bin_python3():
    assert pin("/usr/bin/python3.11", rc=127) == ("", True)


def test_probe_is_read_only():
    probe = load("roles", "cassandra_install", "tasks", "python_interpreter.yml")[0]
    assert probe["check_mode"] is False and probe["changed_when"] is False and probe["failed_when"] is False
    assert probe["ansible.builtin.command"].startswith("/usr/bin/python3 ")


def test_done_first_in_both_roles():
    # cassandra_repository runs deb822_repository; upgrade and update_java run cassandra_install alone
    first = load("roles", "cassandra_install", "tasks", "install.yml")[0]
    assert first["ansible.builtin.import_tasks"] == "python_interpreter.yml"
    first = load("roles", "cassandra_repository", "tasks", "main.yml")[0]
    assert first["ansible.builtin.include_role"] == {
        "name": "community.cassandra.cassandra_install", "tasks_from": "python_interpreter.yml"}
    assert first["when"] == "ansible_python_interpreter is not defined"
