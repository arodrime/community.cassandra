from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster reads the files of a node in one command (playbooks/files/import_read_files.py): each one as
# slurp, stat or find gives it.

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

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
SCRIPT = os.path.join(ROOT, "playbooks", "files", "import_read_files.py")


def read(*wanted):
    with open(SCRIPT, encoding="utf-8") as f:
        script = f.read()
    out = subprocess.run([sys.executable, "-c", script], input=json.dumps(wanted).encode(), stdout=subprocess.PIPE, check=True,
                         env=dict(os.environ, LC_ALL="C", LANG="C"))  # (paths read as UTF-8 whatever the locale)
    return json.loads(out.stdout)


def test_content_as_slurp(tmp_path):
    (tmp_path / "a.yaml").write_text("x: 1\n")
    (tmp_path / "bin").write_bytes(b"\x00\xff\xfe binary")
    out = read(["files", "content", [["a.yaml", str(tmp_path / "a.yaml")], ["bin", str(tmp_path / "bin")],
                                     ["missing", str(tmp_path / "missing")], ["dir", str(tmp_path)]]])["files"]
    assert out[0] == {"item": "a.yaml", "content": base64.b64encode(b"x: 1\n").decode(), "source": str(tmp_path / "a.yaml"),
                      "encoding": "base64"}
    assert base64.b64decode(out[1]["content"]) == b"\x00\xff\xfe binary"
    assert out[2] == {"item": "missing", "failed": True, "msg": "file not found: %s" % (tmp_path / "missing")}
    assert out[3] == {"item": "dir", "failed": True, "msg": "source is a directory and must be a file: %s" % tmp_path}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable(tmp_path):
    f = tmp_path / "secret"
    f.write_text("p")
    f.chmod(0)
    out = read(["files", "content", [["secret", str(f)]]], ["stat", "checksum", [["secret", str(f)]]])
    assert out["files"] == [{"item": "secret", "failed": True, "msg": "file is not readable: %s" % f}]
    assert "checksum" not in out["stat"][0]["stat"] and out["stat"][0]["stat"]["mode"] == "0000"


def test_stat_as_the_module(tmp_path):
    f = tmp_path / "f"
    f.write_text("data")
    f.chmod(0o640)
    (tmp_path / "link").symlink_to(f)
    (tmp_path / "d").mkdir(mode=0o750)
    paths = [["f", str(f)], ["link", str(tmp_path / "link")], ["d", str(tmp_path / "d")], ["none", str(tmp_path / "none")],
             ["under", str(f / "x")]]
    followed = dict((r["item"], r) for r in read(["s", "stat", paths])["s"])
    st = os.stat(str(f))
    assert followed["f"]["stat"]["mode"] == "0640" and followed["f"]["stat"]["isreg"] is True
    assert followed["f"]["stat"]["uid"] == st.st_uid and followed["f"]["stat"]["gid"] == st.st_gid
    assert followed["f"]["stat"]["path"] == str(f) and followed["f"]["stat"]["exists"] is True
    assert "pw_name" in followed["f"]["stat"] and "checksum" not in followed["f"]["stat"]
    assert followed["link"]["stat"]["mode"] == "0640" and followed["link"]["stat"]["islnk"] is False  # followed
    assert followed["d"]["stat"]["isdir"] is True and followed["d"]["stat"]["mode"] == "0750"
    assert followed["none"] == {"item": "none", "stat": {"exists": False}}
    assert followed["under"]["failed"] is True and "stat" not in followed["under"]  # a stat that fails: no stat
    summed = dict((r["item"], r) for r in read(["s", "checksum", paths])["s"])
    assert summed["f"]["stat"]["checksum"] == hashlib.sha1(b"data").hexdigest()
    assert summed["link"]["stat"]["islnk"] is True and "checksum" not in summed["link"]["stat"]  # not followed


def test_find_as_the_module(tmp_path):
    for name in ("cassandra.yaml", "jvm11-server.options", ".cassandra.yaml.swp", "other.txt"):
        (tmp_path / name).write_text("x")
    (tmp_path / "logback.xml").symlink_to(tmp_path / "other.txt")  # not a file (find's lstat)
    (tmp_path / "jvm-dir-server.options").mkdir()
    out = read(["newer", "find", [[str(tmp_path), ["cassandra.yaml", "jvm*-server.options", "logback.xml", ".*"]]]])
    found = out["newer"][0]["files"]
    assert sorted(os.path.basename(f["path"]) for f in found) == ["cassandra.yaml", "jvm11-server.options"]
    assert all(f["ctime"] == os.lstat(f["path"]).st_ctime for f in found)
    assert read(["newer", "find", [[str(tmp_path / "none"), ["*"]]]])["newer"][0]["files"] == []


with open(os.path.join(ROOT, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    READ = next(p for p in yaml.safe_load(f) if p["name"] == "Read every node")
FIRST = next(t for t in READ["tasks"] if t["name"].startswith("Read the config files, when they were written"))
USERS = next(t for t in READ["tasks"] if t["name"] == "Read the JMX users, the repository files' mode and their signing keys")


def trusted(value):
    """value with its strings trusted, nested ones too, as the playbook's YAML loader gives them."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, list):
        return [trusted(v) for v in value]
    if isinstance(value, dict):
        return dict((k, trusted(v)) for k, v in value.items())
    return value


def wanted(task, **variables):
    variables = dict(task["vars"], _files=READ["vars"]["_files"], _cassandra_service_config_files=["cassandra.yaml"],
                     **variables)
    variables = dict((k, trusted(v) if k in task["vars"] or k == "_files" else v) for k, v in variables.items())
    out = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(task["vars"]["_wanted"]))
    return dict((w, (how, items)) for w, how, items in json.loads(json.dumps(out)))  # (as the command gets it)


NODE = {"import_cluster_series": "41x", "import_cluster_conf_dir": "/etc/cassandra/conf", "cassandra_jvm": {"pid": "42", "start": "1"},
        "import_cluster_keep": {}, "import_cluster_unit": {"file": "/etc/systemd/system/cassandra.service"},
        "import_cluster_repo_files": ["/etc/yum.repos.d/cassandra-41x.repo"], "ansible_facts": {"os_family": "RedHat"}}


def test_what_the_first_command_reads():
    out = wanted(FIRST, **NODE)
    assert out["newer"] == ("find", [["/etc/cassandra/conf", ["cassandra.yaml"]]])
    assert out["config"][0] == "content" and out["config"][1][0] == ["cassandra.yaml", "/etc/cassandra/conf/cassandra.yaml"]
    assert out["cmdline"] == ("content", [["cmdline", "/proc/42/cmdline"]])
    assert out["repo"] == ("content", [["/etc/yum.repos.d/cassandra-41x.repo"] * 2])
    assert out["unit"] == ("content", [["cassandra.service", "/etc/systemd/system/cassandra.service"]])
    # the unit kept (not the role's): not read; no JVM pid: no arguments; Debian: the apt credentials too
    node = dict(NODE)
    node.update(import_cluster_keep={"cassandra_service_unit_manage": False}, cassandra_jvm={"pid": ""},
                ansible_facts={"os_family": "Debian"})
    out = wanted(FIRST, **node)
    assert "unit" not in out and "cmdline" not in out and "newer" not in out
    assert out["repo"][1][1] == ["/etc/apt/auth.conf.d/cassandra.conf"] * 2
    # a series the import does not read: only when its files were written
    node = dict(NODE)
    node.update(import_cluster_series="30x", import_cluster_repo_files=[])
    out = wanted(FIRST, **node)
    assert list(out) == ["newer"]


def test_what_the_second_command_reads():
    repo = "[x]\ngpgkey = file:///etc/pki/rpm-gpg/k.asc\n"
    slurp = {"results": [{"item": "/etc/yum.repos.d/cassandra-41x.repo", "content": base64.b64encode(repo.encode()).decode()}]}
    node = dict(NODE, import_cluster_jmx_files={"password": "/p", "access": ""}, import_cluster_repo_slurp=slurp)
    out = wanted(USERS, **node)
    assert out["jmx"] == ("content", [["jmxremote.password", "/p"], ["jmxremote.access", "/etc/cassandra/jmxremote.access"]])
    assert out["repo"] == ("checksum", [["/etc/yum.repos.d/cassandra-41x.repo"] * 2, ["/etc/pki/rpm-gpg/k.asc"] * 2])
    # the JVM's files unknown: none read
    node = dict(NODE, import_cluster_jmx_files={"unknown": "x"})
    node["import_cluster_repo_files"] = []
    out = wanted(USERS, **node)
    assert out == {}


def test_unicode_paths_and_one_file_failing_alone(tmp_path):
    (tmp_path / "\u00fcn\u00ef.yaml").write_text("x")
    out = read(["f", "content", [["u", str(tmp_path / "\u00fcn\u00ef.yaml")]]], ["s", "stat", [["u", str(tmp_path / "\u00fcn\u00ef.yaml")]]],
               ["n", "find", [[str(tmp_path), ["*.yaml"]]]])
    assert base64.b64decode(out["f"][0]["content"]) == b"x" and out["s"][0]["stat"]["exists"] is True
    assert [os.path.basename(f["path"]) for f in out["n"][0]["files"]] == ["\u00fcn\u00ef.yaml"]
    # an unexpected error: that file alone, not read (no content), the others read
    out = read(["f", "content", [["bad", None], ["ok", str(tmp_path / "\u00fcn\u00ef.yaml")]]])
    assert out["f"][0] == {"item": "bad", "failed": True, "msg": "unable to read None: TypeError"}
    assert "content" in out["f"][1]


@pytest.mark.parametrize("first, users, reason", [
    ({"rc": 0}, {"skipped": True}, None),
    ({"rc": 1, "stderr": "Traceback...\nValueError: x", "msg": "non-zero return code"}, {"rc": 0}, "ValueError: x"),
    ({"skipped": True}, {"rc": 2, "stderr": "", "msg": "No such file"}, "No such file"),
    ({"unreachable": True, "msg": "ssh: connect to host n1 port 22: timed out"}, {"skipped": True},
     "ssh: connect to host n1 port 22: timed out"),
])
def test_a_read_that_failed_is_the_node_s_error(first, users, reason):
    note = next(t for t in READ["tasks"] if t["name"] == "Note a node whose files could not be read")
    variables = dict(note["vars"], import_cluster_files=first, import_cluster_users_read=users)
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ %s }}" % note["when"])) is (reason is not None)
    if reason:
        assert templar.template(trust_as_template(note["ansible.builtin.set_fact"]["import_cluster_error"])) == (
            "its files could not be read: " + reason)
