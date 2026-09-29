from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

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


def test_properties_compared_by_settings():
    files = node_files("50x", cassandra_dc="dc1", cassandra_rack="r1")
    files["cassandra-rackdc.properties"] = "rack=r1\ndc=dc1\n"
    out = cassandra_config_import(files, "50x", FACTS)
    assert not [h for h in out["hand_edits"] if "rackdc" in h]
    assert any("cassandra-rackdc.properties: same settings" in n for n in out["normalized"])
    files["cassandra-rackdc.properties"] = "dc=dc1\nrack=r1\nprefer_local=true\n"
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
    assert str(err.value).startswith("cassandra_inventory_layout: AttributeError in ")


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
    assert yaml.safe_load(content) == yaml.safe_load(plain) == variables
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
    assert out["host_vars"] == {"n1": keep, "n2": keep}
    assert "none" in out["differences"]
    assert "LEFT AS IT IS" in out["report"]
    assert "the OS settings (kernel, limits, THP, swap, time sync, disks) (cassandra_linux_manage: false)" in out["report"]
    files = cassandra_inventory_files(out)
    n1 = next(f for f in files if f["path"] == "host_vars/n1/main.yml")
    assert n1["content"].startswith("# Left as it is on this node (set up another way)\n")
    assert yaml.safe_load(n1["content"]) == keep


def test_layout_left_as_it_is_on_every_node_stays_per_node():
    keep = {"cassandra_linux_manage": False}
    nodes = [dict(node(n, "dc1", "r1", cassandra_num_tokens=4), keep=keep) for n in ("n1", "n2")]
    nodes.append(dict(node("n3", "dc1", "r1"), read=False, reason="unreachable", keep=keep))
    out = cassandra_inventory_layout(nodes, "c")
    assert out["group_vars"]["c"] == {"cassandra_num_tokens": 4}
    assert out["host_vars"] == {"n1": keep, "n2": keep, "n3": keep}
    assert "The roles leave their setup as it is" in out["report"]


def test_layout_value_kept_on_a_node_is_reported():
    keep = {"cassandra_service_unit_manage": False, "cassandra_log_dir": "/var/log/cassandra"}
    out = cassandra_inventory_layout([dict(node("n1", "dc1", "r1"), keep=keep)], "c")
    assert out["host_vars"] == {"n1": keep}
    assert "cassandra_log_dir: \"/var/log/cassandra\", as this node has it" in out["report"]


ACCESS_TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "tasks", "access.yml")


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
    with open(ACCESS_TASKS) as f:
        content = yaml.safe_load(f)[0]["vars"]["_cassandra_jmx_access_content"]
    users = _jmx_users("ops s3cret\nmon m0n\n", access)
    assert Templar(loader=DataLoader(), variables={"cassandra_jmx_users": users}).template(trust_as_template(content)) == access
