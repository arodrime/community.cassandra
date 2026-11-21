from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# preflight: JMX over TLS on each running node (its JVM's last -Dcom.sun.management.jmxremote.ssl) against the
# inventory's cassandra_jmx_ssl_enabled. nodetool reaches the nodes with the inventory's (--ssl or not): a running
# node with the other one stops the run before any nodetool call, with the way out.

import os
import shutil
import subprocess

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_preflight_racks import PLAY, render
from ansible_collections.community.cassandra.tests.unit.task_files import find_task, load_tasks

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
TASKS = load_tasks(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "jmx_tls_check.yml"))
READ = find_task(TASKS, "Read whether the running Cassandra serves JMX over TLS")
STOP = find_task(TASKS, "Stop on a JMX TLS the running nodes do not have")


def test_the_check_runs_before_the_first_nodetool_call():
    names = [t["name"] for t in PLAY["tasks"]]
    check = find_task(PLAY, "Check the JMX TLS of the running nodes")
    assert check["ansible.builtin.include_role"]["tasks_from"] == "jmx_tls_check.yml"
    assert names.index("Read this node's settings") < names.index(check["name"]) \
        < names.index("Read the cluster from a running node")
    assert READ["when"] == "_cassandra_preflight_running | bool" and READ["check_mode"] is False
    assert STOP["run_once"] is True


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash runs the script")
@pytest.mark.parametrize("options, out", [
    (["-Dcom.sun.management.jmxremote.ssl=true"], ["ssl=true"]),
    (["-Dcom.sun.management.jmxremote.ssl=true", "-Dcom.sun.management.jmxremote.ssl=false"], ["ssl=false"]),
    ([], ["ssl=false"]),  # (not set: off, as Cassandra reads it)
    (None, []),  # (no Cassandra JVM: nothing said)
])
def test_the_script_reads_the_last_option(tmp_path, options, out):
    """/proc/<pid>/cmdline and pgrep faked: the script's own text otherwise."""
    proc = tmp_path / "proc" / "42"
    proc.mkdir(parents=True)
    main = [] if options is None else ["org.apache.cassandra.service.CassandraDaemon"]
    (proc / "cmdline").write_bytes(("\0".join(["java"] + (options or []) + main) + "\0").encode())
    script = READ["ansible.builtin.shell"].replace("/proc/", str(tmp_path / "proc") + "/")
    run = subprocess.run(["bash", "-c", "pgrep() { echo 42; }\nreadlink() { echo same; }\n" + script],
                         stdout=subprocess.PIPE, check=True)
    assert run.stdout.decode().split() == out


def hostvars(nodes):
    """nodes: host -> (inventory's cassandra_jmx_ssl_enabled, what the read said: None not read (not running))."""
    return dict((h, {"cassandra_jmx_ssl_enabled": inventory,
                     "cassandra_jmx_tls_read": {"stdout_lines": [read]} if read else {"skipped": True}})
                for h, (inventory, read) in nodes.items())


@pytest.mark.parametrize("nodes, differ", [
    ({"n1": (False, "ssl=false"), "n2": (False, "ssl=false")}, []),
    ({"n1": (True, "ssl=true"), "n2": ("true", "ssl=true")}, []),
    ({"n1": (True, "ssl=false"), "n2": (True, None)}, ["n1"]),  # (n2 not running: it starts with the inventory's)
    ({"n1": (False, "ssl=true"), "n2": (True, "ssl=true")}, ["n1"]),  # (a node's own value: host_vars)
])
def test_a_running_node_with_another_jmx_tls_stops_the_run(nodes, differ):
    variables = dict(STOP["vars"], ansible_play_hosts=sorted(nodes), hostvars=hostvars(nodes))
    assert render("{{ _differ }}", **variables) == differ
    assert render("{{ %s }}" % STOP["when"], **variables) is bool(differ)


def test_the_stop_says_why_and_the_way_out():
    nodes = {"n1": (True, "ssl=false"), "n2": (True, "ssl=false"), "n3": (True, "ssl=true")}
    variables = dict(STOP["vars"], ansible_play_hosts=sorted(nodes), hostvars=hostvars(nodes))
    msg = render(STOP["ansible.builtin.fail"]["msg"], **variables).rstrip("\n").split("\n")
    assert msg == [
        "JMX over TLS differs between the running Cassandra and the inventory (cassandra_jmx_ssl_enabled):",
        "  n1    running: off    inventory: on",
        "  n2    running: off    inventory: on",
        "nodetool reaches each node with the inventory's setting (--ssl or not): it cannot reach these nodes.",
        "A switch of JMX TLS on a running cluster is not handled by the collection's runs.",
        "Nothing was changed.",
        "To pass:",
        "  set cassandra_jmx_ssl_enabled to what these nodes run (false), then run again",
        "  or switch it with the whole cluster stopped (guide: Roles, JMX access, switching JMX TLS)"]
