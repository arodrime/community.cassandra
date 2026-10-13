from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The end of the node operations (summary.yml), of reset_node, and the
# preflight's stop on a setting that differs between nodes.

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import (
    cassandra_differs, cassandra_op_recap, cassandra_reset_recap)


def recap(pairs, **kwargs):
    return cassandra_op_recap([{"node": n, "result": r} for n, r in pairs], **kwargs)


@pytest.mark.parametrize("result, operation, outcome", [
    ("add done in 100s", "add_node", "added"),
    ("decommission done in 100s", "decommission_node", "decommissioned"),
    ("restart done in 100s", "rolling_restart", "restarted"),
    ("restart done with its rack in 100s", "rolling_restart", "restarted with its rack"),
    ("reboot done in 100s", "rolling_reboot", "rebooted"),
    ("replace done in 100s", "replace_node", "replaced"),
    ("move done in 100s", "move_node", "moved"),
    ("update_java done in 100s", "update_java", "Java updated"),
    ("upgrade done in 100s", "upgrade", "upgraded"),
    ("upgradesstables done in 100s", "upgrade", "SSTables upgraded"),
    ("cleanup done in 100s", "cleanup", "cleaned up"),
    ("apply_config done in 100s", "apply_config", "applied"),
])
def test_each_operation_ends_with_done(result, operation, outcome):
    assert recap([("node4", result), ("node5", result.replace("100s", "110s"))], cluster="my_cluster") == [
        "DONE  %s  my_cluster  2 %s" % (operation, outcome),
        "  node4, node5  %s  (1m40s..1m50s each)" % outcome]


@pytest.mark.parametrize("result, operation, said", [
    ("would add (--check: nothing was changed)", "add_node", "would add"),
    ("would restart (--check: nothing was changed)", "rolling_restart", "would restart"),
    ("would restart with its rack (--check: nothing was changed)", "rolling_restart", "would restart with its rack"),
    ("would decommission (--check: nothing was changed)", "decommission_node", "would decommission"),
    ("would update Java (--check: nothing was changed)", "update_java", "would update Java"),
    ("would apply the config (--check: nothing was changed)", "apply_config", "would apply the config"),
    ("would upgrade the SSTables (--check: nothing was changed)", "upgrade", "would upgrade the SSTables"),
    ("would clean up", "cleanup", "would clean up"),
])
def test_check_says_what_a_real_run_would_do(result, operation, said):
    on = " on" if len(said.split()) > 2 and said != "would clean up" else ""
    assert recap([("node1", result), ("node2", result), ("node3", result)], check=True, cluster="c") == [
        "CHECK  %s  c  %s%s 3 nodes   nothing was changed" % (operation, said, on),
        "  node1..node3  %s" % said]


def test_a_failed_node_makes_it_failed_the_others_said():
    lines = recap([("node4", "decommission done in 165s"),
                   ("node5", "decommission FAILED after 70s: the stream stopped"),
                   ("node6", "not reached"), ("node7", "not in this run (--limit)")], cluster="c")
    assert lines[0] == "FAILED  decommission_node  c  1 decommissioned, 1 failed, 2 not touched"
    assert [" ".join(line.split()) for line in lines[1:]] == ["node4 decommissioned 2m45s",
                                                              "node5 FAILED: the stream stopped",
                                                              "node6 not touched (not reached)",
                                                              "node7 not touched (left out by --limit)"]


def test_failed_under_check_too():
    lines = recap([("node1", "would restart (--check: nothing was changed)"), ("node2", "restart FAILED after 3s: JMX")],
                  check=True, cluster="c")
    assert lines[0] == "FAILED  rolling_restart  c  would restart 1 node, 1 failed   nothing was changed"


@pytest.mark.parametrize("result, reason", [
    ("already in the ring, nothing to add", "already in the ring, nothing to add"),
    ("already on 5.0.4", "already on 5.0.4"),
    ("not prepared: stopped with the others (a node failed)", "not prepared: stopped with the others (a node failed)"),
])
def test_nodes_not_touched_say_why(result, reason):
    assert recap([("node1", result)], operation="op", cluster="c") == [
        "NOTHING TO DO  op  c  1 not touched", "  node1  not touched (%s)" % reason]


@pytest.mark.parametrize("result, reason", [
    ("add refused: node4 holds data", "refused: node4 holds data"),
    ("add FAILED while preparing it: the repository can't be reached", "while preparing it: the repository can't be reached"),
    ("restart FAILED: One or more items failed", "One or more items failed"),
])
def test_refusals_and_failures(result, reason):
    assert recap([("node4", result)], cluster="c")[1] == "  node4  FAILED: %s" % reason


def test_the_operation_given_wins():
    assert recap([("n1", "decommission done in 5s")], operation="remove_datacenter")[0] == \
        "DONE  remove_datacenter  1 decommissioned"


NODES = [{"node": "node4", "delete": ["a", "b"], "stop": True, "disable": True},
         {"node": "node5", "delete": ["c"], "stop": False, "disable": True},
         {"node": "node6", "delete": [], "stop": False, "disable": False}]
ADD = "ansible-playbook community.cassandra.add_node -e cassandra_target_nodes=node4,node5"


def test_reset_node_done_then_add_them():
    assert cassandra_reset_recap(NODES, cluster="my_cluster", add_command=ADD) == [
        "DONE  reset_node  my_cluster  node4, node5 emptied (3 entries deleted), Cassandra stopped on node4,"
        " Cassandra disabled on node4, node5",
        "  node6  nothing to do (empty, Cassandra stopped and disabled)",
        "",
        "TO DO",
        "  1. add them back to the cluster:",
        "     " + ADD]


def test_reset_node_check_and_nothing_to_do():
    assert cassandra_reset_recap(NODES[:1], check=True, cluster="c", add_command=ADD) == [
        "CHECK  reset_node  c  would empty node4 (2 entries), stop and disable Cassandra on node4   nothing was changed"]
    assert cassandra_reset_recap([dict(NODES[0], delete=[])], cluster="c", add_command=ADD)[0] == \
        "DONE  reset_node  c  Cassandra stopped and disabled on node4"
    assert cassandra_reset_recap([dict(NODES[0], delete=[])], check=True, cluster="c")[0] == \
        "CHECK  reset_node  c  would stop and disable Cassandra on node4   nothing was changed"
    # only what its plan does: already stopped, not stopped again
    assert cassandra_reset_recap([dict(NODES[1], delete=[])], check=True, cluster="c")[0] == \
        "CHECK  reset_node  c  would disable Cassandra on node5   nothing was changed"
    assert cassandra_reset_recap([NODES[1]], cluster="c")[0] == \
        "DONE  reset_node  c  node5 emptied (1 entry deleted), Cassandra disabled on node5"
    # a node with nothing to delete is not said emptied
    assert cassandra_reset_recap([dict(NODES[0], delete=[]), NODES[1]], check=True, cluster="c")[0] == (
        "CHECK  reset_node  c  would empty node5 (1 entry), stop Cassandra on node4, disable Cassandra on node4, node5"
        "   nothing was changed")
    assert cassandra_reset_recap(NODES[2:], cluster="c") == [
        "NOTHING TO DO  reset_node  c  node6: empty, Cassandra stopped and disabled"]
    assert cassandra_reset_recap(NODES[:1], cluster="c", add_command=ADD)[-2] == "  1. add it back to the cluster:"


def test_a_setting_that_differs_says_where_to_fix_it():
    msg = cassandra_differs({"node1": "16", "node2": "16", "node3": "16", "node4": "256"}, "cassandra_num_tokens", "my_cluster")
    assert msg == ("cassandra_num_tokens is not the same on every node of my_cluster (node1..node3: 16; node4: 256):"
                   " every node needs the same. Set it once in group_vars/my_cluster, and remove it from host_vars/node4."
                   " Nothing was changed.")
    assert "{" not in msg and "_cassandra" not in msg


def test_the_seeds_that_differ_named_by_their_variable():
    msg = cassandra_differs({"node1": ["10.0.0.1", "10.0.0.2"], "node2": ["10.0.0.1", "10.0.0.2"], "node3": ["10.0.0.3"]},
                            "_cassandra_service_seeds", "my_cluster")
    assert msg.startswith("The seed list (cassandra_seeds) is not the same on every node of my_cluster"
                          " (node1, node2: 10.0.0.1, 10.0.0.2; node3: 10.0.0.3)")
    assert "_cassandra_service_seeds" not in msg and "[" not in msg and "host_vars/node3" in msg


def test_many_hosts_that_differ_are_cut():
    values = dict(("node%d" % i, "16" if i < 8 else "256") for i in range(1, 13))
    values["node1"] = None
    msg = cassandra_differs(values, "cassandra_num_tokens")
    assert "node1: (none)" in msg and "the cluster's group_vars" in msg and ", ..." in msg


def test_the_operation_from_the_node_action_when_no_node_did_anything():
    assert recap([("node4", "already in the ring, nothing to add")], action="add", cluster="c") == [
        "NOTHING TO DO  add_node  c  1 not touched", "  node4  not touched (already in the ring, nothing to add)"]
    assert recap([("n1", "not reached")], action="restart")[0] == "NOTHING TO DO  rolling_restart  1 not touched"


def test_host_vars_in_natural_order():
    values = {"node1": "16", "node2": "16", "node3": "16", "node10": "8", "node9": "8"}
    assert "host_vars/node9, host_vars/node10." in cassandra_differs(values, "cassandra_num_tokens", "c")


def test_nodes_done_earlier_or_stopped_midway_are_not_said_untouched():
    lines = recap([("n1", "apply_config skipped, done in the interrupted run"),
                   ("n2", "apply_config not finished: the run stopped at another node done at the same time"),
                   ("n3", "apply_config FAILED after 3s: x")], cluster="c")
    assert lines[0] == ("FAILED  apply_config  c  1 done in the interrupted run, 1 not finished (the run stopped at another"
                        " node done at the same time), 1 failed")
    assert "not touched" not in "\n".join(lines)


def test_none_and_empty_differ_as_the_assert_sees_them():
    msg = cassandra_differs({"node1": None, "node2": "", "node3": ""}, "cassandra_initial_token", "c")
    assert msg.startswith('cassandra_initial_token is not the same on every node of c (node2, node3: ""; node1: (none)):')
