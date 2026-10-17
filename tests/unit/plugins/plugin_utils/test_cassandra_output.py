from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def test_plural():
    assert out.plural(1, "node") == "1 node" and out.plural(3, "node") == "3 nodes"
    assert out.plural(2, "copy", "copies") == "2 copies"


@pytest.mark.parametrize("names, text", [
    ([], ""),
    (["node1"], "node1"),
    (["node1", "node2"], "node1, node2"),  # 2 listed
    (["node1", "node2", "node3"], "node1..node3"),  # a range from 3
    (["node3", "node1", "node2", "node5"], "node1..node3, node5"),  # sorted, a gap breaks the range
    (["node1", "node2", "node4", "node5"], "node1, node2, node4, node5"),  # never node1..node5
    (["node1", "node2", "node3", "node5", "node7", "node8"], "6 nodes: node1..node3, node5, node7, node8"),
    (["node%d" % i for i in range(1, 6)], "node1..node5"),  # 5: no count
    (["node9", "node10", "node11"], "node9..node11"),
    (["node09", "node10", "node11"], "node09..node11"),
    (["node008", "node009", "node010"], "node008..node010"),
    (["n1.dc1", "n2.dc1", "n3.dc1"], "n1.dc1..n3.dc1"),
    (["dc1-n1", "dc1-n2", "dc2-n3"], "dc1-n1, dc1-n2, dc2-n3"),  # another number changes
    (["a", "b", "c"], "a, b, c"),
    (["node1", "node1", "node2"], "node1, node2"),
])
def test_nodes(names, text):
    assert out.nodes(names) == text


def test_nodes_order_and_full():
    assert out.nodes(["node3", "node1", "node2"], keep_order=True) == "node3, node1, node2"
    assert out.nodes(["node4", "node5", "node6", "node1"], keep_order=True) == "node4..node6, node1"
    names = ["node%d" % i for i in range(1, 8)]
    assert out.nodes(names, full=True) == ", ".join(names)
    assert out.full_list(["node10", "node2"]) == "node2, node10"


def test_one_mark():
    assert out.warning("swap left on") == ["WARNING - swap left on"]
    assert out.warning("WARNING - swap left on") == ["WARNING - swap left on"]  # marked already: kept
    # a longer one: its next lines indented under it, those indented already kept as they are
    assert out.warning("a:\nb\n    c") == ["WARNING - a:", "  b", "    c"]
    assert out.warning(["a:", "b"]) == ["WARNING - a:", "  b"]
    assert out.warning("") == []
    assert out.is_warning("  WARNING - x") and not out.is_warning("NOTE  x") and not out.is_warning("not a WARNING")
    assert out.warning_text("WARNING - data disks: x") == "data disks: x"
    assert out.warning_text("plain") == "plain"


def test_warnings_once_a_blank_line_apart():
    lines = out.warning_lines(["a", "b:\n  b1\n  b2", "a", "c"])
    assert lines == ["WARNING - a", out.PARAGRAPH, "WARNING - b:", "  b1", "  b2", out.PARAGRAPH, "WARNING - c"]
    seen = set()
    assert out.warning_lines(["a"], seen) == ["WARNING - a"]
    assert out.warning_lines(["a", "b"], seen) == ["WARNING - b"]  # said already: left out
