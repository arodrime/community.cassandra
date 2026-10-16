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
    assert lines[:11] == [
        "====",  # a plan: a heavy rule above, a light one under its first line, a blank line each side of a rule
        "",
        "PLAN  op  my_cluster  2 steps",
        "",
        "----",
        "",
        u"  1. node1  dc1/rack_a  \u2190 here",
        "line one",
        "line two",
        "item a",
        "item b",
    ]
    # the handlers: in whatever order the hosts end
    assert sorted(lines[11:]) == ["handler on node1", "handler on node2"]


def test_check_mode_prints_the_same_messages(tmp_path):
    rc, output = run(tmp_path, PLAYBOOK, "--check")
    assert rc == 0, output
    assert shown(output)[:7] == ["====", "", "PLAN  op  my_cluster  2 steps", "", "----", "", u"  1. node1  dc1/rack_a  \u2190 here"]
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
    assert shown(output.rstrip())[-6:] == ["====", "", "NOT HEALTHY  my_cluster  1 problem", "  ring: 3 members, inventory 2", "",
                                           "===="]
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


@pytest.mark.parametrize("inner, cause", [
    ("""
- name: Install something
  ansible.builtin.fail:
    msg: "Failed to download metadata for repo 'epel'"
""", "Failed to download metadata for repo 'epel'"),
    ("""
- name: Loop
  ansible.builtin.command: "{{ item }}"
  loop: [/bin/true, /bin/false]
""", "One or more items failed"),
    # a verdict of its own (a marked fail): said once too, by the rescue
    ("""
- name: Refuse
  ansible.builtin.fail:
    msg: refused for a reason
  vars:
    cassandra_output: true
""", "refused for a reason")])
def test_a_rescue_that_says_the_failure_itself(tmp_path, inner, cause):
    """cassandra_output_rescued on the block: its rescue prints the cause (as a verdict), nothing before it
    (the failure said once), also through an include and for a looped task."""
    (tmp_path / "inner.yml").write_text(inner)
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Prepare
      vars:
        cassandra_output_rescued: true
      block:
        - name: Inner
          ansible.builtin.include_tasks: inner.yml
      rescue:
        - name: Stop here
          ansible.builtin.fail:
            msg: "FAILED  op  could not be prepared: {{ ansible_failed_result.msg }}"
          vars:
            cassandra_output: true
"""
    rc, output = run(tmp_path, playbook, INVENTORY="node1,")
    assert rc != 0
    assert output.splitlines() == ["node1: FAILED  op  could not be prepared: " + cause]


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
    assert colour("WARNING - seeds: dc1 has one seed") == C.COLOR_WARN  # Ansible's own (option warning_color)
    assert colour("WARNING - x", warning="bright yellow") == "bright yellow"
    assert colour("         its next line", above=C.COLOR_CHANGED) == C.COLOR_CHANGED  # indented: as the line above
    assert colour("DONE  topology  my_cluster") == colour("HEALTHY  my_cluster") == C.COLOR_OK
    assert colour("NOT HEALTHY  my_cluster") == colour("REFUSED  topology") == C.COLOR_ERROR
    assert colour("[1/2] node5 bootstrap  JOINING  STALLED 2/12 checks  35s") == C.COLOR_ERROR
    # a while without progress, before the warning threshold: as progress, its peer line too (indented under it)
    assert colour("[1/1] node4 bootstrap  JOINING  [----------]  8%  0.2/2.3 GiB  no progress for 1m  2m") == "cyan"
    assert colour("[1/1] node4 bootstrap  JOINING  STALLED 5m/30m  8%  0.2/2.3 GiB  7m") == C.COLOR_ERROR
    assert colour("[1/1] node4 bootstrap  JOINING  [----------]  8%  0.2/2.3 GiB  receiving (disk +3.2 GiB in 5m)  7m") \
        == "cyan"
    assert colour("      from node1 0.2/2.3 GiB no progress for 1m", above="cyan") == "cyan"
    # the progress, phase and NOTE lines: cyan (Ansible's dark blue is hard to read on a dark background)
    assert colour("[1/2] node5 bootstrap  JOINING  52%") == colour("Checking the cluster (5 nodes)...") == "cyan"
    assert colour("NOTE  cassandra_foo is not read") == "cyan"
    assert colour("NOTE  x", progress="bright blue") == "bright blue"
    assert colour(u"READY \u2014 nothing to change") == C.COLOR_OK
    assert colour("dc1 after:  6 nodes") is None and colour("  1.  add node5") is None


def test_a_stalled_peer_line_red_the_others_as_the_progress_line():
    from ansible import constants as C
    from ansible_collections.community.cassandra.plugins.callback.ops import CallbackModule, colour

    assert colour("      to node2  0.0/7.2 GiB   0% STALLED", above="cyan") == C.COLOR_ERROR
    assert colour("      90 peers: 87 ok (avg 44%), 1 STALLED; slowest: node5 0% STALLED", above="cyan") == C.COLOR_ERROR
    assert colour("      +13 more: 8 STALLED, 5 behind", above="cyan") == C.COLOR_ERROR
    assert colour("      +3 more: 3 behind", above="cyan") == "cyan"
    assert colour("      to node1  2.7/7.2 GiB  37%", above="cyan") == "cyan"
    assert colour("      a STALLED word in another indented line", above="cyan") == "cyan"
    assert colour("      to do: something STALLED", above="cyan") == "cyan"
    assert colour("      31 peers: 31 ok (avg 44%); slowest: node30 30%, node13 31%", above="cyan") == "cyan"

    class Display(object):
        columns = 100

        def __init__(self):
            self.shown = []

        def display(self, line, color=None):
            self.shown.append((line, color))

    callback = CallbackModule.__new__(CallbackModule)
    callback._display, callback._progress, callback._warning = Display(), "cyan", None
    callback._gap, callback._blank, callback._ruled = False, True, ""
    callback._print("\n".join([
        "[1/2] node5 bootstrap  JOINING  [#####-----]  52%  52.2/100.0 GiB  89 MiB/s  ETA 13:33 (9m)  10m",
        "      from node1  18.0/34.0 GiB  52%           from node3  4.2/17.0 GiB  24% STALLED",
        "      from node4   3.0/3.0 GiB  100%"]))
    assert [color for line, color in callback._display.shown if line.strip()] == ["cyan", C.COLOR_ERROR, "cyan"]


def test_status_verdict_and_table_rows_coloured():
    from ansible import constants as C
    from ansible_collections.community.cassandra.plugins.callback.ops import colour
    assert colour("ALL 5 NODES UP") == colour("ALL 1 NODE UP") == C.COLOR_OK
    assert colour(u"ALL 6 NODES UP \u2014 1 JOINING: node6") == C.COLOR_CHANGED
    assert colour("1 NODE DOWN: node1 (dc1/rack1)") == colour("2 NODES DOWN: node1 (dc1/r1), node4 (dc1/r2)") \
        == colour(u"1 NODE DOWN: node1 (dc1/r1) \u2014 1 LEAVING: node3") == C.COLOR_ERROR
    assert colour("1 NODE NOT REACHED: node3") == colour("2 NODES NOT REACHED: node3, node4") == C.COLOR_ERROR
    # a ring that differs: a warning line of its own, under the nodes' line (coloured by the nodes alone)
    assert colour("WARNING - ring and inventory differ") == C.COLOR_WARN  # Ansible's own (option warning_color)
    from ansible_collections.community.cassandra.plugins.callback.ops import _RING_DIFFER, _ring_verdict
    from ansible_collections.community.cassandra.plugins.filter.cassandra_ring import _DIFFER
    assert _RING_DIFFER.match(_DIFFER)  # the filter's line, the one the callback frames
    verdict = ["ALL 5 NODES UP", "WARNING - ring and inventory differ"]
    assert _ring_verdict(["c", "", "1 NODE NOT REACHED: node3", ""]) == (["c"], ["1 NODE NOT REACHED: node3"])
    assert _ring_verdict(["c", "", "notes", ""] + verdict + [""]) == (["c", "", "notes"], verdict)
    texts = ["c", "", "WARNING - ring and inventory differ"]  # no verdict above it: not the status report
    assert _ring_verdict(texts) == (texts, [])
    # the table rows by their state, never by the line above (a UN row under a DN one stays plain)
    assert colour(u" DN  10.0.0.5  5.25 GiB  16  22.5%  1e96  r1  node1  \u2190 down") == C.COLOR_ERROR
    assert colour(" DL  10.0.0.5  ?  16  22.5%  1e96  r1  node1") == C.COLOR_ERROR
    for state in "JLM":
        assert colour(" U%s  10.0.0.6  1 GiB  16  20.0%%  1e97  r1  node6" % state, above=C.COLOR_ERROR) == C.COLOR_CHANGED
    assert colour(" UN  10.0.0.9  6.88 GiB  16  28.0%  1c7a  r1  node5", above=C.COLOR_ERROR) is None
    # the header row and the summary table: indented, as the line above (a blank one: plain)
    assert colour(" --  Address  Load") is None and colour("  dc1   5 nodes   4 up, 1 down   load 23.2 GiB") is None
    assert colour("  UN rows") is None and colour("UN  not a row") is None
    # two spaces: not a row, the line above's colour kept
    assert colour("  UN  10.0.0.1  1 GiB", above=C.COLOR_ERROR) == C.COLOR_ERROR


def test_colours_on_a_terminal_only(tmp_path):
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Lines
      ansible.builtin.debug:
        msg: ["WARNING - w", "plain"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1")
    assert rc == 0, output
    assert re.search("\x1b\\[[0-9;]+mWARNING - w\x1b\\[0m", output) and "\nplain" in output, repr(output)
    rc, output = run(tmp_path, playbook)  # ANSIBLE_NOCOLOR
    assert rc == 0 and "\x1b" not in output, repr(output)


def test_an_informative_summary_in_the_progress_colour(tmp_path):
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Summary
      ansible.builtin.debug:
        msg: ["Data disks: set now on 1 node", "  node1:", "    /data -> sdb", "WARNING - lvchange failed (node1)"]
      vars:
        cassandra_output: true
        cassandra_output_info: true

    - name: Not a summary
      ansible.builtin.debug:
        msg: plain
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1")
    assert rc == 0, output
    for line in ("Data disks: set now on 1 node", "  node1:", "    /data -> sdb"):
        assert "\x1b[0;36m%s\x1b[0m" % line in output, repr(output)
    assert re.search("\x1b\\[1;35mWARNING - lvchange failed \\(node1\\)\x1b\\[0m", output), repr(output)
    assert "\nplain" in output, repr(output)


@pytest.mark.parametrize("env, code", [
    ({}, "0;36"),  # cyan
    ({"ANSIBLE_CALLBACK_CASSANDRA_OPS_PROGRESS_COLOR": "bright green"}, "1;32"),
    ({"ANSIBLE_CALLBACK_CASSANDRA_OPS_PROGRESS_COLOR": "no such colour"}, "0;36"),
    ({"ANSIBLE_COLOR_VERBOSE": "blue"}, "0;34"),  # set in the configuration: kept
    ({"ANSIBLE_COLOR_VERBOSE": "blue", "ANSIBLE_CALLBACK_CASSANDRA_OPS_PROGRESS_COLOR": "magenta"}, "0;35"),
])
def test_the_progress_colour(tmp_path, env, code):
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Phase
      ansible.builtin.debug:
        msg: Checking the cluster (3 nodes)...
      vars:
        cassandra_output: true
"""
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")  # read first (the run's dir): no user's COLOR_VERBOSE
    rc, output = run(tmp_path, playbook, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1", **env)
    assert rc == 0, output
    assert "\x1b[%smChecking the cluster (3 nodes)...\x1b[0m" % code in output, repr(output)
    assert ("is not a colour Ansible knows" in output) == ("no such colour" in str(env)), output


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


def test_the_status_report_is_a_screen_its_verdict_between_rules(tmp_path):
    playbook = u"""
- hosts: node1
  gather_facts: false
  tasks:
    - name: Show the ring
      ansible.builtin.debug:
        msg: ["my_cluster  (seen from node1, ring = inventory, 3 nodes)", "", "Datacenter: dc1", "",
              " --  Address   Rack", " DN  10.0.0.2  r1", "", "  dc1   3 nodes   2 up, 1 down", "",
              "1 NODE DOWN: node2 (dc1/r1)"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    # blank lines kept (no light rules); a blank line above and under each rule, as everywhere
    assert shown(output) == ["====", "", "my_cluster  (seen from node1, ring = inventory, 3 nodes)", "", "Datacenter: dc1", "",
                             " --  Address   Rack", " DN  10.0.0.2  r1", "", "  dc1   3 nodes   2 up, 1 down", "",
                             "====", "", "1 NODE DOWN: node2 (dc1/r1)", "", "====", ""]


def test_the_status_verdict_and_its_warning_between_rules(tmp_path):
    playbook = u"""
- hosts: node1
  gather_facts: false
  tasks:
    - name: Show the ring
      ansible.builtin.debug:
        msg: ["my_cluster  (ring and inventory differ, 4 nodes)", "", "Datacenter: dc1", "",
              "In the ring, not in the inventory: 10.0.0.9 (dc1, UN)", "",
              "ALL 4 NODES UP", "WARNING - ring and inventory differ"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0 and "\x1b" not in output, repr(output)
    assert shown(output)[-9:] == ["In the ring, not in the inventory: 10.0.0.9 (dc1, UN)", "",
                                  "====", "", "ALL 4 NODES UP", "WARNING - ring and inventory differ", "", "====", ""]
    rc, output = run(tmp_path, playbook, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1")
    assert rc == 0, output
    # the nodes' line green, the warning in the warning colour
    assert "\x1b[0;32mALL 4 NODES UP\x1b[0m\n\x1b[1;35mWARNING - ring and inventory differ\x1b[0m" in output, \
        repr(output)


def test_no_status_screen_without_a_verdict_last(tmp_path):
    playbook = u"""
- hosts: node1
  gather_facts: false
  tasks:
    - name: Say it
      ansible.builtin.debug:
        msg: ["checked: ALL 3 NODES UP", "", "1 NODE DOWN: node2 (dc1/r1)", "", "then something else"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    assert shown(output) == ["checked: ALL 3 NODES UP", "", "1 NODE DOWN: node2 (dc1/r1)", "", "then something else"]


def test_the_import_screen_is_ruled(tmp_path):
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Show the summary
      ansible.builtin.debug:
        msg: ["IMPORT my_cluster (--check, nothing written) - 3 nodes read / 3", "", "READY - nothing to change", "",
              "full report: written by the run without --check"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook, "--check")
    assert rc == 0, output
    assert shown(output) == ["====", "", "IMPORT my_cluster (--check, nothing written) - 3 nodes read / 3", "", "----", "",
                             "READY - nothing to change", "", "----", "", "full report: written by the run without --check"]


def test_air_around_rules_phases_and_paragraphs(tmp_path):
    # a blank line each side of a rule, never two in a row; a phase line a block of its own; a paragraph
    # break (" ", PARAGRAPH) blank inside a screen where "" is a rule
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: A phase
      ansible.builtin.debug:
        msg: Checking the cluster (3 nodes)...
      vars:
        cassandra_output: true
        cassandra_output_gap: true
    - name: Another phase
      ansible.builtin.debug:
        msg: Checking the new nodes (node4)...
      vars:
        cassandra_output: true
        cassandra_output_gap: true
    - name: A screen
      ansible.builtin.debug:
        msg: ["add_node on cluster 'c': add node4", "", "one", " ", "two", "", "  node4", "    its line"]
      vars:
        cassandra_output: true
        cassandra_output_gap: true
    - name: Right under it
      ansible.builtin.debug:
        msg: after
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    assert shown(output) == ["Checking the cluster (3 nodes)...", "", "Checking the new nodes (node4)...", "", "====", "",
                             "add_node on cluster 'c': add node4", "", "----", "", "one", "", "two", "", "----", "",
                             "  node4", "    its line", "after"]


def test_a_phase_line_is_a_block_of_its_own_without_a_gap(tmp_path):
    # no cassandra_output_gap anywhere: the phase line still gets a blank line above and under it
    playbook = """
- hosts: node1
  gather_facts: false
  tasks:
    - name: Plain
      ansible.builtin.debug:
        msg: ["a message", " ", "its second paragraph"]
      vars:
        cassandra_output: true
    - name: A phase
      ansible.builtin.debug:
        msg: Waiting for every node to be UN...
      vars:
        cassandra_output: true
    - name: Plain again
      ansible.builtin.debug:
        msg: ["two lines, the first ending...", "so not a phase line"]
      vars:
        cassandra_output: true
    - name: Right under it
      ansible.builtin.debug:
        msg: under
      vars:
        cassandra_output: true
    - name: Another phase
      ansible.builtin.debug:
        msg: Starting node5 and waiting for its bootstrap...
      vars:
        cassandra_output: true
    - name: Its progress
      ansible.builtin.debug:
        msg: "[1/2] node5 bootstrap  JOINING"
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    assert shown(output) == ["a message", "", "its second paragraph", "", "Waiting for every node to be UN...", "",
                             "two lines, the first ending...", "so not a phase line", "under", "",
                             "Starting node5 and waiting for its bootstrap...", "", "---- [1/2] node5 bootstrap ----",
                             "", "[1/2] node5 bootstrap  JOINING"]


def test_the_status_report_of_a_cluster_named_like_a_recap(tmp_path):
    playbook = u"""
- hosts: node1
  gather_facts: false
  tasks:
    - name: Show the ring
      ansible.builtin.debug:
        msg: ["DONE  (seen from node1, ring = inventory, 3 nodes)", "", "Datacenter: dc1", "", "ALL 3 NODES UP"]
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc == 0, output
    # the status report's framing, not a recap's (its blank lines stay blank, no light rules)
    assert shown(output) == ["====", "", "DONE  (seen from node1, ring = inventory, 3 nodes)", "", "Datacenter: dc1", "",
                             "====", "", "ALL 3 NODES UP", "", "====", ""]


PREPARE = """
- hosts: all
  gather_facts: false
  any_errors_fatal: true
  tasks:
    - name: Prepare this node
      vars:
        cassandra_output_rescued: true
      block:
        - name: Inner
          ansible.builtin.include_tasks: inner.yml
        - name: More
          ansible.builtin.include_tasks: more.yml
        - name: After
          ansible.builtin.command: "true"
          changed_when: false
      rescue:
        - name: Record
          ansible.builtin.set_fact:
            failed_here: true
        - name: Stop here
          ansible.builtin.fail:
            msg: ["FAILED  add_node  my_cluster  node2 could not be prepared: nothing was started", "  node2  Breaks"]
          run_once: true
          vars:
            cassandra_output: true
"""


def test_a_rescue_verdict_reached_at_two_times_is_said_once(tmp_path):
    """add_node's prepare: node2 fails, node1 goes on and comes to the rescue later (any_errors_fatal):
    ansible-core 2.15 and 2.16 run the run_once verdict again for it, the same lines."""
    (tmp_path / "inner.yml").write_text("- name: Breaks\n  ansible.builtin.fail:\n    msg: no\n"
                                        "  when: inventory_hostname == 'node2'\n")
    (tmp_path / "more.yml").write_text("- name: M1\n  ansible.builtin.debug:\n    msg: m1\n"
                                       "- name: M2\n  ansible.builtin.debug:\n    msg: m2\n")
    rc, output = run(tmp_path, PREPARE)
    assert rc != 0
    assert output.count("could not be prepared") == 1, output


class _Task(object):
    def __init__(self, run_once, path="add_node.yml:530", msg_name="Stop here"):
        self.vars = {"cassandra_output": True}
        self.action = "ansible.builtin.fail"
        self.run_once = run_once
        self.loop = None
        self._parent = None
        self._uuid = path
        self._path, self._name = path, msg_name

    def get_path(self):
        return self._path

    def get_name(self):
        return self._name


class _Host(object):
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class _Result(object):
    def __init__(self, task, host, msg):
        self._task, self._host, self._result = task, _Host(host), {"failed": True, "msg": msg}


def verdicts(*results):
    """The lines printed for these failed results (None: a new play starts)."""
    from ansible_collections.community.cassandra.plugins.callback.ops import CallbackModule
    callback = CallbackModule()
    said = []
    callback._display = type("D", (), {"verbosity": 0, "columns": 80,
                                       "display": lambda self, line, color=None, **kw: said.append(line)})()
    for result in results:
        if result is None:
            callback.v2_playbook_on_play_start(None)
        else:
            callback.v2_runner_on_failed(result)
    return [line for line in said if line.strip() and not re.match(r"^[-=]{4,}$", line)]


def test_the_same_run_once_verdict_is_printed_once():
    stop = _Task(run_once=True)
    msg = ["FAILED  add_node  my_cluster  node2 could not be prepared: nothing was started", "  node2  Breaks"]
    assert verdicts(_Result(stop, "node2", msg), _Result(stop, "node1", msg)) == msg
    # another text, or another task: said
    assert verdicts(_Result(stop, "node2", msg), _Result(stop, "node1", ["FAILED  other"])) == msg + ["FAILED  other"]
    assert verdicts(_Result(stop, "node2", msg), _Result(_Task(True, path="topology.yml:9"), "node1", msg)) == msg + msg
    # in another play: said again
    assert verdicts(_Result(stop, "node2", msg), None, _Result(stop, "node1", msg)) == msg + msg
    # a verdict per host (no run_once): each host's own
    each = _Task(run_once=False)
    assert verdicts(_Result(each, "node1", ["REFUSED  x"]), _Result(each, "node2", ["REFUSED  x"])) == [
        "node1: REFUSED  x", "node2: REFUSED  x"]


def test_a_refusal_block_is_red_on_its_verdict_line_only():
    from ansible import constants as C
    from ansible_collections.community.cassandra.plugins.callback.ops import CallbackModule
    callback = CallbackModule()
    said = []
    callback._display = type("D", (), {"verbosity": 0, "columns": 80,
                                       "display": lambda self, line, color=None, **kw: said.append((line, color))})()
    block = ["REFUSED  add_node  my_cluster  nothing was changed", "", "node7 (dc1/rack_b): holds 12.0 GiB", "",
             "TO DO", "  1. empty it:", "     ansible-playbook community.cassandra.reset_node"]
    callback.v2_runner_on_failed(_Result(_Task(run_once=True), "node7", block))
    coloured = dict((line, color) for line, color in said if line.strip() and not re.match(r"^[-=]{4,}$", line))
    assert coloured == {block[0]: C.COLOR_ERROR, block[2]: None, block[4]: None, block[5]: None, block[6]: None}
    # a verdict that is not a block of its own: red
    said[:] = []
    callback.v2_runner_on_failed(_Result(_Task(run_once=True, path="x.yml:1"), "node7", ["No node was added."]))
    assert said == [("No node was added.", C.COLOR_ERROR)]


def test_a_failed_self_check_is_red_on_its_verdict_line_only():
    # import_cluster's stop: a block without rules, its blank lines blank, red on the verdict (and its next line)
    # only
    from ansible import constants as C
    from ansible_collections.community.cassandra.plugins.callback.ops import CallbackModule
    callback = CallbackModule()
    said = []
    callback._display = type("D", (), {"verbosity": 0, "columns": 80,
                                       "display": lambda self, line, color=None, **kw: said.append((line, color))})()
    block = ["SELF-CHECK FAILED  my_cluster  the inventory would change 1 setting on 1 node", "  (written for inspection"
             " only)", "", "  Hand edits no variable covers (cassandra_config would revert them):",
             "    logback.xml   <logger>   hand edit on node1", "", "HOW TO MAKE IT PASS",
             "  hand edits   to keep them as they are: import again with -e import_cluster_keep_hand_edits=true", "",
             "Details: reports/my_cluster/report.txt, section SELF-CHECK"]
    callback.v2_runner_on_failed(_Result(_Task(run_once=True), "localhost", "\n".join(block)))
    assert [line for line, dummy in said] == block  # no rule, the blank lines blank
    coloured = dict((line, color) for line, color in said if line.strip() and not re.match(r"^[-=]{4,}$", line))
    assert coloured == dict([(block[0], C.COLOR_ERROR), (block[1], C.COLOR_ERROR)]
                            + [(line, None) for line in block[3:] if line.strip()])


# --- the help playbook's text ---

HELP_INVENTORY = """
cassandra:
  children:
    cassandra_rack1:
      hosts:
        node1: {ansible_host: 192.0.2.11}
        node2: {ansible_host: 192.0.2.12}
    cassandra_rack2:
      hosts:
        node3: {ansible_host: 192.0.2.13}
  vars:
    cassandra_seeds: 192.0.2.11,192.0.2.13
"""

HELP_TEXT = u"""
- hosts: node1
  gather_facts: false
  tasks:
    - name: Help text
      ansible.builtin.debug:
        msg:
          - "Options of every operation:"
          - "\\x1f42\\x1f    -e cassandra_hosts=<group>\\x1fthe cluster to run on (default:\\xa0the one in the commands)"
          - ""
          - "==== Nodes ===="
          - ""
          - "\\x1f23\\x1f  \\u25cf add_node\\x1fAdds new hosts to the running cluster: all prepared at once, then each started and bootstrapped in turn."
          - "      $ ansible-playbook community.cassandra.add_node -e cassandra_target_nodes=NEW_NODE"
          - "\\x1f$6\\x1fansible-playbook community.cassandra.add_node -e cassandra_target_nodes=NEW_NODE -e x=y"
          - "\\x1f50\\x1f        -e cassandra_a_long_option_name=value\\x1fwhat it does"
          - "\\x1f4\\x1f    $ rack1: a laid out text, not a command"
          - "---- What it does ----"
          - "NOTE  not a note here, nor DONE"
      vars:
        cassandra_output: true
        cassandra_output_help: true

    - name: Another playbook's message, its $ line and NOTE as ever
      ansible.builtin.debug:
        msg: ["NOTE  a note", "  $ ansible-playbook x"]
      vars:
        cassandra_output: true
"""


def test_help_line_colours():
    from ansible import constants as C
    from ansible.utils import color
    from ansible_collections.community.cassandra.plugins.callback.ops import PROGRESS_COLOR, help_command, help_line
    saved = color.ANSIBLE_COLOR
    try:
        color.ANSIBLE_COLOR = True
        ok, cyan = color.parsecolor(C.COLOR_OK), color.parsecolor(PROGRESS_COLOR)
        # an operation's name as ok, an option in the progress colour, the text after it plain
        assert help_line(u"  ● add_node  Adds", u"● add_node") == u"  \x1b[%sm● add_node\x1b[0m  Adds" % ok
        assert help_line("    -e x=y  what", "-e x=y") == "    \x1b[%sm-e x=y\x1b[0m  what" % cyan
        assert help_line("    --check  what", "--check", progress="magenta") == "    \x1b[0;35m--check\x1b[0m  what"
        # a command whole, from its $; a continuation line (no key) and any other line plain
        assert help_line("      $ ansible-playbook x -e y=z", "") == "      \x1b[%sm$ ansible-playbook x -e y=z\x1b[0m" % cyan
        assert help_line("            what it does", "") == "            what it does"
        assert help_command("          -e x=y \\") == "          \x1b[%sm-e x=y \\\x1b[0m" % cyan  # a command's line
        assert help_line("    $ rack1: node1", "", laid=True) == "    $ rack1: node1"  # an inventory's value
        assert help_line("DONE  not coloured here", "") == "DONE  not coloured here"
        color.ANSIBLE_COLOR = False  # ANSIBLE_NOCOLOR, not a terminal: as it is
        assert help_line(u"  ● add_node  Adds", u"● add_node") == u"  ● add_node  Adds"
        assert help_line("      $ ansible-playbook x", "") == "      $ ansible-playbook x"
    finally:
        color.ANSIBLE_COLOR = saved


def test_width_of_rules_and_help():
    from ansible_collections.community.cassandra.plugins.callback.ops import rule_width
    assert rule_width(79) == 78 and rule_width(119) == 100 and rule_width(59) == 58 and rule_width(10) == 40
    assert rule_width(0) == rule_width(-1) == rule_width(None) == 78  # unknown: Ansible's 79


@pytest.mark.parametrize("columns, width", [(120, 100), (81, 79), (80, 78), (60, 58), (0, 78)])  # 0: a terminal that says 0
def test_width_of_the_terminal(monkeypatch, columns, width):
    import collections
    from ansible_collections.community.cassandra.plugins.callback.ops import CallbackModule
    size = collections.namedtuple("size", "columns lines")
    monkeypatch.setattr(os, "get_terminal_size", lambda fd=None: size(columns, 24))
    callback = CallbackModule()
    monkeypatch.setattr(callback._display, "columns", 79)  # Ansible's for any terminal under 80 columns
    assert callback._width() == width


def test_help_text_in_the_log_without_colours(tmp_path):
    log = tmp_path / "ansible.log"
    rc, output = run(tmp_path, HELP_TEXT, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1", ANSIBLE_LOG_PATH=str(log))
    assert rc == 0 and "\x1b[0;36m-e cassandra_hosts=<group>\x1b[0m" in output, output
    text = log.read_text()
    assert "    -e cassandra_hosts=<group>            the cluster to run on" in text and "\x1b" not in text, text


def test_help_text_laid_out_and_coloured(tmp_path):
    # not a terminal: Ansible's 79 columns, so 78; the rules drawn as the others; the lines laid out there
    rc, output = run(tmp_path, HELP_TEXT)
    assert rc == 0, output
    assert output.splitlines() == [
        "Options of every operation:",
        "    -e cassandra_hosts=<group>            the cluster to run on (default: the",
        "                                          one in the commands)",
        "",
        "==== Nodes " + "=" * 67,
        "",
        u"  ● add_node".ljust(23) + "Adds new hosts to the running cluster: all prepared at",
        " " * 23 + "once, then each started and bootstrapped in turn.",
        "      $ ansible-playbook community.cassandra.add_node -e cassandra_target_nodes=NEW_NODE",
        "      $ ansible-playbook community.cassandra.add_node \\",
        "          -e cassandra_target_nodes=NEW_NODE -e x=y",
        "        -e cassandra_a_long_option_name=value",
        "            what it does",
        "    $ rack1: a laid out text, not a command",
        "",
        "---- What it does " + "-" * 60,
        "",
        "NOTE  not a note here, nor DONE",
        "NOTE  a note",
        "  $ ansible-playbook x",
    ], output
    assert "\x1f" not in output and u"\xa0" not in output
    rc, output = run(tmp_path, HELP_TEXT, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1")
    assert rc == 0, output
    assert u"  \x1b[0;32m● add_node\x1b[0m" + " " * 11 + "Adds" in output, repr(output)
    assert "    \x1b[0;36m-e cassandra_hosts=<group>\x1b[0m            the cluster" in output, repr(output)
    assert "      \x1b[0;36m$ ansible-playbook community.cassandra.add_node" in output, repr(output)
    # a command cut: each of its lines coloured, the continuations too
    assert ("      \x1b[0;36m$ ansible-playbook community.cassandra.add_node \\\x1b[0m\n"
            "          \x1b[0;36m-e cassandra_target_nodes=NEW_NODE -e x=y\x1b[0m\n") in output, repr(output)
    assert "        \x1b[0;36m-e cassandra_a_long_option_name=value\x1b[0m\n" in output, repr(output)  # on its own line
    assert "\n    $ rack1: a laid out text, not a command\n" in output, repr(output)
    assert "\nNOTE  not a note here, nor DONE\n" in output  # help lines: no colour by their start
    # another playbook's message keeps its colours: NOTE in cyan, a $ line as the line above
    assert "\x1b[0;36mNOTE  a note\x1b[0m\n\x1b[0;36m  $ ansible-playbook x\x1b[0m" in output, repr(output)


def help_run(tmp_path, *args, **env):
    (tmp_path / "hosts.yml").write_text(HELP_INVENTORY)
    return run(tmp_path, "- import_playbook: community.cassandra.help\n", *args, INVENTORY=str(tmp_path / "hosts.yml"),
               **env)


def test_the_help_playbook_under_the_ops_callback(tmp_path):
    rc, output = help_run(tmp_path, ANSIBLE_NOCOLOR="0", ANSIBLE_FORCE_COLOR="1")
    assert rc == 0, output
    assert u"\x1b[0;32m● add_node\x1b[0m" in output and "\x1b[0;36m-e cassandra_target_nodes=NEW_NODE\x1b[0m" in output
    assert "\x1f" not in output and u"\xa0" not in output
    plain = re.sub("\x1b\\[[0-9;]*m", "", output)
    assert "\n==== Nodes " + "=" * 67 + "\n" in plain
    # (the cluster's line is one line, whatever its length: not help text; the commands cut too)
    assert not [line for line in plain.splitlines() if len(line) > 78 and not line.startswith("CLUSTER ")]
    rc, output = help_run(tmp_path)  # ANSIBLE_NOCOLOR: the same text, plain
    assert rc == 0 and output == plain, output
    # its first line and its last: the cluster, and how it was picked
    said = [line for line in plain.splitlines() if line.strip()]
    banner = "CLUSTER cassandra (3 nodes, dc1) | chosen by: the only cluster in the inventory"
    assert said[0] == banner and said[-1] == banner, plain


def test_the_help_playbook_of_several_clusters_none_named(tmp_path):
    """The help of any cluster (CLUSTER_GROUP in the commands), the clusters found and how to name one; nothing of
    one cluster's inventory, no CLUSTER line."""
    (tmp_path / "hosts.yml").write_text(HELP_INVENTORY + "other:\n  children:\n    other_dc1:\n      hosts:\n        node9:\n")
    rc, output = run(tmp_path, "- import_playbook: community.cassandra.help\n", INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc == 0, output
    plain = " ".join(output.split())
    assert "Clusters found: cassandra (3 nodes, dc1), other (1 node, dc1)" in plain, output
    assert "The help of one cluster: -e cassandra_hosts=CLUSTER_GROUP, or CASSANDRA_CLUSTER=<group or cluster name>" in plain
    assert "$ ansible-playbook -i hosts.yml community.cassandra.rolling_restart -e cassandra_hosts=CLUSTER_GROUP" in plain, output
    assert "Name the cluster for the advice of its inventory." in plain
    assert "CLUSTER " not in output and "node1" not in output and "Traceback" not in output
    # RUNBOOK.md is one cluster's: refused, nothing written
    rc, output = run(tmp_path, "- import_playbook: community.cassandra.help\n", "-e", "help_write=true",
                     INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc != 0 and output.strip().startswith("help_write: no cluster named, and the inventory is not one cluster:"), output
    assert output.strip().endswith("Nothing was written.") and not (tmp_path / "RUNBOOK.md").exists()
    rc, output = run(tmp_path, "- import_playbook: community.cassandra.help\n", "-e", "help_write=true",
                     "-e", "cassandra_hosts=other", INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc == 0 and (tmp_path / "RUNBOOK.md").exists(), output
    rc, output = run(tmp_path, "- import_playbook: community.cassandra.help\n", "-e", "cassandra_hosts=other",
                     INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc == 0 and output.split("\n")[0] == "CLUSTER other (1 node, dc1) | chosen by: -e cassandra_hosts", output
    assert "CLUSTER_GROUP" not in output


def test_the_help_playbook_under_the_default_callback(tmp_path):
    # laid out at 100 columns by the filter, no layout mark left (the ops callback's)
    rc, output = help_run(tmp_path, ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_CALLBACK_RESULT_FORMAT="yaml")
    assert rc == 0, output
    assert u"\n          ● add_node           Adds new hosts to the running cluster" in output, output
    assert "\x1f" not in output and "\\u001f" not in output and u"\xa0" not in output
    # with -v under the ops callback: the default callback's output, no layout mark either
    rc, output = help_run(tmp_path, "-v")
    assert rc == 0 and "\\u001f" not in output and "\x1f" not in output, output


def test_a_marked_loop_says_its_failed_items_once(tmp_path):
    """preflight's "Every seed is up" (a run_once loop over the seeds): the message of each item that
    failed, one block, no assert dump; the items that pass say nothing."""
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Every seed is up
      ansible.builtin.assert:
        that: item == 'a'
        fail_msg: "Seed {{ item }} is not up: bring it back up."
        quiet: true
      loop: [a, b, c]
      run_once: true
      vars:
        cassandra_output: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc != 0
    assert shown(output) == ["Seed b is not up: bring it back up.", "Seed c is not up: bring it back up."], output


def test_a_marked_verdict_whose_template_fails_is_the_default_output(tmp_path):
    """The task's own template error (e.g. -e cassandra_target_nodes='a:&'): core's message with the task's
    name (the default callback's), not printed bare as if it were the verdict."""
    playbook = """
- hosts: all
  gather_facts: false
  tasks:
    - name: Only where the playbook takes it
      ansible.builtin.assert:
        that: "1 in (x | regex_search('('))"
        fail_msg: the verdict
        quiet: true
      vars:
        cassandra_output: true
        x: abc
      run_once: true
"""
    rc, output = run(tmp_path, playbook)
    assert rc != 0
    assert "Only where the playbook takes it" in output and "the verdict" not in output, output
    assert "FAILED!" in output, output


@pytest.mark.parametrize("msg", ["The conditional check 'a in b' failed. The error was: ...",
                                 "The task includes an option with an undefined variable.. 'x' is undefined",
                                 "Task failed: Syntax error in expression",
                                 "Unexpected templating type error occurred on ({{ a + 1 }}): ...",
                                 "An unhandled exception occurred while templating '{{ x }}'. ...",
                                 "the field 'args' has an invalid value ({...}), and could not be converted to a dict.",
                                 "Unexpected failure during module execution: missing ), unterminated subpattern"])
def test_core_template_errors_are_not_verdicts(msg):
    from ansible_collections.community.cassandra.plugins.callback.ops import CallbackModule
    callback = CallbackModule()
    task = _Task(run_once=True)
    task.action = "ansible.builtin.assert"
    assert callback._own_failure(_Result(task, "node1", msg)) is False
    assert callback._own_failure(_Result(task, "node1", "Seed n5 is not up")) is True


def test_a_task_with_diff_false_shows_no_diff():
    # ansible-core 2.16-2.18 call v2_on_file_diff under --diff whatever the task's diff: says
    from ansible_collections.community.cassandra.plugins.callback.ops import CallbackModule
    callback = CallbackModule()
    shown = []
    callback._display = type("D", (), {"display": lambda self, msg, *a, **k: shown.append(msg)})()
    task = _Task(run_once=False)
    task.diff = False
    result = _Result(task, "node1", "")
    result._result = {"changed": True, "diff": {"before": "state: absent\n", "after": "state: directory\n"}}
    callback.v2_on_file_diff(result)
    assert shown == []


CLUSTER_LINE = "CLUSTER prod (2 nodes, dc1) | chosen by: -e cassandra_hosts"
CLUSTER_PLAYBOOK = """
- hosts: localhost
  gather_facts: false
  tasks:
    - name: Say which cluster the run is on
      ansible.builtin.debug:
        msg: "%s"
      vars:
        cassandra_output: true
        cassandra_output_info: true
        cassandra_output_gap: true
    - name: Recap
      ansible.builtin.debug:
        msg: "DONE  op  prod  2 restarted"
      vars:
        cassandra_output: true
    - name: Stop
      ansible.builtin.fail:
        msg: "FAILED  op  prod  1 failed"
      when: stop | default(false) | bool
      vars:
        cassandra_output: true
""" % CLUSTER_LINE


@pytest.mark.parametrize("stop", ["false", "true"])
def test_the_cluster_line_first_and_last(tmp_path, stop):
    """The run's first line names its cluster: said again as its last, whether the run ended well or not."""
    rc, output = run(tmp_path, CLUSTER_PLAYBOOK, "-e", "stop=" + stop)
    assert (rc == 0) == (stop == "false"), output
    said = [line for line in output.splitlines() if line.strip()]
    assert said[0] == CLUSTER_LINE and said[-1] == CLUSTER_LINE, output
    assert output.count(CLUSTER_LINE) == 2


def test_no_cluster_line_no_last_line(tmp_path):
    rc, output = run(tmp_path, CLUSTER_PLAYBOOK.replace(CLUSTER_LINE, "NOTE  something else"))
    assert rc == 0 and "CLUSTER" not in output and output.rstrip().endswith("====")


def test_the_cluster_line_with_a_datacenter_only_the_node_knows(tmp_path):
    """A cassandra_dc templated from the node's facts: dc? on the line, not a failed run (an undefined marker)."""
    (tmp_path / "hosts.yml").write_text(
        "prod:\n  children:\n    prod_dc1:\n      hosts:\n        p1: {cassandra_dc: dc1}\n"
        "        p2: {cassandra_dc: \"{{ ansible_facts['hostname'][:3] }}\"}\n"
        "        p3: {cassandra_dc: \"{{ undefined_thing }}\", cassandra_node_state: absent}\n")
    rc, output = run(tmp_path, "- hosts: localhost\n  gather_facts: false\n  tasks:\n"
                     "    - ansible.builtin.include_role:\n        name: community.cassandra.cassandra_service\n"
                     "        tasks_from: cluster_banner.yml\n", INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc == 0, output
    banner = "CLUSTER prod (2 nodes + 1 marked absent, dc1, dc?) | chosen by: the only cluster in the inventory"
    assert [line for line in output.splitlines() if line.strip()] == [banner, banner], output


TWO_CLUSTERS = """
prod:
  children:
    prod_dc1:
      hosts:
        p1: {}
dev:
  children:
    dev_dc1:
      hosts:
        d1: {}
"""


def test_cassandra_hosts_of_a_playbook_taken_of_the_inventory_refused(tmp_path):
    """A wrapper playbook's vars reach the plays' hosts: taken. The inventory's (group_vars) do not: refused."""
    (tmp_path / "hosts.yml").write_text(TWO_CLUSTERS)
    wrapper = "- import_playbook: community.cassandra.help\n  vars:\n    cassandra_hosts: dev\n"
    rc, output = run(tmp_path, wrapper, INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc == 0 and output.split("\n")[0] == "CLUSTER dev (1 node, dc1) | chosen by: -e cassandra_hosts", output
    (tmp_path / "group_vars").mkdir()
    (tmp_path / "group_vars" / "all.yml").write_text("cassandra_hosts: dev\n")
    rc, output = run(tmp_path, "- import_playbook: community.cassandra.help\n", INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc != 0 and output.strip() == (
        "cassandra_hosts is set in the inventory (dev, group_vars or host_vars): the plays' hosts do not see it."
        " Give it on the command line, -e cassandra_hosts=dev, or CASSANDRA_CLUSTER=dev in your shell."), output
    rc, output = run(tmp_path, "- import_playbook: community.cassandra.help\n", "-e", "cassandra_hosts=prod",
                     INVENTORY=str(tmp_path / "hosts.yml"))
    assert rc == 0 and output.split("\n")[0].startswith("CLUSTER prod (1 node, dc1)"), output
