from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def test_plural():
    assert out.plural(1, "node") == "1 node" and out.plural(3, "node") == "3 nodes"
    assert out.plural(2, "copy", "copies") == "2 copies"


def test_duration():
    assert [out.duration(s) for s in (45, 560, 690, 3 * 3600 + 60, 2 * 86400 + 4 * 3600)] == \
        ["45s", "9m20s", "11m30s", "3h01m", "2d04h"]
    assert out.duration(560, short=True) == "9m" and out.duration(-5) == "0s"


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


P = out.PARAGRAPH  # a blank line between two paragraphs of a block
GIB = 1024 ** 3
MIB = 1024 ** 2


@pytest.mark.parametrize("count, scale, text", [
    (0, None, "0 B"),
    (512, None, "512 B"),
    (1023, None, "1023 B"),
    (1024, None, "1.0 KiB"),
    (2048, None, "2.0 KiB"),
    (1023.97 * 1024, None, "1.0 MiB"),  # not 1024.0 KiB
    (260.6 * MIB, None, "260.6 MiB"),  # alone: the unit it reads >= 1 in
    (1000 * MIB, None, "1000.0 MiB"),
    (1024 * MIB, None, "1.0 GiB"),
    (41.2 * GIB, None, "41.2 GiB"),
    (1023.99 * GIB, None, "1.0 TiB"),
    (5 * 1024 * GIB, None, "5.0 TiB"),
    (0, 100 * GIB, "0.0 GiB"),
    (0.3 * GIB, 100 * GIB, "0.3 GiB"),  # the unit of the scale
    (52 * 1024, 100 * GIB, "<0.1 GiB"),  # never 0.0 for a non-zero size
    (5 * GIB, 0, "5.0 GiB"),  # no scale: its own unit
    (-5, None, "0 B"),
])
def test_size(count, scale, text):
    assert out.size(count, scale) == text


def test_command_full_and_minimal():
    assert out.command("cleanup", "inventory.yml", "my_cluster", ["node1", "node2"],
                       {"cassandra_cleanup_jobs": 2}) == \
        "ansible-playbook -i inventory.yml community.cassandra.cleanup -e cassandra_hosts=my_cluster" \
        " --limit node1,node2 -e cassandra_cleanup_jobs=2"
    assert out.command("status") == "ansible-playbook community.cassandra.status"  # ansible.cfg's inventory
    assert out.command("topology", ["/srv/inv/a.yml", "/srv/inv/b.yml"], cwd="/srv") == \
        "ansible-playbook -i inv/a.yml -i inv/b.yml community.cassandra.topology"
    assert out.command("x", extra={"msg": "two words"}) == \
        """ansible-playbook community.cassandra.x -e '{"msg": "two words"}'"""
    assert out.command("x", options=["--vault-id", "prod@file"], extra=["-e a=1"]) == \
        "ansible-playbook --vault-id prod@file community.cassandra.x -e a=1"


def test_todo():
    assert out.todo([]) == []
    assert out.todo(["remove node7 from the inventory", None,
                     {"text": "cleanup the nodes that gave data", "command": "ansible-playbook x"}]) == [
        "TO DO", "  1. remove node7 from the inventory", P, "  2. cleanup the nodes that gave data:",
        "     ansible-playbook x"]


# --- recap ---

def restarts(times):
    return [{"node": "node%d" % (i + 1), "outcome": "restarted", "seconds": t} for i, t in enumerate(times)]


def test_recap_grouped_with_the_slow_node_apart():
    lines = out.recap("rolling_restart", "my_cluster", restarts([109, 112, 400, 115, 118, 110]), seconds=690)
    assert lines == ["DONE  rolling_restart  my_cluster  6 restarted (11m30s)",
                     "  node1, node2, node4..node6  restarted  (1m49s..1m58s each)",
                     "  node3                       restarted  6m40s  %s slow (median 1m53s)" % out.ARROW]


def test_recap_no_outlier_under_the_factor_and_one_node():
    assert out.recap("rolling_restart", "c", restarts([100, 140]))[1:] == ["  node1, node2  restarted  (1m40s..2m20s each)"]
    assert out.recap("rolling_restart", "c", restarts([100]))[1:] == ["  node1  restarted  1m40s"]


def test_recap_failed_and_skipped():
    lines = out.recap("rolling_restart", "my_cluster", restarts([100]) + [
        {"node": "node2", "status": "failed", "reason": "not UN after 10m"},
        {"node": "node3", "status": "skipped", "reason": "after the failure"},
        {"node": "node4", "status": "skipped", "reason": "after the failure"}])
    assert lines == ["FAILED  rolling_restart  my_cluster  1 restarted, 1 failed, 2 not touched",
                     "  node1         restarted                        1m40s",
                     "  node2         FAILED",
                     "    not UN after 10m",
                     "  node3, node4  not touched (after the failure)"]


def test_recap_columns_not_stretched_by_a_long_reason():
    reason = "Cluster not healthy, nothing more is done: " + "x" * 300
    lines = out.recap("rolling_restart", "c", restarts([96]) + [{"node": "node2", "status": "failed", "reason": reason}])
    assert lines[1:] == ["  node1  restarted  1m36s", "  node2  FAILED", "    " + reason]


def test_recap_check_mode_says_would_and_nothing_changed():
    lines = out.recap("apply_config", "my_cluster", [{"node": "node%d" % i, "outcome": "applied, restarted",
                                                      "seconds": 3} for i in range(1, 5)], check=True, seconds=20,
                      extra=["  cassandra.yaml"])
    assert lines == ["CHECK  apply_config  my_cluster  would apply, restart on 4 nodes   nothing was changed",
                     "  node1..node4  would apply, restart",
                     "",
                     "  cassandra.yaml"]
    assert out.would("restarted") == "would restart" and out.would("frobbed") == "would be frobbed"
    assert out.recap("x", "c", []) == ["NOTHING TO DO  x  c  nothing to do"]
