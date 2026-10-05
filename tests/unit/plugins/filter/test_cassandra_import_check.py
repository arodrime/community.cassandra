from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The import self-check: the imported variables, rendered by the roles'
# templates, must give each node's files back setting by setting.

import os
import re

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    IPV4,
    _mask,
    cassandra_config_import,
    cassandra_inventory_files,
    cassandra_inventory_layout,
)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import (
    _compare_files,
    cassandra_import_self_check,
    cassandra_inventory_host_vars,
)

STOCK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "molecule",
                     "default", "files", "stock-%s")
VERSIONS = {"40x": "4.0.21", "41x": "4.1.12", "50x": "5.0.9"}
FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}


def stock(series):
    d = STOCK % VERSIONS[series]
    return dict((name[:-len(".stock")], open(os.path.join(d, name)).read()) for name in os.listdir(d))


def edit(text, old, new, count=1):
    assert old in text, old
    return text.replace(old, new, count)


def seeded(files, seeds='"10.0.0.1:7000"'):
    """A node of a real cluster: its seeds, dc/rack, its own address."""
    files = dict(files)
    files["cassandra.yaml"] = re.sub(r'(?m)^(\s*- seeds:).*$', r'\1 ' + seeds, files["cassandra.yaml"])
    files["cassandra.yaml"] = re.sub(r"(?m)^listen_address:.*$", "listen_address: 10.0.0.1", files["cassandra.yaml"])
    files["cassandra.yaml"] = re.sub(r"(?m)^endpoint_snitch:.*$", "endpoint_snitch: GossipingPropertyFileSnitch",
                                     files["cassandra.yaml"])
    files["cassandra-rackdc.properties"] = re.sub(r"(?m)^dc=.*$", "dc=dc1", files["cassandra-rackdc.properties"])
    files["cassandra-rackdc.properties"] = re.sub(r"(?m)^rack=.*$", "rack=r1", files["cassandra-rackdc.properties"])
    # the packages' log dir (the stock files are the tarball's)
    files["cassandra-env.sh"] = files["cassandra-env.sh"].replace('CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"',
                                                                  "CASSANDRA_LOG_DIR=/var/log/cassandra")
    return files


def import_node(files, series, facts=None, tamper=None, storage_dir="/var/lib/cassandra", keep=None):
    """What import_cluster does for one node: its variables, the inventory
    files. -> (inventory files, layout)"""
    facts = facts or FACTS
    imported = cassandra_config_import(files, series, facts, "", storage_dir)
    node = {"name": "n1", "address": "10.0.0.1", "hostname": "n1", "dc": "dc1", "rack": "r1", "read": True,
            "vars": dict(imported["vars"], cassandra_version=series, **(tamper or {})),
            "hand_edits": imported["hand_edits"], "normalized": imported["normalized"],
            "comments": imported["comments"], "notes": [],
            # a unit set up another way
            "keep": {"cassandra_service_unit_manage": False} if keep is None else keep}
    layout = cassandra_inventory_layout([node], imported["vars"].get("cassandra_cluster_name", "c"))
    return cassandra_inventory_files(layout), layout


def host_vars(files, series, facts=None):
    inventory, layout = import_node(files, series, facts)
    return cassandra_inventory_host_vars(inventory, layout["hosts"], "n1")


def check(files, series, facts=None, tamper=None, env=None, storage_dir="/var/lib/cassandra", java=None, conf_dir="",
          keep=None):
    facts = facts or FACTS
    inventory, layout = import_node(files, series, facts, tamper, storage_dir, keep)
    java = java or {"40x": "11", "41x": "11", "50x": "17"}[series]
    return cassandra_import_self_check(inventory, layout["hosts"], "n1", facts, files, env or {}, storage_dir, java,
                                       conf_dir)


# --- the variables a host gets from the files -------------------------------

def test_host_vars_child_groups_then_host_win():
    files = [{"path": "group_vars/c/main.yml", "content": "a: 1\nb: 1\nc: 1\n"},
             {"path": "group_vars/c_dc1/main.yml", "content": "b: 2\nc: 2\n"},
             {"path": "group_vars/c_dc1_r1/main.yml", "content": "c: 3\n"},
             {"path": "host_vars/n1/secrets.yml", "content": "d: '4'\n"},
             {"path": "host_vars/n2/main.yml", "content": "a: 9\n"}]
    hosts = {"all": {"children": {"c": {"children": {"c_dc1": {"children": {"c_dc1_r1": {"hosts": {"n1": {}, "n2": {}}}}}}}}}}
    assert cassandra_inventory_host_vars(files, hosts, "n1") == {"a": 1, "b": 2, "c": 3, "d": "4"}


def test_host_vars_of_a_host_not_there():
    with pytest.raises(Exception, match="not in the inventory"):
        cassandra_inventory_host_vars([], {"all": {"children": {}}}, "n1")


# --- whole files: stock, then edited the way people do ----------------------

@pytest.mark.parametrize("series", sorted(VERSIONS))
def test_stock_config_passes(series):
    out = check(seeded(stock(series)), series)
    assert out["differences"] == []


@pytest.mark.parametrize("series", sorted(VERSIONS))
def test_a_wrong_variable_is_caught(series):
    out = check(seeded(stock(series)), series, tamper={"cassandra_seeds": ["127.0.0.1:7000"]})
    assert out["differences"] == [
        "cassandra.yaml: seed_provider[0].parameters[0].seeds: node has '10.0.0.1:7000', import would write '127.0.0.1:7000'"]


@pytest.mark.parametrize("seeds", ['"10.0.0.1:7000, 10.0.0.2"', '"10.0.0.1:7000,10.0.0.2,"', "10.0.0.1:7000 , 10.0.0.2"])
def test_seeds_read_as_the_seed_provider_does(seeds):
    """SimpleSeedProvider trims each address and skips empty ones: spaces or a trailing comma are the same seeds."""
    assert check(seeded(stock("50x"), seeds=seeds), "50x")["differences"] == []


def test_unquoted_seeds_are_the_same_setting():
    # the node's seeds unquoted: as YAML, the same string as the role's quoted one
    rendered = {"cassandra.yaml": 'seed_provider:\n  - parameters:\n      - seeds: "a,b"\n'}
    live = {"cassandra.yaml": "seed_provider:\n  - parameters:\n      - seeds: a,b   # the seeds\n"}
    assert _compare_files(rendered, live)["differences"] == []


def test_addresses_written_as_facts_render_the_node_address():
    files = seeded(stock("50x"))
    assert host_vars(files, "50x")["cassandra_listen_address"] == "{{ ansible_facts['default_ipv4']['address'] }}"
    assert check(files, "50x")["differences"] == []
    inventory, layout = import_node(files, "50x")
    other = dict(FACTS, default_ipv4={"address": "10.0.0.9"})
    assert cassandra_import_self_check(inventory, layout["hosts"], "n1", other, files, {}, "/var/lib/cassandra",
                                       "17")["differences"] == [
        "cassandra.yaml: listen_address: node has '10.0.0.1', import would write '10.0.0.9'"]


def test_directories_left_out_are_under_the_storage_dir():
    role = {"cassandra.yaml": "commitlog_directory: /var/lib/cassandra/commitlog\n"}
    node = {"cassandra.yaml": "# commitlog_directory: /var/lib/cassandra/commitlog\n"}
    assert _compare_files(role, node, storage_dir="/var/lib/cassandra")["differences"] == []
    # a tarball's storagedir: $CASSANDRA_HOME/data
    assert _compare_files(role, node, storage_dir="/opt/cassandra/data")["differences"] == [
        "cassandra.yaml: commitlog_directory: node has '/opt/cassandra/data/commitlog',"
        " import would write '/var/lib/cassandra/commitlog'"]
    assert _compare_files(role, node)["differences"] != []  # storagedir unknown


def test_secrets_are_masked():
    rendered = {"cassandra.yaml": "server_encryption_options:\n  keystore_password: role\n",
                "cassandra-env.sh": 'JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.keyStorePassword=role"\n'}
    live = {"cassandra.yaml": "server_encryption_options:\n  keystore_password: node\n",
            "cassandra-env.sh": 'JVM_OPTS="$JVM_OPTS -Djavax.net.ssl.keyStorePassword=node"\n'}
    out = "\n".join(_compare_files(rendered, live)["differences"])
    assert "role" not in out and "node has '" not in out.replace("node has ****", "")
    assert "keystore_password: node has ****, import would write ****" in out
    assert "keyStorePassword=****" in out


# --- how each file is read --------------------------------------------------

@pytest.mark.parametrize("node, role, same", [
    ("num_tokens: 16\n", "num_tokens: '16'\n", True),  # quoting: Cassandra's setting is typed
    ("auto_snapshot: True\n", "auto_snapshot: true\n", True),
    ("hints_directory:\n", "", True),  # null: the default, as no key
    ("a: 1\na: 2\n", "a: 2\n", True),  # a duplicated key: the last one counts (SnakeYAML too)
    ("a: 1\na: 2\n", "a: 1\n", False),
    ("key_cache_size: 100MiB\n", "key_cache_size: 100MB\n", False),
    ("data_file_directories:\n  - /a\n  - /b\n", "data_file_directories:\n  - /a\n", False),
])
def test_yaml(node, role, same):
    assert (_compare_files({"cassandra.yaml": role}, {"cassandra.yaml": node})["differences"] == []) == same


@pytest.mark.parametrize("node, role, same", [
    ("dc = dc1 \nrack:r1\n", "dc=dc1\nrack=r1\n", True),
    ("# dc=dc2\ndc=dc1\n", "dc=dc1\n", True),
    ("dc=dc1\nprefer_local=true\n", "dc=dc1\n# prefer_local=true\n", False),
    ("dc=dc1 # main\n", "dc=dc1\n", False),  # a comment after a value is part of it
    ("dc=dc1\r\nprefer_local=true\r\n", "dc=dc1\n", False),  # CRLF: Java ends the line at \r, prefer_local on
    ("dc=dc1\r\nprefer_local=true\r\n", "dc=dc1\nprefer_local=true\n", True),
])
def test_properties(node, role, same):
    name = "cassandra-rackdc.properties"
    assert (_compare_files({name: role}, {name: node})["differences"] == []) == same


@pytest.mark.parametrize("node, role, same", [
    ("-Xss256k\n#-Xmx4G\n", "-Xss256k\n", True),
    ("  -Xmx4G\n", "", True),  # bin/cassandra takes the lines starting with '-' only
    ("-Xmx4G\n-Xmx8G\n", "-Xmx8G\n", True),  # the last one counts
    ("-XX:+UseG1GC\n", "-XX:-UseG1GC\n", False),
    ("-XX:MaxGCPauseMillis=300\n", "-XX:MaxGCPauseMillis=500\n", False),
    ("-Dcassandra.foo=1\n", "-Dcassandra.foo=2\n", False),
])
def test_jvm_options(node, role, same):
    name = "jvm-server.options"
    assert (_compare_files({name: role}, {name: node})["differences"] == []) == same


@pytest.mark.parametrize("node, role, same", [
    ('JMX_PORT=7199   # the port\n', 'JMX_PORT="7199"\n', True),
    ('# JMX_PORT="7299"\nJMX_PORT="7199"\n', 'JMX_PORT="7199"\n', True),
    ('JVM_OPTS="$JVM_OPTS -Dx=1"\n', '#JVM_OPTS="$JVM_OPTS -Dx=1"\n', False),
    ('JMX_PORT="${PORT:-7199}"\n', 'JMX_PORT="7199"\n', False),  # the environment may change it
])
def test_shell(node, role, same):
    name = "cassandra-env.sh"
    assert (_compare_files({name: role}, {name: node})["differences"] == []) == same


def test_heap_moved_from_the_unit_to_cassandra_env():
    rendered = {"cassandra-env.sh": 'MAX_HEAP_SIZE="8G"\nHEAP_NEWSIZE="800M"\n'}
    live = {"cassandra-env.sh": '#MAX_HEAP_SIZE="4G"\n#HEAP_NEWSIZE="800M"\n'}
    unit_env = {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "800M", "LOCAL_JMX": "no"}
    assert _compare_files(rendered, live, unit_env, {"LOCAL_JMX": "no"})["differences"] == []
    assert _compare_files(rendered, live, dict(unit_env, MAX_HEAP_SIZE="4G"), {})["differences"] == [
        "heap (cassandra-env.sh, else the unit's Environment): MAX_HEAP_SIZE: node has '4G', import would write '8G'"]
    # cassandra-env.sh sets it: the unit's does not count
    assert _compare_files({"cassandra-env.sh": 'MAX_HEAP_SIZE="8G"\n'}, {"cassandra-env.sh": "MAX_HEAP_SIZE=8G\n"},
                          {"MAX_HEAP_SIZE": "2G"}, {})["differences"] == []


def test_logback_comments_and_layout_do_not_count():
    role = '<configuration>\n  <root level="INFO">\n    <appender-ref ref="SYSTEMLOG" />\n  </root>\n</configuration>\n'
    node = '<configuration><!-- mine --><root level="INFO"><appender-ref ref="SYSTEMLOG"/></root></configuration>'
    assert _compare_files({"logback.xml": role}, {"logback.xml": node})["differences"] == []
    assert _compare_files({"logback.xml": role}, {"logback.xml": node.replace("INFO", "DEBUG")})["differences"] != []


def test_unit_environment_and_multiple_values():
    role = '[Service]\nEnvironment="LOCAL_JMX=no"\nEnvironment="A=1"\nLimitNOFILE=100000\n'
    node = '[Service]\n# mine\nEnvironment=A=1 LOCAL_JMX=no\nLimitNOFILE=100000\n'
    assert _compare_files({"cassandra.service": role}, {"cassandra.service": node})["differences"] == []
    assert _compare_files({"cassandra.service": role}, {"cassandra.service": node.replace("100000", "65536")})[
        "differences"] == ["cassandra.service: [Service] LimitNOFILE: node has '65536', import would write '100000'"]


def test_medusa_ini_as_configparser_reads_it():
    role = "[storage]\nbucket_name = b\nsecure = true\n"
    node = "; mine\n[storage]\nBUCKET_NAME=b\nsecure = True\n"
    assert _compare_files({"medusa.ini": role}, {"medusa.ini": node})["differences"] == []


def test_jmx_files_masked_and_rights_in_any_order():
    role = {"jmxremote.password": "ops s3cret\n", "jmxremote.access": "ops readwrite \\\n    create x \\\n    unregister\n"}
    node = {"jmxremote.password": "# users\nops  s3cret\n", "jmxremote.access": "ops readwrite unregister create x\n"}
    assert _compare_files(role, node)["differences"] == []
    out = _compare_files(role, dict(node, **{"jmxremote.password": "ops other\n"}))["differences"]
    assert out == ["jmxremote.password: ops: node has ****, import would write ****"]


def test_a_file_the_node_lacks_is_a_difference():
    out = _compare_files({"logback.xml": "<configuration/>\n"}, {})
    assert out == {"differences": ["logback.xml: not on the node, the roles would create it"], "notes": []}


def test_a_file_that_cannot_be_read_is_a_difference():
    out = _compare_files({"cassandra.yaml": "a: 1\n"}, {"cassandra.yaml": "a: [\n"})
    assert out["differences"] == ["cassandra.yaml: cannot be read as its program reads it (node's or the roles' version)"]


# --- the import reads settings however the node writes them -----------------
# (each was read as the role default before: the self-check found them)

WRITTEN_ANOTHER_WAY = [
    ("cassandra.yaml", r"(?m)^cluster_name:.*$", 'cluster_name: "Prod Cluster"'),  # double quotes
    ("cassandra.yaml", r"(?m)^cluster_name:.*$", "cluster_name: Prod   # prod"),  # none, and a comment
    ("cassandra.yaml", r"(?m)^num_tokens:.*$", "num_tokens: 8   # fewer vnodes"),
    ("cassandra.yaml", r"(?m)^concurrent_writes:.*$", "concurrent_writes: 32\nconcurrent_writes: 48"),  # last wins
    ("cassandra.yaml", r"(?m)^incremental_backups:.*$", "incremental_backups: True"),
    ("cassandra.yaml", r"(?m)^auto_snapshot:.*$", "auto_snapshot: no"),
    ("cassandra.yaml", r'(?m)^(\s*- seeds:).*$', r"\1 '10.0.0.1:7000,10.0.0.2:7000'"),  # single quotes
    ("cassandra.yaml", r'(?m)^(\s*- seeds:).*$', r'\1 "10.0.0.1:7000"  # the seed'),
    ("cassandra.yaml", r"(?m)^listen_address:.*$", "listen_address:\nlisten_interface: eth0"),  # a line added above
    ("cassandra.yaml", r"(?m)^rpc_address:.*$", "rpc_address: 0.0.0.0\nbroadcast_rpc_address: 10.0.0.1"),
    ("cassandra.yaml", r"(?m)^# commitlog_directory:.*$", "commitlog_directory: /commit/cassandra # ssd"),
    ("cassandra.yaml", r"(?m)^# data_file_directories:\n#     - /var/lib/cassandra/data$",
     "data_file_directories: [/data1/cassandra, /data2/cassandra]"),
    ("cassandra-env.sh", r'(?m)^#MAX_HEAP_SIZE=.*$', "MAX_HEAP_SIZE=8G"),  # no quotes
    ("cassandra-env.sh", r'(?m)^#MAX_HEAP_SIZE=.*$', 'MAX_HEAP_SIZE="8G"  # prod'),
    ("cassandra-env.sh", r'(?m)^JMX_PORT=.*$', "JMX_PORT=7299"),
    ("cassandra-env.sh", r'(?m)^(\s*)LOCAL_JMX=yes$', r"\1LOCAL_JMX=no"),
    ("cassandra-env.sh", r'(?m)^# JVM_OPTS="\$JVM_OPTS -Djava.rmi.server.hostname=<public name>"$',
     'JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.1"'),  # uncommented: no space left before it
    ("cassandra-rackdc.properties", r"(?m)^# prefer_local=true$", "prefer_local = true"),
    ("jvm-server.options", r"(?m)^#-Xmx.*$", "-Xmx4G"),  # a stock line switched on, no variable: kept as an extra option
    ("jvm-server.options", r"(?m)^(-Xss.*)$", r"\1\n-XX:+AlwaysPreTouch"),  # added in the middle
    ("jvm-server.options", r"(?m)^-Xss\S+$", "-Xss512k"),  # a stock line changed in place
    ("logback.xml", r'<root level="INFO">', '<root level="WARN">'),
]


@pytest.mark.parametrize("series", sorted(VERSIONS))
@pytest.mark.parametrize("name, pattern, replace", WRITTEN_ANOTHER_WAY,
                         ids=["%s:%s" % (c[0], c[2].split("\n")[-1][:40]) for c in WRITTEN_ANOTHER_WAY])
def test_settings_written_another_way_are_imported(series, name, pattern, replace):
    files = seeded(stock(series))
    files[name], found = re.subn(pattern, replace, files[name], count=1)
    if not found:
        pytest.skip("not in the %s stock %s" % (series, name))
    assert check(files, series)["differences"] == []


HAND_EDITS = [  # no variable covers them: the roles would change them, the self-check says so
    ("cassandra-env.sh", r'(?m)^(JMX_PORT=.*)$', r'\1\nJVM_OPTS="$JVM_OPTS -Dcassandra.custom=1"',
     'cassandra-env.sh: node has JVM_OPTS="$JVM_OPTS -Dcassandra.custom=1", import would write nothing'),
    ("cassandra-env.sh", r'(?m)^JMX_PORT=.*$', 'JMX_PORT="${JMX_PORT:-7299}"',  # the environment decides
     'cassandra-env.sh: node has JMX_PORT="${JMX_PORT:-7299}", import would write JMX_PORT=7199'),
    ("jvm-server.options", r"(?m)^(-Xss.*)$", r"#\1",  # a stock option switched off
     "jvm-server.options: -Xss: node has nothing, import would write '-Xss256k'"),
]


@pytest.mark.parametrize("name, pattern, replace, difference", HAND_EDITS)
def test_hand_edits_no_variable_covers_fail_the_check(name, pattern, replace, difference):
    files = seeded(stock("50x"))
    files[name] = re.sub(pattern, replace, files[name], count=1)
    assert check(files, "50x")["differences"] == [difference]


# --- values the import must keep as the node has them -----------------------

def enable(text, key, value):
    """The stock commented `#key: ...` line (maybe indented) set to value."""
    new, n = re.subn(r"(?m)^(\s*)#\s?%s:.*$" % key, r"\g<1>%s: %s" % (key, value), text, count=1)
    assert n, key
    return new


@pytest.mark.parametrize("value, imported", [
    ("0123", "0123"),  # text for Cassandra, not the octal 83
    ("1_000", "1_000"),
    ("12:30", "12:30"),
    ("Cassandra", "Cassandra"),  # not the role default cassandra, whatever the case
])
def test_text_values_are_kept_as_text(value, imported):
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = files["cassandra.yaml"].replace("keystore_password: cassandra",
                                                              "keystore_password: %s" % value, 1)
    variables = host_vars(files, "41x")
    assert [v for k, v in variables.items() if k.endswith("keystore_password")][0] == imported
    assert check(files, "41x")["differences"] == []


def test_a_number_as_text_is_caught():
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = files["cassandra.yaml"].replace("keystore_password: cassandra", "keystore_password: 0123", 1)
    key = [k for k in host_vars(files, "41x") if k.endswith("keystore_password")][0]
    assert check(files, "41x", tamper={key: 83})["differences"] != []


@pytest.mark.parametrize("line, var, value", [
    ("commitlog_directory: /data/commitlog  # ssd", "cassandra_commitlog_dir", "/data/commitlog"),
    ('commitlog_directory: "/data/commitlog"', "cassandra_commitlog_dir", "/data/commitlog"),
    ("hints_directory: '/data/hints'", "cassandra_hints_dir", "/data/hints"),
    ("native_transport_port: '9142'", "cassandra_native_transport_port", 9142),
])
def test_quotes_and_comments_are_not_part_of_the_value(line, var, value):
    files = seeded(stock("41x"))
    key = line.split(":")[0]
    files["cassandra.yaml"] = re.sub(r"(?m)^#?\s?%s:.*$" % key, line, files["cassandra.yaml"], count=1)
    assert host_vars(files, "41x")[var] == value
    assert check(files, "41x")["differences"] == []


def test_a_port_from_the_environment_is_not_a_port():
    files = seeded(stock("41x"))
    files["cassandra-env.sh"] = re.sub(r'(?m)^JMX_PORT=.*$', 'JMX_PORT="${JMX_PORT:-7299}"', files["cassandra-env.sh"])
    assert "cassandra_jmx_port" not in host_vars(files, "41x")
    assert check(files, "41x")["differences"] == [
        'cassandra-env.sh: node has JMX_PORT="${JMX_PORT:-7299}", import would write JMX_PORT=7199']


@pytest.mark.parametrize("password", ["ab{#cd", "a{{b}}c", "x{%y"])
def test_values_that_look_like_templates_are_written_unsafe(password):
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = files["cassandra.yaml"].replace("keystore_password: cassandra",
                                                              "keystore_password: '%s'" % password, 1)
    inventory, dummy = import_node(files, "41x")
    assert "!unsafe '%s'" % password in "".join(f["content"] for f in inventory)
    assert check(files, "41x")["differences"] == []


def test_a_template_in_a_value_read_back_is_caught():
    # a value not written !unsafe: Ansible templates it when the roles run, so does the check
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = re.sub(r"(?m)^cluster_name:.*$", "cluster_name: 'Prod{#x#}'", files["cassandra.yaml"])
    inventory, layout = import_node(files, "41x")
    inventory.append({"path": "host_vars/n1/main.yml", "content": "cassandra_cluster_name: 'Prod{#x#}'\n"})
    out = cassandra_import_self_check(inventory, layout["hosts"], "n1", FACTS, files, {}, "/var/lib/cassandra", "11")
    assert out["differences"] == ["cassandra.yaml: cluster_name: node has 'Prod{#x#}', import would write 'Prod'"]


@pytest.mark.parametrize("line, same", [
    ("prefer_local=TRUE", True),  # Boolean.parseBoolean
    ("prefer_local=false", True),  # as when not set
    ("prefer_local=true ", True),  # not trimmed: false for Cassandra, the role leaves it out
])
def test_prefer_local_as_the_snitch_reads_it(line, same):
    files = seeded(stock("41x"))
    files["cassandra-rackdc.properties"] = re.sub(r"(?m)^# prefer_local=true$", line, files["cassandra-rackdc.properties"])
    assert (check(files, "41x")["differences"] == []) == same


def test_prefer_local_trailing_space_is_not_prefer_local():
    files = seeded(stock("41x"))
    files["cassandra-rackdc.properties"] = re.sub(r"(?m)^# prefer_local=true$", "prefer_local=true ",
                                                  files["cassandra-rackdc.properties"])
    assert not host_vars(files, "41x").get("cassandra_prefer_local")


def test_logback_level_in_any_case():
    files = seeded(stock("50x"))
    files["logback.xml"] = files["logback.xml"].replace('<root level="INFO">', '<root level="info">')
    assert check(files, "50x")["differences"] == []


def test_a_file_the_roles_would_create_fails_when_the_running_java_reads_it():
    files = seeded(stock("41x"))
    del files["jvm11-server.options"]
    assert check(files, "41x", java="11")["differences"] == [
        "jvm11-server.options: not on the node, the roles would create it"]
    files = seeded(stock("41x"))
    del files["jvm8-server.options"]
    out = check(files, "41x", java="11")
    assert out["differences"] == []
    assert out["notes"] == ["jvm8-server.options: not on the node, the roles would create it (Java 11 does not read it)"]


def test_a_unit_the_roles_would_replace_must_be_read():
    files = seeded(stock("50x"))
    out = check(files, "50x", keep={})
    assert out["differences"] == ["cassandra.service: the roles would replace the unit, which could not be read"]


def test_the_config_dir_the_roles_would_write_to():
    files = seeded(stock("50x"))
    assert check(files, "50x", conf_dir="/etc/cassandra")["differences"] == []
    assert check(files, "50x", conf_dir="/etc/cassandra/conf")["differences"] == [
        "config dir: node reads /etc/cassandra/conf, the roles would write to /etc/cassandra"]


def test_a_tarball_storage_dir_is_imported():
    files = seeded(stock("50x"))
    out = check(files, "50x", storage_dir="/opt/cassandra/data")
    assert out["differences"] == []
    inventory, layout = import_node(files, "50x", storage_dir="/opt/cassandra/data")
    variables = cassandra_inventory_host_vars(inventory, layout["hosts"], "n1")
    assert variables["cassandra_commitlog_dir"] == "/opt/cassandra/data/commitlog"
    assert variables["cassandra_data_dir"] == "/opt/cassandra/data/data"


@pytest.mark.parametrize("node, role, same", [
    ("JVM_OPTS='$JVM_OPTS -Dx=1'\n", 'JVM_OPTS="$JVM_OPTS -Dx=1"\n', False),  # '' does not expand $JVM_OPTS
    ('JVM_OPTS="$JVM_OPTS -Dx=a" b\n', 'JVM_OPTS="$JVM_OPTS -Dx=a b"\n', False),  # b: a command
    ("X=a#b\n", "X=a\n", False),  # # in a word: not a comment
    ("X='a b'\n", 'X="a b"\n', True),
])
def test_shell_words(node, role, same):
    name = "cassandra-env.sh"
    assert (_compare_files({name: role}, {name: node})["differences"] == []) == same


def test_private_keys_are_masked():
    rendered = {"cassandra.yaml": "client_encryption_options:\n  ssl_context_factory:\n    parameters:\n"
                                  "      private_key: ROLE\n"}
    live = {"cassandra.yaml": rendered["cassandra.yaml"].replace("ROLE", "NODE")}
    out = "\n".join(_compare_files(rendered, live)["differences"])
    assert "ROLE" not in out and "NODE" not in out


def test_a_shell_expansion_is_not_a_path():
    # the tarball's cassandra-env.sh: its log dir is not a path the role could create
    files = seeded(stock("50x"))
    files["cassandra-env.sh"] = files["cassandra-env.sh"].replace("CASSANDRA_LOG_DIR=/var/log/cassandra",
                                                                  'CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"')
    assert "cassandra_log_dir" not in host_vars(files, "50x")
    assert check(files, "50x")["differences"] == [
        'cassandra-env.sh: node has CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs", import would write'
        " CASSANDRA_LOG_DIR=/var/log/cassandra"]


@pytest.mark.parametrize("value", ['"s3 #cret"', "'*star'", '"@x"', '"a: b"', "'%p'", '"yes"', "'True'", "0123"])
def test_values_plain_yaml_would_misread_keep_their_quotes(value):
    # the template writes this password unquoted: the node's quoting stays
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = files["cassandra.yaml"].replace("keystore_password: cassandra",
                                                              "keystore_password: %s" % value, 1)
    assert check(files, "41x")["differences"] == []


def test_a_quoted_yes_is_not_true():
    rendered = {"cassandra.yaml": "keystore_password: true\n"}
    live = {"cassandra.yaml": 'keystore_password: "yes"\n'}
    assert _compare_files(rendered, live)["differences"] != []


def test_the_rmi_line_of_the_former_template_is_read():
    # the role used to write it with a space before JVM_OPTS
    files = seeded(stock("41x"))
    files["cassandra-env.sh"] = re.sub(r'(?m)^# JVM_OPTS="\$JVM_OPTS -Djava.rmi.server.hostname=<public name>"$',
                                       ' JVM_OPTS="$JVM_OPTS -Djava.rmi.server.hostname=10.0.0.1"', files["cassandra-env.sh"])
    assert host_vars(files, "41x")["cassandra_jmx_rmi_hostname"] in ("10.0.0.1", IPV4)
    assert check(files, "41x")["differences"] == []  # the same words, whatever the space before them


def test_an_option_set_again_later_is_not_an_extra():
    files = seeded(stock("41x"))
    files["jvm-server.options"] = files["jvm-server.options"].replace(
        "-XX:+HeapDumpOnOutOfMemoryError", "-XX:-HeapDumpOnOutOfMemoryError\n-XX:+HeapDumpOnOutOfMemoryError", 1)
    assert "cassandra_jvm_extra_options" not in host_vars(files, "41x")
    assert check(files, "41x")["differences"] == []


@pytest.mark.parametrize("line", ["KS_PASSWORD='Sup3r S3cret'", 'export TRUSTSTORE_PASSWORD="Sup3rS3cret"',
                                  "KS_PASSWORD=Sup3r\\ S3cret"])
def test_quoted_secrets_are_masked(line):
    assert "S3cret" not in _mask(line)


@pytest.mark.parametrize("value", ["Yes", "TRUE", "off", "'on'"])
def test_a_text_setting_that_reads_like_a_boolean_stays_text(value):
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = files["cassandra.yaml"].replace("keystore_password: cassandra",
                                                              "keystore_password: %s" % value, 1)
    key = [k for k in host_vars(files, "41x") if k.endswith("keystore_password")][0]
    # 'on': the template writes this one unquoted, so the quotes stay in the value (YAML reads it back as on)
    assert host_vars(files, "41x")[key] == value
    assert check(files, "41x")["differences"] == []
    # a boolean written back instead of the text: caught
    assert check(files, "41x", tamper={key: value.strip("'").lower() in ("yes", "true", "on")})["differences"] != []


@pytest.mark.parametrize("value, imported", [("'true'", True), ('"false"', False), ("yes", True), ("Off", False)])
def test_a_boolean_setting_written_another_way(value, imported):
    files = seeded(stock("41x"))
    files["cassandra.yaml"] = re.sub(r"(?m)^hinted_handoff_enabled:.*$", "hinted_handoff_enabled: %s" % value,
                                     files["cassandra.yaml"])
    assert host_vars(files, "41x").get("cassandra_hinted_handoff_enabled", True) == imported
    assert check(files, "41x")["differences"] == []


@pytest.mark.parametrize("line", ["keystore_password: pa#ss", "keystore_password: ab'cd", "keystore_password: my pass"])
def test_yaml_secrets_are_masked_whole(line):
    assert _mask(line) == "keystore_password: ****"


# --- cassandra-rackdc.properties under a snitch that doesn't read it --------

def ring_import(series, members):
    """import_cluster on several nodes: [(name, files, ring dc, ring rack)] ->
    (inventory files, layout, {name: self-check})."""
    nodes, files_of = [], {}
    for i, (name, files, dc, rack) in enumerate(members):
        facts = dict(FACTS, hostname=name, default_ipv4={"address": "10.100.100.%d" % (i + 1)})
        imported = cassandra_config_import(files, series, facts, "", "/var/lib/cassandra")
        nodes.append({"name": name, "address": "10.100.100.%d" % (i + 1), "hostname": name, "dc": dc, "rack": rack,
                      "read": True, "vars": dict(imported["vars"], cassandra_version=series),
                      "hand_edits": imported["hand_edits"], "normalized": imported["normalized"],
                      "comments": imported["comments"], "notes": [],
                      "keep": {"cassandra_service_unit_manage": False}})
        files_of[name] = (files, facts)
    layout = cassandra_inventory_layout(nodes, "my_cluster")
    inventory = cassandra_inventory_files(layout)
    checks = dict((name, cassandra_import_self_check(inventory, layout["hosts"], name, facts, files, {},
                                                     "/var/lib/cassandra", {"40x": "11", "41x": "11", "50x": "17"}[series]))
                  for name, (files, facts) in files_of.items())
    return inventory, layout, checks


def with_snitch(files, snitch, dc, rack):
    files = seeded(files)
    files["cassandra.yaml"] = re.sub(r"(?m)^endpoint_snitch:.*$", "endpoint_snitch: " + snitch, files["cassandra.yaml"])
    files["cassandra-rackdc.properties"] = re.sub(r"(?m)^dc=.*$", "dc=" + dc, files["cassandra-rackdc.properties"])
    files["cassandra-rackdc.properties"] = re.sub(r"(?m)^rack=.*$", "rack=" + rack, files["cassandra-rackdc.properties"])
    return files


@pytest.mark.parametrize("series, snitch", [
    ("40x", "SimpleSnitch"), ("41x", "org.apache.cassandra.locator.SimpleSnitch"), ("50x", "SimpleSnitch")])
def test_simple_snitch_keeps_the_rackdc_lines_it_ignores(series, snitch):
    """SimpleSnitch: the ring says datacenter1/rack1 whatever the file says; the
    file keeps its own lines, the nodes' identity is the ring's."""
    racks = ["RACK_A", "RACK_A", "RACK_A", "RACK_B", "RACK_B"]
    members = [("node%d" % (i + 1), with_snitch(stock(series), snitch, "DC_EXAMPLE", rack), "datacenter1", "rack1")
               for i, rack in enumerate(racks)]
    inventory, layout, checks = ring_import(series, members)
    assert all(c["differences"] == [] for c in checks.values()), checks
    for i, rack in enumerate(racks):
        hv = cassandra_inventory_host_vars(inventory, layout["hosts"], "node%d" % (i + 1))
        assert (hv["cassandra_dc"], hv["cassandra_rack"]) == ("datacenter1", "rack1")
        assert (hv["cassandra_rackdc_dc"], hv["cassandra_rackdc_rack"]) == ("DC_EXAMPLE", rack)
    assert layout["group_vars"]["my_cluster"]["cassandra_rackdc_dc"] == "DC_EXAMPLE"
    assert "cassandra_rackdc_rack" not in layout["differences"]  # per node by nature
    assert "dc= and rack= kept as the nodes have them" in layout["report"]
    assert "SimpleSnitch does not read them" in layout["report"]


def test_simple_snitch_with_the_ring_values_in_the_file_needs_no_variable():
    members = [("node1", with_snitch(stock("40x"), "SimpleSnitch", "datacenter1", "rack1"), "datacenter1", "rack1")]
    inventory, layout, checks = ring_import("40x", members)
    assert checks["node1"]["differences"] == []
    assert not [k for gv in list(layout["group_vars"].values()) + list(layout["host_vars"].values())
                for k in gv if k.startswith("cassandra_rackdc_")]
    assert "dc= and rack= kept" not in layout["report"]


@pytest.mark.parametrize("snitch", ["GossipingPropertyFileSnitch", "org.apache.cassandra.locator.GossipingPropertyFileSnitch",
                                    "com.example.CustomSnitch"])
def test_a_snitch_that_reads_rackdc_takes_the_ring_values(snitch):
    """GossipingPropertyFileSnitch (or a class that may read the file): dc= and
    rack= are the node's dc and rack, the ring's."""
    members = [("node1", with_snitch(stock("50x"), snitch, "DC_EXAMPLE", "RACK_A"), "DC_EXAMPLE", "RACK_A")]
    inventory, layout, checks = ring_import("50x", members)
    assert checks["node1"]["differences"] == []
    assert not [k for gv in list(layout["group_vars"].values()) + list(layout["host_vars"].values())
                for k in gv if k.startswith("cassandra_rackdc_")]
    # the file says something else than the ring (edited since the node started): caught
    members = [("node1", with_snitch(stock("50x"), snitch, "DC_EXAMPLE", "RACK_B"), "DC_EXAMPLE", "RACK_A")]
    assert ring_import("50x", members)[2]["node1"]["differences"] == [
        "cassandra-rackdc.properties: rack: node has 'RACK_B', import would write 'RACK_A'"]


# --- comments of another release: not hand edits -----------------------------

def older_release(text):
    """cassandra.yaml as an older 4.0.x shipped it (kept by the package on upgrade):
    without the comment blocks later releases added (4.0.1 lacks these)."""
    for start, end in (("# Enable/disable transfering hints to a peer during decommission.",
                        "#transfer_hints_on_decommission: true\n\n"),
                       ("# Strategy to choose the batchlog storage endpoints.", "# batchlog_endpoint_strategy: random_remote\n\n")):
        i = text.index(start)
        text = text[:i] + text[text.index(end, i) + len(end):]
    return text.replace("# cannot go below one mebibyte.", "# cannot go below one megabyte.")


def test_stock_comments_of_another_release_are_not_hand_edits():
    files = seeded(stock("40x"))
    files["cassandra.yaml"] = older_release(files["cassandra.yaml"])
    imported = cassandra_config_import(files, "40x", FACTS, "", "/var/lib/cassandra")
    assert imported["hand_edits"] == []
    assert len(imported["comments"]) == 1 and imported["comments"][0].startswith("cassandra.yaml: ")
    assert "5 lines missing before line " in imported["comments"][0]
    assert check(files, "40x")["differences"] == []
    inventory, layout = import_node(files, "40x")
    assert "Comments only, no setting" in layout["report"]
    assert "No hand edit left" in layout["report"]


def test_an_option_commented_out_is_still_a_hand_edit():
    """A block with a commented-out option: the option is off, not a comment."""
    files = seeded(stock("50x"))
    files["jvm-server.options"] = re.sub(r"(?m)^(-Xss.*)$", r"# switched off\n#\1", files["jvm-server.options"], count=1)
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert "  - -Xss256k" in imported["hand_edits"]
    assert imported["comments"] == []


def test_a_rackdc_line_the_file_lacks_is_not_imported():
    """No rack= under SimpleSnitch: no variable (not the role default), the self-check says what the role would add."""
    files = with_snitch(stock("40x"), "SimpleSnitch", "DC_EXAMPLE", "RACK_A")
    files["cassandra-rackdc.properties"] = re.sub(r"(?m)^rack=.*\n", "", files["cassandra-rackdc.properties"])
    imported = cassandra_config_import(files, "40x", FACTS, "", "/var/lib/cassandra")
    assert imported["vars"]["cassandra_rackdc_dc"] == "DC_EXAMPLE"
    assert "cassandra_rackdc_rack" not in imported["vars"]
    inventory, layout, checks = ring_import("40x", [("node1", files, "datacenter1", "rack9")])
    assert checks["node1"]["differences"] == [
        "cassandra-rackdc.properties: rack: node has nothing, import would write 'rack9'"]


def test_a_setting_among_comment_lines_is_still_a_hand_edit():
    """A block of comments with a setting the role lacks: a hand edit, not comments only."""
    files = seeded(stock("50x"))
    files["cassandra-env.sh"] = edit(files["cassandra-env.sh"], "\n# ", "\n# edited by hand\nexport FOO_HAND=1\n# ")
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert "  + export FOO_HAND=1" in imported["hand_edits"]
    assert imported["comments"] == []


def test_logback_comments_are_not_hash_lines():
    """logback.xml: a line starting with # is text, not a comment."""
    files = seeded(stock("50x"))
    files["logback.xml"] = edit(files["logback.xml"], "<configuration", "# not a comment\n<configuration")
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    assert "  + # not a comment" in imported["hand_edits"]


def test_list_items_swapped_are_a_hand_edit():
    """The same lines in another order are not the same setting (a list's order counts)."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import _same_settings
    assert _same_settings("cassandra.yaml", ["    - /a", "    - /b"], ["    - /b", "    - /a"]) is None
    assert _same_settings("cassandra.yaml", ["    - /a", "# x", "    - /b"], ["# y", "    - /a", "    - /b"]) == [
        ("    - /a", "    - /a"), ("    - /b", "    - /b")]
