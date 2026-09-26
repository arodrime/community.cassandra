from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4,
    _load_role,
    _render,
    cassandra_config_import,
    cassandra_inventory_layout,
    cassandra_ring_nodes,
    cassandra_config_ignored_vars,
    cassandra_inventory_files,
)
from ansible_collections.community.cassandra.plugins.modules.cassandra_status import cluster_up_down

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "modules", "fixtures")
FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}}


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


def node_files(series, facts=None, **changes):
    """The config files cassandra_config writes with these variables."""
    env, ctx, files = _load_role(series, facts or FACTS)
    ctx.update(changes)
    return {name: "\n".join(_render(env, series, name, ctx)[1]) for name in files}


def ring(fixture):
    return cassandra_ring_nodes(cluster_up_down(load_fixture(fixture)))


def test_ring_multi_dc():
    assert [(n["address"], n["dc"], n["rack"], n["state"]) for n in ring("nodetool_status_multi_dc.txt")] == [
        ("10.0.0.1", "datacenter1", "rack1", "UN"),
        ("10.0.1.1", "datacenter2", "rack2", "UN"),  # token column before the rack
    ]


def test_ring_load_unknown():
    assert [(n["address"], n["rack"]) for n in ring("nodetool_status_vnodes_load_unknown.txt")] == [("10.118.154.136", "rack1")]


def test_inventory_files_keep_passwords_apart():
    files = cassandra_inventory_files({
        "group_vars": {"c": {"cassandra_cluster_name": "Prod", "cassandra_server_keystore_password": "k",
                             "cassandra_extra_settings": {"jmx_encryption_options": {"keystore_password": "j"}}},
                       "c_dc1": {"cassandra_dc": "dc1"}},
        "host_vars": {"n1": {"cassandra_listen_address": "10.0.0.1"}},
    })
    assert [(f["path"], f["secret"]) for f in files] == [
        ("group_vars/c/main.yml", False), ("group_vars/c/secrets.yml", True),
        ("group_vars/c_dc1/main.yml", False), ("host_vars/n1/main.yml", False)]
    assert "keystore_password" not in files[0]["content"]
    assert "cassandra_server_keystore_password: k" in files[1]["content"]
    assert "keystore_password: j" in files[1]["content"]


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_defaults_import_to_nothing(series):
    out = cassandra_config_import(node_files(series), series, FACTS)
    assert out["hand_edits"] == []
    assert out["normalized"] == []
    identity = {"cassandra_cluster_name", "cassandra_seeds", "cassandra_endpoint_snitch", "cassandra_num_tokens",
                "cassandra_partitioner", "cassandra_allocate_tokens_for_local_replication_factor"}
    if series == "50x":
        identity.add("cassandra_storage_compatibility_mode")
    assert set(out["vars"]) == identity


@pytest.mark.parametrize("series", ["41x", "50x"])
def test_variables_read_back(series):
    changes = {"cassandra_cluster_name": "Prod", "cassandra_num_tokens": 4,
               "cassandra_seeds": "10.0.0.1,10.0.0.2", "cassandra_listen_address": "10.0.0.1",
               "cassandra_rpc_address": "10.0.0.9", "cassandra_heap_size": "8G", "cassandra_dc": "paris"}
    if series != "50x":
        changes["cassandra_heap_newsize"] = "800M"
    out = cassandra_config_import(node_files(series, **changes), series, FACTS)
    assert out["hand_edits"] == []
    v = out["vars"]
    assert v["cassandra_cluster_name"] == "Prod"
    assert v["cassandra_num_tokens"] == 4
    assert v["cassandra_seeds"] == ["10.0.0.1", "10.0.0.2"]
    assert v["cassandra_listen_address"] == IPV4  # the node's own address: fact expression
    assert v["cassandra_rpc_address"] == "10.0.0.9"
    assert v["cassandra_heap_size"] == "8G"
    assert v["cassandra_dc"] == "paris"


def test_hand_edit_and_normalized():
    files = node_files("50x")
    yaml_lines = files["cassandra.yaml"].split("\n")
    # hand edit no variable can hold: a rewritten comment
    i = next(i for i, line in enumerate(yaml_lines) if line.startswith("# commitlog_total_space:"))
    yaml_lines[i - 1] = "# edited by hand"
    # stock package style: hints_directory left commented, same value
    j = next(i for i, line in enumerate(yaml_lines) if line.startswith("hints_directory:"))
    yaml_lines[j] = "# " + yaml_lines[j]
    files["cassandra.yaml"] = "\n".join(yaml_lines)
    out = cassandra_config_import(files, "50x", FACTS)
    assert any("+ # edited by hand" in line for line in out["hand_edits"])
    assert not any("hints_directory" in line for line in out["hand_edits"])
    assert any(line.startswith("cassandra.yaml: hints_directory:") for line in out["normalized"])
    assert "cassandra_extra_settings" not in out["vars"]


def test_settings_without_a_variable_become_extra_settings():
    files = node_files("50x")
    yaml_lines = files["cassandra.yaml"].split("\n")
    i = next(i for i, line in enumerate(yaml_lines) if line.startswith("# commitlog_total_space:"))
    yaml_lines[i] = "commitlog_total_space: 4096MiB"  # uncommented, other value
    files["cassandra.yaml"] = "\n".join(yaml_lines) + "auto_bootstrap: false\n"  # not in the stock file at all
    out = cassandra_config_import(files, "50x", FACTS)
    assert out["vars"]["cassandra_extra_settings"] == {"commitlog_total_space": "4096MiB", "auto_bootstrap": False}
    assert out["hand_edits"] == []
    assert sum("kept with cassandra_extra_settings" in line for line in out["normalized"]) == 2


def test_unsupported_series():
    with pytest.raises(Exception):
        cassandra_config_import({}, "311x", FACTS)


def node(name, dc, rack, **variables):
    return {"name": name, "dc": dc, "rack": rack, "read": True, "vars": variables,
            "hand_edits": [], "normalized": [], "notes": []}


def test_layout_homogeneous_cluster_goes_to_cluster_level():
    nodes = [node("n1", "dc1", "r1", cassandra_num_tokens=4, cassandra_listen_address=IPV4),
             node("n2", "dc1", "r2", cassandra_num_tokens=4, cassandra_listen_address=IPV4),
             node("n3", "dc2", "r1", cassandra_num_tokens=4, cassandra_listen_address=IPV4)]
    out = cassandra_inventory_layout(nodes, "My Prod")
    assert out["cluster_group"] == "my_prod"
    assert out["group_vars"]["my_prod"] == {"cassandra_num_tokens": 4, "cassandra_listen_address": IPV4}
    assert out["group_vars"]["my_prod_dc1"] == {"cassandra_dc": "dc1"}
    assert out["group_vars"]["my_prod_dc1_r2"] == {"cassandra_rack": "r2"}
    assert out["host_vars"] == {}
    assert "DRIFT" not in out["report"]
    racks = out["hosts"]["all"]["children"]["my_prod"]["children"]["my_prod_dc1"]["children"]
    assert set(racks) == {"my_prod_dc1_r1", "my_prod_dc1_r2"}


def test_layout_drift_goes_down_and_is_reported():
    nodes = [node("n1", "dc1", "r1", cassandra_heap_size="8G"),
             node("n2", "dc1", "r1", cassandra_heap_size="8G"),
             node("n3", "dc2", "r1", cassandra_heap_size="16G"),
             node("n4", "dc2", "r1", cassandra_heap_size="16G", cassandra_concurrent_reads=64),
             node("n5", "dc2", "r1", cassandra_listen_address="10.0.0.5")]
    out = cassandra_inventory_layout(nodes, "c")
    assert out["group_vars"]["c_dc1"]["cassandra_heap_size"] == "8G"  # DC level
    assert out["host_vars"]["n3"] == {"cassandra_heap_size": "16G"}
    assert out["host_vars"]["n4"] == {"cassandra_heap_size": "16G", "cassandra_concurrent_reads": 64}
    assert out["host_vars"]["n5"] == {"cassandra_listen_address": "10.0.0.5"}
    assert "cassandra_heap_size:" in out["report"]
    assert "cassandra_concurrent_reads:" in out["report"]
    assert "cassandra_listen_address:" not in out["report"]  # per node by nature


def test_layout_unread_node_listed():
    nodes = [node("n1", "dc1", "r1"),
             {"name": "10.0.0.2", "ansible_host": "10.0.0.2", "dc": "dc1", "rack": "r1", "read": False,
              "reason": "unreachable"}]
    out = cassandra_inventory_layout(nodes, "c")
    hosts = out["hosts"]["all"]["children"]["c"]["children"]["c_dc1"]["children"]["c_dc1_r1"]["hosts"]
    assert hosts == {"n1": {}, "10.0.0.2": {"ansible_host": "10.0.0.2"}}
    assert "10.0.0.2: unreachable" in out["report"]


class Tagged(dict):
    """Stands for the dict subclasses Ansible passes to filters."""


def test_layout_drift_of_a_dict_value():
    nodes = [node("n1", "dc1", "r1", cassandra_extra_settings=Tagged(commitlog_total_space="8192MiB")),
             node("n2", "dc1", "r1")]
    out = cassandra_inventory_layout(nodes, "c")
    assert '{"commitlog_total_space": "8192MiB"}' in out["report"]


def test_report_masks_passwords():
    nodes = [{"name": "n%d" % i, "dc": "dc1", "rack": "r1", "read": True, "notes": [], "normalized": [],
              "vars": {"cassandra_server_keystore_password": "pw%d" % i},
              "hand_edits": ["cassandra.yaml, line 3:", "  + keystore_password: hand%d" % i]} for i in (1, 2)]
    report = cassandra_inventory_layout(nodes, "Prod")["report"]
    assert "pw1" not in report and "hand1" not in report
    assert "n1: ****" in report
    assert "keystore_password: ****" in report


def test_config_vars_the_target_series_ignores():
    names = ["cassandra_read_request_timeout_in_ms", "cassandra_read_request_timeout", "cassandra_heap_size",
             "cassandra_seeds", "some_other_var"]
    assert cassandra_config_ignored_vars(names, "41x") == ["cassandra_read_request_timeout_in_ms"]
    assert cassandra_config_ignored_vars(names, "40x") == ["cassandra_read_request_timeout"]


def test_jbod_data_directories_read_back():
    dirs = ["/data1/cassandra", "/data2/cassandra"]
    out = cassandra_config_import(node_files("50x", cassandra_data_file_directories=dirs), "50x", FACTS)
    assert out["vars"]["cassandra_data_file_directories"] == dirs
    assert out["vars"]["cassandra_data_dir"] == "/data1/cassandra"
    assert out["hand_edits"] == []
