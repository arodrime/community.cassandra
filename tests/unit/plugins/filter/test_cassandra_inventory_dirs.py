from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# As the reset renders it: tests/unit/roles/test_config_declared.py (test_reset_node_empties_the_cdc_raw_dir)

from ansible_collections.community.cassandra.plugins.filter.cassandra_node_reset import cassandra_inventory_dirs


def test_every_directory_with_its_kind():
    dirs = cassandra_inventory_dirs(["/d1", "/d2"], "/c", "/s", "/h", extra={"local_system_data_file_directory": "/sys"},
                                    cdc_enabled=True, storage_dir="/opt/c/data/")
    assert dirs == [["data", "/d1"], ["data", "/d2"], ["commitlog", "/c"], ["saved_caches", "/s"], ["hints", "/h"],
                    ["data", "/sys"], ["cdc_raw", "/opt/c/data/cdc_raw"]]
    assert cassandra_inventory_dirs([], "/c", "/s", "/h", extra={"cdc_raw_directory": "/old"})[-1] == ["cdc_raw", "/old"]
    assert cassandra_inventory_dirs([], "/c", "/s", "/h")[-1] == ["hints", "/h"]
