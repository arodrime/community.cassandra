from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# health_check says a secret in the inventory's cassandra_service_environment, which apply_config writes to the
# cassandra unit: the unit file readable by every local user, systemctl show prints it. Said as the inventory's
# value (the file on the node is not read), never as what the file holds.

import subprocess
import sys

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_unit_secret_warning
from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    find, load, render)
from ansible_collections.community.cassandra.tests.unit.helpers import playbook_command

TASKS = [t for p in load("playbooks", "health_check.yml") for t in p.get("tasks") or []]
SAY = find(TASKS, "Say a secret in the unit's environment")
SECRET = {"JVM_EXTRA_OPTS": "-Djavax.net.ssl.trustStorePassword=zorglub"}


def said(hostvars):
    variables = dict(SAY["vars"], hostvars=hostvars, ansible_play_hosts=sorted(hostvars))
    if not render("{{ %s }}" % SAY["when"], variables):
        return []
    return render(SAY["ansible.builtin.debug"]["msg"], variables)


def test_a_secret_in_the_unit_environment_is_a_warning_of_the_health_check():
    names = [t.get("name") for t in TASKS]
    assert names.index("Say a secret in the unit's environment") < names.index("Report")
    assert SAY["run_once"] is True and SAY["vars"]["cassandra_output"] is True
    lines = said({"node1": {"cassandra_service_environment": SECRET},
                  "node2": {"cassandra_service_environment": SECRET},
                  "node3": {"cassandra_service_environment": {"MAX_HEAP_SIZE": "8G"}},
                  "node4": {}})
    assert max(len(x) for x in lines) <= 79
    text = " ".join(" ".join(lines).split())
    assert lines[0].startswith("WARNING  node1, node2: the inventory's cassandra_service_environment holds a")
    assert text.startswith(lines[0].split()[0] + " node1, node2: the inventory's cassandra_service_environment holds"
                           " a secret: JVM_EXTRA_OPTS ")
    assert "apply_config writes it to /etc/systemd/system/cassandra.service, mode 0644: every local user can read" \
        " it" in text
    assert "once written, systemctl show cassandra prints it too" in text
    assert "written to" not in text  # (not observed: the file on the node is not read)
    assert "way out: keep it out of cassandra_service_environment" in text
    assert "zorglub" not in text and "node3" not in text and "node4" not in text
    assert text.count("WARNING") == 1  # (one block for the nodes that share it)


def test_the_health_check_says_the_file_apply_config_writes():
    lines = said({"node1": {"cassandra_service_environment": SECRET, "cassandra_service_start": "package_script"}})
    text = " ".join(" ".join(lines).split())
    expected = cassandra_unit_secret_warning(SECRET, "package_script", True, inventory=True)
    assert "/etc/systemd/system/cassandra.service.d/zz-ansible-environment.conf" in expected
    assert lines[0].startswith("WARNING  node1: the inventory's")
    assert text.split(" ", 2)[2] == " ".join(expected.split())  # (after the mark and the node)
    # a unit the role does not write (kept as it is, another start): nothing written by it, nothing said
    assert said({"node1": {"cassandra_service_environment": SECRET, "cassandra_service_unit_manage": False}}) == []
    assert said({"node1": {"cassandra_service_environment": SECRET, "cassandra_service_start": "none"}}) == []
    assert said({"node1": {}}) == []


def test_apply_config_and_the_role_pick_the_file_with_the_same_filter():
    # one place picks the file (cassandra_unit_secret_warning): apply_config, the cassandra_service role, health_check
    apply_config = [t for p in load("playbooks", "apply_config.yml") for t in p.get("tasks") or []]
    text = str(apply_config)
    assert "cassandra_unit_secret_warning(" in text
    assert "zz-ansible-environment.conf" not in text
    assert "zz-ansible-environment.conf" not in str(SAY)


class _Undefined(dict):
    """a node's hostvars as ansible-core 2.19+ gives them when cassandra_service_environment uses a variable not in
    hostvars (a role default): reading it raises"""

    def get(self, key, default=None):
        if key == "cassandra_service_environment":
            from ansible.errors import AnsibleUndefinedVariable
            raise AnsibleUndefinedVariable("'cassandra_data_dir' is undefined")
        return dict.get(self, key, default)


class _Broken(dict):
    """a value whose use fails with an error that quotes it"""

    def items(self):
        raise ValueError("bad template: -Djavax.net.ssl.trustStorePassword=zorglub {{")


def test_a_value_that_cannot_be_read_is_said_not_checked_for_that_node():
    # the run goes on, the other nodes checked; the node said not checked, with the name of the variable missing,
    # never the value's text (it may hold a secret)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_unit_secret_warnings
    hostvars = {"node1": _Undefined(cassandra_dc="dc1"), "node2": {"cassandra_service_environment": SECRET},
                "node3": {"cassandra_service_environment": _Broken(JVM_EXTRA_OPTS="x")}}
    said = cassandra_unit_secret_warnings(["node1", "node2", "node3"], hostvars)
    assert said == [
        {"label": "node1", "text": "the inventory's cassandra_service_environment is not checked for a secret\n"
                                   "its value could not be read ('cassandra_data_dir' is undefined)"},
        {"label": "node2", "text": cassandra_unit_secret_warning(SECRET, inventory=True)},
        {"label": "node3", "text": "the inventory's cassandra_service_environment is not checked for a secret\n"
                                   "its value could not be read (ValueError)"}]


@pytest.mark.playbook
def test_a_value_that_cannot_be_read_does_not_fail_the_run(tmp_path):
    # the task run by ansible-playbook: a variable the value uses is not in hostvars (2.19+: an error on use;
    # before: the template left as it is, nothing said)
    task = dict((k, v) for k, v in SAY.items() if k != "when")
    play = [{"hosts": "all", "gather_facts": False, "tasks": [task]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"node1": {
        "ansible_connection": "local",
        "cassandra_service_environment": {"JVM_EXTRA_OPTS": "-Dx={{ cassandra_data_dir }}/x"}}}}}))
    argv, env = playbook_command(["-i", tmp_path / "hosts.yml", "-e", "ansible_python_interpreter=" + sys.executable,
                                  tmp_path / "play.yml"])
    run = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
                         check=False, cwd=str(tmp_path))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "failed=0" in run.stdout.replace(" ", "")
