import os

import pytest
import testinfra.utils.ansible_runner
import yaml

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')

STOCK_DIR = os.path.join(os.path.dirname(__file__), '..', 'files', 'stock-5.0.9')
FILES = [
    "cassandra.yaml",
    "cassandra-env.sh",
    "jvm-server.options",
    "jvm11-server.options",
    "jvm17-server.options",
    "cassandra-rackdc.properties",
    "logback.xml",
]
OVERRIDE_DIR = "/tmp/cassandra-override"

# Deliberate differences from stock, as the deb/rpm packages ship them.
PACKAGED = {
    "cassandra.yaml": {
        "# hints_directory: /var/lib/cassandra/hints": "hints_directory: /var/lib/cassandra/hints",
        "# data_file_directories:": "data_file_directories:",
        "#     - /var/lib/cassandra/data": "    - /var/lib/cassandra/data",
        "# commitlog_directory: /var/lib/cassandra/commitlog": "commitlog_directory: /var/lib/cassandra/commitlog",
        "# saved_caches_directory: /var/lib/cassandra/saved_caches": "saved_caches_directory: /var/lib/cassandra/saved_caches",
    },
    "cassandra-env.sh": {
        '    CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"': "    CASSANDRA_LOG_DIR=/var/log/cassandra",
    },
}


def conf_dir(host):
    return "/etc/cassandra/conf" if host.system_info.distribution not in ("ubuntu", "debian") else "/etc/cassandra"


def lines(host, path):
    return host.file(path).content_string.split("\n")


@pytest.mark.parametrize("name", FILES)
def test_defaults_match_stock(host, name):
    with open(os.path.join(STOCK_DIR, name)) as f:
        expected = f.read().split("\n")
    changes = PACKAGED.get(name, {})
    expected = [changes.get(line, line) for line in expected]

    assert lines(host, f"{conf_dir(host)}/{name}") == expected


@pytest.mark.parametrize("name", FILES)
def test_defaults_file_mode(host, name):
    assert host.file(f"{conf_dir(host)}/{name}").mode == 0o644


def test_overrides_cassandra_yaml(host):
    conf = yaml.safe_load(host.file(f"{OVERRIDE_DIR}/cassandra.yaml").content_string)

    assert conf["cluster_name"] == "Molecule Cluster"
    assert conf["num_tokens"] == 4
    assert conf["endpoint_snitch"] == "GossipingPropertyFileSnitch"
    assert conf["data_file_directories"] == ["/data/cassandra/data"]
    assert conf["seed_provider"][0]["parameters"][0]["seeds"] == "10.0.0.1:7000,10.0.0.2:7000"


def test_overrides_cassandra_env(host):
    env = lines(host, f"{OVERRIDE_DIR}/cassandra-env.sh")

    assert 'MAX_HEAP_SIZE="512M"' in env
    assert "    LOCAL_JMX=no" in env


def test_overrides_jvm_options(host):
    server = lines(host, f"{OVERRIDE_DIR}/jvm-server.options")
    jvm11 = lines(host, f"{OVERRIDE_DIR}/jvm11-server.options")
    jvm17 = lines(host, f"{OVERRIDE_DIR}/jvm17-server.options")

    assert server[-3:] == ["-Dmolecule.a=1", "-Dmolecule.b=2", ""]
    assert "-XX:MaxGCPauseMillis=500" in jvm11  # common value
    assert "-XX:MaxGCPauseMillis=200" in jvm17  # per-file override wins
    assert "-XX:ParallelGCThreads=8" in jvm11
    assert "-XX:ParallelGCThreads=8" in jvm17
    assert "#-XX:ConcGCThreads=16" in jvm17  # unset stays commented as stock


def test_overrides_rackdc(host):
    rackdc = lines(host, f"{OVERRIDE_DIR}/cassandra-rackdc.properties")

    assert "dc=DC_A" in rackdc
    assert "rack=RACK_1" in rackdc
    assert "prefer_local=true" in rackdc


def test_overrides_logback(host):
    logback = host.file(f"{OVERRIDE_DIR}/logback.xml").content_string

    assert '<root level="WARN">' in logback
    assert '<!-- <appender-ref ref="ASYNCDEBUGLOG" /> -->' in logback
