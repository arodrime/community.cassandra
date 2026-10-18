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

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

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
