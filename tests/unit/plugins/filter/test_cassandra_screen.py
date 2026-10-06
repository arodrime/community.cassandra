from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import (
    cassandra_decommission_screen, cassandra_screen)

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
    head, rest = REAL.split("\n", 1)
    expected = head + "\n--check: nothing will be changed (the plan only, no question).\n" + \
        rest.split("\n\nWARNING - data loss:")[0] + "\n\n(A real run would also warn about: data loss, session.)"
    assert cassandra_screen(SPEC, check=True, session=SESSION) == expected


def test_no_question():
    lines = cassandra_screen(SPEC, asks=False).split("\n")
    assert lines[1] == "cassandra_operation_confirm is false: no question, the run goes on."
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
        "end state: out of the ring, Cassandra stopped and disabled, its data left on disk"]}]
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
