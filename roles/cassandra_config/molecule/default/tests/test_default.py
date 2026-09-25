import os

import pytest
import testinfra.utils.ansible_runner
import yaml

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')

FILES_DIR = os.path.join(os.path.dirname(__file__), '..', 'files')
STOCK_DIR = os.path.join(FILES_DIR, 'stock-5.0.9')
COMMON_FILES = ["cassandra.yaml", "cassandra-env.sh", "jvm-server.options", "cassandra-rackdc.properties", "logback.xml"]
FILES = COMMON_FILES + ["jvm11-server.options", "jvm17-server.options"]
FILES_4X = COMMON_FILES + ["jvm8-server.options", "jvm11-server.options"]
SERIES_4X = [("40x", "4.0.21"), ("41x", "4.1.12")]
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
    with open(os.path.join(STOCK_DIR, name + ".stock")) as f:
        expected = f.read().split("\n")
    changes = PACKAGED.get(name, {})
    expected = [changes.get(line, line) for line in expected]

    assert lines(host, f"{conf_dir(host)}/{name}") == expected


JVM_4X = ["jvm8-server.options", "jvm11-server.options"]


def stock_lines(version, name):
    with open(os.path.join(FILES_DIR, f"stock-{version}", name + ".stock")) as f:
        return [PACKAGED.get(name, {}).get(line, line) for line in f.read().split("\n")]


# Stock 4.x runs CMS: its jvm files match stock under CMS, the rest under G1 (default)
@pytest.mark.parametrize("series,version", SERIES_4X)
@pytest.mark.parametrize("name", FILES_4X)
def test_4x_defaults_match_stock(host, series, version, name):
    rendered = f"/tmp/cassandra-{series}-CMS/{name}" if name in JVM_4X else f"/tmp/cassandra-{series}/{name}"

    assert lines(host, rendered) == stock_lines(version, name)


def gc_flags(host, path):
    return [line for line in lines(host, path) if line.startswith(("-XX:+UseG1GC", "-XX:+UseConcMarkSweepGC", "-XX:+UseParNewGC"))]


@pytest.mark.parametrize("series", ["40x", "41x"])
@pytest.mark.parametrize("name", JVM_4X)
def test_4x_g1_by_default(host, series, name):
    jvm = lines(host, f"/tmp/cassandra-{series}/{name}")

    assert gc_flags(host, f"/tmp/cassandra-{series}/{name}") == ["-XX:+UseG1GC"]
    assert "-XX:MaxGCPauseMillis=300" in jvm
    assert "-XX:InitiatingHeapOccupancyPercent=70" in jvm


def test_50x_cms_on_java11_only(host):
    assert gc_flags(host, "/tmp/cassandra-50x-CMS/jvm11-server.options") == ["-XX:+UseConcMarkSweepGC"]
    assert gc_flags(host, "/tmp/cassandra-50x-CMS/jvm17-server.options") == []


def test_4x_has_no_jvm17_file(host):
    assert not host.file("/tmp/cassandra-41x/jvm17-server.options").exists


def test_40x_overrides(host):
    conf = yaml.safe_load(host.file("/tmp/cassandra-40x-override/cassandra.yaml").content_string)
    env = lines(host, "/tmp/cassandra-40x-override/cassandra-env.sh")

    assert conf["key_cache_save_period"] == 3600  # version-dependent default is overridable
    assert conf["read_request_timeout_in_ms"] == 7000
    assert 'MAX_HEAP_SIZE="512M"' in env
    assert 'HEAP_NEWSIZE="128M"' in env
    assert lines(host, "/tmp/cassandra-40x-override/jvm8-server.options")[-2:] == ["-Dmolecule.jvm8=1", ""]


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


def test_overrides_tls(host):
    conf = yaml.safe_load(host.file(f"{OVERRIDE_DIR}/cassandra.yaml").content_string)
    server, client = conf["server_encryption_options"], conf["client_encryption_options"]

    assert server["internode_encryption"] == "all"
    assert server["keystore_password"] == "s3cret"
    assert server["truststore_password"] == "tru5t"
    assert server["require_client_auth"] is True
    assert server["outbound_keystore"] == "/etc/cassandra/outbound.keystore"
    assert server["outbound_keystore_password"] == "0utb0und"
    assert client["enabled"] is True
    assert client["optional"] is False
    assert client["truststore"] == "/etc/cassandra/client.truststore"
    assert "keystore_password" not in client  # left commented: unset


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
