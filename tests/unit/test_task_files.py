from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks


def write(path, text):
    path.write_text(text)
    return str(path)


def test_imports_inlined_with_their_when_first_and_the_task_vars_winning(tmp_path):
    write(tmp_path / "inner.yml", "- name: a\n  debug: {}\n  when: x\n  vars: {v: task}\n- name: b\n  debug: {}\n")
    write(tmp_path / "middle.yml", "- name: block\n  block:\n    - import_tasks: inner.yml\n      when: [y]\n")
    main = write(tmp_path / "main.yml",
                 "- ansible.builtin.import_tasks: middle.yml\n  when: z\n  vars: {v: import, w: 1}\n- include_tasks: '{{ f }}'\n")
    block, dynamic = load_tasks(main)
    assert block["when"] == ["z"] and block["vars"] == {"v": "import", "w": 1}
    a, b = block["block"]
    assert (a["name"], a["when"], a["vars"]) == ("a", ["y", "x"], {"v": "task"})
    assert (b["name"], b["when"], "vars" not in b) == ("b", ["y"], True)
    assert dynamic == {"include_tasks": "{{ f }}"}


def test_an_import_keyword_it_does_not_apply_is_refused(tmp_path):
    write(tmp_path / "inner.yml", "- name: a\n  debug: {}\n")
    main = write(tmp_path / "main.yml", "- import_tasks: inner.yml\n  tags: [x]\n")
    with pytest.raises(ValueError):
        load_tasks(main)
