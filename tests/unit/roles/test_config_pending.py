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


with open(os.path.join(ROLE, "..", "cassandra_service", "files", "config_record.py")) as f:
    RECORD = f.read()
# config_record.py made to see in /proc only the processes ONLY_PIDS names (its environment)
RECORD_ONLY_PIDS = RECORD.replace("\n__metaclass__ = type\n", "\n__metaclass__ = type\nimport os\n_listdir = os.listdir\n"
                                  "os.listdir = lambda p='.': [x for x in _listdir(p) if p != '/proc' or not x.isdigit()"
                                  " or x in os.environ['ONLY_PIDS'].split()]\n", 1)


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
        "cassandra_pending_unit_files": [],
        "cassandra_jvm": JVM if jvm is None else jvm,
    }
    for name in ("_record", "_record_current", "_started", "_settings", "_units", "_files", "_newer"):
        variables[name] = trust_as_template(NOTE["vars"][name])
    return render(NOTE["vars"]["_changed"], variables), render(NOTE["vars"]["_unknown"], variables)


def test_only_the_files_cassandra_config_writes(tmp_path):
    patterns = yaml.safe_load(open(os.path.join(ROLE, "vars", "main.yml")))["_cassandra_service_config_files"]
    read = task("config_pending.yml", "Checksum the config files and the unit files, read the checksums recorded at the last"
                                      " start")
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
    units = json.dumps([str(tmp_path / "unit"), str(tmp_path / "drop-in.conf")])
    argv = [sys.executable, "-c", read["vars"]["_read"], str(conf), str(tmp_path / "record.json"), units] + patterns
    found = json.loads(subprocess.run(argv, stdout=subprocess.PIPE, check=True).stdout)
    assert sorted(os.path.basename(f["path"]) for f in found["files"]) == sorted(n for n in names if n != "jvm8-server.options")
    for f in found["files"]:  # as the find module (get_checksum) gives them
        assert f["checksum"] == hashlib.sha1(os.path.basename(f["path"]).encode()).hexdigest()
        assert f["ctime"] == os.lstat(f["path"]).st_ctime
    unit, dropin = found["units"]
    assert unit["exists"] is True and unit["path"] == str(tmp_path / "unit")
    assert unit["checksum"] == hashlib.sha1(b"[Unit]\n").hexdigest()
    assert dropin == {"exists": False, "path": str(tmp_path / "drop-in.conf")}
    assert base64.b64decode(found["record"]) == b"{}"
    argv[4:6] = [str(tmp_path / "no-record"), json.dumps([str(tmp_path / "no-unit")])]
    found = json.loads(subprocess.run(argv, stdout=subprocess.PIPE, check=True).stdout)
    assert found["units"] == [{"exists": False, "path": str(tmp_path / "no-unit")}] and found["record"] is None


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


AT_START = "/etc/cassandra/conf/jmx_prometheus.at_start"


@pytest.mark.parametrize("recorded", [("41", 100.5), None])
def test_the_exporter_record_changed_under_a_record_of_another_jvm(recorded):
    # httpServer or openTelemetry changed (by site.yml) since the JVM started, the record not this JVM's: what its
    # agent read is not known, listed as the other files are
    files = [(YAML, "y1", 90.0), (AT_START, "a2", 100.7)]
    assert pending({YAML: "y1", AT_START: "a1"}, files, recorded=recorded) == ([], ["jmx_prometheus.at_start"])
    # not changed since the start: nothing
    assert pending({YAML: "y1", AT_START: "a1"}, [(YAML, "y1", 90.0), (AT_START, "a1", 99.0)], recorded=recorded) \
        == ([], [])


def test_the_exporter_record_first_written_is_not_listed():
    # no earlier record has it (a node imported, the exporter set up since): its first write, nothing to say
    files = [(YAML, "y1", 90.0), (AT_START, "a1", 100.7)]
    assert pending(None, files) == ([], [])
    assert pending({YAML: "y1"}, files, recorded=("41", 100.5)) == ([], [])


def test_seed_leaves_out_the_files_written_since_the_start():
    rec = task("config_seed.yml", "Record the ones not written since it started")
    variables = {
        "cassandra_seed_files": {"files": [{"path": YAML, "checksum": "y1", "ctime": 90.0},
                                           {"path": ENV, "checksum": "e1", "ctime": 110.0}]},
        "cassandra_seed_units": {"results": [{"stat": {"exists": True, "path": UNIT, "checksum": "u1", "ctime": 95.0}},
                                             {"stat": {"exists": False}}]},
        "cassandra_jvm": {"pid": "42", "start": "100.00"},
    }
    for name in ("_files", "_same"):
        variables[name] = trust_as_template(rec["vars"][name])
    jvm = {"pid": "42", "start": 100.0}  # the JVM it records, as config_snapshot.yml does
    assert json.loads(render(rec["ansible.builtin.copy"]["content"], variables)) == {YAML: "y1", UNIT: "u1", "_jvm": jvm}
    # cassandra.yaml left as it is by cassandra_config, as compared (y1): the sha1 of its rendering recorded with it
    same = dict(variables, cassandra_conf_dir="/etc/cassandra/conf/",
                _cassandra_config_same_as={"cassandra.yaml": {"disk": "y1", "same": "r1"}})
    assert json.loads(render(rec["ansible.builtin.copy"]["content"], same)) == {
        YAML: "y1", UNIT: "u1", "_jvm": jvm, "_same_settings": {YAML: "r1"}}
    same["_cassandra_config_same_as"] = {"cassandra.yaml": {"disk": "y0", "same": "r1"}}  # changed since the compare
    assert "_same_settings" not in json.loads(render(rec["ansible.builtin.copy"]["content"], same))
    # the unit written since the start: left out too
    variables["cassandra_seed_units"]["results"][0]["stat"]["ctime"] = 105.0
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
    # (and before its drop-in changes or goes: package_script)
    assert unit[seed]["when"].startswith("cassandra_service_unit_planned is changed or cassandra_service_dropin_planned"
                                         " is changed or cassandra_service_dropin_found.stat.exists")


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
    # (a copied interpreter finds its standard library through PYTHONHOME: a relocatable build does not otherwise)
    proc = subprocess.Popen([str(java), "-c", "import time; time.sleep(30)", "org.apache.cassandra.service.CassandraDaemon"],
                            env=dict(os.environ, PYTHONHOME=sys.base_prefix))
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


def test_import_does_not_read_a_node_whose_java_was_removed():
    # its Java cannot run any more: what it runs is unknown, the node is not read (the import fails, strict)
    playbook = os.path.join(ROLE, "..", "..", "playbooks", "import_cluster.yml")
    with open(playbook) as f:
        plays = yaml.safe_load(f)
    found = dict((t.get("name"), t) for p in plays for t in p.get("tasks", []))
    note = found["Note a running Java that cannot be read"]
    assert "import_cluster_java | default('') == ''" in note["when"]
    assert "rolling_restart" in render(note["ansible.builtin.set_fact"]["import_cluster_error"],
                                       {"import_cluster_java_bin": "/usr/lib/jvm/java-17-openjdk-17.0.9/bin/java"})
    assert "import_cluster_error is not defined" in found["Read this node's config"]["when"]


def test_import_lists_the_config_files_written_since_the_start():
    # import_cluster reports them (the node may not run what they say), with the start jvm_started.yml reads
    playbook = os.path.join(ROLE, "..", "..", "playbooks", "import_cluster.yml")
    with open(playbook) as f:
        plays = yaml.safe_load(f)
    found = dict((t.get("name"), t) for p in plays for t in p.get("tasks", []))
    assert found["Find when the running Cassandra started"]["ansible.builtin.include_role"]["tasks_from"] == "jvm_started.yml"
    read = found["Read the config files, when they were written, the JVM's arguments, the repository files and the unit"]
    assert "[['newer', 'find', [[import_cluster_conf_dir, _cassandra_service_config_files]]]]" in read["vars"]["_wanted"]
    note = found["Note the ones written since it started"]["ansible.builtin.set_fact"]["import_cluster_newer_files"]
    files = [{"path": "/etc/cassandra/conf/%s" % n, "ctime": m}
             for n, m in [("cassandra.yaml", 90.0), ("jvm17-server.options", 100.6), ("cassandra-env.sh", 100.4)]]
    assert render(note, {"import_cluster_conf_files": {"files": files}, "cassandra_jvm": {"start": "100.50"}}) == [
        "jvm17-server.options"]


def test_the_record_of_a_start_is_that_jvms_and_leaves_out_the_files_written_since(tmp_path):
    """config_snapshot.yml: the files the JVM read (not one written after its start), and that JVM (pid,
    start), for which apply_config trusts the record only."""
    record = task("config_snapshot.yml", "Record the config Cassandra started with")["ansible.builtin.command"]["argv"]
    assert record[3:] == ["start", "{{ _cassandra_service_config_snapshot }}", "{{ _cassandra_service_conf_dir }}",
                          "--same={{ _cassandra_config_same_as | default({}) | to_json }}",
                          "/etc/systemd/system/cassandra.service", "{{ _cassandra_service_environment_dropin }}"]
    conf = tmp_path / "conf"
    conf.mkdir()
    for name in ("cassandra.yaml", "jvm17-server.options", ".hidden"):
        (conf / name).write_text(name)
    (tmp_path / "unit").write_text("[Unit]\n")
    out = tmp_path / "record.json"
    assert RECORD_ONLY_PIDS != RECORD
    # (the processes of this test only: another Cassandra JVM on a shared host, e.g. a test run next to this one,
    # would be taken first)
    argv = [sys.executable, "-c", RECORD_ONLY_PIDS, "start", str(out), str(conf), str(tmp_path / "unit"),
            str(tmp_path / "none.conf")]
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
        subprocess.run(argv, check=True, env=dict(os.environ, ONLY_PIDS="%d %d" % (jvm.pid, other.pid)))
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
    subprocess.run(argv, check=True, env=dict(os.environ, ONLY_PIDS=""))
    got = json.loads(out.read_text())
    assert "_jvm" not in got and sorted(got) == sorted([str(conf / "cassandra.yaml"), str(conf / "jvm17-server.options"),
                                                        str(tmp_path / "unit")])


def update(tmp_path, record, request, env=None):
    path = tmp_path / "record.json"
    if record is not None:
        path.write_text(json.dumps(record))
    elif path.exists():
        path.unlink()
    out = subprocess.run([sys.executable, "-c", RECORD, "update", str(path), json.dumps(request)], check=True,
                         stdout=subprocess.PIPE, universal_newlines=True, env=env).stdout.strip()
    return out, (json.loads(path.read_text()) if path.exists() else None)


def test_files_recorded_as_the_running_cassandras_only_as_planned(tmp_path):
    # one helper for every update of the record: that JVM's record, the files on disk as
    # planned (a path under ~ or $VAR as the record keys it), written whole, root only
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "jvm-server.options").write_text("new")
    new = hashlib.sha1(b"new").hexdigest()
    jvm = {"pid": "42", "start": 100.0}
    env = dict(os.environ, HOME=str(tmp_path))
    request = {"jvm": {"pid": "42", "start": "100.5"}, "files": {"~/conf/jvm-server.options": new}}
    out, record = update(tmp_path, {"_jvm": jvm, str(conf / "jvm-server.options"): "old"}, request, env)
    assert out == "recorded" and record == {"_jvm": jvm, str(conf / "jvm-server.options"): new}
    assert oct(os.stat(str(tmp_path / "record.json")).st_mode & 0o777) == "0o600"
    for record, request, why in [
            (None, request, "no record"),
            ({"_jvm": {"pid": "41", "start": 100.0}}, request, "the record of another JVM"),  # restarted since
            ({}, dict(request, jvm=None), "the record of another JVM"),  # a record of no JVM
            ({"_jvm": jvm}, dict(request, files={"~/conf/jvm-server.options": "other"}), "written otherwise"),
            ({"_jvm": jvm}, dict(request, files={"~/conf/gone": None}), "written otherwise"),  # (missing)
            ({"_jvm": jvm, "/u": "a"}, dict(request, before={"/u": "b"}), "started with other files")]:
        assert update(tmp_path, record, request, env)[0] == why
    assert not [p for p in os.listdir(str(tmp_path)) if p.startswith(".ansible_config_at_start.")]


def test_every_update_of_the_record_goes_through_the_one_helper():
    # the record rewritten in one place: config_record.py, for the start and the updates
    for name in ("config_snapshot.yml", "config_record_update.yml"):
        argv = tasks(name)[0]["ansible.builtin.command"]["argv"]
        assert argv[2] == "{{ lookup('ansible.builtin.file', 'config_record.py') }}"
    assert tasks("unit_no_restart.yml")[0]["ansible.builtin.include_tasks"] == "config_record_update.yml"
    proven = task("action_apply_config.yml", "Record them when they are on disk as planned and that Cassandra still runs")
    assert proven["ansible.builtin.include_role"]["tasks_from"] == "config_record_update.yml"


@pytest.mark.parametrize("settings, expected", [("y2", []), ("y3", ["cassandra.yaml"]), (None, ["cassandra.yaml"])])
def test_a_file_with_the_settings_its_start_read_is_not_pending(settings, expected):
    # the start read y1, a file cassandra_config left as it is whose settings the role renders as y2: y2 on disk
    # (written back by apply_config) holds the settings that start read
    record = {YAML: "y1"}
    if settings:
        record["_same_settings"] = {YAML: settings}
    assert pending(record, [(YAML, "y2", 999)]) == (expected, [])


def test_the_record_of_a_start_has_the_rendering_of_a_file_left_as_it_is(tmp_path):
    record = task("config_snapshot.yml", "Record the config Cassandra started with")["ansible.builtin.command"]["argv"]
    assert record[6] == "--same={{ _cassandra_config_same_as | default({}) | to_json }}"
    conf = tmp_path / "conf"
    conf.mkdir()
    for name in ("cassandra.yaml", "jvm-server.options", "logback.xml"):
        (conf / name).write_text(name)
    out = tmp_path / "record.json"
    same = {"cassandra.yaml": {"disk": hashlib.sha1(b"cassandra.yaml").hexdigest(), "same": "r1"},
            "jvm-server.options": {"disk": "another file", "same": "r2"},  # (changed since the compare: none)
            "gone.xml": {"disk": "x", "same": "r3"}}
    argv = [sys.executable, "-c", RECORD_ONLY_PIDS, "start", str(out), str(conf), "--same=" + json.dumps(same),
            str(tmp_path / "unit")]
    subprocess.run(argv, check=True, env=dict(os.environ, ONLY_PIDS=""))
    got = json.loads(out.read_text())
    assert got["_same_settings"] == {str(conf / "cassandra.yaml"): "r1"}
    assert sorted(k for k in got if not k.startswith("_")) == [str(conf / n) for n in
                                                               ("cassandra.yaml", "jvm-server.options", "logback.xml")]
    # none to record: no key (as before)
    subprocess.run(argv[:6] + ["--same={}", str(tmp_path / "unit")], check=True, env=dict(os.environ, ONLY_PIDS=""))
    assert "_same_settings" not in json.loads(out.read_text())


def test_an_update_of_a_file_keeps_its_rendering_only_when_given(tmp_path):
    jvm = {"pid": "42", "start": 100.0}
    (tmp_path / "a").write_text("a2")
    (tmp_path / "b").write_text("b")
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    record = {"_jvm": jvm, a: "a1", b: "b", "_same_settings": {a: "ra", b: "rb"}}
    request = {"jvm": {"pid": "42", "start": "100"}, "files": {a: None}}
    out, got = update(tmp_path, record, request)
    assert out == "recorded" and got["_same_settings"] == {b: "rb"}  # a recorded anew: its old rendering dropped
    out, got = update(tmp_path, record, dict(request, same={a: "ra2"}))
    assert out == "recorded" and got["_same_settings"] == {a: "ra2", b: "rb"}
    out, got = update(tmp_path, dict(record, _same_settings={a: "ra"}), request)
    assert "_same_settings" not in got


def test_live_seeds_recorded_over_a_file_written_back_with_its_start_s_settings():
    # the record has the file the start read (y1) and its rendering (r1), cassandra.yaml written back as r1:
    # the seeds reloaded live are recorded too, else "restart pending" came back
    rec = task("seeds_apply.yml", "Record the live seeds in it")
    when = "{{ %s }}" % rec["when"][-1].strip()
    path = "/etc/cassandra/conf/cassandra.yaml"
    started = {path: "y1", "_same_settings": {path: "r1"}}
    for before, recorded in (("y1", True), ("r1", True), ("other", False)):
        assert render(when, {"_seeds_started": started, "_seeds_file": path,
                             "cassandra_seeds_file_before": {"stat": {"checksum": before}}}) is recorded
