from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import os
import re
import subprocess

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_build

ROOT = cassandra_build.ROOT


def test_the_version_is_galaxy_yml_s():
    with open(os.path.join(ROOT, "galaxy.yml")) as f:
        assert cassandra_build.version() == str(yaml.safe_load(f)["version"])


def test_no_commit_in_the_tree():
    """COMMIT is written by tools/build.sh in the copy it builds, never committed (it could not name its own
    commit)."""
    assert cassandra_build.COMMIT == ""


def test_the_build_script_writes_the_commit_line():
    script = os.path.join(ROOT, "tools", "build.sh")
    if not os.path.exists(script):
        pytest.skip("tools/ not in this copy (left out of the build)")
    with open(script) as f:
        sed = re.search(r'sed -i "s/(.*?)/(.*?)/" "\$file"', f.read())
    with open(cassandra_build.__file__.replace(".pyc", ".py")) as f:
        text = f.read()
    pattern = sed.group(1).replace('\\"', '"')
    assert len(re.findall(pattern, text, re.M)) == 1


def test_an_installed_collection_reads_its_manifest(tmp_path):
    (tmp_path / "MANIFEST.json").write_text(json.dumps({"collection_info": {"version": "3.0.0"}}))
    (tmp_path / "galaxy.yml").write_text("version: 9.9.9\n")
    assert cassandra_build.version(str(tmp_path)) == "3.0.0"
    assert cassandra_build.label(str(tmp_path), built="1a2b3c4d5e6f") == "community.cassandra 3.0.0, commit 1a2b3c4d5e6f"


def test_only_what_is_known_is_said(tmp_path):
    # (owner, rc1) an installed collection not built by tools/build.sh: its version alone, never "commit unknown"
    assert cassandra_build.label(str(tmp_path), built="") == "community.cassandra"
    (tmp_path / "MANIFEST.json").write_text(json.dumps({"collection_info": {"version": "3.0.0-rc1"}}))
    assert cassandra_build.label(str(tmp_path), built="") == "community.cassandra 3.0.0-rc1"


def git(where, *args):
    return subprocess.check_output(("git", "-C", str(where), "-c", "user.name=t", "-c", "user.email=t@t") + args,
                                   stderr=subprocess.STDOUT).decode().strip()


@pytest.fixture
def repo(tmp_path):
    try:
        git(tmp_path, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("no git")
    (tmp_path / "galaxy.yml").write_text("version: 3.0.0-rc1\n")
    git(tmp_path, "add", "galaxy.yml")
    git(tmp_path, "commit", "-qm", "c")
    return tmp_path


def test_a_git_tree_of_the_collection_says_its_head(repo):
    head = git(repo, "rev-parse", "--short=12", "HEAD")
    assert cassandra_build.label(str(repo), built="") == "community.cassandra 3.0.0-rc1, commit %s" % head
    (repo / "galaxy.yml").write_text("version: 3.0.0-rc2\n")  # a tracked file changed: not that commit
    assert cassandra_build.commit(str(repo), built="") == head + "+"


def test_a_collection_inside_another_git_tree_says_no_commit(repo):
    """(a playbook repository holding the collection: its commit is not the collection's)"""
    inner = repo / "collections" / "community" / "cassandra"
    inner.mkdir(parents=True)
    assert cassandra_build.commit(str(inner), built="") == ""
