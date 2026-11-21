from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: JMX over TLS on a given node (its JVM's last -Dcom.sun.management.jmxremote.ssl) is found before
# nodetool runs there: nodetool then gets --ssl, and the run stops when root has no nodetool-ssl.properties.

import os
import shutil
import subprocess

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_import_cluster_unsupported import FIND, READ, render
from ansible_collections.community.cassandra.tests.unit.task_files import find_task

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
FIND_TLS = find_task(FIND, "Find whether JMX runs over TLS on the given nodes")
USE_TLS = find_task(FIND, "Use JMX over TLS where the JVM serves it")
STOP = find_task(FIND, "Stop on JMX over TLS without nodetool's settings")
NODE_READ = find_task(READ, "Read the running Cassandra (conf dir, Cassandra and Java versions)")


def test_tls_is_found_before_nodetool_runs():
    names = [t["name"] for t in FIND["tasks"]]
    assert names.index(STOP["name"]) < names.index("Read what runs on the given nodes") < names.index("Read the ring")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash runs the script")
@pytest.mark.parametrize("options, properties, out", [
    (["-Dcom.sun.management.jmxremote.ssl=true"], False, ["ssl=true"]),
    (["-Dcom.sun.management.jmxremote.ssl=true"], True, ["ssl=true", "properties=yes"]),
    (["-Dcom.sun.management.jmxremote.ssl=true", "-Dcom.sun.management.jmxremote.ssl=false"], False, ["ssl=false"]),
    ([], False, ["ssl="]),
])
def test_the_script_reads_the_last_option(tmp_path, options, properties, out):
    """/proc/<pid>/cmdline and pgrep faked: the script's own text otherwise."""
    proc = tmp_path / "proc" / "42"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(("\0".join(["java"] + options + ["org.apache.cassandra.service.CassandraDaemon"])
                                    + "\0").encode())
    home = tmp_path / "home"
    (home / ".cassandra").mkdir(parents=True)
    if properties:
        (home / ".cassandra" / "nodetool-ssl.properties").write_text("-Djavax.net.ssl.trustStore=/srv/zorglub/ts.jks\n")
    script = FIND_TLS["ansible.builtin.shell"].replace("/proc/", str(tmp_path / "proc") + "/")
    run = subprocess.run(["bash", "-c", "pgrep() { echo 42; }\n" + script], stdout=subprocess.PIPE, check=True,
                         env=dict(os.environ, HOME=str(home)))
    assert run.stdout.decode().split() == out


@pytest.mark.parametrize("reads, missing", [
    ({"n1": "ssl=true\nproperties=yes", "n2": "ssl=false"}, []),
    ({"n1": "ssl=true", "n2": "ssl=true\nproperties=yes", "n3": None}, ["n1"]),
])
def test_the_stop_names_the_nodes_without_nodetools_settings(reads, missing):
    hostvars = dict((h, {"import_cluster_jmx_tls": {"stdout_lines": r.split("\n")}} if r is not None else {})
                    for h, r in reads.items())
    variables = dict(STOP["vars"], import_cluster_given=list(reads), hostvars=hostvars)
    assert render("{{ _missing }}", **variables) == missing
    if missing:
        msg = " ".join(render(STOP["ansible.builtin.assert"]["fail_msg"], **variables).split())
        assert msg.startswith("JMX runs over TLS on n1 (-Dcom.sun.management.jmxremote.ssl=true), and nodetool finds"
                              " no ~/.cassandra/nodetool-ssl.properties for root there.")
        assert msg.endswith("then run import_cluster again. Nothing was changed.")


def test_nodetool_gets_ssl_where_the_jvm_serves_it():
    assert render("{{ %s }}" % USE_TLS["when"], import_cluster_jmx_tls={"stdout_lines": ["ssl=true"]}) is True
    assert render("{{ %s }}" % USE_TLS["when"], import_cluster_jmx_tls={"stdout_lines": ["ssl=false"]}) is False


@pytest.mark.parametrize("options, called", [
    (["-Dcom.sun.management.jmxremote.ssl=true"], "version --ssl"),
    (["-Dcom.sun.management.jmxremote.ssl=true", "-Dcom.sun.management.jmxremote.ssl=false"], "version"),
    ([], "version"),
])
def test_a_ring_node_read_gives_nodetool_ssl_from_its_own_jvm(tmp_path, options, called):
    """A node of the ring the given nodes did not tell: the read's nodetool (the JVM's classpath names no Cassandra
    jar) gets --ssl from that JVM's last option. The script as the node gets it, /proc/<pid>/cmdline faked."""
    (tmp_path / "cmdline").write_bytes(("\0".join(["java"] + options + ["org.apache.cassandra.service.CassandraDaemon"])
                                        + "\0").encode())
    nodetool = tmp_path / "nodetool"
    nodetool.write_text('#!/bin/sh\necho "$*" > "%s"\necho "ReleaseVersion: 4.1.5"\n' % (tmp_path / "called"))
    nodetool.chmod(0o755)
    script = render(NODE_READ["ansible.builtin.shell"], playbook_dir=os.path.join(TOP, "playbooks"),
                    _cassandra_service_nodetool=str(nodetool))
    lines = script.split("\n")
    first = next(i for i, line in enumerate(lines) if line.startswith("tls=$("))
    last = next(i for i, line in enumerate(lines) if line.startswith('echo "version=$ver"'))
    snippet = "\n".join(lines[first:last + 1]).replace("/proc/$pid/cmdline", str(tmp_path / "cmdline"))
    run = subprocess.run(["sh", "-c", snippet], stdout=subprocess.PIPE, check=True, universal_newlines=True)
    assert run.stdout == "version=4.1.5\n"
    assert (tmp_path / "called").read_text() == called + "\n"
