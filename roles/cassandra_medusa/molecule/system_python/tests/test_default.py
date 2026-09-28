import os

import testinfra.utils.ansible_runner

testinfra_hosts = [h for h in testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all') if h.endswith('rhel810')]

VERSION = "from importlib.metadata import version; print(version('cassandra-medusa'))"


def test_medusa_in_python311(host):
    cmd = host.run("/usr/bin/python3.11 -c \"%s\"" % VERSION)
    assert cmd.rc == 0, cmd.stderr
    assert cmd.stdout.strip() == "0.30.1"


def test_medusa_script_from_pip(host):
    medusa = host.file("/usr/local/bin/medusa")
    assert medusa.is_file and not medusa.is_symlink
    assert medusa.content_string.splitlines()[0] == "#!/usr/bin/python3.11"
    assert host.run("/usr/local/bin/medusa --help").rc == 0


def test_no_virtualenv_nor_profile(host):
    assert not host.file("/opt/cassandra-medusa").exists
    assert not host.file("/etc/profile.d/cassandra-medusa.sh").exists
