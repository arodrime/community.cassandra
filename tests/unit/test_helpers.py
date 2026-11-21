from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The shared test helpers: how they render, the playbook runner's guard and output dump.

import os

import pytest

from ansible_collections.community.cassandra.tests.unit import helpers
from ansible_collections.community.cassandra.tests.unit.helpers import (
    fake_nodetool, golden, golden_lines, path_with, render, render_as_given, render_with, ring_node, run_playbook, trusted, view)


def test_render_trusts_the_string_variables():
    assert render("{{ a }}", a="{{ b }}", b="x") == "x"
    assert render_with("{{ a }}-{{ c }}", {"a": "{{ b }}", "b": "x", "c": [2]}) == "x-[2]"
    # a nested string too, with trusted()
    assert render_as_given("{{ a.x }}", trusted({"a": {"x": "{{ b }}"}, "b": "y"})) == "y"


def test_ring_node_and_view():
    node = ring_node("10.0.0.1", "D", rack="r2", load="1 GiB")
    assert node == {"address": "10.0.0.1", "status": "D", "state": "N", "rack": "r2", "load": "1 GiB"}
    assert view("n1", node) == {"from": "n1", "result": {"cluster_status": {"dc1": {"nodes": [node]}}}}


def test_fake_nodetool_comes_first_on_the_path(tmp_path):
    path = fake_nodetool(tmp_path / "bin", "#!/bin/sh\necho fake\n")
    assert os.access(path, os.X_OK)
    assert path_with(tmp_path / "bin").startswith(str(tmp_path / "bin") + ":")


def test_a_playbook_run_needs_the_mark():
    with pytest.raises(AssertionError, match="marked @pytest.mark.playbook"):
        run_playbook(["--version"])


@pytest.mark.playbook
def test_the_output_is_dumped_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSANDRA_TEST_DUMP", str(tmp_path / "dump"))
    (tmp_path / "play.yml").write_text("- hosts: localhost\n  gather_facts: false\n  tasks:\n"
                                       "    - ansible.builtin.debug:\n        msg: hello\n")
    rc, out = run_playbook(["-i", "localhost,"] + helpers.LOCAL + [str(tmp_path / "play.yml")], cwd=tmp_path)
    assert rc == 0 and "hello" in out, out
    run_playbook(["-i", "localhost,"] + helpers.LOCAL + [str(tmp_path / "play.yml")], cwd=tmp_path)
    dumped = sorted(os.listdir(str(tmp_path / "dump")))
    assert len(dumped) == 2 and dumped[0].endswith("test_the_output_is_dumped_when_asked.txt"), dumped
    with open(str(tmp_path / "dump" / dumped[0])) as f:
        assert f.read() == out


def test_golden_reads_the_file_and_writes_it_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("CASSANDRA_GOLDEN_WRITE", raising=False)
    path = tmp_path / "out.txt"
    path.write_text("kept\ntext\n")
    assert golden(str(path), "new") == "kept\ntext"
    assert golden_lines(str(path), ["new"]) == ["kept", "text"]
    monkeypatch.setenv("CASSANDRA_GOLDEN_WRITE", "1")  # (pytest --regen)
    assert golden_lines(str(path), ["new", "lines"]) == ["new", "lines"]
    assert path.read_text() == "new\nlines\n"
