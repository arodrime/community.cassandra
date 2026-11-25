from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.tests.unit.task_files import find_play, find_task, load_tasks


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


def test_a_static_include_keeps_its_task_with_the_tasks_of_its_file_under_it(tmp_path):
    write(tmp_path / "inner.yml", "- name: a\n  debug: {}\n  when: x\n")
    main = write(tmp_path / "main.yml", "- name: part\n  include_tasks: inner.yml\n  when: z\n  vars: {v: 1}\n"
                                        "- include_tasks: inner.yml\n  loop: [1]\n")
    part, looped = load_tasks(main)
    assert (part["include_tasks"], part["when"], part["vars"]) == ("inner.yml", "z", {"v": 1})
    assert part["block"] == [{"name": "a", "debug": {}, "when": "x"}]
    assert find_task([part], "a")["when"] == "x"
    assert looped == {"include_tasks": "inner.yml", "loop": [1]}  # (another keyword: left as it is)


def test_an_import_keyword_it_does_not_apply_is_refused(tmp_path):
    write(tmp_path / "inner.yml", "- name: a\n  debug: {}\n")
    main = write(tmp_path / "main.yml", "- import_tasks: inner.yml\n  tags: [x]\n")
    with pytest.raises(ValueError):
        load_tasks(main)


OUTER = {"name": "outer", "block": [{"name": "b", "debug": {}}], "rescue": [{"name": "c", "debug": {"msg": 1}}]}
PLAYS = [{"name": "one", "hosts": "all", "tasks": [{"name": "a", "debug": {}}, OUTER]},
         {"name": "two", "hosts": "localhost", "pre_tasks": [{"name": "d", "debug": {}}],
          "tasks": [{"name": "c", "debug": {"msg": 2}}, {"name": "a", "debug": {}}]}]


def test_a_task_found_in_any_play_and_block():
    assert find_task(PLAYS, "b") == {"name": "b", "debug": {}}
    assert find_task(PLAYS, "d")["name"] == "d"
    assert find_task(PLAYS, "a") == {"name": "a", "debug": {}}  # (the same task twice: one)
    assert find_task(PLAYS[0]["tasks"], "outer")["block"][0]["name"] == "b"
    assert find_task(PLAYS, "c")["debug"]["msg"] == 2  # (the outer one)
    assert find_task(find_play(PLAYS, "one"), "c")["debug"]["msg"] == 1
    # the same name inside a block and outside: the outer one
    tasks = [{"name": "outer", "block": [{"name": "x", "debug": {"msg": "inner"}}]}, {"name": "x", "debug": {}}]
    assert find_task(tasks, "x") == {"name": "x", "debug": {}}


@pytest.mark.parametrize("source, name", [
    ([{"name": "x", "debug": {"msg": 1}}, {"name": "x", "debug": {"msg": 2}}], "x"), (PLAYS, "none"), (PLAYS, "one")])
def test_no_task_or_two_different_ones_fail(source, name):
    with pytest.raises(AssertionError):
        find_task(source, name)


def test_a_play_by_its_name():
    assert find_play(PLAYS, "two")["hosts"] == "localhost"
    with pytest.raises(AssertionError):
        find_play(PLAYS, "three")
