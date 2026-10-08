from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service progress.yml, run by ansible-playbook on two local hosts:
# where the progress dir goes by default (next to the inventory, else the
# current dir), a relative cassandra_rolling_progress_dir, the refusals on an
# unusable dir, and no become on the controller even with an inventory
# ansible_become=true (a fake sudo records each use).

import os
import stat
import subprocess
import sys

import pytest

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))

PLAYBOOK = """
- hosts: all
  gather_facts: false
  vars:
    cassandra_service_node_action: restart
  tasks:
    - name: Progress
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_service
        tasks_from: progress.yml

    - name: Show
      ansible.builtin.debug:
        msg: "DIR={{ cassandra_progress_dir }} FILE={{ cassandra_progress_file }}"
      run_once: true
"""

FAKE_SUDO = """#!/bin/sh
echo used >> "$(dirname "$0")/sudo.log"
while [ $# -gt 0 ]; do case "$1" in -u) shift 2; break;; -*) shift;; *) break;; esac; done
exec "$@"
"""


def run(tmp_path, inventory, *extra, cwd=None):
    playbook = tmp_path / "progress.yml"
    playbook.write_text(PLAYBOOK)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", inventory,
            "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable, str(playbook)] + list(extra)
    result = subprocess.run(argv, env=env, cwd=str(cwd or tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=300, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


def inventory_in(directory, extra=""):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hosts.ini"
    path.write_text("[cassandra]\nnode1\nnode2\n" + extra)
    return str(path)


def progress_dir(output):
    return output.split("DIR=", 1)[1].split(" ", 1)[0]


def test_next_to_the_inventory(tmp_path):
    rc, out = run(tmp_path, inventory_in(tmp_path / "inv"), cwd=tmp_path / "inv" / "..")
    assert rc == 0, out
    assert progress_dir(out) == str(tmp_path / "inv" / ".cassandra_progress")
    assert len(list((tmp_path / "inv" / ".cassandra_progress").glob("cassandra-restart-*.done"))) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_current_dir_when_the_inventory_dir_is_not_writable(tmp_path):
    inventory = inventory_in(tmp_path / "inv")
    (tmp_path / "work").mkdir()
    os.chmod(str(tmp_path / "inv"), stat.S_IRUSR | stat.S_IXUSR)
    try:
        rc, out = run(tmp_path, inventory, cwd=tmp_path / "work")
    finally:
        os.chmod(str(tmp_path / "inv"), stat.S_IRWXU)
    assert rc == 0, out
    assert progress_dir(out) == str(tmp_path / "work" / ".cassandra_progress")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_an_existing_dir_in_a_read_only_inventory_dir_is_used(tmp_path):
    inventory = inventory_in(tmp_path / "inv")
    (tmp_path / "inv" / ".cassandra_progress").mkdir()
    (tmp_path / "work").mkdir()
    os.chmod(str(tmp_path / "inv"), stat.S_IRUSR | stat.S_IXUSR)
    try:
        rc, out = run(tmp_path, inventory, cwd=tmp_path / "work")
    finally:
        os.chmod(str(tmp_path / "inv"), stat.S_IRWXU)
    assert rc == 0, out
    assert progress_dir(out) == str(tmp_path / "inv" / ".cassandra_progress")


def test_current_dir_without_an_inventory_dir(tmp_path):
    (tmp_path / "None" / ".cassandra_progress").mkdir(parents=True)  # inventory_dir is 'None' there
    rc, out = run(tmp_path, "node1,node2,", "-e", "cassandra_hosts=all")
    assert rc == 0, out
    assert progress_dir(out) == str(tmp_path / ".cassandra_progress")


def test_relative_dir_is_from_the_current_dir(tmp_path):
    rc, out = run(tmp_path, inventory_in(tmp_path / "inv"), "-e", "cassandra_rolling_progress_dir=runs/p")
    assert rc == 0, out
    assert progress_dir(out) == str(tmp_path / "runs" / "p")


def test_resume_finds_the_file_again(tmp_path):
    inventory = inventory_in(tmp_path / "inv")
    rc, out = run(tmp_path, inventory)
    assert rc == 0, out
    first = out.split("FILE=", 1)[1].split('"', 1)[0]
    with open(first, "a") as f:
        f.write("node1\n")
    rc, out = run(tmp_path, inventory, "-e", "cassandra_rolling_resume=true")
    assert rc == 0, out
    assert "FILE=" + first in out
    assert "1 node(s) already done, skipped: node1" in out


def test_absolute_dir_as_given(tmp_path):
    rc, out = run(tmp_path, inventory_in(tmp_path / "inv"), "-e", "cassandra_rolling_progress_dir=%s" % (tmp_path / "abs"))
    assert rc == 0, out
    assert progress_dir(out) == str(tmp_path / "abs")


def test_a_file_in_the_way_is_refused(tmp_path):
    (tmp_path / "inv").mkdir()
    (tmp_path / "inv" / ".cassandra_progress").write_text("")
    rc, out = run(tmp_path, inventory_in(tmp_path / "inv"))
    assert rc != 0
    assert ".cassandra_progress is not a directory: remove it or set cassandra_rolling_progress_dir" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_dir_the_user_cannot_write_is_refused(tmp_path):
    (tmp_path / "inv" / ".cassandra_progress").mkdir(parents=True)
    os.chmod(str(tmp_path / "inv" / ".cassandra_progress"), stat.S_IRUSR | stat.S_IXUSR)
    try:
        rc, out = run(tmp_path, inventory_in(tmp_path / "inv"))
    finally:
        os.chmod(str(tmp_path / "inv" / ".cassandra_progress"), stat.S_IRWXU)
    assert rc != 0
    assert ".cassandra_progress is not writable by" in out
    assert "sudo chown -R" in out


def test_no_become_on_the_controller(tmp_path):
    sudo = tmp_path / "bin" / "sudo"
    sudo.parent.mkdir()
    sudo.write_text(FAKE_SUDO)
    sudo.chmod(0o755)
    inventory = inventory_in(tmp_path / "inv", "[all:vars]\nansible_become=true\nansible_become_exe=%s\n" % sudo)
    rc, out = run(tmp_path, inventory, "-b")
    assert rc == 0, out
    assert not (tmp_path / "bin" / "sudo.log").exists(), out
    # the fake sudo does work: a task on the nodes goes through it
    (tmp_path / "check.yml").write_text("- hosts: cassandra\n  gather_facts: false\n  tasks:\n"
                                        "    - ansible.builtin.command: 'true'\n")
    env = dict(os.environ, ANSIBLE_NOCOLOR="1")
    subprocess.run([sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", inventory, "-c", "local",
                    "-e", "ansible_python_interpreter=" + sys.executable, "-b", str(tmp_path / "check.yml")],
                   env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300, check=True)
    assert (tmp_path / "bin" / "sudo.log").exists()


def test_check_mode_creates_and_reads_no_file(tmp_path):
    # --check: the new progress file is not created, and reading it must not fail
    rc, out = run(tmp_path, inventory_in(tmp_path / "inv"), "--check")
    assert rc == 0, out
    assert list((tmp_path / "inv").glob(".cassandra_progress/*.done")) == []


def test_check_diff_says_nothing_of_the_progress_dir(tmp_path):
    # --check --diff: no dir made, no diff of it, no warning of a find in a dir that is not there
    rc, out = run(tmp_path, inventory_in(tmp_path / "inv"), "--check", "--diff", cwd=tmp_path)
    assert rc == 0, out
    assert not (tmp_path / "inv" / ".cassandra_progress").exists()
    assert "state: directory" not in out and "not a directory" not in out and "[WARNING]" not in out
