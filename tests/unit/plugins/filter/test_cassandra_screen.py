from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import (
    cassandra_apply_config_changes, cassandra_apply_config_recap, cassandra_decommission_screen, cassandra_reset_warnings, cassandra_screen)

SPEC = {
    "operation": "stop_rack", "cluster": "Orders", "version": "4.1.5", "summary": "stop dc1 / rack2",
    "intro": ["Stops Cassandra on every node of the rack at once.", ""],
    "blocks": [{"title": "node2  10.0.0.2", "lines": ["drained, then stopped", {"pre": ["  a   b", "  c   d"]}]},
               {"title": "node5  10.0.0.5"}],
    "after": ["They stay down until start_rack."],
    "warnings": [
        {"label": "replication", "each": ["orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails",
                                          "orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails"]},
        {"label": "replicas", "text": ["forced although:", "orders loses 2 replicas", "events loses 2 replicas"]},
        {"label": "empty", "text": ""},
        {"label": "data loss", "text": "everything is deleted.", "real_run": True},
    ],
}
SESSION = "this run is not inside tmux or screen: if the SSH session to this machine drops, the run stops."

REAL = """\
stop_rack on cluster 'Orders' (Cassandra 4.1.5): stop dc1 / rack2

Stops Cassandra on every node of the rack at once.

  node2  10.0.0.2
    drained, then stopped
      a   b
      c   d

  node5  10.0.0.5

They stay down until start_rack.

WARNING - replication: orders has 2 replicas in dc1: with one down, (LOCAL_)QUORUM fails
WARNING - replicas: forced although:
  - orders loses 2 replicas
  - events loses 2 replicas
WARNING - data loss: everything is deleted.
WARNING - session: this run is not inside tmux or screen: if the SSH session to this machine drops,
  the run stops."""


def test_layout():
    assert cassandra_screen(SPEC, session=SESSION) == REAL


def test_check_names_the_real_run_warnings_on_one_line():
    # the warnings together, then in the question's place what --check means, one blank line apart
    expected = REAL.split("\nWARNING - data loss:", maxsplit=1)[0] + "\n(A real run would also warn about: data loss, session.)" + \
        "\n\n--check: nothing will be changed (the plan only, no question)."
    assert cassandra_screen(SPEC, check=True, session=SESSION) == expected


def test_no_question():
    lines = cassandra_screen(SPEC, asks=False).split("\n")
    assert lines[-2:] == ["", "cassandra_operation_confirm is false: no question, the run goes on."]
    assert "WARNING - session" not in "\n".join(lines)  # no session given: short operation


def test_wrapping_keeps_words_and_paths_whole():
    path = "/var/lib/cassandra/" + "x" * 120
    text = cassandra_screen({"operation": "op", "intro": ["word " * 30 + path]})
    lines = text.split("\n")
    assert all(len(line) <= 100 for line in lines if path not in line)
    assert lines[-1] == path
    assert lines[2].startswith("word word")


def test_minimal_spec():
    assert cassandra_screen({"operation": "apply_config"}) == "apply_config"
    assert cassandra_screen({}) == ""


NODES = [{"name": "node%d" % i, "address": "10.0.0.%d" % i, "dc": "dc1" if i < 7 else "dc2",
          "rack": "rack%d" % (i % 2 + 1), "seed": i == 1} for i in range(1, 9)]


def ring(skip=()):
    out = {}
    for n in NODES:
        if n["name"] not in skip:
            out.setdefault(n["dc"], {"nodes": []})["nodes"].append(
                {"address": n["address"], "rack": n["rack"], "load": "1 GiB", "owns": "?", "status": "U", "state": "N"})
    return out


def test_decommission_two_racks_rf2_goes_to_the_rack():
    keyspaces = {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 2, "dc2": 1}}}
    spec = cassandra_decommission_screen([{"name": "node3"}], NODES, ring(), keyspaces, "node1")
    assert spec["summary"] == "remove node3"
    assert spec["intro"] == ["One node to remove: node3. It streams its data to the nodes that stay (hours on a big"
                             " node), then Cassandra is stopped and disabled on it."]
    assert spec["operation"] == "decommission_node"
    assert spec["blocks"] == [{"title": "node3  10.0.0.3  dc1 / rack2", "lines": [
        "not a seed",
        "load 1 GiB, share unknown (the keyspaces replicate differently)",
        "data goes to the other nodes of rack2 (2 racks in dc1, its replication factor): node1, node5",
        "runs on node3 (nodetool decommission), the ring checked from node1 before and after",
        "end state: out of the ring, Cassandra stopped and disabled, its data left on disk"],
        "step": {"node": "node3", "dc": "dc1", "rack": "rack2", "text": "load 1 GiB -> node1, node5 (the other nodes of rack2)"}}]
    assert spec["after"] == ["Afterwards dc1 keeps 5 nodes: node1, node2, node4, node5, node6.",
                             "Then remove node3 from the inventory; wipe the data directories before reusing the host."]
    assert spec["warnings"] == []


def test_decommission_racks_not_the_rf_goes_to_the_datacenter():
    keyspaces = {"orders": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 3}},
                 "events": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 2}}}
    spec = cassandra_decommission_screen([{"name": "node3"}], NODES, ring(), keyspaces, "node1")
    assert "data goes to the other nodes of dc1: node1, node2, node4, node5, node6" in spec["blocks"][0]["lines"]


def test_decommission_multi_dc_simple_strategy_and_order():
    keyspaces = {"system_auth": {"class": "SimpleStrategy", "rf": {"*": 1}}}
    spec = cassandra_decommission_screen([{"name": "node7"}, {"name": "node8"}], NODES, ring(), keyspaces, "node1")
    first, second = spec["blocks"]
    assert first["lines"][2:4] == [
        "data goes to the other nodes of dc2: node8; SimpleStrategy keyspaces (system_auth): any node of the cluster",
        "node8 is removed later and hands this data on again"]
    assert second["lines"][2] == ("data goes to the other nodes of dc2: none; SimpleStrategy keyspaces (system_auth):"
                                  " any node of the cluster")
    # the same on one line (topology's plan)
    assert first["step"]["text"] == "load 1 GiB -> node8; SimpleStrategy keyspaces: any node; node8 removed later"
    assert second["step"]["text"] == "load 1 GiB -> none (no node left in dc2); SimpleStrategy keyspaces: any node"
    assert spec["intro"][0].startswith("2 nodes to remove, one after the other: first node7, then node8.")
    assert spec["after"][0] == "Afterwards dc2 has no node left."


def test_decommission_states_and_no_ring():
    spec = cassandra_decommission_screen([{"name": "node5", "state": "decommissioned"}, {"name": "node6", "state": "leaving"}],
                                         NODES, {}, {}, "node1")
    assert spec["blocks"][0]["lines"] == [
        "not a seed", "already out of the ring (an earlier run): Cassandra only stopped and disabled on it"]
    # without a ring: the inventory's nodes, no load line
    assert spec["blocks"][1]["lines"][1:3] == [
        "still leaving (a decommission an earlier run started): waited for",
        "data goes to the other nodes of dc1: node1, node2, node3, node4"]


def test_decommission_node_missing_from_the_ring():
    spec = cassandra_decommission_screen([{"name": "node6"}], NODES, ring(skip=("node6",)), {}, "node1")
    assert spec["blocks"][0]["lines"][1] == "not in the ring as node1 sees it"


def test_decommission_forced_replication():
    spec = cassandra_decommission_screen([{"name": "node6"}], NODES, ring(), {}, "node1",
                                         replication_problems=["orders needs 3 replicas in dc1, which would keep 2 node(s)"])
    assert spec["warnings"] == [{"label": "replication", "text": [
        "cassandra_decommission_force is true: the removal goes on although too few nodes are left for some keyspaces;"
        " those replicas are lost and QUORUM can fail:", "orders needs 3 replicas in dc1, which would keep 2 node(s)"]}]


def test_reset_warnings_one_per_node_with_something_to_do():
    plans = [
        {"name": "node7", "plan": {"stop": "True", "disable": True, "delete": ["/d/data/system", "/d/commitlog/a.log"],
                                   "dirs": ["/d/data (data; from the inventory): 1 entries: system"]}},
        {"name": "node8", "plan": {"stop": False, "disable": False, "delete": [], "dirs": ["/d/data: empty or missing"]}},
        {"name": "node9", "plan": {"stop": False, "disable": "True", "delete": [], "dirs": []}},
    ]
    warnings = cassandra_reset_warnings(plans)
    assert warnings == [
        {"label": "data loss", "real_run": True,
         "text": ["node7: Cassandra stopped, kept from starting at boot, then 2 entries DELETED for good"
                  " (no snapshot, no backup), in:", {"pre": ["/d/data (data; from the inventory): 1 entries: system"]}]},
        {"label": "reset", "real_run": True, "text": "node9: kept from starting at boot first (nothing to delete)"},
    ]
    text = cassandra_screen({"operation": "add_node", "warnings": warnings})
    assert "WARNING - data loss: node7: Cassandra stopped, kept from starting at boot, then 2 entries DELETED" in text
    assert "    /d/data (data; from the inventory): 1 entries: system" in text
    assert "(A real run would also warn about: data loss, reset.)" in cassandra_screen(
        {"operation": "add_node", "warnings": warnings}, check=True)
    assert cassandra_reset_warnings([]) == []


def test_a_step_of_another_playbook_asks_nothing():
    text = cassandra_screen({"operation": "add_node", "summary": "add node7"}, asked_by="topology")
    assert text == "add_node: add node7\n\nA step of topology, confirmed on its screen: no question here."
    # --check says so in its place: nothing changes
    assert cassandra_screen({"operation": "add_node"}, check=True, asked_by="topology").endswith(
        "--check: nothing will be changed (the plan only, no question).")


# cassandra_config's _cassandra_config_items: a masked diff per file, an owner/mode change per file
PERMS = [{"item": "/etc/cassandra/conf/cassandra.yaml (owner:group mode)", "before": "root:cassandra 0640",
          "after": "cassandra:dbgrp 0640"},
         {"item": "/etc/cassandra/conf/cassandra-env.sh (owner:group mode)", "before": "root:cassandra 0640",
          "after": "cassandra:dbgrp 0644"}]
DIFF = ("--- /etc/cassandra/conf/cassandra.yaml (live)\n+++ (new)\n@@ -10,3 +10,3 @@\n cluster_name: Orders\n"
        "-concurrent_writes: 32\n+concurrent_writes: 48\n # a comment\n-# old comment\n+\n"
        "-jmx_password: ****\n+jmx_password: ****\n")
SETTINGS = [{"item": "/etc/cassandra/conf/cassandra.yaml", "before": "current", "after": "4 line(s) changed", "diff": DIFF}]


def recap_nodes():
    return [{"name": "node1", "todo": True, "done": True, "then": "none", "items": PERMS},
            {"name": "node2", "todo": True, "done": True, "then": "restart", "items": SETTINGS},
            {"name": "node3", "todo": False, "result": "nothing to apply"},
            {"name": "node4", "todo": False, "result": "nothing to apply"},
            {"name": "node5", "todo": False, "result": "nothing to apply"}]


def test_apply_config_recap_under_check():
    assert cassandra_apply_config_recap(recap_nodes(), check=True, cluster="my_cluster", seconds=75) == [
        "CHECK  apply_config  my_cluster  2 would apply, 3 nothing to apply (1m15s)",
        "node1  would apply, no restart",
        "  cassandra.yaml      owner/group/mode  root:cassandra 0640 -> cassandra:dbgrp 0640",
        "  cassandra-env.sh    owner/group/mode  root:cassandra 0640 -> cassandra:dbgrp 0644",
        "node2  would apply, then restart",
        "  cassandra.yaml",
        "    - concurrent_writes: 32",
        "    + concurrent_writes: 48",
        "    - jmx_password: ****",
        "    + jmx_password: ****",
        "node3..node5  nothing to apply"]


def test_apply_config_recap_real_run():
    nodes = recap_nodes()
    nodes[2] = {"name": "node3", "todo": True, "done": True, "then": "start", "items": PERMS[1:]}
    nodes[3] = {"name": "node4", "todo": True, "done": True, "then": "write", "items": SETTINGS + PERMS[:1]}
    # the changes were in the plan, before its question: the outcomes here
    assert cassandra_apply_config_recap(nodes, cluster="my_cluster", seconds=700) == [
        "DONE  apply_config  my_cluster  4 applied, 1 nothing to apply (11m40s)",
        "node1  applied, no restart",
        "node2  applied, restarted",
        "node3  applied, started",
        "node4  applied, left stopped",
        "node5  nothing to apply"]


def test_apply_config_recap_failed():
    nodes = [{"name": "node1", "todo": True, "done": True, "then": "restart", "items": SETTINGS},
             {"name": "node2", "todo": True, "done": False, "result": "apply_config FAILED after 30s: no answer"},
             {"name": "node3", "todo": True, "result": "not reached"}]
    assert cassandra_apply_config_recap(nodes, cluster="my_cluster") == [
        "FAILED  apply_config  my_cluster  1 applied, 1 failed, 1 not reached",
        "node1  applied, restarted",
        "node2  apply_config FAILED after 30s: no answer",
        "node3  not reached"]


def test_apply_config_recap_groups_same_changes_and_says_the_others_as_they_are():
    nodes = [{"name": "node%d" % i, "todo": True, "done": True, "then": "restart", "items": SETTINGS} for i in (1, 2)]
    nodes += [{"name": "node3", "todo": True, "done": True, "then": "restart",
               "notes": ["restart pending (cassandra.yaml changed since the running Cassandra started)"]},
              {"name": "node4", "todo": True, "done": False, "result": "apply_config skipped, done in the interrupted run"},
              {"name": "node5", "todo": True, "result": "not reached"},
              {"name": "node6", "result": "not in this run (--limit)"}]
    assert cassandra_apply_config_recap(nodes, check=True) == [
        "CHECK  apply_config  3 would apply, 1 skipped, 1 not reached, 1 not in this run",
        "node1, node2  would apply, then restart",
        "  cassandra.yaml",
        "    - concurrent_writes: 32",
        "    + concurrent_writes: 48",
        "    - jmx_password: ****",
        "    + jmx_password: ****",
        "node3  would apply, then restart",
        "  restart pending (cassandra.yaml changed since the running Cassandra started)",
        "node4  apply_config skipped, done in the interrupted run",
        "node5  not reached",
        "node6  not in this run (--limit)"]


def test_apply_config_recap_long_and_comment_only_diffs():
    long_diff = "--- a (live)\n+++ (new)\n" + "".join("+key_%d: %d\n" % (i, i) for i in range(25))
    items = [{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": long_diff},
             {"item": "/etc/cassandra/conf/logback.xml", "diff": "--- a (live)\n+++ (new)\n-# old\n+# new\n"},
             {"item": "/etc/cassandra/conf", "before": "/etc/cassandra/default.conf", "after": "/etc/cassandra/site"}]
    out = cassandra_apply_config_changes(items)
    assert out[:2] == ["  /etc/cassandra/conf  /etc/cassandra/default.conf -> /etc/cassandra/site", "  cassandra.yaml"]
    assert out[2:22] == ["    + key_%d: %d" % (i, i) for i in range(20)]
    assert out[22:] == ["    ... 5 more lines (the whole diff: -v)", "  logback.xml", "    (comments or layout only)"]


def test_apply_config_recap_nothing_to_apply():
    nodes = [{"name": "node%d" % i, "todo": False, "result": "nothing to apply"} for i in (1, 2, 3)]
    assert cassandra_apply_config_recap(nodes) == ["DONE  apply_config  3 nothing to apply", "node1..node3  nothing to apply"]
    assert cassandra_apply_config_recap(nodes, check=True) == [
        "CHECK  apply_config  3 nothing to apply", "node1..node3  nothing to apply"]


def test_apply_config_recap_nested_settings_and_whitespace():
    # an indented setting under its key (an unchanged line of the diff), once per key;
    # a line whose only change is its line ending is said so
    diff = ("--- a (live)\n+++ (new)\n@@ -1,8 +1,8 @@\n client_encryption_options:\n"
            "   # a comment\n-  enabled: false\n+  enabled: true\n-  optional: true\n+  optional: false\n"
            "@@ -20,3 +20,3 @@\n server_encryption_options:\n   internode_encryption: none\n-  enabled: false\n+  enabled: true\n"
            "@@ -40,2 +40,2 @@\n-MAX_HEAP_SIZE=8G\r\n+MAX_HEAP_SIZE=8G\n")
    items = [{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}]
    assert cassandra_apply_config_changes(items) == [
        "  cassandra.yaml",
        "      client_encryption_options:",
        "    -   enabled: false",
        "    +   enabled: true",
        "    -   optional: true",
        "    +   optional: false",
        "      server_encryption_options:",
        "    -   enabled: false",
        "    +   enabled: true",
        "      MAX_HEAP_SIZE=8G  (whitespace or line ending only)"]


def test_apply_config_recap_moved_lines_and_same_parent_names():
    # the same text removed in one place and added in another is no whitespace change; a key named as
    # one shown before (another hunk) is shown again
    diff = ("--- a (live)\n+++ (new)\n@@ -1,4 +1,3 @@\n client_encryption_options:\n-  enabled: true\n   optional: false\n"
            "@@ -30,3 +29,4 @@\n audit_logging_options:\n   logger: BinAuditLogger\n+  enabled: true\n"
            "@@ -40,2 +40,2 @@\n   parameters:\n-      - seeds: a\n+      - seeds: b\n"
            "@@ -60,2 +60,2 @@\n   parameters:\n-      - chunk_length_in_kb: 16\n+      - chunk_length_in_kb: 64\n"
            "@@ -70,3 +70,3 @@\n-key_a: 1\n same: x\n+key_a: 1\n")
    items = [{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}]
    assert cassandra_apply_config_changes(items) == [
        "  cassandra.yaml",
        "      client_encryption_options:",
        "    -   enabled: true",
        "      audit_logging_options:",
        "    +   enabled: true",
        "        parameters:",
        "    -       - seeds: a",
        "    +       - seeds: b",
        "        parameters:",
        "    -       - chunk_length_in_kb: 16",
        "    +       - chunk_length_in_kb: 64",
        "    - key_a: 1",
        "    + key_a: 1"]


def test_apply_config_recap_a_line_moved_past_a_comment_is_no_whitespace_change():
    diff = "--- a (live)\n+++ (new)\n@@ -1,3 +1,3 @@\n-d: 1\n # a comment\n \n+d: 1\n"
    out = cassandra_apply_config_changes([{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}])
    assert out == ["  cassandra.yaml", "    - d: 1", "    + d: 1"]


def test_apply_config_recap_names_the_nodes_of_a_group_as_ranges():
    # Q3: a range from 3 consecutive names, 2 listed, a gap breaks it, the count over 5
    def node(i, then):
        return {"name": "node%d" % i, "todo": True, "done": True, "then": then, "items": SETTINGS}
    nodes = [node(i, "restart") for i in (1, 2, 3, 5)] + [node(i, "none") for i in (4, 6)]
    nodes += [{"name": "node%d" % i, "todo": False, "result": "nothing to apply"} for i in range(7, 13)]
    out = cassandra_apply_config_recap(nodes)
    assert [line for line in out if not line.startswith(" ")] == [
        "DONE  apply_config  6 applied, 6 nothing to apply", "node1..node3, node5  applied, restarted", "node4, node6  applied, no restart",
        "6 nodes: node7..node12  nothing to apply"]


def test_apply_config_recap_masks_every_secret_kind():
    # cassandra_config masks password/secret keys; the shared rule also hides a private key value
    diff = "--- a (live)\n+++ (new)\n@@ -1 +1 @@\n-ssl_private_key: abc\n+ssl_private_key: def\n"
    out = cassandra_apply_config_recap([{"name": "node1", "todo": True, "done": True, "then": "restart",
                                         "items": [{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}]}], check=True)
    assert out[2:] == ["  cassandra.yaml", "    - ssl_private_key: ****", "    + ssl_private_key: ****"]
    assert not any("abc" in line or "def" in line for line in out)


def test_apply_config_recap_directories_and_warnings():
    # a directory's owner and mode after the files; a node's warnings said whatever its outcome
    dirs = [{"item": "data dir /srv/data (owner:group mode)", "path": "/srv/data", "dir": "data dir",
             "before": "root:cassandra 0750", "after": "cassandra:cassandra 0750"}]
    warning = "WARNING data dir /srv/data: not owned by cassandra: /srv/data/ks1 (left as they are: ...)"
    nodes = [{"name": "node1", "todo": True, "done": True, "then": "none", "items": PERMS[:1] + dirs},
             {"name": "node2", "todo": True, "done": True, "then": "start", "items": dirs},
             {"name": "node3", "todo": False, "result": "nothing to apply", "notes": [warning]},
             {"name": "node4", "todo": False, "result": "nothing to apply"}]
    assert cassandra_apply_config_recap(nodes, check=True) == [
        "CHECK  apply_config  2 would apply, 2 nothing to apply",
        "node1  would apply, no restart",
        "  cassandra.yaml    owner/group/mode  root:cassandra 0640 -> cassandra:dbgrp 0640",
        "  data dir /srv/data  owner/group/mode  root:cassandra 0750 -> cassandra:cassandra 0750",
        "node2  would apply, then start",
        "  data dir /srv/data  owner/group/mode  root:cassandra 0750 -> cassandra:cassandra 0750",
        "node3  nothing to apply",
        "  " + warning,
        "node4  nothing to apply"]


def test_apply_config_recap_masks_a_secret_whose_line_ending_only_changed():
    diff = "--- a (live)\n+++ (new)\n@@ -1 +1 @@\n-ssl_private_key: abc\r\n+ssl_private_key: abc\n"
    out = cassandra_apply_config_changes([{"item": "/etc/cassandra/conf/cassandra.yaml", "diff": diff}])
    assert out and not any("abc" in line for line in out)
