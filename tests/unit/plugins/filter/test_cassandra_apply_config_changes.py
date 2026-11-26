from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config's WILL CHANGE / WOULD CHANGE view (2026-11-25, owner's request): the nodes with the same changes
# together, each file with + added, - removed, ~ kept at another line, a -> b a value changed, 79 columns.

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config_changes import (
    DIFF_LINES, cassandra_apply_config_changes, cassandra_apply_config_diff_text)

from .test_cassandra_apply_config import ENV, JVM, YAML, Node

JVM17 = "-XX:+UseG1GC\n-Xms512M\n-Xmx512M\n-Xss256k\n"
# the role's rendering of the production shape before rc3: the heap moved to jvm-server.options, -Xss1024M at its end
NODE_JVM = JVM.replace("-Xss256k", "-Xss1024M")
ROLE_JVM = JVM + "-Xms512M\n-Xmx512M\n-Xss1024M\n"
ROLE_JVM17 = "-XX:+UseG1GC\n-Xss256k\n"


def changes(*nodes, **kwargs):
    kwargs.setdefault("width", 79)
    return cassandra_apply_config_changes([n.data() for n in nodes], **kwargs)


def owners_shape(tmp_path, name):
    return Node(tmp_path, name, files={"jvm-server.options": NODE_JVM, "jvm17-server.options": JVM17,
                                       "cassandra-env.sh": "# a comment\n" + ENV},
                targets={"jvm-server.options": ROLE_JVM, "jvm17-server.options": ROLE_JVM17,
                         "cassandra-env.sh": ENV})


def test_the_production_shape_one_group_moved_lines_one_row_each(tmp_path):
    lines = changes(*[owners_shape(tmp_path, "node%02d" % i) for i in range(1, 25)], diff_file="reports/c/x.diff")
    assert lines == [
        "WILL CHANGE  the same on all 24 nodes",
        "  cassandra-env.sh  rewritten: comments and layout only",
        "  jvm-server.options  rewritten",
        "    + -Xss256k   added (jvm17-server.options has it too)",
        "    + -Xms512M   moved here from jvm17-server.options",
        "    + -Xmx512M   moved here from jvm17-server.options",
        "    ~ -Xss1024M  kept, at another line",
        "  jvm17-server.options  rewritten",
        "    - -Xms512M  moved to jvm-server.options",
        "    - -Xmx512M  moved to jvm-server.options",
        "real diffs: reports/c/x.diff"]
    assert "" not in lines  # (the ops callback draws a rule at a blank line)
    assert all(len(line) <= 79 for line in lines)
    assert changes(owners_shape(tmp_path, "n1"), check=True)[0] == "WOULD CHANGE"  # (one node: named below)


def test_groups_of_nodes_and_a_changed_value(tmp_path):
    up = [Node(tmp_path, "node%d" % i) for i in (1, 2)]
    reads = [Node(tmp_path, "node%d" % i, files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 48")})
             for i in (3, 4, 5)]
    other = [Node(tmp_path, "node6", files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 64")})]
    lines = changes(*(up + reads + other), check=True)
    # (the same files on node3..node6: one group, the rows of the nodes that differ apart)
    assert lines == [
        "WOULD CHANGE",
        "on node3..node6",
        "  cassandra.yaml  rewritten",
        "   on node3..node5:",
        "    concurrent_reads  48 -> 32",
        "   on node6:",
        "    concurrent_reads  64 -> 32"]
    assert changes(*up) == []


def test_diff_under_each_file_once_per_group_and_cut(tmp_path):
    nodes = [owners_shape(tmp_path, "node%d" % i) for i in (1, 2, 3)]
    lines = changes(*nodes, show_diff=True, diff_file="reports/c/x.diff")
    at = lines.index("  jvm-server.options  rewritten")
    assert lines[at + 5] == "    same diff on all 3 nodes:"
    assert lines[at + 6].startswith("      @@ ")
    assert not [line for line in lines if line.startswith("real diffs")]
    long_yaml = "".join("k%d: %d\n" % (i, i) for i in range(40))
    node = Node(tmp_path, "n1", files={"cassandra.yaml": long_yaml},
                targets={"cassandra.yaml": long_yaml.replace(": ", ":  ").replace("k", "key_")})
    lines = changes(node, show_diff=True, diff_file="reports/c/x.diff")
    assert lines[-1].startswith("      (") and lines[-1].endswith(" more lines: see reports/c/x.diff)")
    assert len([line for line in lines if line.startswith("      ") and not line.startswith("      (")]) == DIFF_LINES


def test_the_diff_file_has_every_diff_once_per_group(tmp_path):
    nodes = [owners_shape(tmp_path, "node%d" % i) for i in (1, 2)] + [Node(tmp_path, "node3")]
    text = cassandra_apply_config_diff_text([n.data() for n in nodes], "c", check=True)
    assert text.startswith("apply_config --check c: the files' diffs (what a real run would write), secrets masked")
    assert text.count("== jvm-server.options  on node1, node2") == 1 and "node3" not in text
    assert cassandra_apply_config_diff_text([Node(tmp_path, "n").data()], "c") == ""


def test_header_owner_and_mode(tmp_path):
    node = Node(tmp_path, "n1", perms=[{"item": "/etc/cassandra/conf/cassandra-env.sh (owner:group mode)",
                                        "before": "root:root 0640", "after": "cassandra:cassandra 0640"}])
    node.items.append({"item": "/etc/cassandra/conf/cassandra.yaml", "header": True})
    lines = changes(node)
    assert lines == [
        "WILL CHANGE",
        "on n1",
        "  cassandra-env.sh",
        "    owner:group mode  root:root 0640 -> cassandra:cassandra 0640",
        '  header "# Managed by Ansible" added (a comment)',
        "    cassandra.yaml"]


def test_long_node_names_fit_in_79_columns(tmp_path):
    # (2026-11-25 audit) a group's heading and the diff's "same diff on" line listed every name on one line
    names = ["cass-prod-dc1-rack%d-node%02d.example.internal" % (i % 3, i) for i in range(0, 24, 2)]
    reads = [Node(tmp_path, n, files={"cassandra.yaml": YAML.replace("reads: 32", "reads: 48")}) for n in names]
    files = {"cassandra.yaml": YAML.replace("reads: 32", "reads: 64"), "jvm-server.options": NODE_JVM}
    others = [Node(tmp_path, "other%d" % i, files=files) for i in (1, 2)]
    path = "reports/a-long-cluster-group-name/apply_config-check-2026-11-25-120000.diff"
    for show_diff in (False, True):
        lines = changes(*(reads + others), show_diff=show_diff, diff_file=path)
        assert lines[1].startswith("on 12 nodes: cass-prod-dc1-rack0-node00.example.internal,"), lines
        assert [line for line in lines if len(line) > 79] == [], lines


def test_no_diff_file_named_when_none_is_written(tmp_path):
    # (audit 2026-11-25) header lines only, the usual first run after an import: no diff, no .diff file written,
    # none named
    node = Node(tmp_path, "n1")
    node.items.append({"item": "/etc/cassandra/conf/cassandra.yaml", "header": True})
    lines = changes(node, diff_file="reports/c/apply_config-2026-11-25-120000.diff")
    assert lines[-1] == "    cassandra.yaml" and not [line for line in lines if "real diffs" in line]
    assert cassandra_apply_config_diff_text([node.data()], "c") == ""


def test_a_long_diff_line_is_cut_to_79_columns(tmp_path):
    # (audit 2026-11-25, owner's rule) a diff line on the screen fits in 79 columns: cut, its next parts on lines
    # starting with "|" where a diff line has its +, - or space (round 2: a mark at the end would read as a shell
    # line's own continuation " \"); the .diff file has it whole
    long_option = "-Dcom.sun.management.jmxremote.password.file=/etc/cassandra/a/very/long/path/to/jmxremote.password"
    node = Node(tmp_path, "n1", files={"jvm-server.options": JVM + long_option + " \\\n"})
    lines = changes(node, show_diff=True)
    assert [line for line in lines if len(line) > 79] == [], lines
    at = next(i for i, line in enumerate(lines) if line.startswith("      -" + long_option[:30]))
    assert lines[at + 1].startswith("      |")
    assert (lines[at][len("      "):] + lines[at + 1][len("      |"):]).endswith(long_option + " \\")
    assert ("-" + long_option + " \\") in cassandra_apply_config_diff_text([node.data()], "c").split("\n")


def test_the_diff_file_named_in_79_columns_or_not_at_all(tmp_path):
    # (audit round 3) a long path of the .diff file cut after a /, never over 79 columns; without a file (its write
    # failed), none named: the lines over DIFF_LINES "not shown"
    long_jvm = JVM + "".join("-Dx%d=%d\n" % (i, i) for i in range(DIFF_LINES))
    node = Node(tmp_path, "n1", files={"jvm-server.options": long_jvm})
    path = "/srv/ansible/inventories/production-cluster-eu/reports/prod-eu/apply_config-check-2026-11-25-120000.diff"
    for show_diff in (False, True):
        lines = changes(node, show_diff=show_diff, diff_file=path)
        assert [line for line in lines if len(line) > 79] == [], lines
        assert "".join(line.strip() for line in lines).count(path) == 1, lines
        unnamed = changes(node, show_diff=show_diff)
        assert not [line for line in unnamed if "real diffs" in line or ".diff" in line], unnamed
    assert [line for line in changes(node, show_diff=True) if "more lines" in line][0].endswith("more lines not shown)")
