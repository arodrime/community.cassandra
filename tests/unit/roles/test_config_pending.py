from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config restarts a node only for a config change Cassandra has not
# picked up: the files cassandra_config writes, compared by content with the
# ones it started with. A node with no record of them (imported), or whose
# record is another JVM's (restarted by hand since), restarts for nothing;
# files written since its JVM started are only listed.

import base64
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_operation")


def tasks(name):
    """A task file, from the role that holds it (the record of the config at start: config and service)."""
    path = next(p for p in (os.path.join(ROLE, "..", r, "tasks", name)
                            for r in ("cassandra_operation", "cassandra_service", "cassandra_config")) if os.path.exists(p))
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


NOTE = task("config_pending.yml", "Note whether a restart is pending")
YAML = "/etc/cassandra/conf/cassandra.yaml"
ENV = "/etc/cassandra/conf/cassandra-env.sh"
UNIT = "/etc/systemd/system/cassandra.service"


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


JVM = {"pid": "42", "start": "100.50", "exe": "/usr/bin/java"}  # the running JVM, as jvm_started.yml finds it


def pending(snapshot, files, jvm=None, recorded=("42", 100.5)):
    """(the files pending, the ones unknown) for a record (snapshot, written for the JVM recorded: pid, start;
    None: an older record without it) and the files found, with jvm running (JVM by default, {} when none)."""
    if snapshot is not None and recorded is not None:
        snapshot = dict(snapshot, _jvm={"pid": recorded[0], "start": recorded[1]})
    variables = {
        "cassandra_pending_files": {"files": [{"path": p, "checksum": c, "ctime": m} for p, c, m in files]},
        "cassandra_pending_snapshot": ({"content": base64.b64encode(json.dumps(snapshot).encode()).decode()}
                                       if snapshot is not None else {}),
        "cassandra_pending_unit_file": {"stat": {"exists": False}},
        "cassandra_jvm": JVM if jvm is None else jvm,
    }
    for name in ("_record", "_record_current", "_started", "_files", "_newer"):
        variables[name] = trust_as_template(NOTE["vars"][name])
    return render(NOTE["vars"]["_changed"], variables), render(NOTE["vars"]["_unknown"], variables)


def test_only_the_files_cassandra_config_writes(tmp_path):
    patterns = yaml.safe_load(open(os.path.join(ROLE, "defaults", "main.yml")))["_cassandra_service_config_files"]
    read = task("config_pending.yml", "Checksum the config files and the unit, read the checksums recorded at the last start")
    assert "+ _cassandra_service_config_files" in read["ansible.builtin.command"]["argv"]
    names = ["cassandra.yaml", "cassandra-env.sh", "jvm-server.options", "jvm11-server.options", "jvm17-server.options",
             "jvm8-server.options", "cassandra-rackdc.properties", "logback.xml",
             "jmxremote.access"]  # in the conf dir on Debian: read at start too
    # a keystore touched, an editor backup, the topology file: not the config Cassandra restarts for
    others = [".keystore", "cassandra.yaml~", "cassandra.yaml.12345.2026-10-06@10:00:00~", "cassandra-topology.properties",
              "commitlog_archiving.properties", "logback-tools.xml",
              "jmxremote.password"]  # read at each JMX login: a new one applies at once
    conf = tmp_path / "conf"
    conf.mkdir()
    for name in names + others:
        (conf / name).write_text(name)
    (conf / "jvm8-server.options").unlink()
    (conf / "jvm8-server.options").symlink_to(conf / "logback.xml")  # a link: not a file of find's
    (tmp_path / "unit").write_text("[Unit]\n")
    (tmp_path / "record.json").write_text("{}")
    argv = [sys.executable, "-c", read["vars"]["_read"], str(conf), str(tmp_path / "unit"), str(tmp_path / "record.json")] + patterns
    found = json.loads(subprocess.run(argv, stdout=subprocess.PIPE, check=True).stdout)
    assert sorted(os.path.basename(f["path"]) for f in found["files"]) == sorted(n for n in names if n != "jvm8-server.options")
    for f in found["files"]:  # as the find module (get_checksum) gives them
        assert f["checksum"] == hashlib.sha1(os.path.basename(f["path"]).encode()).hexdigest()
        assert f["ctime"] == os.lstat(f["path"]).st_ctime
    assert found["unit"]["exists"] is True and found["unit"]["path"] == str(tmp_path / "unit")
    assert found["unit"]["checksum"] == hashlib.sha1(b"[Unit]\n").hexdigest()
    assert base64.b64decode(found["record"]) == b"{}"
    argv[4:6] = [str(tmp_path / "no-unit"), str(tmp_path / "no-record")]
    found = json.loads(subprocess.run(argv, stdout=subprocess.PIPE, check=True).stdout)
    assert found["unit"] == {"exists": False} and found["record"] is None


@pytest.mark.parametrize("checksum, expected", [("y1", []), ("y2", ["cassandra.yaml"])])
def test_recorded_compares_contents(checksum, expected):
    # a file rewritten with the same content after the start is not pending
    assert pending({YAML: "y1"}, [(YAML, checksum, 999)]) == (expected, [])


def test_recorded_without_the_file_is_pending():
    assert pending({ENV: "e1"}, [(YAML, "y1", 1)]) == (["cassandra.yaml"], [])


def test_no_record_is_not_pending():
    # imported: written after the JVM started, its content then is unknown: listed, not restarted
    assert pending(None, [(YAML, "y1", 100.2), (ENV, "e1", 100.7)]) == ([], ["cassandra-env.sh"])


def test_no_record_and_no_jvm():
    assert pending(None, [(YAML, "y1", 100.2)], {}) == ([], [])


@pytest.mark.parametrize("recorded", [("41", 100.5), ("42", 60.0), None])
def test_a_record_of_another_jvm_is_not_trusted(recorded):
    # restarted by hand since the record (another pid, the same pid after a reboot, or an older record that
    # does not say): the running Cassandra read the files as they were then, which the record does not say:
    # as no record, a file written since its JVM started only listed
    files = [(YAML, "y2", 100.2), (ENV, "e2", 100.7)]
    assert pending({YAML: "y1", ENV: "e1"}, files, recorded=recorded) == ([], ["cassandra-env.sh"])
    assert pending({YAML: "y1", ENV: "e1"}, files) == (["cassandra-env.sh", "cassandra.yaml"], [])  # the same JVM
    assert pending({YAML: "y1", ENV: "e1"}, files, recorded=("42", 101.9)) == (["cassandra-env.sh", "cassandra.yaml"], [])
    # the JVM is always read: the record is trusted for it only
    read = task("config_pending.yml", "Read when the running Cassandra started")
    assert read["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_config", "tasks_from": "jvm_started.yml"}
    assert "when" not in read


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
    main = load_tasks(os.path.join(ROLE, "..", "cassandra_config", "tasks", "main.yml"))
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
