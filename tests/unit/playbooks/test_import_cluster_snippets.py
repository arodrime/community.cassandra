from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the lines a node adds at the end of logback.xml's <configuration> or of cassandra-env.sh written
# in a template piece next to the inventory (cassandra_config_template_snippets), not in its YAML; the self-check
# renders the snippets about to be written; a re-import owns and rewrites them.

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_inventory_files, cassandra_inventory_generated, cassandra_inventory_layout, cassandra_inventory_layout_over,
    cassandra_inventory_leftovers, written_for)
from ansible_collections.community.cassandra.tests.unit.playbooks.test_import_cluster_end_lines import (
    imported, self_check, with_env)


def written(layout):
    return dict((f["path"], f) for f in cassandra_inventory_files(layout))


def test_snippets_per_node_when_their_lines_differ():
    a, na = imported(with_env("export A=1"))
    b, nb = imported(with_env("export B=1"), "n2", "10.0.0.2")
    files_out = written(cassandra_inventory_layout([na, nb], "c"))
    assert files_out["host_vars/n1/cassandra-env.sh.snippet.j2"]["content"] == "export A=1\n"
    assert files_out["host_vars/n2/cassandra-env.sh.snippet.j2"]["content"] == "export B=1\n"
    assert yaml.safe_load(files_out["host_vars/n2/main.yml"]["content"])["cassandra_config_template_snippets"] == {
        "cassandra-env.sh": "{{ inventory_dir }}/host_vars/n2/cassandra-env.sh.snippet.j2"}
    assert "group_vars/c/cassandra-env.sh.snippet.j2" not in files_out


def test_both_files_one_dict_per_node():
    # logback's the same on every node (group), cassandra-env.sh's per node: each host's dict names both
    files = with_env("export A=1")
    files["logback.xml"] = files["logback.xml"].replace("</configuration>", '  <logger name="x" level="WARN" additivity="false"/>\n'
                                                                            "</configuration>")
    other = with_env("export B=1")
    other["logback.xml"] = files["logback.xml"]
    a, na = imported(files)
    b, nb = imported(other, "n2", "10.0.0.2")
    layout = cassandra_inventory_layout([na, nb], "c")
    files_out = written(layout)
    assert yaml.safe_load(files_out["host_vars/n1/main.yml"]["content"])["cassandra_config_template_snippets"] == {
        "cassandra-env.sh": "{{ inventory_dir }}/host_vars/n1/cassandra-env.sh.snippet.j2",
        "logback.xml": "{{ inventory_dir }}/group_vars/c/logback.xml.snippet.j2"}
    assert self_check(files, layout)["differences"] == [] and self_check(other, layout, "n2")["differences"] == []


def test_the_snippets_first_line_is_a_jinja_comment_a_reimport_owns():
    text = cassandra_inventory_generated("export A=1\n", "c", "group_vars/c/cassandra-env.sh.snippet.j2")
    assert text.startswith("{# Written by community.cassandra.import_cluster for c: ") and text.endswith(
        " #}\nexport A=1\n")
    assert written_for(text) == "c"
    # an earlier import's snippet this one no longer writes: removed; one it writes again: not a conflict
    paths = ["c.yml", "group_vars/c/main.yml", "group_vars/c/logback.xml.snippet.j2",
             "host_vars/n1/cassandra-env.sh.snippet.j2", "group_vars/c/notes.j2"]
    read = dict((p, cassandra_inventory_generated("x\n", "c", p)) for p in paths[1:4])
    read["c.yml"] = cassandra_inventory_generated("all:\n  children:\n    c:\n      hosts:\n        n1:\n", "c")
    read["group_vars/c/notes.j2"] = "mine\n"
    out = cassandra_inventory_leftovers(paths, read, ["c.yml", "group_vars/c/main.yml",
                                                      "group_vars/c/logback.xml.snippet.j2"], "c")
    assert out["conflicts"] == [] and out["replaced"] == []
    assert out["stale"] == ["host_vars/n1/cassandra-env.sh.snippet.j2"]
    assert "group_vars/c/notes.j2" in out["kept"]  # (yours: no first line of the import)


def test_your_own_snippets_stay_in_the_dict_the_import_writes(tmp_path):
    # (audit) Ansible replaces a dict whole: yours (group_vars/all) kept in the one the import writes
    (tmp_path / "env.j2").write_text("export A=1\n")
    files = with_env("export A=1")
    files["logback.xml"] = files["logback.xml"].replace("</configuration>", '  <logger name="x" level="WARN" additivity="false"/>\n'
                                                                            "</configuration>")
    out, n = imported(files)
    yours = {"path": "group_vars/all/t.yml",
             "content": "cassandra_config_template_snippets:\n  cassandra-env.sh: %s\n" % (tmp_path / "env.j2")}
    layout = cassandra_inventory_layout_over([n], "c", [yours])
    main = yaml.safe_load(written(layout)["group_vars/c/main.yml"]["content"])
    assert main["cassandra_config_template_snippets"] == {
        "cassandra-env.sh": str(tmp_path / "env.j2"), "logback.xml": "{{ inventory_dir }}/group_vars/c/logback.xml.snippet.j2"}
    assert "group_vars/c/cassandra-env.sh.snippet.j2" not in written(layout)  # (your snippet renders them)
    assert self_check(files, layout, extra_files=[yours])["differences"] == []
