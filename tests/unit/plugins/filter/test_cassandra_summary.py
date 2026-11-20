from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The end of the node operations (summary.yml), of reset_node, and the
# preflight's stop on a setting that differs between nodes.

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import (
    cassandra_differs, cassandra_op_recap, cassandra_reset_recap, cassandra_stop_block)


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
        "  node4, node5  %s  in 1m40s..1m50s each" % outcome]


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
    assert [" ".join(line.split()) for line in lines[1:]] == ["node4 decommissioned in 2m45s",
                                                              "node5 FAILED", "the stream stopped",
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
    ("add FAILED while preparing it: the repository can't be reached", "while preparing it: the repository can't be reached"),
    ("restart FAILED: One or more items failed", "One or more items failed"),
])
def test_failures(result, reason):
    assert recap([("node4", result)], cluster="c")[1:] == ["  node4  FAILED", "    %s" % reason]


def test_a_refusal_with_nothing_done_is_refused_not_failed():
    # (UX audit B7) a check before any change: REFUSED, nothing was changed, the reason once
    assert recap([("node4", "replace refused: node4 holds data")], cluster="c") == [
        "REFUSED  replace_node  c  nothing was changed", "  node4  REFUSED", "    node4 holds data"]
    # a node done before it: something changed, FAILED
    lines = recap([("node3", "add done in 5s"), ("node4", "add refused: node4 holds data")], cluster="c")
    assert lines[0].startswith("FAILED") and "  node4  FAILED" in lines


def test_the_operation_given_wins():
    assert recap([("n1", "decommission done in 5s")], operation="remove_datacenter")[0] == \
        "DONE  remove_datacenter  1 decommissioned"
    assert recap([("n1", "decommission done in 5s")], operation="remove_datacenter", seconds=65)[0] == \
        "DONE  remove_datacenter  1 decommissioned  1m05s"


RESULTS = [("node1", "restart done in 110s"), ("node2", "restart FAILED after 629s: cluster health check failed"),
           ("node3", "not run yet"), ("node4", "not reached"), ("node5", "not in this run (--limit)")]
HEALTH = ["node4 (10.0.0.4, r1) is DN, seen from node1 and node2",
          "waited 10m29s (cassandra_service_health_timeout: 600s)",
          "WAY OUT  bring node4 back to UN (its host and/or Cassandra), then resume",
          "IF node4 IS LOST FOR GOOD",
          "replace it: NEW_NODE (a blank host) in prod, node4 out of the inventory",
          "  ansible-playbook -i hosts.yml community.cassandra.replace_node -e cassandra_hosts=prod"
          " -e cassandra_target_nodes=NEW_NODE -e cassandra_replace_address=10.0.0.4"]


def stop(failure, stage="", **kwargs):
    return cassandra_stop_block([{"node": n, "result": r, "stage": stage if n == "node2" else ""} for n, r in RESULTS],
                                "node2", failure, cluster="prod", action="restart", **kwargs)


@pytest.mark.parametrize("stage, row", [
    # (UX audit A1) the row of the node it stopped at says what was done to it, never "not done" for a node touched;
    # "restarted" only once its restart was confirmed (the node NORMAL again)
    ("after", "  node2  restarted; cluster health check failed"),
    ("unconfirmed", "  node2  restart run, not waited for; cluster health check failed"),
    ("during", "  node2  restart started, not finished; cluster health check failed"),
    ("before", "  node2  not touched; cluster health check failed"),
    ("", "  node2  not done                           cluster health check failed")])
def test_the_stopped_node_s_row_says_what_was_done_to_it(stage, row):
    assert stop(HEALTH, stage=stage)[4] == row


def test_under_check_the_stopped_node_s_row_says_nothing_changed():
    # --check restarted nothing, whatever the stage reached
    for stage in ("before", "during", "after", "unconfirmed"):
        assert stop(HEALTH, stage=stage, check=True)[4] == "  node2  nothing changed (--check); cluster health check failed"


def test_a_stop_on_purpose_says_the_cause_the_table_and_the_way_out_once():
    # (UX audit A1) the verdict first, the cause on its own lines, the table, the way out in numbered steps, the
    # commands cut between their arguments: 79 columns at most
    lines = stop(HEALTH, stage="after", resume=True, progress_file="/inv/.cassandra_progress/x", inventory=["/inv/hosts.yml"],
                 hosts="prod", cwd="/inv")
    assert lines == [
        "STOPPED  rolling_restart  prod  at node2",
        "  Why:  node4 (10.0.0.4, r1) is DN, seen from node1 and node2",
        "        waited 10m29s (cassandra_service_health_timeout: 600s)",
        "  node1  restarted                          in 1m50s",
        "  node2  restarted; cluster health check failed",
        "  node3  not touched",
        "  node4  not touched (not reached)",
        "  node5  not touched (left out by --limit)",
        "",  # (review 3) the way out apart from the table
        "WAY OUT",
        "  1. bring node4 back to UN (its host and/or Cassandra)",
        "  2. resume (node2 restarts again):",  # (B1, review 4) not in the progress file: restarted again
        "     ansible-playbook -i hosts.yml community.cassandra.rolling_restart \\",
        "       -e cassandra_hosts=prod -e cassandra_rolling_resume=true",
        "     progress: .cassandra_progress/x",  # (under the command it is for)
        "",  # (review 3) a node lost for good: a block of its own, no next step of the resume
        "IF node4 IS LOST FOR GOOD",
        "  replace it: NEW_NODE (a blank host) in prod, node4 out of the inventory",
        "    ansible-playbook -i hosts.yml community.cassandra.replace_node \\",
        "      -e cassandra_hosts=prod -e cassandra_target_nodes=NEW_NODE \\",
        "      -e cassandra_replace_address=10.0.0.4"]
    assert max(len(line) for line in lines) <= 79


def test_a_long_cause_is_wrapped_under_its_why():
    lines = stop(["x " * 60 + "end", "WAY OUT  fix it"], resume=False)
    assert lines[1].startswith("  Why:  x x") and lines[2].startswith("        x x")
    assert max(len(line) for line in lines) <= 79


def test_the_hint_stays_under_the_failures_own_way_out():
    # add_node has no resume: the health check's "then resume" becomes "then:" and the hint says how
    lines = stop(HEALTH, resume=False, hint="run add_node again, the same command: the nodes already in the ring are only checked")
    assert lines[-11:] == [
        "",
        "WAY OUT",
        "  bring node4 back to UN (its host and/or Cassandra), then:",
        "  run add_node again, the same command: the nodes already in the ring are only",
        "  checked",
        "",
        "IF node4 IS LOST FOR GOOD",
        "  replace it: NEW_NODE (a blank host) in prod, node4 out of the inventory",
        "    ansible-playbook -i hosts.yml community.cassandra.replace_node \\",
        "      -e cassandra_hosts=prod -e cassandra_target_nodes=NEW_NODE \\",
        "      -e cassandra_replace_address=10.0.0.4"]


def test_a_failed_command_is_named_with_the_last_line_of_its_stderr():
    # Ansible's generic words for a failed command: the task's name and its stderr say what happened
    lines = stop("non-zero return code", resume=True, task="Find the running Cassandra JVM",
                 stderr="pgrep: no such option\nusage: pgrep [options]")
    assert lines[:3] == ["FAILED  rolling_restart  prod  at node2",
                         "  Why:  Find the running Cassandra JVM: non-zero return code (usage: pgrep",
                         "        [options])"]
    # a message of its own: the task's name only
    assert stop("Timeout waiting for NORMAL", resume=False, task="Wait")[1] == "  Why:  Wait: Timeout waiting for NORMAL"


def test_a_task_that_failed_is_failed_with_the_hint_or_the_resume():
    # not one of our stops (no WAY OUT line): FAILED, the way out is the hint given, else the resume, else the status
    lines = stop("Timeout waiting for NORMAL", resume=False, hint="Once fixed, run move_node again.")
    assert lines[:2] == ["FAILED  rolling_restart  prod  at node2", "  Why:  Timeout waiting for NORMAL"]
    assert lines[-2:] == ["WAY OUT", "  Once fixed, run move_node again."]
    lines = stop("boom", resume=True)
    assert lines[-5:] == ["WAY OUT", "  1. fix it", "  2. resume:",
                          "     ansible-playbook community.cassandra.rolling_restart \\",
                          "       -e cassandra_rolling_resume=true"]
    status = "  fix it, then check where the cluster stands (the status playbook) before"
    assert stop("boom", resume=False)[-2:] == [status, "    running it again"]
    # no table to say it (no node's result): which nodes were touched said on a line
    assert cassandra_stop_block([], "node2", "boom", resume=False)[:3] == [
        "FAILED  at node2", "  Why:  boom", "  No node after it was touched."]
    assert cassandra_stop_block([], "node2", "boom", resume=False, batch=True)[2] == (
        "  The nodes done at the same time were not finished.")
    # a cause said twice (the same check failed from two nodes): said once
    twice = "the ring has 2 nodes, the inventory 3 (seen from node1)"
    assert cassandra_stop_block([], "node2", [twice, twice, "other"], resume=False)[:4] == [
        "FAILED  at node2", "  Why:  " + twice, "        other", "  No node after it was touched."]


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


def test_under_check_no_resume_nor_progress_file():
    # --check wrote no progress file: the way out is the same check again, no .done file named
    lines = stop("boom", resume=True, check=True, progress_file="/inv/.cassandra_progress/x", cwd="/inv")
    assert lines[-3:] == ["  1. fix it", "  2. check again:", "     ansible-playbook community.cassandra.rolling_restart --check"]
    assert not any("progress:" in line or "cassandra_rolling_resume" in line for line in lines)
    # a stop's own way out ending "then resume"
    lines = stop(HEALTH[:3], resume=True, check=True, progress_file="/inv/.cassandra_progress/x", cwd="/inv")
    assert lines[-4:] == ["WAY OUT", "  1. bring node4 back to UN (its host and/or Cassandra)", "  2. check again:",
                          "     ansible-playbook community.cassandra.rolling_restart --check"]


def test_the_resume_says_which_nodes_it_restarts_again():
    # (review 4) a node touched and not in the progress file is restarted again by the resume; one not touched,
    # or a resume of another action, says nothing; --check restarted nothing
    for stage, said in (("after", "  2. resume (node2 restarts again):"), ("during", "  2. resume (node2 restarts again):"),
                        ("unconfirmed", "  2. resume (node2 restarts again):"), ("before", "  2. resume:")):
        assert said in stop(HEALTH, stage=stage, resume=True), stage
    assert "  2. check again:" in stop(HEALTH, stage="after", resume=True, check=True)
    # a rack: the nodes not finished too
    rack = cassandra_stop_block(
        [{"node": "n1", "result": "restart FAILED: not back in Mode NORMAL in time", "stage": "during"},
         {"node": "n2", "result": "restart not finished: the rack stopped at n1", "stage": "during"},
         {"node": "n3", "result": "not run yet"}], "the rack n1, n2", "boom", action="restart", resume=True, batch=True)
    assert "  2. resume (n1, n2 restart again):" in rack


def test_a_resumed_node_s_partial_time_is_never_compared():
    # the node a resumed run found drained: only started by this run, its time no measure of a restart
    results = [{"node": "n1", "result": "restart done (resumed) in 15s"}, {"node": "n2", "result": "restart done in 36s"},
               {"node": "n3", "result": "restart done in 37s"}]
    lines = cassandra_op_recap(results, cluster="c", operation="rolling_restart")
    assert not [line for line in lines if "slow" in line], lines
    assert lines[1:] == ["  n1..n3  restarted  in 15s..37s each"]
    # nor slow itself, against the others
    results = [{"node": "n1", "result": "restart done (resumed) in 90s"}, {"node": "n2", "result": "restart done in 36s"},
               {"node": "n3", "result": "restart done in 37s"}]
    assert not [line for line in cassandra_op_recap(results, cluster="c", operation="rolling_restart") if "slow" in line]


def test_an_apply_config_row_says_only_what_was_observed_of_its_config():
    # "config write started, not finished" was said when the config was fully written (a restart that
    # failed after), or not written at all (a release check before it): the stage of the write as noted
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import _failed_row, outcome
    assert _failed_row("apply_config", "during", "release check failed") == "config not written; release check failed"
    assert _failed_row("apply_config", "writing", "config write failed") == (
        "config write started, not finished; config write failed")
    assert _failed_row("apply_config", "written", "start failed") == "config written; start failed"
    assert _failed_row("apply_config", "written", "x", check=True) == "nothing changed (--check); x"
    assert _failed_row("update_java", "during", "x") == "Java update started, not finished; x"
    # a resume does it again
    for stage in ("during", "writing", "written"):
        assert outcome("n1", "apply_config FAILED after 3s: x", stage)[0]["touched"] is True
    assert outcome("n1", "apply_config FAILED after 3s: x", "before")[0]["touched"] is False
