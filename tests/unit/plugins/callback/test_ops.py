from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The stdout callback community.cassandra.ops, run by ansible-playbook on
# local hosts: only the marked messages, the failures in full, -v as default.

import os
import subprocess
import sys

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


def test_only_the_marked_messages(tmp_path):
    rc, output = run(tmp_path, PLAYBOOK)
    assert rc == 0, output
    assert output.splitlines() == [
        "PLAN  op  my_cluster  2 steps",
        u"  1. node1  dc1/rack_a  ← here",
        "line one",
        "line two",
        "item a",
        "item b",
        "handler on node1",
        "handler on node2",
    ]


def test_check_mode_prints_the_same_messages(tmp_path):
    rc, output = run(tmp_path, PLAYBOOK, "--check")
    assert rc == 0, output
    assert output.splitlines()[:2] == ["PLAN  op  my_cluster  2 steps", u"  1. node1  dc1/rack_a  ← here"]
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
    assert output.rstrip().splitlines()[-2:] == ["NOT HEALTHY  my_cluster  1 problem", "  ring: 3 members, inventory 2"]
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
        msg: "not healthy here"
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
