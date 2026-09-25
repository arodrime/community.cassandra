import os

import testinfra.utils.ansible_runner

testinfra_hosts = testinfra.utils.ansible_runner.AnsibleRunner(
    os.environ['MOLECULE_INVENTORY_FILE']
).get_hosts('all')


def test_native_unit_in_use(host):
    unit = host.run("systemctl show cassandra -p FragmentPath -p SourcePath").stdout

    assert "FragmentPath=/etc/systemd/system/cassandra.service" in unit
    assert "SourcePath=/etc/init.d" not in unit


def test_service_running_and_enabled(host):
    svc = host.service("cassandra")

    assert svc.is_running
    assert svc.is_enabled


def test_runs_as_cassandra_with_limits(host):
    pid = host.run("systemctl show cassandra -p MainPID --value").stdout.strip()
    limits = host.file(f"/proc/{pid}/limits").content_string

    assert host.run(f"ps -o user= -p {pid}").stdout.strip() == "cassandra"
    assert "CassandraDaemon" in host.file(f"/proc/{pid}/cmdline").content_string.replace("\0", " ")
    assert [line.split()[3:5] for line in limits.splitlines() if line.startswith("Max open files")] == [["1048576", "1048576"]]


def test_node_up_normal(host):
    status = host.run("nodetool status").stdout

    assert "Datacenter: dc_molecule" in status
    assert any(line.startswith("UN ") for line in status.splitlines())
    assert "Mode: NORMAL" in host.run("nodetool netstats").stdout


def test_cluster_name_applied(host):
    assert "Name: Molecule Cluster" in host.run("nodetool describecluster").stdout


def test_crash_is_restarted_by_systemd(host):
    # Restart=on-failure: a killed JVM comes back (RestartSec=30)
    before = host.run("systemctl show cassandra -p MainPID --value").stdout.strip()
    host.run(f"kill -9 {before}")
    after = host.run(
        "for i in $(seq 1 60); do p=$(systemctl show cassandra -p MainPID --value);"
        " [ \"$p\" != 0 ] && [ \"$p\" != %s ] && echo $p && exit; sleep 2; done" % before
    ).stdout.strip()

    assert after and after != before
