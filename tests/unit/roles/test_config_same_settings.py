from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config leaves a running node's file alone when its settings are the
# same as the role's (a config written by hand: no header, other comments or
# layout), and writes remote JMX users the way the node has them.

import json
import os
import subprocess
import sys

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks")
HEADER = "# Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file.\n"


def task(name, tasks_file="main.yml"):
    with open(os.path.join(TASKS, tasks_file), encoding="utf-8") as f:
        todo = list(yaml.safe_load(f))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


SCRIPT = task("Diff them against the live files")["ansible.builtin.command"]["argv"][2]


def compare(tmp_path, name, live, new):
    """The preview's exit code for a live file and the role's: 0 same, 1 differ, 2 same settings."""
    (tmp_path / "live").mkdir()
    (tmp_path / "new").mkdir()
    live_path, new_path = tmp_path / "live" / name, tmp_path / "new" / name
    if live is not None:
        live_path.write_text(live)
    new_path.write_text(new)
    return subprocess.run([sys.executable, "-c", SCRIPT, str(live_path), str(new_path)],
                          stdout=subprocess.PIPE, check=False).returncode


@pytest.mark.parametrize("name, live, new, rc", [
    ("jvm-server.options", "-Xss256k\n", "-Xss256k\n", 0),
    # only the role's header line is missing (written by hand)
    ("jvm-server.options", "# stock comment\n-Xss256k\n", HEADER + "# stock comment\n-Xss256k\n", 2),
    ("cassandra-env.sh", "MAX_HEAP_SIZE=8G\n", HEADER + "# a comment\n\nMAX_HEAP_SIZE=8G\n", 2),
    ("cassandra-env.sh", "MAX_HEAP_SIZE=8G\n", HEADER + "MAX_HEAP_SIZE=4G\n", 1),
    # a switched-off line is a setting too
    ("cassandra-env.sh", "#JVM_OPTS=-Da\n", "JVM_OPTS=-Da\n", 1),
    # key=value files: order, spacing and comments don't count
    ("cassandra-rackdc.properties", "rack=r1\ndc = dc1\n", HEADER + "# the DC\ndc=dc1\nrack=r1\n# prefer_local=true\n", 2),
    ("cassandra-rackdc.properties", "dc=dc1\nrack=r1\n", "dc=dc1\nrack=r2\n", 1),
    ("cassandra.yaml", "cluster_name: 'A'\nnum_tokens: 16\n", HEADER + "# tokens\nnum_tokens: 16\ncluster_name: 'A'\n", 2),
    ("cassandra.yaml", "cluster_name: 'A'\n", "cluster_name: 'B'\n", 1),
    # 1, 1.0 and true are not the same setting
    ("cassandra.yaml", "x: 1\n", HEADER + "x: true\n", 1),
    ("logback.xml", "<configuration>\n</configuration>\n",
     "<!-- Managed by Ansible -->\n<!--\n a licence\n-->\n<configuration>\n</configuration>\n", 2),
    ("logback.xml", "<configuration/>\n", "<configuration>\n</configuration>\n", 1),
    # no live file: written
    ("jvm-server.options", None, HEADER, 1),
])
def test_preview_tells_same_settings(tmp_path, name, live, new, rc):
    assert compare(tmp_path, name, live, new) == rc


def kept(results, initialized=True, normalize=False):
    template = task("List the files to change and whether to ask first")["vars"]["_kept"]
    return render(template, cassandra_config_normalize=normalize,
                  cassandra_config_initialized={"stat": {"exists": initialized}},
                  cassandra_config_preview={"results": results})


RESULTS = [{"cassandra_config_file": "cassandra.yaml", "rc": 1, "stdout": "diff"},
           {"cassandra_config_file": "jvm-server.options", "rc": 2, "stdout": "diff"},
           {"cassandra_config_file": "logback.xml", "rc": 0, "stdout": ""}]


def test_same_settings_kept_on_a_running_node_only():
    assert kept(RESULTS) == ["jvm-server.options"]
    assert kept(RESULTS, initialized=False) == []  # a new node gets every file of the role
    assert kept(RESULTS, normalize=True) == []


JMX = task("Write the JMX users", "access.yml")
ACCESS = JMX["vars"]["_cassandra_jmx_access_content"]
JMX_SCRIPT = task("Compare the JMX files with the users", "access.yml")["ansible.builtin.command"]["argv"][2]


@pytest.mark.parametrize("create_unregister, access", [
    (True, "ops readwrite \\\n    create javax.management.monitor.*,javax.management.timer.* \\\n    unregister\nmon readonly\n"),
    (False, "ops readwrite\nmon readonly\n"),
])
def test_jmx_access_rights(create_unregister, access):
    users = [{"name": "ops", "password": "s3cret", "access": "readwrite", "create_unregister": create_unregister},
             {"name": "mon", "password": "m0n", "access": "readonly"}]
    assert render(ACCESS, cassandra_jmx_users=users) == access


def jmx_same(tmp_path, password, access, users):
    """What the JMX compare finds the same on a node with these files."""
    root = tmp_path / "etc" / "cassandra"
    root.mkdir(parents=True)
    for name, text in (("password", password), ("access", access)):
        if text is not None:
            (root / ("jmxremote." + name)).write_text(text)
    script = JMX_SCRIPT.replace("/etc/cassandra/", str(root) + "/")
    wanted = {"password": render(JMX["vars"]["_cassandra_jmx_password_content"], cassandra_jmx_users=users),
              "access": render(ACCESS, cassandra_jmx_users=users)}
    out = subprocess.run([sys.executable, "-c", script], input=json.dumps(wanted).encode(),
                         stdout=subprocess.PIPE, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("password, access, same", [
    # written by hand: comments, spacing, rights in another order
    ("# JMX\nops  s3cret\nmon m0n\n", "# rights\nmon readonly\nops readwrite unregister create javax.management.monitor.*,javax.management.timer.*\n",
     ["password", "access"]),
    ("ops other\nmon m0n\n", "ops readwrite\nmon readonly\n", []),
    (None, None, []),
])
def test_jmx_files_with_the_same_users_are_kept(tmp_path, password, access, same):
    users = [{"name": "ops", "password": "s3cret", "access": "readwrite"},
             {"name": "mon", "password": "m0n", "access": "readonly"}]
    assert jmx_same(tmp_path, password, access, users) == same
