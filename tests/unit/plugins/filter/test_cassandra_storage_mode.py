from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The move of a 5.0 cluster to storage_compatibility_mode NONE: by the inventory and by each node's cassandra.yaml
# (what Cassandra reads), the earlier of the two; the next step of the move.

from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import (
    cassandra_storage_mode_behind, cassandra_storage_mode_nodes, cassandra_storage_mode_row, cassandra_storage_mode_step)


def behind(*nodes):
    return cassandra_storage_mode_behind([{"node": "n%d" % i, "inventory": inv, "yaml": yaml}
                                          for i, (inv, yaml) in enumerate(nodes, 1)])


def test_the_inventory_at_none_before_apply_config_is_still_behind():
    # the inventory moved to NONE, apply_config not run yet: the nodes still read CASSANDRA_4
    found = behind(("NONE", "CASSANDRA_4"), ("NONE", "NONE"), ("NONE", "''"))
    assert [b["node"] for b in found] == ["n1", "n3"]  # an empty value: 5.0's default
    assert cassandra_storage_mode_step(found).startswith("Now set cassandra_storage_compatibility_mode: UPGRADING")
    assert cassandra_storage_mode_row(found[0]) == "  n1  inventory NONE  cassandra.yaml CASSANDRA_4"
    # no line at all in its cassandra.yaml
    assert cassandra_storage_mode_row(behind(("NONE", ""))[0]) == (
        "  n1  inventory NONE  cassandra.yaml no such line (5.0 runs CASSANDRA_4)")


def test_the_earlier_of_the_two_and_the_step():
    assert behind(("NONE", "NONE"), (None, "NONE")) == []
    assert cassandra_storage_mode_step([]) == ""
    found = behind(("UPGRADING", "NONE"), ("NONE", "UPGRADING"))
    assert [b["mode"] for b in found] == ["UPGRADING", "UPGRADING"]
    assert cassandra_storage_mode_step(found) == (
        "Now set cassandra_storage_compatibility_mode: NONE in the inventory and run apply_config (NONE is the point"
        " of no return). ")
    # not read (no file): the inventory alone
    found = behind(("CASSANDRA_4", None))
    assert found[0]["mode"] == "CASSANDRA_4" and cassandra_storage_mode_row(found[0]).endswith("cassandra.yaml not read")
    assert behind(("NONE", None)) == []


def test_nodes_from_their_variables():
    hostvars = [{"inventory_hostname": "n1", "cassandra_storage_compatibility_mode": "NONE",
                 "cassandra_storage_mode_read": {"rc": 0, "stdout": "CASSANDRA_4"}},
                {"inventory_hostname": "n2", "cassandra_storage_mode_read": {"rc": 3, "stdout": ""}},
                {"inventory_hostname": "n3"}]
    assert cassandra_storage_mode_nodes(hostvars) == [{"node": "n1", "inventory": "NONE", "yaml": "CASSANDRA_4"},
                                                      {"node": "n2", "inventory": None, "yaml": None},
                                                      {"node": "n3", "inventory": None, "yaml": None}]
