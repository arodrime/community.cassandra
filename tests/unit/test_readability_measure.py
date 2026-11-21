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


@pytest.mark.parametrize("size, says", [(400, "shorter now: lower its size"), (600, "x: 600, listed 500")])
def test_a_listed_one_stays_at_its_size(monkeypatch, size, says):
    # grown or shrunk: the list says the size measured, so that a string made shorter cannot grow back
    monkeypatch.setattr(test_readability, "measured", lambda: {"x": size})
    monkeypatch.setattr(test_readability, "allowed", lambda: {"x": 500})
    with pytest.raises(AssertionError, match=says):
        test_readability.test_a_listed_one_is_listed_at_its_size()
    monkeypatch.setattr(test_readability, "measured", lambda: {"x": 500})
    test_readability.test_a_listed_one_is_listed_at_its_size()


def test_a_long_bare_expression_of_debugs_var_is_measured(tmp_path, monkeypatch):
    found = measured(tmp_path, monkeypatch, """
        - name: T
          ansible.builtin.debug:
            var: {0}
        """.format(LONG))
    assert list(found.values()) == [len(LONG)], found


def test_a_long_string_of_jinja_statements_only_is_measured(tmp_path, monkeypatch):
    text = "{%% if %s %%}yes{%% endif %%}" % LONG
    found = measured(tmp_path, monkeypatch, """
        - name: T
          ansible.builtin.debug:
            msg: "{0}"
        """.format(text))
    assert list(found.values()) == [len(text)], found


def test_the_blanks_of_a_jinja_string_count_as_one(tmp_path, monkeypatch):
    # the layout is free: a call indented under its first line measures as one written on a line
    words = LONG.split(" ")
    found = measured(tmp_path, monkeypatch, """
        - name: T
          ansible.builtin.debug:
            msg: >-
              {{{{ {0}
                   {1} }}}}
        """.format(" ".join(words[:40]), "   ".join(words[40:])))
    assert list(found.values()) == [len("{{ %s }}" % LONG)], found


@pytest.mark.parametrize("task", [
    "- name: T\n  ansible.builtin.shell: >\n{script}",
    "- name: T\n  ansible.builtin.shell: >-\n{script}",
    "- name: T\n  ansible.builtin.shell:\n    cmd: >-\n{folded}",
    "- name: T\n  ansible.builtin.shell: echo 0\n{plain}",
])
def test_a_long_folded_script_is_measured_by_its_lines(tmp_path, monkeypatch, task):
    # YAML joins the lines of a folded script (or of a plain string written on several lines): measured as written
    found = measured(tmp_path, monkeypatch, task.format(script=textwrap.indent(SCRIPT, " " * 4),
                                                        folded=textwrap.indent(SCRIPT, " " * 6),
                                                        plain=textwrap.indent(SCRIPT.split("\n", 1)[1], " " * 4)))
    assert list(found.values()) == [30] and list(found)[0].endswith(" (shell)"), found
