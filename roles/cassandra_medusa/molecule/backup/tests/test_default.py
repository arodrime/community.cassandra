import os

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')


def medusa(host, args):
    # as the account Cassandra runs as, which owns medusa.ini (runuser: the
    # rpm's account has no shadow entry here, which sudo's PAM refuses)
    cmd = host.run("cd /tmp && runuser -u cassandra -- /usr/local/bin/medusa " + args)
    assert cmd.rc == 0, cmd.stdout + cmd.stderr
    return cmd.stdout


def test_backup_to_local_storage(host):
    medusa(host, "backup --backup-name=molecule")
    assert "molecule (started:" in medusa(host, "list-backups")
    out = medusa(host, "verify --backup-name=molecule")
    assert "Completion: OK" in out
    assert "Manifest validated: OK" in out
    assert host.file("/var/backups/medusa/cassandra_backups/127.0.0.1/molecule").is_directory
