from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config reads a node in a few runs of files/config_read.py, its
# results shaped as the stat, slurp, service_facts and package_facts tasks it
# replaces registered them.

import base64
import grp
import importlib.util
import os
import pwd

import pytest

from ansible.modules.stat import format_output

from ansible_collections.community.cassandra.tests.unit.roles.config_reader import ROLE, config_read

spec = importlib.util.spec_from_file_location("config_read", os.path.join(ROLE, "files", "config_read.py"))
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def stat_module(path, follow):
    """What ansible.builtin.stat returns (get_checksum, get_mime, get_attributes: false)."""
    st = os.stat(path) if follow else os.lstat(path)
    out = format_output(None, path, st)
    for name, mode in (("readable", os.R_OK), ("writeable", os.W_OK), ("executable", os.X_OK)):
        out[name] = os.access(path, mode)
    if out["islnk"]:
        out["lnk_source"] = os.path.realpath(path)
        out["lnk_target"] = os.readlink(path)
    out["pw_name"] = pwd.getpwuid(st.st_uid).pw_name
    out["gr_name"] = grp.getgrgid(st.st_gid).gr_name
    return out


def node(tmp_path, **request):
    base = {"step": "node", "dirs": [], "yaml": str(tmp_path / "cassandra.yaml"), "conf_target": "", "keep": [], "files": [],
            "dir_stats": []}
    base.update(request)
    return config_read(base)


def test_stats_as_the_stat_module_gives_them(tmp_path):
    (tmp_path / "cassandra.yaml").write_text("cluster_name: x\n")
    (tmp_path / "cassandra.yaml").chmod(0o640)
    (tmp_path / "conf").symlink_to(tmp_path)
    files = [["cassandra.yaml", str(tmp_path / "conf" / "cassandra.yaml")], ["logback.xml", str(tmp_path / "logback.xml")]]
    answer = node(tmp_path, files=files, keep=[["cassandra.yaml", str(tmp_path / "cassandra.yaml")]],
                  conf_target=str(tmp_path / "conf"), dirs=[str(tmp_path), str(tmp_path / "none")])
    wanted = stat_module(str(tmp_path / "conf" / "cassandra.yaml"), True)
    found = answer["files"][0]
    assert found["cassandra_config_file"] == "cassandra.yaml"
    del wanted["atime"]  # (the reads in between may move it)
    assert dict((k, found["stat"][k]) for k in wanted) == wanted
    assert answer["files"][1] == {"cassandra_config_file": "logback.xml", "stat": {"exists": False}}
    assert answer["keep"][0]["item"] == "cassandra.yaml" and answer["keep"][0]["stat"]["exists"] is True
    # /etc/cassandra/conf: the link and where it points (lstat)
    assert answer["conf_target"]["stat"]["islnk"] is True and answer["conf_target"]["stat"]["lnk_source"] == str(tmp_path)
    assert [r["stat"]["exists"] for r in answer["dirs"]] == [True, False] and answer["errors"] == []
    # the live cassandra.yaml as slurp gives it
    assert answer["yaml_stat"]["exists"] is True and base64.b64decode(answer["yaml"]) == b"cluster_name: x\n"
    assert answer["pyyaml"] is True


def test_a_stat_that_fails_stops_the_run(tmp_path):
    (tmp_path / "file").write_text("")
    answer = node(tmp_path, dirs=[str(tmp_path / "file" / "data")])
    assert answer["errors"] == ["%s: Not a directory" % (tmp_path / "file" / "data")]


def test_a_live_cassandra_yaml_not_readable(tmp_path):
    (tmp_path / "cassandra.yaml").symlink_to(tmp_path / "gone")  # there, but not readable: no content
    answer = node(tmp_path)
    assert answer["yaml_stat"]["exists"] is True and answer["yaml"] is None


@pytest.mark.parametrize("output, state", [
    ("  UNIT              LOAD   ACTIVE SUB     DESCRIPTION\n  cassandra.service loaded active running Cassandra\n", "running"),
    ("cassandra.service loaded activating auto-restart Cassandra\n", "stopped"),
    ("● cassandra.service loaded failed failed Cassandra\n", "stopped"),
    ("0 loaded units listed.\n", ""),
])
def test_the_cassandra_unit_as_service_facts_reads_it(monkeypatch, output, state):
    monkeypatch.setattr(reader, "run", lambda argv, stdin=None: (0, output, ""))
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/1/comm":
            from io import StringIO
            return StringIO("systemd\n")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr("builtins.open", fake_open)
    assert reader.cassandra_state() == state


@pytest.mark.parametrize("rpm, dpkg, version", [
    ((0, "5.0.4\n", ""), None, "5.0.4"),
    ((1, "package cassandra is not installed\n", ""), (0, "installed 4.1.9\n", ""), "4.1.9"),
    ((None, "", "no rpm"), (0, "config-files 4.1.9\n", ""), ""),  # removed, its config kept
    ((None, "", "no rpm"), (1, "", "no packages found"), ""),
])
def test_the_package_version(monkeypatch, rpm, dpkg, version):
    monkeypatch.setattr(reader, "run", lambda argv, stdin=None: rpm if argv[0] == "rpm" else dpkg)
    assert reader.package_version() == version


def test_the_config_files_that_are_links(tmp_path):
    # the template task replaces a link with a file: such a file is written even with the same content
    (tmp_path / "stock.yaml").write_text("x: 1\n")
    (tmp_path / "cassandra.yaml").symlink_to(tmp_path / "stock.yaml")
    (tmp_path / "logback.xml").write_text("<a/>")
    answer = node(tmp_path, files=[["cassandra.yaml", str(tmp_path / "cassandra.yaml")], ["logback.xml", str(tmp_path / "logback.xml")]])
    assert answer["links"] == ["cassandra.yaml"]


def test_a_live_file_the_diff_cannot_read_is_written(tmp_path):
    # as the diff command that failed (rc 1, nothing on stdout): written again by the template task
    (tmp_path / "cassandra.yaml").mkdir()
    answer = config_read({"step": "compare", "conf_dir": str(tmp_path), "names": ["cassandra.yaml"],
                          "files": {"cassandra.yaml": "x: 1\n"}, "compare": True, "identity": None})
    assert answer["preview"] == [{"cassandra_config_file": "cassandra.yaml", "rc": 1, "stdout": ""}]
    assert answer["live"] == {"cassandra.yaml": None}


def test_an_error_is_said_without_the_files(tmp_path):
    # a step that fails: the last line of its traceback for the role to stop on (the task itself is no_log)
    answer = config_read({"step": "dirs", "system": ["/x\u0000y"], "tries": [], "foreign": [], "user": "root"})
    assert set(answer) == {"error"} and answer["error"].startswith("ValueError")
    # a new cassandra.yaml PyYAML cannot read (a date out of range): its error's name, not its text
    answer = config_read({"step": "compare", "conf_dir": str(tmp_path), "names": ["cassandra.yaml"],
                          "files": {"cassandra.yaml": "d: 2020-13-45\nkeystore_password: Secret-Value\n"}, "compare": False,
                          "identity": None})
    assert answer["yaml_error"] == "the new cassandra.yaml could not be read as YAML (ValueError)"


def test_the_account_as_a_number(tmp_path):
    # (ansible-core 2.19 keeps an int): tried and compared as its text
    (tmp_path / "data" / "ks").mkdir(parents=True)
    answer = config_read({"step": "dirs", "system": [], "tries": [str(tmp_path / "data")], "foreign": [str(tmp_path / "data")],
                          "user": os.getuid()})
    assert "error" not in answer and answer["foreign"] == [[]] and len(answer["tries"]) == 1
