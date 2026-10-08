from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re

import pytest
import yaml

from ansible.errors import AnsibleFilterError, AnsibleUndefinedVariable

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4,
    _load_role,
    _read_line,
    _render,
    cassandra_config_import,
    cassandra_inventory_layout,
    cassandra_ring_nodes,
    cassandra_config_ignored_vars,
    cassandra_inventory_files,
    cassandra_unit_environment,
    cassandra_import_error,
    _jmx_users,
    _same_setting,
)
from ansible_collections.community.cassandra.plugins.filter import cassandra_import
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
    assert [(n["address"], n["rack"]) for n in ring("nodetool_status_vnodes_load_unknown.txt")] == [("10.100.100.136", "rack1")]


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
                "cassandra_partitioner", "cassandra_allocate_tokens_for_local_replication_factor",
                # SimpleSnitch doesn't read rackdc's dc= and rack=: as on the node (the layout drops them if the ring's)
                "cassandra_rackdc_dc", "cassandra_rackdc_rack"}
    if series == "50x":
        identity.add("cassandra_storage_compatibility_mode")
    assert set(out["vars"]) == identity


@pytest.mark.parametrize("series", ["41x", "50x"])
def test_variables_read_back(series):
    changes = {"cassandra_cluster_name": "Prod", "cassandra_num_tokens": 4,
               "cassandra_seeds": "10.0.0.1,10.0.0.2", "cassandra_listen_address": "10.0.0.1",
               "cassandra_rpc_address": "10.0.0.9", "cassandra_heap_size": "8G", "cassandra_rackdc_dc": "paris"}
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
    assert v["cassandra_rackdc_dc"] == "paris"  # SimpleSnitch: the file's, not the node's dc


@pytest.mark.parametrize("series, window", [("50x", "cassandra_commitlog_sync_group_window"),
                                            ("41x", "cassandra_commitlog_sync_group_window"),
                                            ("40x", "cassandra_commitlog_sync_group_window_in_ms")])
def test_commitlog_group_window_read_back(series, window):
    # its line is on in group mode only (else a commented placeholder)
    value = 15 if series == "40x" else "15ms"
    out = cassandra_config_import(node_files(series, cassandra_commitlog_sync="group", **{window: value}), series, FACTS)
    assert out["hand_edits"] == []
    assert out["vars"]["cassandra_commitlog_sync"] == "group"
    assert out["vars"][window] == value
    out = cassandra_config_import(node_files(series), series, FACTS)
    assert out["hand_edits"] == [] and window not in out["vars"]


def test_hand_edit_and_normalized():
    files = node_files("50x")
    yaml_lines = files["cassandra.yaml"].split("\n")
    # a rewritten comment: listed apart, not a hand edit
    i = next(i for i, line in enumerate(yaml_lines) if line.startswith("# commitlog_total_space:"))
    yaml_lines[i - 1] = "# edited by hand"
    # stock package style: hints_directory left commented, same value
    j = next(i for i, line in enumerate(yaml_lines) if line.startswith("hints_directory:"))
    yaml_lines[j] = "# " + yaml_lines[j]
    files["cassandra.yaml"] = "\n".join(yaml_lines)
    out = cassandra_config_import(files, "50x", FACTS)
    assert not any("edited by hand" in line for line in out["hand_edits"])  # a comment sets nothing
    assert out["comments"] == ["cassandra.yaml: line %d" % i]
    assert not any("hints_directory" in line for line in out["hand_edits"])
    assert any(line.startswith("cassandra.yaml: hints_directory:") for line in out["normalized"])
    assert "cassandra_extra_settings" not in out["vars"]


def test_properties_compared_by_settings():
    files = node_files("50x", cassandra_dc="dc1", cassandra_rack="r1")
    files["cassandra-rackdc.properties"] = "rack=r1\ndc=dc1\n"
    out = cassandra_config_import(files, "50x", FACTS)
    assert not [h for h in out["hand_edits"] if "rackdc" in h]
    assert any("cassandra-rackdc.properties: same settings" in n for n in out["normalized"])
    files["cassandra-rackdc.properties"] = "dc=dc1\nrack=r1\nprefer_local=true\n"
    out = cassandra_config_import(files, "50x", FACTS)
    assert out["vars"]["cassandra_prefer_local"] is True
    files["cassandra-rackdc.properties"] = "dc=dc1\r\nrack=r1\r\nprefer_local=true\r\n"  # CRLF, as Java reads it
    out = cassandra_config_import(files, "50x", FACTS)
    assert out["vars"]["cassandra_prefer_local"] is True
    assert not [h for h in out["hand_edits"] if "rackdc" in h]
    files["cassandra-rackdc.properties"] = "dc=dc1\nrack=r1\ndc_suffix=_x\n"
    out = cassandra_config_import(files, "50x", FACTS)
    assert "cassandra-rackdc.properties: dc_suffix" in out["hand_edits"]
    assert "  + dc_suffix=_x" in out["hand_edits"]


def test_line_switched_on_by_a_bool_or_by_another_variable():
    tpl = "{{ '' if switch else '#' }}opt=1"
    # a bool that only switches the line on: read back as true
    assert _read_line(tpl, "opt=1", {"switch": False}) == {"switch": True}
    # a list (e.g. JMX users) switching the line on: its value can't be read from
    # the line, so no variable (the line is left to the report as a hand edit)
    assert _read_line(tpl, "opt=1", {"switch": []}) == {}
    # switched off: empty, whatever the type
    assert _read_line(tpl, "#opt=1", {"switch": []}) == {"switch": ""}


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
    assert out["differences"] == "DIFFERENCES BETWEEN NODES (kept per group or node, check they are wanted):\n  none"
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
    # sorted like the vars files (JVM before cassandra.yaml settings), the listen address is per node by nature
    assert out["differences"].split("\n")[1:] == [
        '  cassandra_heap_size: "16G" on n3, n4; "8G" on DC dc1; (role default) on n5',
        '  cassandra_concurrent_reads: (role default) on 4 nodes; 64 on n4']
    assert out["report"].split("\n")[3:6] == out["differences"].split("\n")  # at the top


def test_layout_unread_node_listed():
    nodes = [dict(node("n1", "dc1", "r1"), address="10.0.0.1", ansible_host="10.0.0.1"),
             {"name": "10.0.0.2", "address": "10.0.0.2", "ansible_host": "10.0.0.2", "dc": "dc1", "rack": "r1",
              "read": False, "reason": "unreachable"}]
    out = cassandra_inventory_layout(nodes, "c")
    hosts = out["hosts"]["all"]["children"]["c"]["children"]["c_dc1"]["children"]["c_dc1_r1"]["hosts"]
    assert hosts == {"n1": {"ansible_host": "10.0.0.1"}, "10.0.0.2": {"ansible_host": "10.0.0.2"}}
    assert "10.0.0.2: unreachable" in out["report"]


def test_layout_nodes_sharing_a_name_are_named_by_address():
    # e.g. the same short hostname in two domains: one entry would hide the other
    nodes = [dict(node("db", "dc1", "r1", cassandra_heap_size="8G"), address="10.0.0.1", ansible_host="10.0.0.1"),
             dict(node("db", "dc1", "r1", cassandra_heap_size="16G"), address="10.0.0.2", ansible_host="10.0.0.2"),
             dict(node("n3", "dc1", "r1", cassandra_heap_size="16G"), address="10.0.0.3", ansible_host="10.0.0.3")]
    out = cassandra_inventory_layout(nodes, "c")
    hosts = out["hosts"]["all"]["children"]["c"]["children"]["c_dc1"]["children"]["c_dc1_r1"]["hosts"]
    assert sorted(hosts) == ["10.0.0.1", "10.0.0.2", "n3"]
    assert out["host_vars"]["10.0.0.1"] == {"cassandra_heap_size": "8G"}
    assert "SAME NAME for several nodes, named by their address instead: db" in out["report"]


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
    assert "cassandra_server_keystore_password: **** on n1; **** on n2" in report
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


def test_one_data_directory_elsewhere_read_back():
    files = node_files("50x", cassandra_data_file_directories=["/data/c1/cassandra/data"])
    out = cassandra_config_import(files, "50x", FACTS)
    assert out["vars"]["cassandra_data_dir"] == "/data/c1/cassandra/data"
    assert "cassandra_data_file_directories" not in out["vars"]
    assert out["hand_edits"] == []


def test_remote_jmx_users_read_back():
    # remote JMX with authentication: its users go to secrets.yml, the files match
    users = [{"name": "ops", "password": "s3cret", "access": "readwrite"},
             {"name": "mon", "password": "m0n", "access": "readonly"}]
    files = node_files("50x", cassandra_local_jmx=False, cassandra_jmx_users=users)
    files["jmxremote.password"] = "# by hand\nops s3cret\nmon m0n\n"
    files["jmxremote.access"] = ("ops readwrite \\\n    create javax.management.monitor.*,javax.management.timer.* \\\n"
                                 "    unregister\nmon readonly\n")
    out = cassandra_config_import(files, "50x", FACTS)
    assert out["hand_edits"] == []
    assert out["vars"]["cassandra_local_jmx"] is False
    assert out["vars"]["cassandra_jmx_users"] == users
    layout = cassandra_inventory_files({"group_vars": {"c": out["vars"]}, "host_vars": {}})
    assert "s3cret" not in layout[0]["content"] and "s3cret" in layout[1]["content"]


def test_jmx_users_not_imported_without_the_access_file():
    # stock cassandra-env.sh: the access.file line commented out, the rights elsewhere
    # (e.g. the JDK's own jmxremote.access): importing them as readonly would lock the user out
    files = node_files("50x", cassandra_local_jmx=False)
    files["jmxremote.password"] = "ops s3cret\n"
    out = cassandra_config_import(files, "50x", FACTS)
    assert "cassandra_jmx_users" not in out["vars"]
    assert any("users NOT imported" in line for line in out["hand_edits"])


def test_commented_line_same_setting_in_yaml_only():
    # a commented stock value in cassandra.yaml is the default; a commented JVM_OPTS line is off
    line = 'JVM_OPTS="$JVM_OPTS -Dcom.sun.management.jmxremote.access.file=/etc/cassandra/jmxremote.access"'
    assert not _same_setting("cassandra-env.sh", line, "#" + line)
    assert _same_setting("cassandra.yaml", "num_tokens: 16", "# num_tokens: 16")


@pytest.mark.parametrize("optional", [False, True])
def test_encryption_optional_read_back(optional):
    # 'true' if X == '' else (X | string | lower): it once came out as a variable named "=="
    files = node_files("41x", cassandra_server_encryption_optional=optional)
    out = cassandra_config_import(files, "41x", FACTS)
    assert "==" not in out["vars"]
    assert out["hand_edits"] == []
    assert out["vars"].get("cassandra_server_encryption_optional", True) is optional


@pytest.mark.parametrize("password_file, access_file, users", [
    # a # inside a password is part of it; comment lines are skipped
    # (a readwrite line without the create and unregister rights the role adds: kept so)
    ("# comment\nops Pa#ss\n", "ops readwrite\n",
     [{"name": "ops", "password": "Pa#ss", "access": "readwrite", "create_unregister": False}]),
    # hashed passwords (jmxremote.password.toHashes): not read back
    ("ops c2FsdA== aGFzaA== SHA3-512\n", "ops readwrite\n", None),
    # a user without rights in the file
    ("ops s3cret\n", "", None),
    # rights the role can't write back (it would widen them): not imported
    ("ops s3cret\n", "ops readwrite unregister\n", None),
    ("ops s3cret\n", "ops readwrite create javax.management.monitor.* unregister\n", None),
])
def test_jmx_users_parsed(password_file, access_file, users):
    assert _jmx_users(password_file, access_file) == users


@pytest.mark.parametrize("text, env", [
    ("CASSANDRA_LOG_DIR=/data/log MAX_HEAP_SIZE=512M", {"CASSANDRA_LOG_DIR": "/data/log", "MAX_HEAP_SIZE": "512M"}),
    ('LOCAL_JMX=no "JVM_EXTRA_OPTS=-Da=1 -Db=2"', {"LOCAL_JMX": "no", "JVM_EXTRA_OPTS": "-Da=1 -Db=2"}),
    ("", {}),
])
def test_unit_environment(text, env):
    assert cassandra_unit_environment(text) == env


def test_list_under_a_secret_name_is_a_secret():
    files = cassandra_inventory_files({"group_vars": {"c": {"cassandra_old_passwords": ["a", "b"]}}, "host_vars": {}})
    assert [f["path"] for f in files] == ["group_vars/c/secrets.yml"]


def test_password_file_path_is_not_a_secret():
    files = cassandra_inventory_files({"group_vars": {"c": {"cassandra_jmx_username": "ops",
                                                            "cassandra_jmx_password_file": "/etc/cassandra/jmx.pw"}},
                                       "host_vars": {}})
    assert [f["path"] for f in files] == ["group_vars/c/main.yml"]


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_remote_jmx_read_back(series):
    # LOCAL_JMX=no: cassandra_local_jmx false (it once came out as a variable named "else")
    out = cassandra_config_import(node_files(series, cassandra_local_jmx=False), series, FACTS)
    assert out["vars"]["cassandra_local_jmx"] is False
    assert "else" not in out["vars"]


def test_unexpected_error_hides_its_message(monkeypatch):
    # its message may quote a config line: only the file, the type and where
    def boom(*args):
        raise ValueError("keystore_password: hunter2")
    monkeypatch.setattr(cassandra_import, "_import_file", boom)
    with pytest.raises(AnsibleFilterError) as err:
        cassandra_config_import({"cassandra.yaml": "keystore_password: hunter2"}, "41x", FACTS)
    msg = str(err.value)
    assert "hunter2" not in msg
    assert msg.startswith("cassandra_config_import: cassandra.yaml, ValueError in _config_import(), line ")
    assert err.value.__context__ is None and err.value.__cause__ is None
    assert cassandra_import_error({"failed": True, "msg": "templating failed: " + msg}) == msg


def test_layout_error_hides_its_message():
    with pytest.raises(AnsibleFilterError) as err:
        cassandra_inventory_layout([{"name": "n1", "dc": "dc1", "rack": "r1", "read": True, "vars": "hunter2"}], "c")
    assert "hunter2" not in str(err.value)
    assert re.match(r"cassandra_inventory_layout: \w+Error in ", str(err.value))


def test_import_error_keeps_what_shows_no_value():
    # ansible-core 2.19+: a loop's failed items; 2.16: the task's message only
    loop = {"failed": True, "msg": "One or more items failed", "results": [
        {"failed": False, "item": {"address": "10.0.0.1"}},
        {"failed": True, "item": {"address": "10.0.0.2", "dc": "dc1"},
         "msg": "Error while resolving value for '_nodes': object of type 'dict' has no attribute 'heap'"},
        {"failed": True, "item": {"address": "10.0.0.3"}, "msg": "could not convert string to float: 'hunter2'"}]}
    assert cassandra_import_error(loop) == "10.0.0.2: missing field 'heap'; 10.0.0.3: failed"
    old = {"failed": True, "msg": "The task includes an option with an undefined variable. The error was: "
                                  "'ansible.vars.hostvars.HostVarsVars object' has no attribute 'import_cluster_given'"}
    assert cassandra_import_error(old) == "missing field 'import_cluster_given'"
    assert cassandra_import_error({"failed": True, "msg": "'_given' is undefined"}) == "undefined variable '_given'"
    assert cassandra_import_error({"failed": True, "msg": "invalid literal for int(): 'hunter2'"}) == ""
    assert cassandra_import_error({"censored": "hidden"}) == ""
    assert cassandra_import_error(None) == ""


def test_missing_field_error_goes_through():
    # a missing field of the nodes' data (lazy templating) names the field, not a value
    with pytest.raises(AnsibleUndefinedVariable):
        cassandra_inventory_layout([{"name": "n1", "dc": "dc1", "rack": "r1", "read": True,
                                     "vars": _Missing()}], "c")


class _Missing(dict):
    def __iter__(self):
        raise AnsibleUndefinedVariable("'dict object' has no attribute 'heap'")


def test_other_undefined_errors_stay_hidden():
    # jinja's "has no element <key>" quotes a key of the data: hidden
    class Element(dict):
        def __iter__(self):
            raise AnsibleUndefinedVariable("'dict object' has no element 'hunter2'")
    with pytest.raises(AnsibleFilterError) as err:
        cassandra_inventory_layout([{"name": "n1", "dc": "dc1", "rack": "r1", "read": True, "vars": Element()}], "c")
    assert "hunter2" not in str(err.value)


def test_safe_reasons_only_match_the_error_shapes():
    # a value shaped like the error text is not picked up
    assert cassandra_import_error({"failed": True, "msg": "bad value: pw has no attribute 'hunter2' here"}) == ""
    assert cassandra_import_error({"failed": True, "msg": "bad value: x 'hunter2' is undefined here"}) == ""


def test_password_in_a_value_is_a_secret():
    # the unit's Environment, imported as cassandra_service_environment
    env = {"LOCAL_JMX": "no", "JVM_EXTRA_OPTS": "-Djavax.net.ssl.keyStorePassword=xyz -Dx=1"}
    files = cassandra_inventory_files({"group_vars": {"c": {"cassandra_service_environment": env,
                                                            "cassandra_cluster_name": "Prod"}},
                                       "host_vars": {}})
    assert [f["path"] for f in files] == ["group_vars/c/main.yml", "group_vars/c/secrets.yml"]
    assert "xyz" not in files[0]["content"] and "xyz" in files[1]["content"]


def test_medusa_keys_are_secrets_and_empty_values_are_not():
    files = cassandra_inventory_files({"group_vars": {"c": {
        "cassandra_medusa_s3_access_key_id": "AKIA",
        "cassandra_medusa_extra_settings": {"storage": {"sse_c_key": "k"}},
        "cassandra_medusa_cql_password": "",
        "cassandra_medusa_bucket_name": "b"}}, "host_vars": {}})
    by_path = {f["path"]: yaml.safe_load(f["content"]) for f in files}
    assert sorted(by_path["group_vars/c/secrets.yml"]) == ["cassandra_medusa_extra_settings",
                                                           "cassandra_medusa_s3_access_key_id"]
    assert sorted(by_path["group_vars/c/main.yml"]) == ["cassandra_medusa_bucket_name", "cassandra_medusa_cql_password"]


def test_inventory_files_note_what_the_nodes_cannot_tell():
    files = cassandra_inventory_files({"group_vars": {"c": {"cassandra_install_method": "packages",
                                                            "cassandra_package_version": "5.0.7"}}, "host_vars": {}})
    assert files[0]["content"] == """# Versions & packages
cassandra_package_version: 5.0.7
cassandra_install_method: packages
# TODO: cassandra_install_url: the directory of the package files, for new nodes (not read from the nodes)
"""


def test_inventory_files_grouped_by_subject():
    variables = {"cassandra_zzz_unknown": 1, "cassandra_concurrent_writes": 8, "cassandra_version": "50x",
                 "cassandra_seeds": ["i1", "i3"], "cassandra_cluster_name": "Imp Test", "cassandra_heap_size": "256M",
                 "cassandra_listen_address": IPV4, "cassandra_extra_settings": {"b": 1, "a": "x: y"},
                 "cassandra_medusa_venv": "/opt/medusa", "cassandra_medusa_version": "0.22.0",
                 "cassandra_medusa_link_dir": "/usr/local/bin", "cassandra_concurrent_reads": 8,
                 "cassandra_service_restart": "always", "cassandra_jmx_username": "admin", "cassandra_log_level": "INFO",
                 "cassandra_data_dir": "/data", "cassandra_package_version": "5.0.9", "cassandra_rolling_progress_dir": "/x"}
    files = cassandra_inventory_files({"group_vars": {"c": variables}, "host_vars": {}})
    assert files[0]["content"] == """# Cluster & topology
cassandra_cluster_name: Imp Test
cassandra_seeds:
- i1
- i3

# Versions & packages
cassandra_version: 50x
cassandra_package_version: 5.0.9

# Directories
cassandra_data_dir: /data
cassandra_rolling_progress_dir: /x

# Network & ports
cassandra_listen_address: '{{ ansible_facts[''default_ipv4''][''address''] }}'

# JMX
cassandra_jmx_username: admin

# JVM & heap (cassandra-env.sh, jvm*-server.options)
cassandra_heap_size: 256M

# Other cassandra.yaml settings
cassandra_concurrent_reads: 8
cassandra_concurrent_writes: 8

# cassandra.yaml settings no variable covers
cassandra_extra_settings:
  a: 'x: y'
  b: 1

# Logging (logback.xml)
cassandra_log_level: INFO

# systemd unit & service
cassandra_service_restart: always

# Medusa
cassandra_medusa_version: 0.22.0
cassandra_medusa_venv: /opt/medusa
cassandra_medusa_link_dir: /usr/local/bin

# Other
cassandra_zzz_unknown: 1
"""


@pytest.mark.parametrize("variables", [
    {"cassandra_cluster_name": "Prod", "cassandra_dc": "dc1"},
    {"cassandra_version": "40x", "cassandra_num_tokens": 16, "cassandra_seeds": "a,b", "cassandra_package_version": "4.0.10",
     "cassandra_extra_settings": {"x": [1, {"y": None}], "long": "w " * 60, "quoted": "'#{{ x }}\n"},
     "cassandra_jvm_extra_options": ["-Da=1", "-XX:+Foo"], "cassandra_heap_size": "8G", "cassandra_compaction_throughput": "64MiB/s",
     "cassandra_auto_snapshot": True, "cassandra_row_cache_size": "0MiB", "cassandra_listen_address": IPV4,
     "cassandra_service_environment": {"LOCAL_JMX": "no"}, "cassandra_medusa_prefix": "", "other_var": "yes",
     "cassandra_storage_port": 7000, "cassandra_initial_token": "-9223372036854775808", "cassandra_float": 0.5,
     "cassandra_null": None, "cassandra_on": "on", "cassandra_date": "2026-01-01", "cassandra_octal": "0755"},
])
def test_inventory_files_same_values_as_a_plain_dump(variables):
    # the grouped file loads back to the same dict, types and quoting included
    files = cassandra_inventory_files({"group_vars": {"c": variables}, "host_vars": {}})
    content = "".join(f["content"] for f in files if f["path"] == "group_vars/c/main.yml")
    plain = yaml.safe_dump(variables, default_flow_style=False, sort_keys=True)
    # a value that looks like a template is written !unsafe (Ansible's loader knows the tag)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import Loader
    assert yaml.load(content, Loader=Loader) == yaml.safe_load(plain) == variables
    assert ("!unsafe '" in content) == ("cassandra_extra_settings" in variables)
    assert "\n\n\n" not in content and not content.startswith("\n") and content.endswith("\n")


def test_secrets_file_grouped_too():
    files = cassandra_inventory_files({"group_vars": {}, "host_vars": {"n1": {
        "cassandra_server_keystore_password": "k", "cassandra_jmx_password": "j", "cassandra_medusa_cql_password": "m"}}})
    assert files[0]["path"] == "host_vars/n1/secrets.yml"
    assert files[0]["content"] == ("# JMX\ncassandra_jmx_password: j\n\n# Other cassandra.yaml settings\n"
                                   "cassandra_server_keystore_password: k\n\n# Medusa\ncassandra_medusa_cql_password: m\n")


def test_differences_by_rack_and_counts():
    nodes = [node("n%d" % i, "dc1", "r1" if i < 3 else "r2", cassandra_heap_size="8G" if i < 3 else "16G")
             for i in range(1, 8)]
    lines = cassandra_inventory_layout(nodes, "c")["differences"].split("\n")
    assert lines[1:] == ['  cassandra_heap_size: "16G" on rack dc1/r2; "8G" on rack dc1/r1']
    nodes[0]["vars"]["cassandra_heap_size"] = "12G"
    lines = cassandra_inventory_layout(nodes, "c")["differences"].split("\n")
    assert lines[1:] == ['  cassandra_heap_size: "16G" on rack dc1/r2; "12G" on n1; "8G" on n2']


def test_differences_mask_passwords():
    nodes = [node("n1", "dc1", "r1", cassandra_truststore_password="a"),
             node("n2", "dc1", "r1", cassandra_truststore_password="b")]
    out = cassandra_inventory_layout(nodes, "c")
    assert out["differences"].split("\n")[1:] == ["  cassandra_truststore_password: **** on n1; **** on n2"]


def test_address_equal_to_the_hostname_is_the_fact():
    facts = dict(FACTS, hostname="node1")
    v = cassandra_config_import(node_files("41x", facts, cassandra_rpc_address="node1",
                                           cassandra_jmx_rmi_hostname="10.0.0.1"), "41x", facts)["vars"]
    assert v["cassandra_rpc_address"] == "{{ ansible_facts['hostname'] }}"
    assert v["cassandra_jmx_rmi_hostname"] == IPV4


def test_differences_name_the_nodes_of_other_values():
    nodes = [node("n%d" % i, "dc1", "r%d" % (i % 4), cassandra_heap_size="8G" if i % 2 else "16G") for i in range(8)]
    lines = cassandra_inventory_layout(nodes, "c")["differences"].split("\n")
    assert lines[1:] == ['  cassandra_heap_size: "16G" on 4 nodes; "8G" on n1, n3, n5, n7']


def test_secret_in_a_list_of_strings():
    nodes = [node("n1", "dc1", "r1", cassandra_jvm_extra_options=["-Dx.keyStorePassword=S3cr3t"]), node("n2", "dc1", "r1")]
    out = cassandra_inventory_layout(nodes, "c")
    assert "S3cr3t" not in out["report"]
    files = cassandra_inventory_files(out)
    assert [f["path"] for f in files if "S3cr3t" in f["content"]] == ["host_vars/n1/secrets.yml"]


@pytest.mark.parametrize("facts, target, alternative", [
    # the RPM's config edited in place: the role writes there too, no move to its own conf dir
    ({"os_family": "RedHat"}, "/etc/cassandra/default.conf", ""),
    ({"os_family": "RedHat"}, "/opt/cassandra/conf", ""),
    # already the role's
    ({"os_family": "RedHat"}, "/etc/cassandra/ansible.conf", None),
    ({"os_family": "RedHat"}, "", None),
    ({"os_family": "Debian"}, "/etc/cassandra", None),
])
def test_rpm_conf_dir_kept_where_the_node_reads_it(facts, target, alternative):
    facts = dict(FACTS, **facts)
    out = cassandra_config_import(node_files("50x", facts), "50x", facts, target)
    assert out["vars"].get("cassandra_rpm_conf_alternative") == alternative


def test_layout_what_is_left_as_it_is_goes_to_each_node():
    # same on every node, but a node added later must get the roles' setup: host_vars only
    keep = {"cassandra_linux_manage": False, "cassandra_service_unit_manage": False}
    nodes = [dict(node("n1", "dc1", "r1", cassandra_num_tokens=4), keep=keep),
             dict(node("n2", "dc1", "r1", cassandra_num_tokens=4), keep=keep),
             node("n3", "dc1", "r1", cassandra_num_tokens=4)]
    out = cassandra_inventory_layout(nodes, "c")
    assert out["group_vars"]["c"] == {"cassandra_num_tokens": 4}
    marked = dict(keep, cassandra_imported_host=True)  # these switches come from the import (add_node checks it)
    assert out["host_vars"] == {"n1": marked, "n2": marked}
    assert "none" in out["differences"]
    assert "LEFT AS IT IS" in out["report"]
    assert "the OS settings (kernel, limits, THP, swap, time sync, disks) (cassandra_linux_manage: false)" in out["report"]
    files = cassandra_inventory_files(out)
    n1 = next(f for f in files if f["path"] == "host_vars/n1/main.yml")
    assert n1["content"].startswith("# Left as it is on this node (set up another way)\n")
    assert yaml.safe_load(n1["content"]) == marked


def test_layout_left_as_it_is_on_every_node_stays_per_node():
    keep = {"cassandra_linux_manage": False}
    nodes = [dict(node(n, "dc1", "r1", cassandra_num_tokens=4), keep=keep) for n in ("n1", "n2")]
    nodes.append(dict(node("n3", "dc1", "r1"), read=False, reason="unreachable", keep=keep))
    out = cassandra_inventory_layout(nodes, "c")
    assert out["group_vars"]["c"] == {"cassandra_num_tokens": 4}
    marked = dict(keep, cassandra_imported_host=True)
    assert out["host_vars"] == {"n1": marked, "n2": marked, "n3": marked}
    assert "The roles leave their setup as it is (host_vars: cassandra_linux_manage false)" in out["report"]


def test_layout_node_not_read_names_what_it_keeps():
    # every switch its host_vars get, the package ones too
    keep = {"cassandra_linux_manage": False, "cassandra_firewall_manage": False, "cassandra_install_tools": False}
    out = cassandra_inventory_layout([node("n1", "dc1", "r1"), dict(node("n2", "dc1", "r1"), read=False, keep=keep)], "c")
    assert ("The roles leave their setup as it is (host_vars: cassandra_firewall_manage, cassandra_linux_manage,"
            " cassandra_install_tools false)") in out["report"]


def test_layout_value_kept_on_a_node_is_reported():
    keep = {"cassandra_service_unit_manage": False, "cassandra_log_dir": "/var/log/cassandra"}
    out = cassandra_inventory_layout([dict(node("n1", "dc1", "r1"), keep=keep)], "c")
    assert out["host_vars"] == {"n1": dict(keep, cassandra_imported_host=True)}


def test_layout_no_marker_for_the_repositories_alone():
    # a blank host misses nothing the package checks don't see: no marker, the operator's own switches stay theirs
    keep = {"cassandra_repository_manage": False}
    out = cassandra_inventory_layout([dict(node("n1", "dc1", "r1"), keep=keep)], "c")
    assert out["host_vars"] == {"n1": keep}


def test_layout_no_marker_without_a_switch():
    # a value kept (log dir) but every part set up by the roles: nothing for add_node to refuse
    keep = {"cassandra_log_dir": "/var/log/cassandra"}
    out = cassandra_inventory_layout([dict(node("n1", "dc1", "r1"), keep=keep)], "c")
    assert out["host_vars"] == {"n1": keep}
    assert "cassandra_log_dir: \"/var/log/cassandra\", as this node has it" in out["report"]


ACCESS_TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "templates",
                               "jmxremote.access.j2")


@pytest.mark.parametrize("access", [
    # the role's own lines
    "ops readwrite \\\n    create javax.management.monitor.*,javax.management.timer.* \\\n    unregister\nmon readonly\n",
    # plain readwrite, written by hand: written back so
    "ops readwrite\nmon readonly\n",
])
def test_jmx_access_written_back_as_imported(access):
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    with open(ACCESS_TEMPLATE) as f:
        content = f.read()
    users = _jmx_users("ops s3cret\nmon m0n\n", access)
    assert Templar(loader=DataLoader(), variables={"cassandra_jmx_users": users}).template(trust_as_template(content)) == access


STOCK = {"40x": "stock-4.0.21", "41x": "stock-4.1.12", "50x": "stock-5.0.9"}


def stock_yaml(series):
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "molecule",
                        "default", "files", STOCK[series], "cassandra.yaml.stock")
    with open(path) as f:
        return f.read()


def with_seeds_line(series, line):
    """The stock cassandra.yaml, its seeds line replaced by line (indent kept unless line has one)."""
    text = stock_yaml(series)
    stock = next(n for n in text.split("\n") if n.strip().startswith('- seeds: "127.0.0.1:7000"'))
    indent = stock[:len(stock) - len(stock.lstrip())]
    return text.replace(stock, line if line.startswith(" ") else indent + line)


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
@pytest.mark.parametrize("line, seeds", [
    ("- seeds: 10.0.0.35,10.0.0.36,10.0.0.37", ["10.0.0.35", "10.0.0.36", "10.0.0.37"]),
    ("- seeds: 10.0.0.35,10.0.0.36,10.0.0.37\n# - seeds: \"10.0.0.35,10.0.0.36\"", ["10.0.0.35", "10.0.0.36", "10.0.0.37"]),
    ("- seeds: '10.0.0.35,10.0.0.36'", ["10.0.0.35", "10.0.0.36"]),
    ("- seeds: \"10.0.0.35:7000,10.0.0.36:7000\"", ["10.0.0.35:7000", "10.0.0.36:7000"]),
    ("- seeds: 10.0.0.35, 10.0.0.36 ,10.0.0.37", ["10.0.0.35", "10.0.0.36", "10.0.0.37"]),
    ("- seeds: node1.example.com,node2.example.com  # the first two", ["node1.example.com", "node2.example.com"]),
    ("- seeds: 10.0.0.35", ["10.0.0.35"]),
])
def test_seeds_read_however_written(series, line, seeds):
    live = with_seeds_line(series, line)
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["vars"]["cassandra_seeds"] == seeds
    # round trip: the role writes back the same seed list
    env, ctx, dummy = _load_role(series, FACTS)
    ctx.update(out["vars"])
    written = yaml.safe_load("\n".join(_render(env, series, "cassandra.yaml", ctx)[1]))
    assert [s.strip() for s in written["seed_provider"][0]["parameters"][0]["seeds"].split(",")] == seeds


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_seeds_indented_otherwise(series):
    text = stock_yaml(series)
    stock = next(n for n in text.split("\n") if n.strip().startswith('- seeds: "127.0.0.1:7000"'))
    live = text.replace(stock, " " * (len(stock) - len(stock.lstrip()) + 2) + "- seeds: 10.0.0.35,10.0.0.36")
    assert yaml.safe_load(live)["seed_provider"][0]["parameters"][0]["seeds"] == "10.0.0.35,10.0.0.36"
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["vars"]["cassandra_seeds"] == ["10.0.0.35", "10.0.0.36"]
    assert out["hand_edits"] == []  # the role writes the same seeds, indented its way


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
@pytest.mark.parametrize("old, new, var, value", [
    ("cluster_name: 'Test Cluster'", "cluster_name: Prod Cluster", "cassandra_cluster_name", "Prod Cluster"),
    ("cluster_name: 'Test Cluster'", 'cluster_name: "Prod"  # do not change', "cassandra_cluster_name", "Prod"),
    ("num_tokens: 16", "num_tokens: 256 # legacy", "cassandra_num_tokens", 256),
    ("endpoint_snitch: SimpleSnitch", 'endpoint_snitch: "GossipingPropertyFileSnitch"', "cassandra_endpoint_snitch",
     "GossipingPropertyFileSnitch"),
    ("partitioner: org.apache.cassandra.dht.Murmur3Partitioner", "partitioner: 'org.apache.cassandra.dht.RandomPartitioner'",
     "cassandra_partitioner", "org.apache.cassandra.dht.RandomPartitioner"),
])
def test_identity_read_however_written(series, old, new, var, value):
    text = stock_yaml(series)
    assert re.search("^" + re.escape(old) + "$", text, re.M)
    live = re.sub("^" + re.escape(old) + "$", new, text, flags=re.M)
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["vars"][var] == value
    assert out["hand_edits"] == []  # same setting, written the role's way
    stock = cassandra_config_import({"cassandra.yaml": text}, series, FACTS)["normalized"]
    added = [n for n in out["normalized"] if n not in stock]
    assert len(added) == 1 and added[0].endswith("(node: %s)" % new)


def test_quoted_number_read_as_a_number():
    live = stock_yaml("41x").replace("\nnum_tokens: 16\n", '\nnum_tokens: "8"\n')
    assert cassandra_config_import({"cassandra.yaml": live}, "41x", FACTS)["vars"]["cassandra_num_tokens"] == 8


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
@pytest.mark.parametrize("drop, name", [
    (r"^cluster_name:.*$", "cluster_name"),
    (r"^num_tokens:.*$", "num_tokens: ''"),
    (r"^partitioner:.*$", "partitioner"),
    (r"^endpoint_snitch:.*$", "endpoint_snitch"),
    (r"^(\s*- seeds:).*$", "seeds"),
])
def test_identity_not_read_fails(series, drop, name):
    live = re.sub(drop, r"\1 ''" if "seeds" in drop else name if ":" in name else "", stock_yaml(series), count=1,
                  flags=re.M)
    name = name.split(":")[0]
    with pytest.raises(AnsibleFilterError, match=r"cannot read %s from cassandra.yaml" % name) as err:
        cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    # the reason reaches the report: it names settings, no value
    assert cassandra_import_error({"failed": True, "msg": str(err.value)}).startswith(
        "cassandra_config_import: cannot read %s from cassandra.yaml" % name)


def test_identity_of_a_file_that_is_not_yaml_fails():
    with pytest.raises(AnsibleFilterError, match=r"cassandra.yaml is not valid YAML \(line 2\)$") as err:
        cassandra_config_import({"cassandra.yaml": "cluster_name: [\n"}, "40x", FACTS)
    assert cassandra_import_error({"failed": True, "msg": str(err.value)}) == \
        "cassandra_config_import: cassandra.yaml is not valid YAML (line 2)"


def test_storage_compatibility_mode_absent_is_cassandra_4():
    live = re.sub(r"^storage_compatibility_mode:.*$", "", stock_yaml("50x"), flags=re.M)
    assert cassandra_config_import({"cassandra.yaml": live}, "50x", FACTS)["vars"][
        "cassandra_storage_compatibility_mode"] == "CASSANDRA_4"


@pytest.mark.parametrize("line, value", [
    ("concurrent_reads: 64  # tuned", 64),
    ('concurrent_reads: "64"', 64),  # the number SnakeYAML gives the int setting, not the text "64"
])
def test_other_setting_read_however_written(line, value):
    live = re.sub(r"^concurrent_reads:.*$", line, stock_yaml("41x"), flags=re.M)
    assert cassandra_config_import({"cassandra.yaml": live}, "41x", FACTS)["vars"]["cassandra_concurrent_reads"] == value


def test_num_tokens_absent_is_one():
    live = re.sub(r"^num_tokens:.*$", "# num_tokens: 16\ninitial_token: -9223372036854775808", stock_yaml("41x"),
                  flags=re.M)
    assert cassandra_config_import({"cassandra.yaml": live}, "41x", FACTS)["vars"]["cassandra_num_tokens"] == 1


def test_invalid_yaml_said_so_without_values():
    live = re.sub(r"^cluster_name:.*$", "cluster_name: a: s3cret", stock_yaml("41x"), flags=re.M)
    with pytest.raises(AnsibleFilterError, match=r"cassandra.yaml is not valid YAML \(line \d+\)$"):
        cassandra_config_import({"cassandra.yaml": live}, "41x", FACTS)


@pytest.mark.parametrize("name, seeds", [("0123", "010"), ("1_000", "10.0.0.1"), ("12:30", "10.0.0.1")])
def test_identity_text_not_a_number(name, seeds):
    """cluster_name and seeds are text for Cassandra: not the number PyYAML makes of them."""
    live = re.sub(r"^cluster_name:.*$", "cluster_name: " + name, stock_yaml("41x"), flags=re.M)
    live = re.sub(r"^(\s*)- seeds:.*$", r"\1- seeds: " + seeds, live, count=1, flags=re.M)
    got = cassandra_config_import({"cassandra.yaml": live}, "41x", FACTS)["vars"]
    assert (got["cassandra_cluster_name"], got["cassandra_seeds"]) == (name, [seeds])


# Passwords YAML would not read back as written unquoted, and plain ones
PASSWORDS = ["cassandra", "abc #def", "a: b", "x #", "@x", "%x", "*x", "&x", "!x", "{x}", "[x]", "-x", "? x", "|x", ">x",
             "'q'", '"d"', "it's", "yes", "Off", "null", "~", "123", "0123", "1_000", "12:30", "a\\b", "p@ss/w=rd+1",
             " lead", "trail ", "{{ x }}", "{%x", ""]


def secret_vars(series):
    """The variables of the cassandra.yaml passwords the role quotes."""
    with open(os.path.join(cassandra_import.ROLE, "templates", cassandra_import.SERIES[series], "cassandra.yaml.j2")) as f:
        return sorted(set(re.findall(r"\((\w+)(?: or '[^']*')?\) \| regex_replace", f.read())))


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
def test_every_password_line_is_quoted(series):
    with open(os.path.join(cassandra_import.ROLE, "templates", cassandra_import.SERIES[series], "cassandra.yaml.j2")) as f:
        lines = [line for line in f.read().split("\n") if re.search(r"password: .*\{\{", line)]
    assert len(lines) == {"40x": 5, "41x": 6, "50x": 7}[series]
    assert [line for line in lines if not re.search(r"\w*password: \{\{ %s \}\}$" % cassandra_import.QUOTED, line)] == []


@pytest.mark.parametrize("role, node, same", [
    ("keystore_password: '0123'", "keystore_password: 0123", True),  # the same text for Cassandra
    ("keystore_password: 'null'", "keystore_password: null", False),  # null is no value
    ("keystore_password: ''", "keystore_password:", False),
    ("        - keystore: ''", "        - keystore:", False),  # in a list item too
])
def test_same_setting_as_text_but_null(role, node, same):
    assert _same_setting("cassandra.yaml", role, node) is same


@pytest.mark.parametrize("line", ["key_password: null", "key_password: ~", "key_password:", 'key_password: "a\\nb"'])
def test_password_not_read_as_text_is_a_hand_edit(line):
    """No value is null for Cassandra, not the text null; a line break would not render back: left as they are."""
    live = re.sub(r"^(\s*)key_password:.*$", lambda m: m.group(1) + line, stock_yaml("50x"), count=1, flags=re.M)
    out = cassandra_config_import({"cassandra.yaml": live}, "50x", FACTS)
    assert "cassandra_tde_key_password" not in out["vars"]
    assert ["+"] + cassandra_import._mask(line).split() in [h.split() for h in out["hand_edits"]]
    assert not [h for h in out["hand_edits"] if re.search(r"\bb\b", h)]  # no line of a password unmasked


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
@pytest.mark.parametrize("password", PASSWORDS)
def test_password_round_trip(series, password):
    """Written by the role as YAML reads it back, imported as the password itself."""
    secrets = secret_vars(series)
    live = node_files(series, **dict((v, password) for v in secrets))["cassandra.yaml"]
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    env, ctx, dummy = _load_role(series, FACTS)
    assert dict((v, out["vars"].get(v, ctx[v])) for v in secrets) == dict((v, password) for v in secrets)
    assert (out["hand_edits"], out["normalized"]) == ([], [])
    if password:  # "" leaves the 5.0 ones commented out
        assert yaml.safe_load(live)["transparent_data_encryption_options"]["key_provider"][0]["parameters"][0][
            "key_password"] == password


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
@pytest.mark.parametrize("line, password", [
    ('"#abc"', "#abc"), ('"*abc"', "*abc"), ("'a # b'", "a # b"), ('"x: y"', "x: y"), ("'it''s'", "it's"),
    ('"say \\"hi\\""', 'say "hi"'), ("'0123'", "0123"), ("0123", "0123"), ("yes", "yes"), ('"S3cr3t" # rotated', "S3cr3t"),
    ("'a #b'   # rotated", "a #b"),
])
def test_password_however_quoted(series, line, password):
    live = re.sub(r"^(\s*key_password:).*$", lambda m: m.group(1) + " " + line, stock_yaml(series), count=1, flags=re.M)
    assert "key_password: " + line in live
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["vars"]["cassandra_tde_key_password"] == password
    assert out["hand_edits"] == []


@pytest.mark.parametrize("line", ["keystore_password: 'it''s a secret'", 'keystore_password: "a \\" b"',
                                  "keystore_password: plain # c"])
def test_mask_hides_the_whole_quoted_value(line):
    masked = cassandra_import._mask(line)
    assert masked.startswith("keystore_password: ****") and not re.search(r"secret|\bb\b|plain", masked)


def test_quoted_secret_with_a_comment_not_in_the_report():
    live = re.sub(r"^(\s*keystore_password:).*$", r'\1 "S3cr3t" # rotated', stock_yaml("50x"), count=1, flags=re.M)
    out = cassandra_config_import({"cassandra.yaml": live}, "50x", FACTS)
    assert "S3cr3t" not in "\n".join(out["hand_edits"] + out["normalized"])


@pytest.mark.parametrize("series", ["40x", "41x", "50x"])
@pytest.mark.parametrize("line, name", [("cluster_name: 'it''s'", "it's"), ("cluster_name: ' Prod '", " Prod ")])
def test_cluster_name_round_trip(series, line, name):
    live = re.sub(r"^cluster_name:.*$", line, stock_yaml(series), flags=re.M)
    out = cassandra_config_import({"cassandra.yaml": live}, series, FACTS)
    assert out["vars"]["cassandra_cluster_name"] == name
    env, ctx, dummy = _load_role(series, FACTS)
    ctx.update(out["vars"])
    assert yaml.safe_load("\n".join(_render(env, series, "cassandra.yaml", ctx)[1]))["cluster_name"] == name


@pytest.mark.parametrize("line, value", [("auto_snapshot: yes", True), ("auto_snapshot: False", False)])
def test_bool_read_however_written(line, value):
    live = re.sub(r"^auto_snapshot:.*$", line, stock_yaml("41x"), flags=re.M)
    assert cassandra_config_import({"cassandra.yaml": live}, "41x", FACTS)["vars"].get(
        "cassandra_auto_snapshot", True) is value


def _vaulted(text, password="pw"):
    from ansible.parsing.vault import VaultLib, VaultSecret
    return VaultLib([("default", VaultSecret(password.encode()))]).encrypt(text).decode()


def _read(found):
    """What the playbook reads: the files it looks at, not the others (None)."""
    return dict((p, t) for p, t in found.items() if t is not None)


def test_reimport_keeps_what_it_did_not_write():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_generated, cassandra_inventory_leftovers)
    ours = GENERATED % "prod"
    assert cassandra_inventory_generated("a: 1\n", "prod") == ours + "\na: 1\n"
    secret, mine = _vaulted(ours + "\np: 1\n"), _vaulted("p: 1\n")
    found = {
        "prod.yml": ours,
        "group_vars/prod/main.yml": ours,                      # written again
        "group_vars/all/mirror.yml": "cassandra_install_url: x",  # the user's
        "group_vars/all/vault.yml": mine,                      # the user's, vaulted
        "group_vars/all/main.yml": "a: 1",                     # the user's, named like the import's
        "ansible.cfg": None, "notes.txt": None,                # not read
        "host_vars/gone/main.yml": ours,                       # a node gone from the ring
        "host_vars/gone/secrets.yml": secret,
        "host_vars/gone/notes/x.yml": None,                    # the user's, in the dir of a node gone
        "host_vars/old/main.yml": "# Written before the header existed",
        "host_vars/old/secrets.yml": mine,
        "host_vars/clear/secrets.yml": ours,
        "group_vars/prod_dc1/secrets.yml": mine,               # the user's own vault, next to a main.yml of the import
        "group_vars/prod_dc1/main.yml": ours,
    }
    written = ["prod.yml", "group_vars/prod/main.yml", "group_vars/prod_dc1/main.yml"]
    out = cassandra_inventory_leftovers(list(found), _read(found), written, "prod", "pw")
    assert out["stale"] == ["host_vars/clear/secrets.yml", "host_vars/gone/main.yml", "host_vars/gone/secrets.yml"]
    assert out["kept"] == ["ansible.cfg", "group_vars/all/main.yml", "group_vars/all/mirror.yml",
                           "group_vars/all/vault.yml", "group_vars/prod_dc1/secrets.yml", "host_vars/gone/notes/x.yml",
                           "host_vars/old/main.yml", "host_vars/old/secrets.yml", "notes.txt"]
    assert out["replaced"] == [] and out["unsure"] == [] and out["conflicts"] == []
    # without the password, a vaulted file cannot be told: kept
    out = cassandra_inventory_leftovers(list(found), _read(found), written, "prod")
    assert "host_vars/gone/secrets.yml" in out["kept"]


def test_reimport_names_what_it_replaces():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    out = cassandra_inventory_leftovers(["prod.yml", "group_vars/prod/main.yml"],
                                        {"prod.yml": "all:", "group_vars/prod/main.yml": GENERATED % "prod"},
                                        ["prod.yml", "group_vars/prod/main.yml"], "prod")
    assert out == {"stale": [], "kept": [], "replaced": ["prod.yml"], "unsure": [], "conflicts": []}


def test_reimport_header_only_at_the_top():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    out = cassandra_inventory_leftovers(["host_vars/x/main.yml"], {"host_vars/x/main.yml": "a: 1\n" + GENERATED % "prod"},
                                        [], "prod")
    assert out == {"stale": [], "kept": ["host_vars/x/main.yml"], "replaced": [], "unsure": [], "conflicts": []}


def test_header_names_the_cluster():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED_UNNAMED, cassandra_inventory_generated, written_for)
    assert written_for(cassandra_inventory_generated("a: 1\n", "cluster_a")) == "cluster_a"
    assert written_for(GENERATED_UNNAMED + "\na: 1\n") == ""
    assert written_for("# Written by hand\n") is None
    assert written_for("") is None and written_for(None) is None


# an inventory dir of two clusters: cluster_a.yml and cluster_b.yml, each cluster's group_vars and host_vars
HOSTS_A = """all:
  children:
    cluster_a:
      children:
        cluster_a_dc1:
          children:
            cluster_a_dc1_rack1:
              hosts:
                node1: {ansible_host: 10.100.100.1}
                gone: {ansible_host: 10.100.100.9}
"""
HOSTS_B = """all:
  children:
    cluster_b:
      children:
        cluster_b_dc1:
          children:
            cluster_b_dc1_rack1:
              hosts:
                node2: {ansible_host: 10.100.100.2}
"""
NEW_A = {"all": {"children": {"cluster_a": {"children": {"cluster_a_dc1": {"children": {
    "cluster_a_dc1_rack1": {"hosts": {"node1": {"ansible_host": "10.100.100.1"}}}}}}}}}}


def _two_clusters(a_line, b_line):
    return {"cluster_a.yml": a_line + "\n" + HOSTS_A, "cluster_b.yml": b_line + "\n" + HOSTS_B,
            "group_vars/all/main.yml": "cassandra_install_url: x",
            "group_vars/cluster_a/main.yml": a_line, "group_vars/cluster_a_dc1/main.yml": a_line,
            "host_vars/node1/main.yml": a_line, "host_vars/gone/main.yml": a_line,
            "group_vars/cluster_b/main.yml": b_line, "group_vars/cluster_b_dc1/main.yml": b_line,
            "host_vars/node2/main.yml": b_line}


WRITTEN_A = ["cluster_a.yml", "group_vars/cluster_a/main.yml", "group_vars/cluster_a_dc1/main.yml",
             "host_vars/node1/main.yml"]


def test_reimport_removes_only_its_own_files():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    found = _two_clusters(GENERATED % "cluster_a", GENERATED % "cluster_b")
    # the user's own files: in this cluster's dirs or no cluster's, listed as kept; in another cluster's, not
    found.update({"group_vars/cluster_b/local.yml": "a: 1", "group_vars/cluster_b_dc1.yml": "a: 1",
                  "group_vars/cluster_a/local.yml": "a: 1", "host_vars/node9/main.yml": "a: 1"})
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out == {"stale": ["host_vars/gone/main.yml"], "replaced": [], "unsure": [], "conflicts": [],
                   "kept": ["group_vars/all/main.yml", "group_vars/cluster_a/local.yml", "host_vars/node9/main.yml"]}


def test_files_of_an_earlier_release():
    """No cluster in their first line: a file is this cluster's when its hosts file alone names its group or
    host; another cluster's hosts file naming it, or none, leaves it as it is."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED_UNNAMED, cassandra_inventory_leftovers)
    found = _two_clusters(GENERATED_UNNAMED, GENERATED_UNNAMED)
    found["host_vars/nobody/main.yml"] = GENERATED_UNNAMED  # no hosts file names it
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out == {"stale": ["host_vars/gone/main.yml"], "kept": ["group_vars/all/main.yml"], "replaced": [],
                   "unsure": ["host_vars/nobody/main.yml"], "conflicts": []}
    # the same, read as cluster_b's re-import: cluster_a's files untouched
    out = cassandra_inventory_leftovers(list(found), found, ["cluster_b.yml", "host_vars/node2/main.yml"], "cluster_b",
                                        inventory={"all": {"children": {"cluster_b": {"hosts": {"node2": {}}}}}})
    assert out == {"stale": ["group_vars/cluster_b/main.yml", "group_vars/cluster_b_dc1/main.yml"],
                   "kept": ["group_vars/all/main.yml"], "replaced": [], "unsure": ["host_vars/nobody/main.yml"],
                   "conflicts": []}


# as an import from before the first line was written, moved by hand from inventories/cluster_a/hosts.yml
OLD_MAIN_A = "---\n\n\n# Cluster & Topology\ncassandra_cluster_name: Cluster A\n"


@pytest.mark.parametrize("first", ["---", "", "# moved from inventories/cluster_a/hosts.yml"])
def test_hosts_file_moved_from_a_dir_of_its_own(first):
    """An earlier import's hosts.yml moved by hand to <cluster group>.yml, without the import's first line, next to
    its group_vars and host_vars without it either (or with the unnamed one): this cluster's, replaced with a
    backup."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, GENERATED_UNNAMED, cassandra_inventory_leftovers)
    found = _two_clusters(GENERATED_UNNAMED, GENERATED % "cluster_b")
    found.update({"cluster_a.yml": first + "\n" + HOSTS_A, "group_vars/cluster_a/main.yml": OLD_MAIN_A,
                  "group_vars/cluster_a_dc1/main.yml": "---\ncassandra_dc: dc1\n", "host_vars/node1/main.yml": "---\n"})
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A,
                                        cluster_name="Cluster A")
    assert out == {"stale": ["host_vars/gone/main.yml"], "kept": ["group_vars/all/main.yml"],
                   "replaced": ["cluster_a.yml", "group_vars/cluster_a/main.yml", "group_vars/cluster_a_dc1/main.yml",
                                "host_vars/node1/main.yml"], "unsure": [], "conflicts": []}
    # without the all level, as an inventory may be written too
    found["cluster_a.yml"] = first + "\n" + yaml.safe_dump(yaml.safe_load(HOSTS_A)["all"]["children"])
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == [] and out["stale"] == ["host_vars/gone/main.yml"] and "cluster_a.yml" in out["replaced"]
    # another cluster whose name makes the same group: its main.yml tells, with no first line too
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A,
                                        cluster_name="cluster-a")
    assert out["conflicts"] == ["group_vars/cluster_a/main.yml: cluster 'Cluster A', not 'cluster-a' (two clusters,"
                                " one group cluster_a)"]


@pytest.mark.parametrize("text, conflicts", [
    (HOSTS_A + "    linux:\n      hosts: {node1: }\n",                 # a group of the user's next to the cluster's
     ["group cluster_a: in cluster_a.yml too", "group cluster_a_dc1: in cluster_a.yml too",
      "group cluster_a_dc1_rack1: in cluster_a.yml too"]),
    (HOSTS_A + "  vars: {x: 1}\n",                                        # vars on all
     ["group cluster_a: in cluster_a.yml too", "group cluster_a_dc1: in cluster_a.yml too",
      "group cluster_a_dc1_rack1: in cluster_a.yml too"]),
    (HOSTS_A + "  hosts: {web1: }\n",                                     # hosts on all
     ["group cluster_a: in cluster_a.yml too", "group cluster_a_dc1: in cluster_a.yml too",
      "group cluster_a_dc1_rack1: in cluster_a.yml too"]),
    ("cluster_a_dc1:\n  hosts: {node1: }\n", ["group cluster_a_dc1: in cluster_a.yml too"]),  # part of the cluster
])
def test_hosts_file_without_the_line_not_the_clusters_alone(text, conflicts):
    """<cluster group>.yml without the import's first line, holding more than this cluster's group: the user's."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_leftovers
    found = {"cluster_a.yml": text}
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == conflicts


def test_hosts_file_with_a_looping_anchor():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_leftovers
    found = {"cluster_a.yml": "cluster_a: &x\n  children:\n    sub: *x\n"}
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == [] and out["replaced"] == ["cluster_a.yml"]


def test_first_import_next_to_another_cluster():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    found = dict((p, t) for p, t in _two_clusters(GENERATED % "cluster_a", GENERATED % "cluster_b").items()
                 if "cluster_a" not in p and "node1" not in p and "gone" not in p)
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out == {"stale": [], "kept": ["group_vars/all/main.yml"], "replaced": [], "unsure": [], "conflicts": []}


def test_stops_on_another_clusters_names():
    """A host of another cluster: its host_vars are never written over, nor is the host in two clusters."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, GENERATED_UNNAMED, cassandra_inventory_leftovers)
    inventory = {"all": {"children": {"cluster_a": {"hosts": {"node1": {}, "node2": {}}}}}}
    written = WRITTEN_A + ["host_vars/node2/main.yml"]
    found = _two_clusters(GENERATED % "cluster_a", GENERATED % "cluster_b")
    out = cassandra_inventory_leftovers(list(found), found, written, "cluster_a", inventory=inventory)
    assert out["conflicts"] == ["host_vars/node2/main.yml: written for cluster_b", "node2: in cluster_b.yml too"]
    assert "host_vars/node2/main.yml" not in out["stale"] + out["kept"] + out["replaced"]
    # files of an earlier release: the same
    found = _two_clusters(GENERATED_UNNAMED, GENERATED_UNNAMED)
    out = cassandra_inventory_leftovers(list(found), found, written, "cluster_a", inventory=inventory)
    assert out["conflicts"] == ["host_vars/node2/main.yml: written for cluster_b", "node2: in cluster_b.yml too"]
    # a host no hosts file names, whose host_vars an earlier release wrote: not known to be this cluster's
    found["host_vars/node2/main.yml"] = GENERATED_UNNAMED
    found["cluster_b.yml"] = GENERATED_UNNAMED + "\nall: {}\n"
    out = cassandra_inventory_leftovers(list(found), found, written, "cluster_a", inventory=inventory)
    assert out["conflicts"] == ["host_vars/node2/main.yml: written by an earlier import, not known to be cluster_a's"]


def test_two_clusters_one_group():
    """Cluster names that make the same group: the second one never takes the first one's files, even forced."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    found = _two_clusters(GENERATED % "cluster_a", GENERATED % "cluster_b")
    found["group_vars/cluster_a/main.yml"] += "\ncassandra_cluster_name: Cluster A\n"
    clash = ["group_vars/cluster_a/main.yml: cluster 'Cluster A', not 'cluster-a' (two clusters, one group cluster_a)"]
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A,
                                        cluster_name="cluster-a")
    assert out["conflicts"] == clash
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A,
                                        cluster_name="Cluster A")
    assert out["conflicts"] == [] and out["stale"] == ["host_vars/gone/main.yml"]


@pytest.mark.parametrize("line, written, name, clash", [
    ("cassandra_cluster_name: !unsafe 'x{{a}}'", "x{{a}}", "X{{a}}", True),    # how the import writes it
    ("cassandra_cluster_name: !unsafe 'x{{a}}'", "x{{a}}", "x{{a}}", False),
    ("cassandra_cluster_name: '1.10'", "1.10", "1.1", True),
    ("cassandra_cluster_name: '1.10'", "1.10", "1.10", False),
    ('cassandra_cluster_name: "{{ my_name }}"', "", "Prod", False),             # a template of the user's: not told
])
def test_two_clusters_one_group_names(line, written, name, clash):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    found = {"x_a.yml": GENERATED % "x_a", "group_vars/x_a/main.yml": GENERATED % "x_a" + "\n" + line + "\n"}
    out = cassandra_inventory_leftovers(list(found), found, ["x_a.yml"], "x_a", cluster_name=name)
    assert bool(out["conflicts"]) is clash and (clash or out["stale"] == ["group_vars/x_a/main.yml"])


def test_two_clusters_one_group_written_by_an_earlier_release():
    """No cluster in the first line: its cluster name is checked too."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED_UNNAMED, cassandra_inventory_leftovers)
    found = {"prod.yml": GENERATED_UNNAMED + "\nall: {children: {prod: {hosts: {a1: }}}}\n",
             "host_vars/a1/main.yml": GENERATED_UNNAMED,
             "group_vars/prod/main.yml": GENERATED_UNNAMED + "\ncassandra_cluster_name: Prod\n"}
    out = cassandra_inventory_leftovers(list(found), found, ["prod.yml", "group_vars/prod/main.yml"], "prod",
                                        cluster_name="prod")
    assert out["conflicts"] == ["group_vars/prod/main.yml: cluster 'Prod', not 'prod' (two clusters, one group prod)"]


@pytest.mark.parametrize("path, text", [
    ("mine.yaml", "cluster_a_dc1:\n  hosts:\n    x9:\n"),
    ("mine.json", '{"cluster_a_dc1": {"hosts": {"x9": null}}}'),
    ("hosts", "[linux]\nnode1\n[cluster_a_dc1:children]  # its own\nweb\n"),
    ("sub/more.ini", "[cluster_a_dc1]\nx9\n"),
    ("hosts", "all: {children: {cluster_a_dc1: {hosts: {x9: }}}}\n"),       # YAML without an extension
    ("hosts.ini", "  [cluster_a_dc1:vars]\n  x=1\n"),                       # indented, as Ansible strips it
    ("own.json", '{\n\t"cluster_a_dc1": {\n\t\t"vars": {"x": 1}\n\t}\n}'),  # tabs: JSON, not YAML
    ("own.yml", "[cluster_a_dc1]\nx9\n"),                                   # INI in a .yml, as Ansible falls back
])
def test_groups_of_the_users_inventory_files_any_format(path, text):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_leftovers
    out = cassandra_inventory_leftovers([path], {path: text}, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == ["group cluster_a_dc1: in %s too" % path]
    # not read as inventory by Ansible (ignored extensions, its own dirs): not looked at
    for other in ("vars_plugins/" + path, "sub/group_vars/" + path, ".git/" + path):
        assert cassandra_inventory_leftovers([other], {other: text}, WRITTEN_A, "cluster_a",
                                             inventory=NEW_A)["conflicts"] == []
    out = cassandra_inventory_leftovers([path + ".bak"], {path + ".bak": text}, WRITTEN_A, "cluster_a",
                                        inventory=NEW_A)
    assert out["conflicts"] == []


def test_groups_of_the_users_own_inventory_files():
    """A group of this cluster's in an inventory file of the user's: Ansible would merge them. A host there is fine."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_leftovers
    found = {"linux.yml": "all:\n  children:\n    linux:\n      hosts: {node1: {}}\n    cluster_a_dc1:\n"
                          "      hosts:\n        web1:\n          p: !vault |\n            $ANSIBLE_VAULT;1.1;AES256\n"
                          "            3031\n"}
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == ["group cluster_a_dc1: in linux.yml too"] and out["kept"] == ["linux.yml"]


def test_tags_in_another_clusters_hosts_file():
    """An inline !vault value in another cluster's hosts file: its names still count."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    hosts = ("all:\n  children:\n    cluster_b:\n      hosts:\n        node1:\n          p: !vault |\n"
             "            $ANSIBLE_VAULT;1.1;AES256\n            3031\n        node7: !unsafe x\n")
    found = {"cluster_b.yml": GENERATED % "cluster_b" + "\n" + hosts}
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == ["node1: in cluster_b.yml too"]


@pytest.mark.parametrize("hosts", ["5", "[{x: 1}]", "nx1"])
def test_hosts_not_a_mapping(hosts):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, cassandra_inventory_leftovers)
    found = {"cluster_b.yml": GENERATED % "cluster_b" + "\nall:\n  children:\n    cluster_b:\n      hosts: %s\n" % hosts}
    inventory = {"all": {"children": {"cluster_a": {"hosts": {"x": {}, "n": {}}}}}}
    out = cassandra_inventory_leftovers(list(found), found, ["cluster_a.yml"], "cluster_a", inventory=inventory)
    assert out["conflicts"] == []


def test_hosts_file_of_an_import_into_a_dir_of_its_own():
    """The hosts.yml an earlier import wrote in a dir of its own: refused (Ansible would read its hosts too)."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED, GENERATED_UNNAMED, cassandra_inventory_leftovers)
    for header in (GENERATED_UNNAMED, GENERATED % "cluster_a"):
        # at the top, or in a subdir (Ansible reads the subdirs of an inventory dir too)
        for path in ("hosts.yml", "cluster_a/hosts.yml", "old/cluster_b.yml"):
            found = {path: header + "\n" + HOSTS_A, "cluster_a/report.txt": header}  # a report: not read by Ansible
            out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
            assert out["conflicts"] == ["%s: an import's hosts file of a dir of its own (an earlier layout), read by"
                                        " Ansible too: move it out of the inventory dir" % path], path
    # the hosts file of a cluster whose group is hosts: its own, re-imported
    found = {"hosts.yml": GENERATED % "hosts" + "\n" + HOSTS_B.replace("cluster_b", "hosts")}
    out = cassandra_inventory_leftovers(list(found), found, ["hosts.yml"], "hosts")
    assert out["conflicts"] == [] and out["replaced"] == []
    out = cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == []  # another cluster's, nothing in common
    # a hosts.yml of the user's: an inventory file like the others
    found = {"hosts.yml": HOSTS_B}
    assert cassandra_inventory_leftovers(list(found), found, WRITTEN_A, "cluster_a", inventory=NEW_A)["conflicts"] == []


@pytest.mark.parametrize("group", ["all", "ungrouped", "cassandra"])
def test_ansible_groups(group):
    """cassandra: the playbooks would take it without -e cassandra_hosts, whatever the other clusters."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_leftovers
    out = cassandra_inventory_leftovers([], {}, [group + ".yml"], group)
    assert out["conflicts"] == ["cluster group %s (from the cluster name): a group the playbooks or Ansible take on"
                                " their own, not one cluster's of an inventory dir" % group]


@pytest.mark.parametrize("path, text", [
    ("README", "[cluster_a_dc1] is our main datacenter\n"),           # not an INI section to Ansible
    ("README", "[cluster a dc1]\n"),
    ("hosts", "[parents:children]\ncluster_a_dc1\n"),                  # only named as a child: its hosts unchanged
    ("groups.yml", "all:\n  children:\n    parents:\n      children:\n        cluster_a_dc1:\n"),
    ("hosts.ini", "[cluster_a_dc1]\n# none yet\n[linux]\nnode1\n"),         # only declared
    ("old.bak/hosts", "[cluster_a_dc1]\nx9\n"),                             # a dir Ansible does not read
    ("loop.yml", "a: &x\n  children:\n    b: *x\n"),                        # no crash on an anchor that loops
])
def test_users_files_that_do_not_define_the_group(path, text):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_leftovers
    out = cassandra_inventory_leftovers([path], {path: text}, WRITTEN_A, "cluster_a", inventory=NEW_A)
    assert out["conflicts"] == [] and out["kept"] == [path]


def test_hosts_file_not_parsed():
    """A hosts file that does not parse names nothing: the files of an earlier release it would name are kept."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        GENERATED_UNNAMED, cassandra_inventory_leftovers)
    found = {"cluster_a.yml": GENERATED_UNNAMED + "\nall: [\n", "host_vars/gone/main.yml": GENERATED_UNNAMED}
    out = cassandra_inventory_leftovers(list(found), found, ["cluster_a.yml"], "cluster_a", inventory=NEW_A)
    assert out["stale"] == [] and out["unsure"] == ["host_vars/gone/main.yml"]


def test_same_secret_not_encrypted_again():
    from ansible.parsing.vault import VaultLib, VaultSecret
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_same_secret
    vaulted = VaultLib([("default", VaultSecret(b"pw"))]).encrypt("a: 1\n").decode()
    assert cassandra_inventory_same_secret(vaulted, "a: 1\n", "pw") is True
    assert cassandra_inventory_same_secret(vaulted, "a: 2\n", "pw") is False
    assert cassandra_inventory_same_secret(vaulted, "a: 1\n", "other") is False  # another password
    assert cassandra_inventory_same_secret("a: 1\n", "a: 1\n", "pw") is False   # in clear: encrypted now
    assert cassandra_inventory_same_secret("", "a: 1\n", "pw") is False
