import os

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')

EXPECTED_PORTS = ['22/tcp', '7000/tcp', '7001/tcp', '9042/tcp']  # 7199: from 10.0.9.0/24 only


def is_debian(host):
    return host.system_info.distribution in ("ubuntu", "debian")


def test_ensure_firewall_commands(host):
    cmds = ["ufw"] if is_debian(host) else ["firewall-cmd", "firewalld", "firewall-offline-cmd"]
    for cmd in cmds:
        assert host.exists(cmd)


def test_ensure_cassandra_ports_open(host):
    if is_debian(host):
        out = host.run("ufw show added").stdout
        opened = sorted(p for p in EXPECTED_PORTS if "ufw allow {0}".format(p) in out)
    else:
        # Output is not always in the same order so we need to order it ourselves
        opened = sorted(host.run("firewall-cmd --list-ports").stdout.split())

    assert opened == EXPECTED_PORTS


def test_jmx_open_to_its_source_only(host):
    if is_debian(host):
        out = host.run("ufw show added").stdout
        assert "ufw allow from 10.0.9.0/24 to any port 7199 proto tcp" in out
        assert "ufw allow 7199/tcp" not in out
    else:
        rules = host.run("firewall-cmd --list-rich-rules").stdout
        assert 'source address="10.0.9.0/24" port port="7199" protocol="tcp" accept' in rules
