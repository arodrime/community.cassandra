import os

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')

VENV = "/opt/medusa-venv"

# Medusa's own config loader reads the file the role wrote
LOAD = """
import pathlib
from medusa.config import load_config
c = load_config({}, pathlib.Path('/etc/medusa/medusa.ini'))
print(c.storage.storage_provider, c.storage.bucket_name, c.storage.host, c.storage.port, c.storage.secure,
      c.storage.prefix, c.storage.key_file, c.storage.transfer_max_bandwidth)
print(c.cassandra.cql_username, c.cassandra.cql_password, c.cassandra.nodetool_username,
      c.cassandra.nodetool_password, c.cassandra.nodetool_password_file_path, c.cassandra.nodetool_port,
      c.cassandra.config_file)
print(c.checks.enable_md5_checks)
"""


def test_medusa_version(host):
    cmd = host.run(VENV + "/bin/python -c \"from importlib.metadata import version; print(version('cassandra-medusa'))\"")
    assert cmd.rc == 0, cmd.stderr
    assert cmd.stdout.strip() == "0.30.1"


def test_medusa_in_path(host):
    link = host.file("/usr/local/bin/medusa")
    assert link.is_symlink
    assert link.linked_to == VENV + "/bin/medusa"
    cmd = host.run("medusa --help")
    assert cmd.rc == 0, cmd.stderr
    assert "backup" in cmd.stdout


def test_virtualenv_python(host):
    # RHEL 8's python3 is 3.6: python3.11 there; Ubuntu 24.04's python3 (3.12) fits
    expected = "3.11" if host.system_info.distribution in ("rhel", "redhat") else "3.12"
    cmd = host.run(VENV + "/bin/python -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])'")
    assert cmd.stdout.strip() == expected


def test_config_file_private(host):
    for path in ("/etc/medusa/medusa.ini", "/etc/medusa/credentials"):
        f = host.file(path)
        assert f.user == "cassandra"
        assert f.group == "cassandra"
        assert f.mode == 0o600


def test_medusa_reads_the_config(host):
    conf = "/etc/cassandra/conf" if host.system_info.distribution in ("rhel", "redhat") else "/etc/cassandra"
    host.run("cat > /tmp/medusa_load.py <<'EOF'\n" + LOAD + "EOF\n")
    cmd = host.run(VENV + "/bin/python /tmp/medusa_load.py")
    assert cmd.rc == 0, cmd.stderr
    assert cmd.stdout.splitlines() == [
        "s3_compatible backups s3.example.com 443 true orders /etc/medusa/credentials 50MB/s",
        "medusa cql-secret cassops None /etc/cassandra/jmxremote.password 7199 " + conf + "/cassandra.yaml",
        "true",
    ]


def test_s3_credentials(host):
    content = host.file("/etc/medusa/credentials").content_string
    lines = [line for line in content.splitlines() if not line.startswith("#")]
    assert lines == ["[default]", "aws_access_key_id = AKIAEXAMPLE", "aws_secret_access_key = s3-secret/example"]


def test_login_shells_get_the_virtualenv(host):
    cmd = host.run("bash -lc 'command -v python; echo $PATH'")
    assert cmd.stdout.splitlines()[0] == VENV + "/bin/python"
    # sourced twice (nested login shell): added once
    cmd = host.run("bash -lc 'bash -lc \"echo \\$PATH\"'")
    assert cmd.stdout.count(VENV + "/bin") == 1
