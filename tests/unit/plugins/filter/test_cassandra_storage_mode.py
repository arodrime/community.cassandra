from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The move of a cluster upgraded from 4.x to 5.0 to storage_compatibility_mode NONE: by the inventory, by each
# node's cassandra.yaml and by the running Cassandra, the earliest; a mode not read or unknown is never NONE.

from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import (
    STAYS, cassandra_storage_mode_behind, cassandra_storage_mode_from_4x, cassandra_storage_mode_headline,
    cassandra_storage_mode_nodes, cassandra_storage_mode_rows, cassandra_storage_mode_step, cassandra_storage_mode_way_out)

behind_of = cassandra_storage_mode_behind


def behind(*nodes):
    return cassandra_storage_mode_behind([{"node": "n%d" % i, "inventory": inv, "yaml": yaml, "running": running}
                                          for i, (inv, yaml, running) in enumerate(nodes, 1)])


def test_the_earliest_of_the_three():
    assert behind(("NONE", "NONE", "NONE"), (None, "NONE", "NONE")) == []
    found = behind(("NONE", "CASSANDRA_4", "CASSANDRA_4"), ("UPGRADING", "NONE", "NONE"), ("NONE", "''", "NONE"))
    # n3: no such line (5.0 runs CASSANDRA_4 then) while it runs NONE: edited back
    assert [(b["node"], b["mode"], b["reverted"]) for b in found] == [
        ("n1", "CASSANDRA_4", False), ("n2", "UPGRADING", False), ("n3", "CASSANDRA_4", True)]
    # the earliest of the three, the running one too
    assert behind(("NONE", "UPGRADING", "CASSANDRA_4"))[0]["mode"] == "CASSANDRA_4"
    assert cassandra_storage_mode_step(found).startswith("Now set cassandra_storage_compatibility_mode: UPGRADING")
    assert cassandra_storage_mode_step(found[1:2]).startswith("Now set cassandra_storage_compatibility_mode: NONE")
    assert cassandra_storage_mode_step([]) == ""


def test_not_read_unknown_pending():
    found = behind(("NONE", None, None))
    assert found[0]["unread"] == ["its cassandra.yaml", "the mode it runs"] and found[0]["mode"] == "NONE"
    assert "On n1, neither its cassandra.yaml nor the mode it runs could be read" in cassandra_storage_mode_way_out(found)
    found = behind(("NONNE", "NONE", "NONE"))
    assert found[0]["unknown"] is True
    found = behind(("NONE", "NONE", "UPGRADING"))
    assert found[0]["pending"] is True
    assert cassandra_storage_mode_rows(behind(("NONE", "", "CASSANDRA_4"))) == [
        "  node  inventory  cassandra.yaml                       running",
        "  n1    NONE       no such line (5.0 runs CASSANDRA_4)  CASSANDRA_4"]


def test_pending_alone_says_restart():
    # NONE written everywhere, the node runs UPGRADING (logged at its start): a restart, not the inventory
    found = behind(("NONE", "NONE", "UPGRADING"))
    assert cassandra_storage_mode_way_out(found, restart_command="ansible-playbook x --limit n1") == (
        "Restart n1, so it runs the mode its cassandra.yaml has: ansible-playbook x --limit n1. Then run this phase again.")
    # changed since its start, nothing logged, CQL not read: not known, read first
    found = cassandra_storage_mode_behind([{"node": "n1", "inventory": "NONE", "yaml": "NONE", "running": None,
                                            "pending": True}])
    assert found[0]["running"] == "not known (its cassandra.yaml changed after Cassandra started)"
    assert cassandra_storage_mode_way_out(found).startswith("On n1, read the mode its Cassandra runs")


def test_a_cluster_staying_before_none_has_nothing_to_rewrite():
    found = behind(("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4"))
    assert cassandra_storage_mode_way_out(found, from_4x=False).startswith(
        "SSTables keep the 4.x format while storage_compatibility_mode is not NONE: nothing to rewrite.")


def test_nodes_from_their_variables():
    def read(*lines):
        return {"stdout_lines": list(lines)}
    hostvars = [
        {"inventory_hostname": "n1", "cassandra_storage_compatibility_mode": "NONE",
         "cassandra_storage_mode_read": read("yaml_read=yes", "yaml=NONE", "running=yes")},  # started after the change
        {"inventory_hostname": "n2", "cassandra_storage_mode_read": read("file=/etc/c/cassandra.yaml", "yaml_read=yes",
                                                                         "yaml=NONE", "running=yes", "logged=UPGRADING")},
        {"inventory_hostname": "n3", "cassandra_storage_mode_read": read("yaml_read=yes", "yaml=NONE", "running=yes",
                                                                         "pending=yes")},
        {"inventory_hostname": "n4", "cassandra_storage_mode_read": read("yaml_read=no", "running=no")},
        {"inventory_hostname": "n5"}]
    flags = {"stopped": False, "start_unread": False, "start_later": False, "future": False, "file": "cassandra.yaml",
             "cql_host": "127.0.0.1"}
    assert cassandra_storage_mode_nodes(hostvars) == [
        dict(flags, node="n1", inventory="NONE", yaml="NONE", running="NONE", pending=False),
        dict(flags, node="n2", inventory=None, yaml="NONE", running="UPGRADING", pending=False, file="/etc/c/cassandra.yaml"),
        dict(flags, node="n3", inventory=None, yaml="NONE", running=None, pending=True),
        dict(flags, node="n4", inventory=None, yaml=None, running=None, pending=False, stopped=True),
        dict(flags, node="n5", inventory=None, yaml=None, running=None, pending=False)]
    # the logged mode against the file: a restart pending
    assert behind_of(cassandra_storage_mode_nodes(hostvars[1:2]))[0]["pending"] is True


def test_from_4x():
    def h(series, *copies):
        return {"cassandra_upgrade_from_series": series, "cassandra_upgrade_copies": {"stdout_lines": list(copies)}}
    assert cassandra_storage_mode_from_4x([h("41x"), h("50x")], "5.0.9") is True
    assert cassandra_storage_mode_from_4x([h("50x", "4.1.12")], "5.0.9") is True
    assert cassandra_storage_mode_from_4x([h("50x", "4.1.12", "5.0.8")], "5.0.9") is False  # a patch upgrade
    assert cassandra_storage_mode_from_4x([h("50x")], "5.0.9") is False
    assert cassandra_storage_mode_from_4x([h("50x", "5.0.10")], "5.0.9") is False  # not below the target


def test_a_start_not_read_and_a_file_time_after_now():
    found = behind_of([{"node": "n1", "inventory": "NONE", "yaml": "NONE", "running": None, "start_unread": True}])
    assert found[0]["running"] == "not read (the start of Cassandra)" and found[0]["unread"] == ["the start of its Cassandra"]
    assert cassandra_storage_mode_way_out(found) == (
        "On n1, the start of its Cassandra could not be read: from /proc/stat (btime) and /proc/<its pid>/stat."
        " Then run this phase again.")
    found = behind_of([{"node": "n1", "inventory": "NONE", "yaml": "NONE", "running": None, "start_unread": True,
                        "start_later": True}])
    assert found[0]["running"] == "not read (the start of Cassandra is after now)"
    assert cassandra_storage_mode_way_out(found) == (
        "On n1, the start of its Cassandra could not be read: its start reads as after now (the btime of /proc/stat),"
        " check its clock. Then run this phase again.")
    found = behind_of([{"node": "n%d" % i, "inventory": "NONE", "yaml": "NONE", "running": None, "future": True,
                        "file": "/etc/cassandra/cassandra.yaml"} for i in (1, 2)])
    assert found[0]["running"] == "cassandra.yaml has a time after now" and not found[0]["pending"]
    assert cassandra_storage_mode_headline(found) == "On n1, n2, cassandra.yaml has a time after now. Nothing was done."
    # the clock first, then the file touched (written again with the same content, it keeps its time), then the
    # restart (its start then after the file)
    assert cassandra_storage_mode_way_out(found, restart_command="ansible-playbook x --limit n1,n2") == (
        "Set the clock of n1, n2 right (their cassandra.yaml has a time after now), then on them:"
        " touch /etc/cassandra/cassandra.yaml. Restart n1, n2, so they run the mode their cassandra.yaml has:"
        " ansible-playbook x --limit n1,n2. Then run this phase again.")
    # no Cassandra process: start it
    found = behind_of([{"node": "n1", "inventory": "NONE", "yaml": "NONE", "running": None, "stopped": True}])
    assert cassandra_storage_mode_way_out(found) == (
        "On n1, the mode it runs (no Cassandra process found) could not be read: start Cassandra on it"
        " (systemctl start cassandra). Then run this phase again.")
    # not from 4.x, a restart pending alone: run again too
    found = behind(("NONE", "NONE", "UPGRADING"))
    assert cassandra_storage_mode_way_out(found, from_4x=False).endswith("Then run this phase again.")
    # read lines: never "not pending" when the start was not read
    nodes = cassandra_storage_mode_nodes([{"inventory_hostname": "n1", "cassandra_storage_mode_read": {
        "stdout_lines": ["yaml_read=yes", "yaml=NONE", "running=yes", "start_read=no"]}}])
    assert nodes[0]["running"] is None and nodes[0]["start_unread"] is True


def test_a_move_under_way_not_from_4x_gets_its_next_step_and_the_restart():
    # not from 4.x, one node moved to UPGRADING (the others at NONE): how to finish the move
    found = behind(("UPGRADING", "UPGRADING", "UPGRADING"), ("NONE", "NONE", "NONE"))
    out = cassandra_storage_mode_way_out(found, from_4x=False, total=3)
    assert out.startswith(STAYS) and "To finish the move to NONE: now set cassandra_storage_compatibility_mode: NONE" in out
    # a node running an earlier mode than its file: its restart alone first, then this phase (the step then)
    found = behind(("UPGRADING", "UPGRADING", "UPGRADING"), ("UPGRADING", "UPGRADING", "CASSANDRA_4"))
    assert cassandra_storage_mode_way_out(found, from_4x=False, total=3) == (
        "Restart n2, so it runs the mode its cassandra.yaml has (rolling_restart --limit). Then run this phase again.")
    # the two ways a move is under way, each alone: modes that differ, every node behind
    found = behind(("UPGRADING", "UPGRADING", "UPGRADING"), ("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4"))
    assert "To finish the move to NONE" in cassandra_storage_mode_way_out(found, from_4x=False, total=2)
    # one mode, some nodes at NONE already (fewer behind than the nodes)
    found = behind(("UPGRADING", "UPGRADING", "UPGRADING"), ("UPGRADING", "UPGRADING", "UPGRADING"))
    assert "To finish the move to NONE" in cassandra_storage_mode_way_out(found, from_4x=False, total=3)
    assert "To finish the move to NONE" not in cassandra_storage_mode_way_out(found, from_4x=False, total=2)
    # every node before NONE, none moved: nothing but the skip
    assert cassandra_storage_mode_way_out(behind(("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4")), from_4x=False, total=1) == STAYS
    # several nodes: the plural
    found = behind(("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4"), ("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4"))
    assert cassandra_storage_mode_headline(found, from_4x=False).startswith(
        "storage_compatibility_mode is not NONE on n1, n2: their sstables are in the format they write.")
