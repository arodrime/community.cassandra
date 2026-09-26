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


def test_restart_policy_is_no(host):
    unit = host.file("/etc/systemd/system/cassandra.service").content_string

    assert "Restart=no" in unit
    assert "StartLimitBurst" not in unit  # would also block manual restarts


def test_tasks_max_unlimited(host):
    assert host.run("systemctl show cassandra -p TasksMax --value").stdout.strip() == "infinity"


def test_stop_drains_the_node(host):
    # a plain "systemctl stop" (OS patching) drains first, through ExecStop
    drained = "grep -c 'DRAINED' /var/log/cassandra/system.log"
    before = int(host.run(drained).stdout.strip() or 0)
    assert host.run("systemctl stop cassandra").rc == 0

    assert int(host.run(drained).stdout.strip() or 0) > before
    assert not host.service("cassandra").is_running

    assert host.run("systemctl start cassandra").rc == 0
    assert host.run("for i in $(seq 1 60); do nodetool status 2>/dev/null | grep -q '^UN ' && exit 0; sleep 5; done; exit 1").rc == 0


def test_crashed_node_stays_down(host):
    # Restart=no: a killed JVM is not brought back behind the operator's back
    pid = host.run("systemctl show cassandra -p MainPID --value").stdout.strip()
    host.run(f"kill -9 {pid}")
    host.run("sleep 45")

    assert host.run("systemctl show cassandra -p MainPID --value").stdout.strip() == "0"
    assert not host.service("cassandra").is_running
