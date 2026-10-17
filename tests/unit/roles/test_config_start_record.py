from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The record of the config files a running Cassandra started with
# (cassandra_service at each start, cassandra_config and the unit for a node
# with none): the files not written since its JVM started, found by its main
# class.

import hashlib
import json
import os
import subprocess
import sys
import time

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")


def tasks(name):
    """A task file, from the role that holds it (the record of the config at start: config and service)."""
    path = next(p for p in (os.path.join(ROLES, r, "tasks", name)
                            for r in ("cassandra_service", "cassandra_config")) if os.path.exists(p))
    with open(path) as f:
        todo = list(yaml.safe_load(f))
    out = []
    while todo:
        t = todo.pop(0)
        out.append(t)
        todo += t.get("block", [])
    return out


def task(tasks_file, name):
    return next(t for t in tasks(tasks_file) if t.get("name") == name)


YAML = "/etc/cassandra/conf/cassandra.yaml"
ENV = "/etc/cassandra/conf/cassandra-env.sh"
UNIT = "/etc/systemd/system/cassandra.service"


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_seed_leaves_out_the_files_written_since_the_start():
    rec = task("config_seed.yml", "Record the ones not written since it started")
    variables = {
        "cassandra_seed_files": {"files": [{"path": YAML, "checksum": "y1", "ctime": 90.0},
                                           {"path": ENV, "checksum": "e1", "ctime": 110.0}]},
        "cassandra_seed_unit": {"stat": {"exists": True, "path": UNIT, "checksum": "u1", "ctime": 95.0}},
        "cassandra_jvm": {"pid": "42", "start": "100.00"},
    }
    variables["_files"] = trust_as_template(rec["vars"]["_files"])
    jvm = {"pid": "42", "start": 100.0}  # the JVM it records, as config_snapshot.yml does
    assert json.loads(render(rec["ansible.builtin.copy"]["content"], variables)) == {YAML: "y1", UNIT: "u1", "_jvm": jvm}
    # the unit written since the start: left out too
    variables["cassandra_seed_unit"]["stat"]["ctime"] = 105.0
    assert json.loads(render(rec["ansible.builtin.copy"]["content"], variables)) == {YAML: "y1", "_jvm": jvm}


def test_service_seeds_before_changing_the_unit():
    # cassandra_service records what the JVM started with before it changes the unit of a node started another way
    names = [t["name"] for t in tasks("unit.yml")]
    plan, seed = names.index("Tell whether the unit changes"), names.index("Record the config the running Cassandra started with")
    assert plan < seed < names.index("Install the cassandra systemd unit")
    unit = tasks("unit.yml")
    assert unit[plan]["check_mode"] is True
    assert "mode" not in unit[plan]["ansible.builtin.template"]  # a mode change alone records nothing
    assert "not ansible_check_mode" in unit[plan]["when"]
    assert unit[seed]["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_config", "tasks_from": "config_seed.yml"}
    assert unit[seed]["when"] == "cassandra_service_unit_planned is changed"


def test_config_seeds_before_writing():
    # cassandra_config records what the JVM started with before its first change of a node started another way
    main = load_tasks(os.path.join(ROLES, "cassandra_config", "tasks", "main.yml"))
    block = next(t for t in main if t.get("name") == "Configure")["block"]
    names = [t["name"] for t in block]
    seed = names.index("Record the config the running Cassandra started with")
    assert seed < names.index("Template Cassandra config files")
    assert block[seed]["ansible.builtin.include_tasks"] == "config_seed.yml"
    assert "not ansible_check_mode" in block[seed]["when"]


def test_jvm_found_by_its_main_class(tmp_path):
    # whatever started it (no systemd MainPID): a process named java with CassandraDaemon on its command line
    java = tmp_path / "java"
    java.symlink_to(sys.executable)
    before = time.time()
    proc = subprocess.Popen([str(java), "-c", "import time; time.sleep(30)", "-Dcassandra.storagedir=/srv/c",
                             "org.apache.cassandra.service.CassandraDaemon"])
    try:
        for dummy in range(100):  # until the interpreter has the command line
            with open("/proc/%d/cmdline" % proc.pid, "rb") as f:
                if b"CassandraDaemon" in f.read():
                    break
            time.sleep(0.05)
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
        assert found["storagedir"] == "/srv/c"  # where the directories cassandra.yaml leaves out are
    finally:
        proc.kill()
        proc.wait()


def test_jvm_whose_java_was_removed(tmp_path):
    # a JDK update removed the directory of the running java: its path is still given, without " (deleted)"
    import shutil
    jdk = tmp_path / "jdk" / "bin"
    jdk.mkdir(parents=True)
    java = jdk / "java"
    shutil.copy(os.path.realpath(sys.executable), str(java))
    proc = subprocess.Popen([str(java), "-c", "import time; time.sleep(30)", "org.apache.cassandra.service.CassandraDaemon"])
    try:
        for dummy in range(100):
            with open("/proc/%d/cmdline" % proc.pid, "rb") as f:
                if b"CassandraDaemon" in f.read():
                    break
            time.sleep(0.05)
        shutil.rmtree(str(tmp_path / "jdk"))
        script = task("jvm_started.yml", "Find the running Cassandra JVM")["ansible.builtin.shell"]
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "pgrep").write_text("#!/bin/sh\n[ \"$*\" = '-x java' ] && echo %d\n" % proc.pid)
        (tmp_path / "bin" / "pgrep").chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (tmp_path / "bin", os.environ["PATH"]))
        out = subprocess.run(["sh", "-c", script], stdout=subprocess.PIPE, check=True, universal_newlines=True,
                             env=env).stdout
        assert dict(line.split("=", 1) for line in out.splitlines())["exe"] == str(java)
    finally:
        proc.kill()
        proc.wait()


def test_the_record_of_a_start_is_that_jvms_and_leaves_out_the_files_written_since(tmp_path):
    """config_snapshot.yml: the files the JVM read (not one written after its start), and that JVM (pid,
    start), for which apply_config trusts the record only."""
    record = task("config_snapshot.yml", "Record the config Cassandra started with")
    conf = tmp_path / "conf"
    conf.mkdir()
    for name in ("cassandra.yaml", "jvm17-server.options", ".hidden"):
        (conf / name).write_text(name)
    (tmp_path / "unit").write_text("[Unit]\n")
    out = tmp_path / "record.json"
    argv = [sys.executable, "-c", record["vars"]["_record"], str(conf), str(tmp_path / "unit"), str(out)]
    # a process taken for Cassandra (a java, its main class on its command line), started after the files;
    # one that is not a java (a sudo, a grep) with that argument too is not it
    java = tmp_path / "java"
    java.symlink_to(sys.executable)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "org.apache.cassandra.service.CassandraDaemon"])
    time.sleep(0.05)
    jvm = subprocess.Popen([str(java), "-c", "import time; time.sleep(60)", "org.apache.cassandra.service.CassandraDaemon"])
    try:
        time.sleep(1.2)
        (conf / "jvm17-server.options").write_text("edited after the start")
        subprocess.run(argv, check=True)
        got = json.loads(out.read_text())
        assert got["_jvm"]["pid"] == str(jvm.pid) and abs(got["_jvm"]["start"] - time.time()) < 30
        assert sorted(k for k in got if k != "_jvm") == [str(conf / "cassandra.yaml"), str(tmp_path / "unit")]
        assert got[str(conf / "cassandra.yaml")] == hashlib.sha1(b"cassandra.yaml").hexdigest()
        assert oct(os.stat(str(out)).st_mode & 0o777) == "0o600"  # root only (the JMX password file's checksum)
        # never a file planted at a temporary path (the directory is Cassandra's account's)
        assert not [p for p in os.listdir(str(tmp_path)) if p.startswith(".ansible_config_at_start.")]
    finally:
        for p in (jvm, other):
            p.kill()
            p.wait()
    # no JVM found: every file, no _jvm (apply_config then trusts it for nothing)
    subprocess.run(argv, check=True)
    got = json.loads(out.read_text())
    assert "_jvm" not in got and sorted(got) == sorted([str(conf / "cassandra.yaml"), str(conf / "jvm17-server.options"),
                                                        str(tmp_path / "unit")])
