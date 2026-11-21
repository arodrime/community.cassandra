from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# What the readability guardrails (test_readability.py) measure, on task files written here.

import textwrap

import pytest

from ansible_collections.community.cassandra.tests.unit import test_readability

LONG = "x is defined and " * 20 + "true"  # a bare expression of 344 characters
SCRIPT = "\n".join("echo %d" % i for i in range(30))  # 30 lines


def measured(tmp_path, monkeypatch, text):
    path = tmp_path / "playbooks" / "t.yml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(textwrap.dedent(text))
    monkeypatch.setattr(test_readability, "TOP", str(tmp_path))
    monkeypatch.setattr(test_readability, "_files", lambda: [str(path)])
    return test_readability.measured()


@pytest.mark.parametrize("key", ["when", "that", "until", "failed_when", "changed_when"])
def test_a_long_bare_expression_is_measured(tmp_path, monkeypatch, key):
    found = measured(tmp_path, monkeypatch, """
        - name: T
          ansible.builtin.assert:
            {0}:
              - {1}
          {0}: {1}
        """.format(key, LONG))
    assert set(found.values()) == {len(LONG)}, found
    assert len(found) == 2, found


@pytest.mark.parametrize("task", [
    "- ansible.builtin.shell: |\n{script}",  # no name
    "- name: T\n  ansible.legacy.shell: |\n{script}",
    "- name: T\n  ansible.builtin.command:\n    cmd: |\n{script}",
    "- name: T\n  ansible.builtin.shell:\n  args:\n    cmd: |\n{script}",
    "- name: T\n  ansible.builtin.raw: |\n{script}",
    "- name: T\n  raw: |\n{script}",
    "- name: T\n  command: |\n{script}",
])
def test_a_long_inline_script_is_measured(tmp_path, monkeypatch, task):
    script = textwrap.indent(SCRIPT, " " * (8 if "cmd:" in task else 4))
    found = measured(tmp_path, monkeypatch, task.format(script=script))
    assert list(found.values()) == [30] and list(found)[0].endswith(" (shell)"), found


def test_the_same_name_twice_keeps_its_number_whatever_the_size_of_the_first(tmp_path, monkeypatch):
    # the second of two same-named strings stays #2 when the first is short: its listed size stays its own
    tasks = """
        - name: T
          ansible.builtin.debug:
            msg: "{{ %s }}"
        - name: T
          ansible.builtin.debug:
            msg: "{{ %s }}"
        """
    both = measured(tmp_path, monkeypatch, tasks % (LONG, LONG + " or false"))
    assert sorted(both) == ["playbooks/t.yml :: T :: ansible.builtin.debug.msg #2 (jinja)",
                            "playbooks/t.yml :: T :: ansible.builtin.debug.msg (jinja)"]
    second = measured(tmp_path, monkeypatch, tasks % ("x", LONG + " or false"))
    assert second == {"playbooks/t.yml :: T :: ansible.builtin.debug.msg #2 (jinja)": len("{{ %s or false }}" % LONG)}
