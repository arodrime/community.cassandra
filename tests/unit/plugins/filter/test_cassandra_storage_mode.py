from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The move of a cluster upgraded from 4.x to 5.0 to storage_compatibility_mode NONE: by the inventory, by each
# node's cassandra.yaml and by the running Cassandra, the earliest; a mode not read or unknown is never NONE.

from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import (
    cassandra_storage_mode_behind, cassandra_storage_mode_from_4x, cassandra_storage_mode_nodes,
    cassandra_storage_mode_rows, cassandra_storage_mode_step, cassandra_storage_mode_way_out)


def behind(*nodes):
    return cassandra_storage_mode_behind([{"node": "n%d" % i, "inventory": inv, "yaml": yaml, "running": running}
                                          for i, (inv, yaml, running) in enumerate(nodes, 1)])


def test_the_earliest_of_the_three():
    assert behind(("NONE", "NONE", "NONE"), (None, "NONE", "NONE")) == []
    found = behind(("NONE", "CASSANDRA_4", "CASSANDRA_4"), ("UPGRADING", "NONE", "NONE"), ("NONE", "''", "NONE"))
    assert [(b["node"], b["mode"]) for b in found] == [("n1", "CASSANDRA_4"), ("n2", "UPGRADING"), ("n3", "CASSANDRA_4")]
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
        "Restart n1, so it runs the mode its cassandra.yaml has: ansible-playbook x --limit n1.")
    # pending, nothing logged (the log rolled over): the same
    found = cassandra_storage_mode_behind([{"node": "n1", "inventory": "NONE", "yaml": "NONE", "running": None,
                                            "pending": True}])
    assert found[0]["running"] == "restart pending (its cassandra.yaml changed since its start)"
    assert cassandra_storage_mode_way_out(found).startswith("Restart n1")


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
        {"inventory_hostname": "n2", "cassandra_storage_mode_read": read("yaml_read=yes", "yaml=NONE", "running=yes",
                                                                         "pending=yes", "logged=UPGRADING")},
        {"inventory_hostname": "n3", "cassandra_storage_mode_read": read("yaml_read=yes", "yaml=NONE", "running=yes",
                                                                         "pending=yes")},
        {"inventory_hostname": "n4", "cassandra_storage_mode_read": read("yaml_read=no", "running=no")},
        {"inventory_hostname": "n5"}]
    assert cassandra_storage_mode_nodes(hostvars) == [
        {"node": "n1", "inventory": "NONE", "yaml": "NONE", "running": "NONE", "pending": False, "stopped": False},
        {"node": "n2", "inventory": None, "yaml": "NONE", "running": "UPGRADING", "pending": True, "stopped": False},
        {"node": "n3", "inventory": None, "yaml": "NONE", "running": None, "pending": True, "stopped": False},
        {"node": "n4", "inventory": None, "yaml": None, "running": None, "pending": False, "stopped": True},
        {"node": "n5", "inventory": None, "yaml": None, "running": None, "pending": False, "stopped": False}]


def test_from_4x():
    def h(series, *copies):
        return {"cassandra_upgrade_from_series": series, "cassandra_upgrade_copies": {"stdout_lines": list(copies)}}
    assert cassandra_storage_mode_from_4x([h("41x"), h("50x")], "5.0.9") is True
    assert cassandra_storage_mode_from_4x([h("50x", "4.1.12")], "5.0.9") is True
    assert cassandra_storage_mode_from_4x([h("50x", "4.1.12", "5.0.8")], "5.0.9") is False  # a patch upgrade
    assert cassandra_storage_mode_from_4x([h("50x")], "5.0.9") is False
    assert cassandra_storage_mode_from_4x([h("50x", "5.0.10")], "5.0.9") is False  # not below the target
