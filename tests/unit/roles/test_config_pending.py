from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config restarts a node only for a config change Cassandra has not
# picked up: the files cassandra_config writes, compared by content with the
# ones it started with. A node with no record of them (imported) restarts for
# nothing; files written since its JVM started are only listed.

import base64
import json
import os
import subprocess
import sys
import time

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


def tasks(name):
    with open(os.path.join(ROLE, "tasks", name)) as f:
        todo = list(yaml.safe_load(f))
    out = []
    while todo:
        t = todo.pop(0)
        out.append(t)
        todo += t.get("block", [])
    return out


def task(tasks_file, name):
    return next(t for t in tasks(tasks_file) if t.get("name") == name)


NOTE = task("config_pending.yml", "Note whether a restart is pending")
YAML = "/etc/cassandra/conf/cassandra.yaml"
ENV = "/etc/cassandra/conf/cassandra-env.sh"


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def pending(snapshot, files, jvm=None):
    variables = {
        "cassandra_pending_files": {"files": [{"path": p, "checksum": c, "ctime": m} for p, c, m in files]},
        "cassandra_pending_snapshot": ({"content": base64.b64encode(json.dumps(snapshot).encode()).decode()}
                                       if snapshot is not None else {}),
    }
    if jvm is not None:
        variables["cassandra_jvm"] = jvm
    variables["_started"] = trust_as_template(NOTE["vars"]["_started"])
    return render(NOTE["vars"]["_changed"], variables), render(NOTE["vars"]["_unknown"], variables)


def test_only_the_files_cassandra_config_writes():
    patterns = yaml.safe_load(open(os.path.join(ROLE, "defaults", "main.yml")))["_cassandra_service_config_files"]
    find = task("config_pending.yml", "Checksum the config files")["ansible.builtin.find"]
    assert find["patterns"] == "{{ _cassandra_service_config_files }}"
    import fnmatch
    names = ["cassandra.yaml", "cassandra-env.sh", "jvm-server.options", "jvm11-server.options", "jvm17-server.options",
             "jvm8-server.options", "cassandra-rackdc.properties", "logback.xml",
             "jmxremote.password", "jmxremote.access"]  # in the conf dir on Debian: read at start too
    assert all(any(fnmatch.fnmatch(n, p) for p in patterns) for n in names)
    # a keystore touched, an editor backup, the topology file: not the config Cassandra restarts for
    for other in [".keystore", "cassandra.yaml~", "cassandra.yaml.12345.2026-10-06@10:00:00~", "cassandra-topology.properties",
                  "commitlog_archiving.properties", "logback-tools.xml"]:
        assert not any(fnmatch.fnmatch(other, p) for p in patterns), other


@pytest.mark.parametrize("checksum, expected", [("y1", []), ("y2", ["cassandra.yaml"])])
def test_recorded_compares_contents(checksum, expected):
    # a file rewritten with the same content after the start is not pending
    assert pending({YAML: "y1"}, [(YAML, checksum, 999)]) == (expected, [])


def test_recorded_without_the_file_is_pending():
    assert pending({ENV: "e1"}, [(YAML, "y1", 1)]) == (["cassandra.yaml"], [])


def test_no_record_is_not_pending():
    # imported: written after the JVM started, its content then is unknown: listed, not restarted
    jvm = {"pid": "42", "start": "100.50", "exe": "/usr/bin/java"}
    assert pending(None, [(YAML, "y1", 100.2), (ENV, "e1", 100.7)], jvm) == ([], ["cassandra-env.sh"])


def test_no_record_and_no_jvm():
    assert pending(None, [(YAML, "y1", 100.2)], {}) == ([], [])


def test_seed_leaves_out_the_files_written_since_the_start():
    rec = task("config_seed.yml", "Record the ones not written since it started")
    variables = {
        "cassandra_seed_files": {"files": [{"path": YAML, "checksum": "y1", "ctime": 90.0},
                                           {"path": ENV, "checksum": "e1", "ctime": 110.0}]},
        "cassandra_jvm": {"start": "100.00"},
    }
    variables["_files"] = trust_as_template(rec["vars"]["_files"])
    assert json.loads(render(rec["ansible.builtin.copy"]["content"], variables)) == {YAML: "y1"}


def test_config_seeds_before_writing():
    # cassandra_config records what the JVM started with before its first change of a node started another way
    with open(os.path.join(ROLE, "..", "cassandra_config", "tasks", "main.yml")) as f:
        main = yaml.safe_load(f)
    block = next(t for t in main if t.get("name") == "Configure")["block"]
    names = [t["name"] for t in block]
    seed = names.index("Record the config the running Cassandra started with")
    assert seed < names.index("Template Cassandra config files")
    assert block[seed]["ansible.builtin.include_role"]["tasks_from"] == "config_seed.yml"
    assert "not ansible_check_mode" in block[seed]["when"]


def test_jvm_found_by_its_main_class(tmp_path):
    # whatever started it (no systemd MainPID): a process named java with CassandraDaemon on its command line
    java = tmp_path / "java"
    java.symlink_to(sys.executable)
    before = time.time()
    proc = subprocess.Popen([str(java), "-c", "import time; time.sleep(30)", "org.apache.cassandra.service.CassandraDaemon"])
    try:
        time.sleep(0.5)
        script = task("jvm_started.yml", "Find the running Cassandra JVM")["ansible.builtin.shell"]
        # pgrep sees this test's java only (a Cassandra of a container on this host would be found too)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "pgrep").write_text("#!/bin/sh\n[ \"$*\" = '-x java' ] && echo %d\n" % proc.pid)
        (tmp_path / "bin" / "pgrep").chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path / "bin", os.environ["PATH"]))
        out = subprocess.run(["sh", "-c", script], stdout=subprocess.PIPE, check=True, universal_newlines=True,
                             env=env).stdout
        found = dict(line.split("=", 1) for line in out.splitlines())
        assert found["pid"] == str(proc.pid)
        # to a tenth of a second or so (the boot time of /proc/stat alone is whole seconds)
        assert abs(float(found["start"]) - before) < 0.5
        assert found["exe"] == os.path.realpath(sys.executable)
    finally:
        proc.kill()
        proc.wait()


def test_import_lists_the_config_files_written_since_the_start(tmp_path):
    # import_cluster reports them (the node may not run what they say), with the same start as jvm_started.yml
    playbook = os.path.join(ROLE, "..", "..", "playbooks", "import_cluster.yml")
    with open(playbook) as f:
        plays = yaml.safe_load(f)
    shell = next(t for p in plays for t in p.get("tasks", [])
                 if t.get("name", "").startswith("Read the running Cassandra"))["ansible.builtin.shell"]
    lines = shell.splitlines()
    first = next(i for i, s in enumerate(lines) if "btime=" in s)
    last = next(i for i, s in enumerate(lines) if "-newermt" in s)
    script = "\n".join(lines[first:last + 1])
    for name, age in [("cassandra.yaml", 3600), ("cassandra-env.sh", 3600), ("jvm17-server.options", 0),
                      ("cassandra-topology.properties", 0), ("cassandra.yaml~", 0)]:
        (tmp_path / name).write_text("x\n")
        os.utime(str(tmp_path / name), (time.time() - age, time.time() - age))
    # this test's own process started before the files of age 0, after the others
    out = subprocess.run(["sh", "-c", "d=$1; pid=$2\n" + script, "sh", str(tmp_path), str(os.getpid())],
                         stdout=subprocess.PIPE, check=True, universal_newlines=True).stdout
    assert out.split("=", 1)[1].split() == ["jvm17-server.options"]
