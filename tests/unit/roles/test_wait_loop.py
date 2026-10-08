from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service wait_loop.yml, run by ansible-playbook on a local host with
# a check that ends the wait at its Nth run: exactly N checks, then the wait
# stops at once (what is left of the round: a few skipped tasks, never a
# burst of whole checks), also across rounds (the include nesting per round).

import os
import subprocess
import sys

import pytest

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))

PLAYBOOK = """
- hosts: all
  gather_facts: false
  vars:
    _cassandra_wait_size: %(size)d
  tasks:
    - name: Start
      ansible.builtin.set_fact:
        _status: going
        _checks: 0

    - name: Wait
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_service
        tasks_from: wait_loop.yml
      vars:
        _cassandra_wait_check: "%(check)s"
        _cassandra_wait_going: "{{ _status == 'going' }}"

    - name: Show
      ansible.builtin.debug:
        msg: "CHECKS={{ _checks }}"
"""

CHECK = """
- name: One check
  ansible.builtin.set_fact:
    _checks: "{{ _checks | int + 1 }}"

- name: Over at check %(stop)d
  ansible.builtin.set_fact:
    _status: done
  when: _checks | int >= %(stop)d
"""


def run(tmp_path, size, stop):
    check = tmp_path / "check.yml"
    check.write_text(CHECK % {"stop": stop})
    playbook = tmp_path / "wait.yml"
    playbook.write_text(PLAYBOOK % {"size": size, "check": str(check)})
    inventory = tmp_path / "hosts.ini"
    inventory.write_text("node1\n")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_DISPLAY_SKIPPED_HOSTS="1", ANSIBLE_STDOUT_CALLBACK="default")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", str(inventory),
            "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable, str(playbook)]
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=600, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


def after_the_end(out):
    """The task results printed after the check that ended the wait."""
    tail = out.rsplit("TASK [community.cassandra.cassandra_service : Over at check", 1)[1].split("TASK [Show]", 1)[0]
    lines = [line for line in tail.splitlines() if line.startswith(("ok:", "skipping:", "changed:", "included:"))]
    return lines[1:]  # the first one: that check's own result


@pytest.mark.parametrize("size, stop", [(3, 1), (3, 4), (3, 9), (3, 10), (3, 23), (2, 2)])
def test_stops_right_after_the_check_that_ends_it(tmp_path, size, stop):
    rc, out = run(tmp_path, size, stop)
    assert rc == 0, out
    assert "CHECKS=%d" % stop in out, out
    # the rest of its group and of its round, one skipped task each, and the round not started again
    rest = after_the_end(out)
    assert len(rest) <= 2 * size, "\n".join(rest)
    assert not [line for line in rest if line.startswith(("ok:", "changed:", "included:"))], "\n".join(rest)
    # no check skipped one task at a time (the old rounds skipped every task of every check left)
    assert "One check" not in "\n".join(out.rsplit("Over at check", 1)[1:])
