from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The stdout callback community.cassandra.ops, run by ansible-playbook on
# local hosts: only the marked messages, the failures in full, -v as default.

import os
import re
import select
import subprocess
import sys
import time

import pytest

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "..", ".."))

PLAYBOOK = """
- hosts: all
  gather_facts: false
  tasks:
    - name: A command
      ansible.builtin.command: echo noise
      changed_when: false

    - name: Skipped
      ansible.builtin.debug:
        msg: never
      when: false

    - name: Not marked
      ansible.builtin.debug:
        msg: plain debug output

    - name: Plan
      ansible.builtin.debug:
        msg: ["PLAN  op  my_cluster  2 steps", "  1. node1  dc1/rack_a  {{ arrow }} here"]
      run_once: true
      vars:
        cassandra_output: true
        arrow: "\\u2190"

    - name: One string
      ansible.builtin.debug:
        msg: "line one\\nline two"
      run_once: true
      vars:
        cassandra_output: true

    - name: Per item
      ansible.builtin.debug:
        msg: "item {{ item }}"
      loop: [a, b]
      when: inventory_hostname == 'node1'
      vars:
        cassandra_output: true

    - name: Ignored failure
      ansible.builtin.command: /bin/false
      ignore_errors: true

    - name: Ignored item failure
      ansible.builtin.command: "{{ item }}"
      loop: [/bin/false]
      ignore_errors: true

    - name: Waits
      ansible.builtin.command: /bin/true
      register: waited
      until: waited.attempts | default(0) > 1
      retries: 3
      delay: 0
      changed_when: false

    - name: Changes
      ansible.builtin.command: /bin/true
      notify: Handler

  handlers:
    - name: Handler
      ansible.builtin.debug:
        msg: "handler on {{ inventory_hostname }}"
      vars:
        cassandra_output: true
"""

FAILING = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Breaks on node2
      ansible.builtin.command: sh -c 'echo some output; echo the reason >&2; exit 3'
      when: inventory_hostname == 'node2'

    - name: Item breaks
      ansible.builtin.command: "{{ item }}"
      loop: [/bin/true, /bin/false]
      when: inventory_hostname == 'node1'

- hosts: all
  gather_facts: false
  tasks:
    - name: Verdict
      ansible.builtin.assert:
        that: false
        fail_msg: ["NOT HEALTHY  my_cluster  1 problem", "  ring: 3 members, inventory 2"]
      run_once: true
      vars:
        cassandra_output: true
"""


def run(tmp_path, playbook, *args, **env_extra):
    path = tmp_path / "playbook.yml"
    path.write_text(playbook)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_FORCE_COLOR="0")
    env.update(env_extra)
    env.pop("ANSIBLE_CONFIG", None)
    inventory = env.pop("INVENTORY", "node1,node2,")
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()", "-i", inventory, "-c", "local",
            "-e", "ansible_python_interpreter=" + sys.executable, str(path)] + list(args)
    proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          check=False, cwd=str(tmp_path), timeout=300)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def shown(output):
    """The lines, each rule as 4 of its characters (its width is the terminal's)."""
    return [re.sub(r"^([-=])\1{3,}$", lambda m: m.group(1) * 4, re.sub(r" -{4,}$", " ----", line))
            for line in output.splitlines()]


def test_only_the_marked_messages(tmp_path):
    rc, output = run(tmp_path, PLAYBOOK)
    assert rc == 0, output
    lines = shown(output)
    assert lines[:8] == [
        "====",  # a plan: a heavy rule above, a light one under its first line
        "PLAN  op  my_cluster  2 steps",
        "----",
        u"  1. node1  dc1/rack_a  \u2190 here",
        "line one",
        "line two",
        "item a",
        "item b",
    ]
    # the handlers: in whatever order the hosts end
    assert sorted(lines[8:]) == ["handler on node1", "handler on node2"]


def test_check_mode_prints_the_same_messages(tmp_path):
    rc, output = run(tmp_path, PLAYBOOK, "--check")
    assert rc == 0, output
    assert shown(output)[:4] == ["====", "PLAN  op  my_cluster  2 steps", "----", u"  1. node1  dc1/rack_a  \u2190 here"]
    assert "TASK [" not in output and "PLAY RECAP" not in output


def test_failures_in_full_and_the_verdict_as_is(tmp_path):
    rc, output = run(tmp_path, FAILING, INVENTORY="node1,node2,node3,")  # node3 goes on to the verdict
    assert rc != 0
    # the task name, the host, the message and stderr of the failed command
    assert "TASK [Breaks on node2]" in output
    assert "fatal: [node2]: FAILED!" in output and "the reason" in output and "some output" in output
    # a failed loop item: the item and its task
    assert "TASK [Item breaks]" in output and "failed: [node1] (item=/bin/false)" in output
    # the verdict: its own lines, nothing else of that task
    assert shown(output.rstrip())[-4:] == ["====", "NOT HEALTHY  my_cluster  1 problem", "  ring: 3 members, inventory 2", "===="]
    assert "TASK [Verdict]" not in output and "PLAY RECAP" not in output and "NO MORE HOSTS LEFT" not in output


def test_any_errors_fatal_stops_and_shows_the_failure(tmp_path):
    playbook = FAILING.replace("  gather_facts: false\n", "  gather_facts: false\n  any_errors_fatal: true\n", 1)
    rc, output = run(tmp_path, playbook)
    assert rc != 0
    assert "fatal: [node2]: FAILED!" in output and "the reason" in output
    assert "NOT HEALTHY" not in output  # the play stopped at the first failure


def test_unreachable_hosts(tmp_path):
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Ping, the play goes on without the host
      ansible.builtin.ping:
      ignore_unreachable: true

    - name: Ping again
      ansible.builtin.ping:
"""
    inventory = tmp_path / "hosts.ini"
    inventory.write_text("node1 ansible_connection=local\n"
                         "node3 ansible_connection=ssh ansible_host=127.0.0.1 ansible_port=9 ansible_ssh_timeout=3\n")
    rc, output = run(tmp_path, playbook, INVENTORY=str(inventory))
    assert rc != 0
    lines = output.splitlines()
    assert any(line.startswith("UNREACHABLE  node3  ") for line in lines), output  # ignored: one line
    assert "TASK [Ping again]" in output and "fatal: [node3]: UNREACHABLE!" in output, output  # not ignored: in full


def test_diff_is_shown(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("old\n")
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Write a file
      ansible.builtin.copy:
        dest: %s
        content: "new\\n"
""" % target
    rc, output = run(tmp_path, playbook, "--diff")
    assert rc == 0, output
    assert "-old" in output and "+new" in output


def test_verbose_delegates_to_the_default_callback(tmp_path):
    rc, output = run(tmp_path, PLAYBOOK, "-v")
    assert rc == 0, output
    assert "TASK [A command]" in output and "PLAY RECAP" in output and "skipping: [node1]" in output
    assert "plain debug output" in output and "...ignoring" in output


def test_a_verdict_without_text_and_one_per_host(tmp_path):
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Empty verdict
      ansible.builtin.assert:
        that: inventory_hostname != 'node1'
        fail_msg: "{{ [] }}"
      vars:
        cassandra_output: true
    - name: Per host verdict
      ansible.builtin.fail:
        msg: ["", "not healthy here"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc != 0
    # nothing to say: the default callback's failure, task and host named
    assert "TASK [Empty verdict]" in output and "fatal: [node1]: FAILED!" in output
    # run on each host: each line says which one
    assert "node2: not healthy here" in output.splitlines()


def test_warnings_are_shown(tmp_path):
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "warner.py").write_text(
        "from ansible.module_utils.basic import AnsibleModule\n"
        "m = AnsibleModule(argument_spec={})\nm.warn('WARN-FROM-MODULE')\nm.exit_json(changed=False)\n")
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Warns
      warner:
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    assert "WARN-FROM-MODULE" in output


def test_a_rescued_failure_on_one_line(tmp_path):
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Outer
      block:
        - name: Inner
          block:
            - name: Read something optional
              ansible.builtin.command: sh -c 'echo OpenJDK warning >&2; echo no CQL access >&2; exit 2'
            - name: Never run
              ansible.builtin.debug:
                msg: never
          always:
            - name: Always
              ansible.builtin.debug:
                msg: always
      rescue:
        - name: Go on without it
          ansible.builtin.debug:
            msg: "going on"
          vars:
            cassandra_output: true
    - name: Items
      block:
        - name: An item fails
          ansible.builtin.command: "{{ item }}"
          loop: [/bin/true, /bin/false]
      rescue:
        - name: Fine
          ansible.builtin.debug:
            msg: fine
    - name: A long one
      block:
        - name: Long
          ansible.builtin.fail:
            msg: "{{ 'x' * 300 }}"
      rescue:
        - name: Fine too
          ansible.builtin.debug:
            msg: fine
    - name: In a rescue itself
      block:
        - name: Fails
          ansible.builtin.fail:
            msg: first
      rescue:
        - name: Fails again
          ansible.builtin.fail:
            msg: the real failure
"""
    rc, output = run(tmp_path, playbook, INVENTORY="node1,")
    assert rc != 0
    lines = output.splitlines()
    assert lines[0] == "node1: Read something optional: no CQL access (the playbook handles it)"
    assert lines[1] == "going on"
    assert "node1: Fails: first (the playbook handles it)" in lines
    assert "node1: Long: %s... (the playbook handles it)" % ("x" * 157) in lines
    # a failed item of a rescued loop: one line too, no item dump
    assert "node1: An item fails: non-zero return code (the playbook handles it)" in lines
    assert "failed: [node1] (item=/bin/false)" not in output
    # a failure in a rescue is not handled: in full
    assert "TASK [Fails again]" in output and "the real failure" in output


def test_a_play_with_no_host_says_nothing(tmp_path):
    playbook = """
- hosts: "{{ groups['nothing_to_do'] | default([]) }}"
  gather_facts: false
  tasks:
    - name: Never
      ansible.builtin.debug:
        msg: never
- hosts: all
  gather_facts: false
  tasks:
    - name: Load defaults
      ansible.builtin.include_role:
        name: "community.cassandra.{{ item }}"
        tasks_from: defaults.yml
      loop: [cassandra_service, cassandra_config, cassandra_install, cassandra_medusa, cassandra_repository]
      when: true
    - name: Done
      ansible.builtin.debug:
        msg: done
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook, INVENTORY="node1,")
    assert rc == 0, output
    assert output.splitlines() == ["done"]  # no "skipping: no hosts matched", no noop warning


def test_blocks_one_blank_line_apart_never_two(tmp_path):
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: First
      ansible.builtin.debug:
        msg: ["", "first"]
      vars:
        cassandra_output: true
    - name: A block starts after it
      ansible.builtin.set_fact:
        started: true
      vars:
        cassandra_output_gap: true
    - name: Second
      ansible.builtin.debug:
        msg: ["second", "", "", "third"]
      vars:
        cassandra_output: true
    - name: Same block
      ansible.builtin.debug:
        msg: same block
      vars:
        cassandra_output: true
    - name: A block of its own, a blank line of its own too
      ansible.builtin.debug:
        msg: ["", "fourth"]
      vars:
        cassandra_output: true
        cassandra_output_gap: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    assert output.splitlines() == ["first", "", "second", "", "third", "same block", "", "fourth"]


def test_never_two_blank_lines_around_pauses_diffs_loops_and_failures(tmp_path):
    (tmp_path / "same.txt").write_text("same\n")
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: One
      ansible.builtin.debug:
        msg: one
      vars:
        cassandra_output: true
    - name: A question skipped, not one of the operator's
      ansible.builtin.pause:
        prompt: never
      when: false
    - name: Two
      ansible.builtin.debug:
        msg: ["two", "three", ""]
      vars:
        cassandra_output: true
    - name: Unchanged, with a diff the default callback does not print
      ansible.builtin.file:
        path: "%s"
        state: file
    - name: Four
      ansible.builtin.debug:
        msg: four
      vars:
        cassandra_output: true
        cassandra_output_gap: true
    - name: Items, one blank line before the first
      ansible.builtin.debug:
        msg: "item {{ item }}"
      loop: [a, b]
      vars:
        cassandra_output: true
        cassandra_output_gap: true
    - name: Five
      ansible.builtin.debug:
        msg: ["five", ""]
      vars:
        cassandra_output: true
    - name: Fails
      ansible.builtin.command: /bin/false
""" % (tmp_path / "same.txt")
    rc, output = run(tmp_path, playbook, "--diff")
    assert rc != 0
    lines = output.splitlines()
    assert lines[:10] == ["one", "two", "three", "", "four", "", "item a", "item b", "five", ""], output
    # the default callback's task header: its own blank line above left out
    assert lines[10].startswith("TASK [Fails] *"), output


def test_colours_by_line_start():
    from ansible import constants as C
    from ansible_collections.community.cassandra.plugins.callback.ops import colour
    assert colour("WARNING  seeds: dc1 has one seed") == C.COLOR_CHANGED
    assert colour("         its next line", above=C.COLOR_CHANGED) == C.COLOR_CHANGED  # indented: as the line above
    assert colour("DONE  topology  my_cluster") == colour("HEALTHY  my_cluster") == C.COLOR_OK
    assert colour("NOT HEALTHY  my_cluster") == colour("REFUSED  topology") == C.COLOR_ERROR
    assert colour("[1/2] node5 bootstrap  JOINING  STALLED 2/12 checks  35s") == C.COLOR_ERROR
    assert colour("[1/2] node5 bootstrap  JOINING  52%") == colour("Checking the cluster (5 nodes)...") == C.COLOR_VERBOSE
    assert colour("NOTE  cassandra_foo is not read") == C.COLOR_VERBOSE
    assert colour("dc1 after:  6 nodes") is None and colour("  1.  add node5") is None


def test_colours_on_a_terminal_only(tmp_path):
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Lines
      ansible.builtin.debug:
        msg: ["WARNING  w", "plain"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1")
    assert rc == 0, output
    assert re.search("\x1b\\[[0-9;]+mWARNING  w\x1b\\[0m", output) and "\nplain" in output, repr(output)
    rc, output = run(tmp_path, playbook)  # ANSIBLE_NOCOLOR
    assert rc == 0 and "\x1b" not in output, repr(output)


def test_verbose_task_headers_keep_their_blank_line(tmp_path):
    rc, output = run(tmp_path, PLAYBOOK, "-v")
    assert rc == 0, output
    assert "\n\nTASK [A command]" in output and "\n\nTASK [Not marked]" in output


def test_a_warning_then_a_block(tmp_path):
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "warner.py").write_text(
        "from ansible.module_utils.basic import AnsibleModule\n"
        "m = AnsibleModule(argument_spec={})\nm.warn('careful')\nm.exit_json(changed=False)\n")
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: One
      ansible.builtin.debug:
        msg: ["one", ""]
      vars:
        cassandra_output: true
    - name: Warns
      warner:
    - name: Two
      ansible.builtin.debug:
        msg: two
      vars:
        cassandra_output: true
        cassandra_output_gap: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    lines = output.splitlines()
    assert lines[:2] == ["one", ""] and "careful" in output, output
    assert lines[-2:] == ["", "two"], output  # a blank line between the warning and the block


def test_keys_typed_during_the_run_do_not_reach_the_shell(tmp_path):
    """An answer typed again while the run is quiet is dropped at the end: the
    shell would run it ("yes": y lines forever)."""
    pty = pytest.importorskip("pty")
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Confirm
      ansible.builtin.include_role:
        name: community.cassandra.cassandra_service
        tasks_from: confirm.yml
      vars:
        cassandra_confirm_prompt: "Go?"
    - name: Quiet for a while
      ansible.builtin.wait_for:
        timeout: 6
    - name: Done
      ansible.builtin.debug:
        msg: done
      vars:
        cassandra_output: true
"""
    (tmp_path / "playbook.yml").write_text(playbook)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_STDOUT_CALLBACK="community.cassandra.ops",
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_RETRY_FILES_ENABLED="0")
    env.pop("ANSIBLE_CONFIG", None)
    # the shell after the run: what it reads from the terminal next
    script = ("%s -c 'from ansible.cli.playbook import main; main()' -i node1, -c local "
              "-e ansible_python_interpreter=%s playbook.yml; read -t 3 left; echo \"LEFT=[$left]\"") % (
        sys.executable, sys.executable)
    pid, fd = pty.fork()
    if pid == 0:  # the child
        os.chdir(str(tmp_path))
        os.execve("/bin/bash", ["/bin/bash", "--norc", "-c", script], env)
    output, deadline, typed = b"", time.time() + 120, 0
    while b"LEFT=[" not in output or not output.rstrip().endswith(b"]"):
        assert time.time() < deadline, output.decode(errors="replace")
        ready, dummy, dummy = select.select([fd], [], [], 0.5)
        data = b""
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
        output += data
        if typed == 0 and b"Answer yes to go on" in output:
            time.sleep(1)  # pause drops what was typed before it reads
            os.write(fd, b"yes\r")
            typed, at = 1, time.time()
        elif typed == 1 and time.time() - at > 2:
            os.write(fd, b"yes\r")  # typed again during the quiet part
            typed = 2
    os.waitpid(pid, 0)
    text = output.decode(errors="replace")
    assert typed == 2, text
    assert "done" in text
    assert "LEFT=[]" in text, text
