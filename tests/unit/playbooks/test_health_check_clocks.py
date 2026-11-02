from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# health_check run for real by ansible-playbook with the ops callback on 3 local hosts: nodetool a script that
# prints a ring (test_output_golden), the time sync daemons stand-ins (test_clocks_check). The CLOCKS block,
# every clock in sync and one off or not read: the whole output compared.

import os
import socket
import stat
import subprocess
import sys

import pytest

from ansible_collections.community.cassandra.tests.unit.playbooks.test_output_golden import (
    CLUSTER, COLLECTIONS, DESCRIBE, FIXTURES, NODETOOL, STATUS, ruled)
from ansible_collections.community.cassandra.tests.unit.roles.test_clocks_check import (
    CHRONYC, DETECT_VIRT, SOURCES, SYSTEMCTL, TIMEDATECTL, tracking)

INVENTORY = """[my_cluster]
node1 ansible_host=10.100.100.3 cassandra_dc=dc1 cassandra_rack=rack_a ansible_python_interpreter=%(bin)s/python-node1
node2 ansible_host=10.100.100.4 cassandra_dc=dc1 cassandra_rack=rack_b ansible_python_interpreter=%(bin)s/python-node2
node3 ansible_host=10.100.100.5 cassandra_dc=dc1 cassandra_rack=rack_c ansible_python_interpreter=%(bin)s/python-node3

[my_cluster_dc1:children]
my_cluster

[my_cluster:vars]
ansible_connection=local
cassandra_listen_address=127.0.0.1
cassandra_rpc_address=127.0.0.1
"""

# each host's own Python tells the stand-ins which node runs them; one whose command module fails (the clock
# probe is a shell task) leaves the other reads alone
PYTHON = '#!/bin/sh\ncase "$*" in *AnsiballZ_command*) [ -n "$FAIL_COMMANDS" ] && exit 127;; esac\n' \
         'FAKE_NODE=%s PATH=%s:$PATH exec %s "$@"\n'


@pytest.fixture
def port():
    """A port that answers, for the storage and CQL port checks."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    yield server.getsockname()[1]
    server.close()


def synced(seconds=0.002, way="slow"):
    return {"active": "chronyd", "tracking": tracking(seconds, way), "sources": SOURCES}


def run(tmp_path, port, nodes, unread=(), *args):
    """nodes: {name: the stand-ins' files}; unread: the nodes whose clock probe fails; args: more options."""
    answers = tmp_path / "nodetool"
    answers.mkdir()
    (answers / "version.txt").write_text("ReleaseVersion: 4.1.5\n")
    (answers / "statusgossip.txt").write_text("running\n")
    (answers / "statusbinary.txt").write_text("running\n")
    with open(os.path.join(FIXTURES, "nodetool_netstats_normal.txt")) as f:
        (answers / "netstats.txt").write_text(f.read())
    (answers / "describecluster.txt").write_text(DESCRIBE)
    (answers / "status.txt").write_text(STATUS % "U")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nodetool").write_text(NODETOOL % (answers, answers))
    for name, text in (("systemctl", SYSTEMCTL), ("chronyc", CHRONYC), ("timedatectl", TIMEDATECTL),
                       ("systemd-detect-virt", DETECT_VIRT)):
        (bin_dir / name).write_text(text)
    fake = tmp_path / "fake"
    for name, files in nodes.items():
        (fake / name).mkdir(parents=True)
        for fname, content in files.items():
            (fake / name / fname).write_text(content)
        python = PYTHON % (name, bin_dir, sys.executable)
        if name in unread:
            python = python.replace("#!/bin/sh\n", "#!/bin/sh\nFAIL_COMMANDS=1\n")
        (bin_dir / ("python-" + name)).write_text(python)
    for f in bin_dir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    (fake / "log").write_text("")
    (tmp_path / "hosts.ini").write_text(INVENTORY % {"bin": bin_dir})
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0",
               ANSIBLE_DEPRECATION_WARNINGS="0", FAKE_DIR=str(fake), PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "")),
               PWD=str(tmp_path))  # (the shell's: the inventory path of the command relative to it)
    env.pop("ANSIBLE_CONFIG", None)
    env.pop("CASSANDRA_CLUSTER", None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", "hosts.ini",
            "community.cassandra.health_check", "-e", "cassandra_storage_port=%d" % port,
            "-e", "cassandra_native_transport_port=%d" % port] + list(args)
    proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          check=False, cwd=str(tmp_path), timeout=600)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


IN_SYNC = "CLOCKS  in sync: synchronised to a source, offset plus the daemon's error bound within 50 ms (cassandra_clock_max_offset_ms)"


def test_every_clock_in_sync(tmp_path, port):
    rc, output = run(tmp_path, port, {"node1": synced(), "node2": synced(0.003, "fast"), "node3": synced(0.0)})
    assert rc == 0, output
    assert ruled(output) == [
        CLUSTER,
        "",
        IN_SYNC,
        u"  node1  ok  chronyd, synchronised to 192.0.2.1, -2.0 ms (±0.7 ms)",
        u"  node2  ok  chronyd, synchronised to 192.0.2.1, +3.0 ms (±0.7 ms)",
        u"  node3  ok  chronyd, synchronised to 192.0.2.1, -0.0 ms (±0.7 ms)",
        "",
        "====",
        "",
        "HEALTHY  my_cluster  3 nodes  3 UN, schema agreed, no streams, ports open",
        "",
        "====",
    ]
    assert (tmp_path / "fake" / "log").read_text().count("chronyc -n tracking") == 3  # one read per node


def test_a_clock_off_and_one_not_read_are_a_problem(tmp_path, port):
    # node2 ahead by 200 ms (Cassandra runs there: the warning about the writes it stamped), node3's probe fails:
    # NOT READ, its other checks done; one problem, sync_clocks --check the way out; a non-zero exit for scheduling
    rc, output = run(tmp_path, port, {"node1": synced(), "node2": synced(0.2, "fast"), "node3": synced()}, unread=["node3"])
    assert rc != 0, output
    lines = ruled(output)
    assert lines[:5] == [
        CLUSTER,
        "",
        IN_SYNC,
        u"  node1  ok        chronyd, synchronised to 192.0.2.1, -2.0 ms (±0.7 ms)",
        u"  node2  OFF       chronyd, synchronised to 192.0.2.1, +200.0 ms (±0.7 ms): ahead of its source by 200.0 ms,"
        " more than 50.0 ms",
    ]
    assert lines[5].startswith("  node3  NOT READ  its clock could not be read: Module result deserialization failed")
    assert lines[6].startswith("WARNING  node2's clock is AHEAD by about 200.0 ms: the writes it stamped as coordinator")
    verdict = lines.index("NOT HEALTHY  my_cluster  3 nodes checked, 1 problem")
    assert lines[verdict - 3:verdict] == ["", "====", ""]
    assert lines[verdict:] == [
        "NOT HEALTHY  my_cluster  3 nodes checked, 1 problem",
        "  clocks:  node2 OFF, node3 NOT READ (CLOCKS above says why)",
        "TO DO",
        "  1. bring the clocks in sync (sync_clocks; --check first shows what it would do and changes nothing):",
        "     ansible-playbook -i hosts.ini community.cassandra.sync_clocks --check",
        "",
        "====",
    ]


def test_the_clocks_can_be_left_out(tmp_path, port):
    nodes = {"node1": synced(), "node2": synced(0.2, "fast"), "node3": synced()}
    rc, output = run(tmp_path, port, nodes, (), "-e", "cassandra_clock_check=false")
    assert rc == 0, output
    assert "CLOCKS" not in output and "node2" not in output
    assert (tmp_path / "fake" / "log").read_text() == ""  # nothing read
