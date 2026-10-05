from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_new_node import (
    cassandra_new_node_dirs, cassandra_new_node_kept_setup, cassandra_new_node_load, cassandra_new_node_network, cassandra_new_node_packages,
    cassandra_new_node_urls, parse_load)

GIB = 1024 ** 3
ROOT = {"mount": "/", "device": "/dev/sda1", "fstype": "xfs", "size_available": 30 * GIB, "size_total": 50 * GIB}
DATA = {"mount": "/var/lib/cassandra", "device": "/dev/sdb1", "fstype": "xfs", "size_available": 900 * GIB,
        "size_total": 1000 * GIB}
DIRS = [{"kind": "data", "path": "/var/lib/cassandra/data"}, {"kind": "commitlog", "path": "/var/lib/cassandra/commitlog"},
        {"kind": "hints", "path": "/var/lib/cassandra/hints"},
        {"kind": "saved_caches", "path": "/var/lib/cassandra/saved_caches"}]
FSTAB = """# comment
#/dev/sdd1 /var/lib/cassandra/data xfs defaults 0 0
UUID=abc / xfs defaults 0 0
/dev/sde1 /var/lib/cassandra/hints swap sw 0 0
/dev/sdb1 /var/lib/cassandra xfs defaults,noatime 0 0
/dev/sdc1 /mnt/spare ext4 noauto 0 0
/swapfile none swap sw 0 0
"""


def test_dirs_on_their_mounted_disk():
    out = cassandra_new_node_dirs(DIRS, [ROOT, DATA], fstab=FSTAB)
    assert out["problems"] == []
    assert out["info"][0] == "data /var/lib/cassandra/data: on /var/lib/cassandra (xfs /dev/sdb1), 900.0 GiB free of 1000.0 GiB"
    assert out["data_free"] == 900 * GIB


def test_fstab_mount_not_mounted_lands_on_root():
    out = cassandra_new_node_dirs(DIRS, [ROOT], fstab=FSTAB)
    assert len(out["problems"]) == 4
    assert out["problems"][0] == ("data directory /var/lib/cassandra/data: /var/lib/cassandra (/dev/sdb1 in /etc/fstab)"
                                  " is not mounted, it would go to /")


def test_noauto_and_swap_entries_are_not_expected():
    out = cassandra_new_node_dirs([{"kind": "data", "path": "/mnt/spare/data"}], [ROOT], fstab=FSTAB)
    assert out["problems"] == []


def test_systemd_mount_unit_expected():
    units = "var-lib-cassandra.mount enabled enabled\ndata\\x2dold.mount disabled enabled\n-.mount generated -\n"
    out = cassandra_new_node_dirs(DIRS, [ROOT], unit_files=units)
    assert "var-lib-cassandra.mount (systemd)" in out["problems"][0]
    # root expected by a unit, facts without it: not a problem
    out = cassandra_new_node_dirs([{"kind": "data", "path": "/srv/d"}], [DATA], unit_files=units)
    assert out["problems"] == []
    for state in ("generated", "linked", "enabled-runtime", "linked-runtime"):
        out = cassandra_new_node_dirs(DIRS[:1], [ROOT], unit_files="var-lib-cassandra.mount %s -\n" % state)
        assert len(out["problems"]) == 1, state
    # a disabled unit is not expected mounted
    out = cassandra_new_node_dirs([{"kind": "data", "path": "/data-old/d"}], [ROOT], unit_files=units)
    assert out["problems"] == []


def test_escaped_unit_name():
    units = "srv-cass\\x2ddata.mount enabled enabled\n"
    out = cassandra_new_node_dirs([{"kind": "data", "path": "/srv/cass-data/data"}], [ROOT], unit_files=units)
    assert "/srv/cass-data (srv-cass\\x2ddata.mount (systemd)) is not mounted" in out["problems"][0]


def test_deeper_actual_mount_is_fine():
    deeper = dict(DATA, mount="/var/lib/cassandra/data")
    out = cassandra_new_node_dirs(DIRS[:1], [ROOT, DATA, deeper], fstab=FSTAB)
    assert out["problems"] == []


def test_min_free_space_on_data_only():
    small = dict(DATA, size_available=100 * GIB)
    out = cassandra_new_node_dirs(DIRS, [ROOT, small], min_free_gb=200)
    assert out["problems"] == ["data directory /var/lib/cassandra/data: 100.0 GiB free on /var/lib/cassandra,"
                               " cassandra_new_node_min_free_gb asks for 200 GiB"]
    assert cassandra_new_node_dirs(DIRS, [ROOT, small], min_free_gb=0)["problems"] == []


def test_jbod_free_space_counted_once_per_file_system():
    dirs = [{"kind": "data", "path": "/d1/data"}, {"kind": "data", "path": "/d1/data2"}, {"kind": "data", "path": "/d2/data"}]
    d1 = {"mount": "/d1", "size_available": 10 * GIB, "size_total": 20 * GIB}
    d2 = {"mount": "/d2", "size_available": 5 * GIB, "size_total": 20 * GIB}
    assert cassandra_new_node_dirs(dirs, [ROOT, d1, d2])["data_free"] == 15 * GIB
    # the other directories' file systems don't count
    other = [{"kind": "commitlog", "path": "/d3/cl"}]
    d3 = {"mount": "/d3", "size_available": 7 * GIB, "size_total": 20 * GIB}
    assert cassandra_new_node_dirs(dirs + other, [ROOT, d1, d2, d3])["data_free"] == 15 * GIB


def test_non_empty_dirs():
    found = ["/var/lib/cassandra/data/lost+found", "/var/lib/cassandra/data/system",
             "/var/lib/cassandra/commitlog/CommitLog-7-1.log", "/var/lib/cassandra/hints/.keep"]
    out = cassandra_new_node_dirs(DIRS, [ROOT, DATA], found=found)
    assert len(out["problems"]) == 3
    assert out["problems"][0].startswith("data directory /var/lib/cassandra/data is not empty (system)")
    assert "(.keep)" in out["problems"][2]


def test_non_empty_dirs_with_a_reset_only_warn():
    found = ["/var/lib/cassandra/data/system", "/var/lib/cassandra/commitlog/CommitLog-7-1.log"]
    out = cassandra_new_node_dirs(DIRS, [ROOT, DATA], found=found, reset=True)
    assert out["problems"] == []
    assert out["warnings"] == [
        "data directory /var/lib/cassandra/data is not empty (system): the reset asked for empties it first",
        "commitlog directory /var/lib/cassandra/commitlog is not empty (CommitLog-7-1.log): the reset asked for empties it first"]


def test_other_cassandra_dirs_inside_a_data_dir_dont_count():
    dirs = [{"kind": "data", "path": "/data"}, {"kind": "commitlog", "path": "/data/commitlog"}]
    out = cassandra_new_node_dirs(dirs, [ROOT], found=["/data/commitlog", "/data/lost+found"])
    assert out["problems"] == []


def test_many_entries_shortened():
    found = ["/x/f%d" % i for i in range(8)]
    out = cassandra_new_node_dirs([{"kind": "data", "path": "/x"}], [ROOT], found=found)
    assert "(f0, f1, f2, f3, f4...)" in out["problems"][0]


def test_parse_load():
    assert parse_load("66.2 KiB") == 66.2 * 1024
    assert parse_load("1.5 TiB") == 1.5 * 1024 ** 4
    assert parse_load("512 bytes") == 512
    assert parse_load("?") is None
    assert parse_load(None) is None


def ring(*nodes):
    out = {}
    for dc, rack, address, load in nodes:
        out.setdefault(dc, {"nodes": []})["nodes"].append({"address": address, "rack": rack, "load": load})
    return out


def test_load_of_the_same_rack_against_free_space():
    status = ring(("dc1", "r1", "10.0.0.1", "400 GiB"), ("dc1", "r1", "10.0.0.2", "600 GiB"),
                  ("dc1", "r2", "10.0.0.3", "10 GiB"), ("dc2", "r1", "10.1.0.1", "5 TiB"))
    out = cassandra_new_node_load(status, "dc1", "r1", 900 * GIB)
    assert out["warnings"][0].startswith("the nodes of rack r1 (dc1) hold 500.0 GiB on average (nodetool status),"
                                         " this host has 900.0 GiB free for data: the new node gets about as much")
    out = cassandra_new_node_load(status, "dc1", "r1", 1100 * GIB)
    assert out["warnings"] == [] and "500.0 GiB on average" in out["info"][0]


def test_load_of_the_datacenter_for_a_new_rack():
    status = ring(("dc1", "r1", "10.0.0.1", "10 GiB"), ("dc1", "r2", "10.0.0.2", "30 GiB"))
    out = cassandra_new_node_load(status, "dc1", "r9", 100 * GIB)
    assert out["info"] == ["the nodes of datacenter dc1 hold 20.0 GiB on average (nodetool status), this host has"
                           " 100.0 GiB free for data"]


def test_load_leaves_out_the_node_itself_and_unknown_loads():
    status = ring(("dc1", "r1", "10.0.0.1", "10 GiB"), ("dc1", "r1", "10.0.0.9", "900 GiB"), ("dc1", "r1", "10.0.0.2", "?"))
    out = cassandra_new_node_load(status, "dc1", "r1", 100 * GIB, address="10.0.0.9")
    assert "10.0 GiB on average" in out["info"][0]
    assert cassandra_new_node_load({}, "dc1", "r1", GIB)["info"] == ["no load known for the nodes of datacenter dc1"]


APT = """cassandra:
  Installed: (none)
  Candidate: 5.0.5
  Version table:
     5.0.5 500
        500 https://debian.cassandra.apache.org 50x/main amd64 Packages
     5.0.4 500
        500 https://debian.cassandra.apache.org 50x/main amd64 Packages
python3.11:
  Installed: (none)
  Candidate: (none)
  Version table:
"""
DNF = "python3.11 3.11.9\npython3.11 3.11.7\ncassandra 5.0.4\ncassandra 5.0.5\n"


def test_packages_available_from_apt_with_the_pinned_version():
    needs = [{"name": "cassandra", "why": "Cassandra", "mode": "either", "version": "5.0.4"},
             {"name": "python3.11", "why": "cqlsh", "mode": "either"}]
    out = cassandra_new_node_packages(needs, {}, APT, "Debian")
    assert out["info"] == ["cassandra 5.0.4 (Cassandra): available"]
    assert out["problems"] == ["python3.11 (cqlsh) is not installed nor available from the configured repositories"]


def test_pinned_version_missing_from_the_repositories():
    needs = [{"name": "cassandra", "why": "Cassandra", "mode": "either", "version": "5.0.3"}]
    out = cassandra_new_node_packages(needs, {}, DNF, "RedHat")
    assert out["problems"] == ["cassandra 5.0.3 (Cassandra) is not installed nor available from the configured"
                               " repositories (they have 5.0.4, 5.0.5)"]


def test_packages_from_dnf_and_installed():
    needs = [{"name": "python3.11", "why": "cqlsh", "mode": "either"},
             {"name": "java-17-openjdk-headless", "why": "Java", "mode": "either"}]
    installed = {"java-17-openjdk-headless": [{"version": "17.0.12"}]}
    out = cassandra_new_node_packages(needs, installed, DNF, "RedHat")
    assert out["problems"] == []
    assert out["info"] == ["python3.11 (cqlsh): available", "java-17-openjdk-headless (Java): installed"]


def test_offline_packages_must_be_installed_at_the_version():
    needs = [{"name": "cassandra", "why": "Cassandra", "mode": "installed", "version": "5.0.4"},
             {"name": "java-17-openjdk-headless", "why": "Java", "mode": "installed"},
             {"name": "python3.11", "why": "cqlsh", "mode": "warn"}]
    installed = {"cassandra": [{"version": "5.0.2"}]}
    out = cassandra_new_node_packages(needs, installed, "", "RedHat")
    assert out["problems"] == ["cassandra 5.0.4 (Cassandra): 5.0.2 installed",
                               "java-17-openjdk-headless (Java) is not installed, and cassandra_offline downloads nothing"]
    assert out["warnings"] == ["python3.11 (cqlsh) is not installed (cassandra_offline): install it from your image or mirror"]
    ok = cassandra_new_node_packages(needs[:1], {"cassandra": [{"version": "5.0.4-1"}]}, "", "RedHat")
    assert ok["problems"] == []


def test_unreadable_repositories_only_warn():
    needs = [{"name": "python3.11", "why": "cqlsh", "mode": "either"}]
    out = cassandra_new_node_packages(needs, {}, "", "Debian", query_ok=False)
    assert out["problems"] == [] and "could not tell" in out["warnings"][0]


def test_fallback_source():
    needs = [{"name": "python3.11", "why": "cqlsh", "mode": "either", "fallback": "the deadsnakes PPA"}]
    out = cassandra_new_node_packages(needs, {}, APT, "Debian")
    assert out["problems"] == []
    assert out["info"] == ["python3.11 (cqlsh): not in the configured repositories, comes from the deadsnakes PPA"]


SS = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      4096       0.0.0.0:22        0.0.0.0:*
LISTEN 0      50     127.0.0.1:7199       0.0.0.0:*
LISTEN 0      4096          [::]:9042          [::]:*
ESTAB  0      0      10.0.0.5:7000      10.0.0.1:41234
"""
PORTS = [{"name": "storage", "port": 7000}, {"name": "native (CQL)", "port": 9042}, {"name": "JMX", "port": 7199}]


def test_network():
    reached = {"results": [
        {"item": {"name": "storage", "host": "10.0.0.1", "port": 7000}, "elapsed": 0},
        {"item": {"name": "native (CQL)", "host": "10.0.0.1", "port": 9042}, "failed": False,
         "msg": "Timeout when waiting for 10.0.0.1:9042"}]}
    out = cassandra_new_node_network(reached, PORTS, SS)
    assert out["problems"] == [
        "can't reach native (CQL) port 9042 of 10.0.0.1 from here: check the address and the firewalls between the nodes",
        "port 9042 (native (CQL)) is already in use here",
        "port 7199 (JMX) is already in use here"]
    assert out["info"] == ["storage port 7000 of 10.0.0.1 reached"]


def test_network_running_and_no_ss():
    out = cassandra_new_node_network({"results": []}, PORTS, None, running=True)
    assert out["problems"][0].startswith("Cassandra is running here")
    assert out["warnings"] == ["could not list the listening ports (ss)"]


def test_network_running_with_a_reset_only_warns():
    out = cassandra_new_node_network({"results": []}, PORTS, SS, running=True, reset=True)
    assert out["problems"] == []
    assert out["warnings"] == [
        "Cassandra is running here: the reset asked for stops it first (refused if it is a member of a cluster)",
        "port 9042 (native (CQL)) is already in use here (by Cassandra?)", "port 7199 (JMX) is already in use here (by Cassandra?)"]
    # not running: a busy port is someone else's, the reset changes nothing to it
    out = cassandra_new_node_network({"results": []}, PORTS, SS, running=False, reset=True)
    assert out["problems"] == ["port 9042 (native (CQL)) is already in use here", "port 7199 (JMX) is already in use here"]


def test_urls():
    results = {"results": [
        {"item": {"name": "Cassandra repository", "url": "https://m/repodata/repomd.xml"}, "status": 200},
        {"item": {"name": "Java tarball", "url": "https://m/jdk.tgz"}, "status": 401},
        {"item": {"name": "Medusa pip index", "url": "https://p/cassandra-medusa/", "match": "cassandra[-_]medusa-0\\.30\\.1[.-]",
                  "what": "cassandra-medusa 0.30.1"}, "status": 200, "content": "cassandra_medusa-0.29.0.tar.gz"},
        {"item": {"name": "Other", "url": "https://x/"}, "status": -1, "msg": "Request failed: <urlopen error timed out>"},
        {"item": {"name": "Method refused", "url": "https://y/"}, "status": 405},
        {"item": {"name": "Tarball part", "url": "https://z/jdk.tgz"}, "status": 206}]}
    out = cassandra_new_node_urls(results)
    assert out["problems"] == [
        "Java tarball: https://m/jdk.tgz answers HTTP 401: check the credentials",
        "Medusa pip index: https://p/cassandra-medusa/ has no cassandra-medusa 0.30.1"]
    assert out["warnings"] == ["Other: no answer from https://x/ (a proxy set only for the package manager or pip is not"
                               " used by this check)"]
    assert out["info"] == ["Cassandra repository: https://m/repodata/repomd.xml reached", "Method refused: https://y/ reached",
                           "Tarball part: https://z/jdk.tgz reached"]
    ok = {"results": [dict(results["results"][2], content="cassandra_medusa-0.30.1-py3-none-any.whl")]}
    assert cassandra_new_node_urls(ok)["problems"] == []


def test_urls_never_show_credentials_or_signatures():
    results = {"results": [
        {"item": {"name": "pip", "url": "https://bob:s3cret@mirror:8443/simple/cassandra-medusa/"}, "status": 404},
        {"item": {"name": "Java", "url": "https://bucket.s3/jdk.tgz?X-Amz-Signature=abc"}, "status": 200}]}
    out = cassandra_new_node_urls(results)
    assert out["problems"] == ["pip: https://mirror:8443/simple/cassandra-medusa/ answers HTTP 404"]
    assert out["info"] == ["Java: https://bucket.s3/jdk.tgz reached"]


def test_link_target_decides_the_file_system():
    dirs = [{"kind": "data", "path": "/var/lib/cassandra/data", "real": "/data/cassandra/data"}]
    disk = {"mount": "/data", "device": "/dev/sdb1", "fstype": "xfs", "size_available": 900 * GIB, "size_total": 1000 * GIB}
    out = cassandra_new_node_dirs(dirs, [ROOT, disk], fstab="/dev/sdb1 /data xfs defaults 0 0\n", min_free_gb=500)
    assert out["problems"] == [] and out["data_free"] == 900 * GIB
    out = cassandra_new_node_dirs(dirs, [ROOT], fstab="/dev/sdb1 /data xfs defaults 0 0\n")
    assert out["problems"][0].startswith("data directory /var/lib/cassandra/data (/data/cassandra/data): /data")


def test_no_mount_facts_only_warns():
    out = cassandra_new_node_dirs(DIRS, [], fstab=FSTAB, min_free_gb=10)
    assert out["problems"] == [] and out["info"] == []
    assert out["warnings"][0].startswith("no mount in the facts")


def test_unknown_installed_packages_only_warn():
    needs = [{"name": "cassandra", "why": "Cassandra", "mode": "installed"},
             {"name": "python3.11", "why": "cqlsh", "mode": "either"}]
    out = cassandra_new_node_packages(needs, None, DNF, "RedHat")
    assert out["problems"] == []
    assert out["warnings"] == ["cassandra (Cassandra): could not read the installed packages (python3-apt missing?)"]
    assert out["info"] == ["python3.11 (cqlsh): available"]


def test_native_port_of_the_seeds_only_warns():
    reached = {"results": [{"item": {"name": "native (CQL)", "host": "s1", "port": 9042, "optional": True},
                            "msg": "Timeout when waiting for s1:9042"}]}
    out = cassandra_new_node_network(reached, [], "")
    assert out["problems"] == [] and "clients do" in out["warnings"][0]


def test_default_pypi_refused_only_warns():
    results = {"results": [{"item": {"name": "Medusa pip index", "url": "https://pypi.org/simple/cassandra-medusa/",
                                     "soft": True}, "status": 403}]}
    out = cassandra_new_node_urls(results)
    assert out["problems"] == [] and "HTTP 403" in out["warnings"][0]


KEPT = {"cassandra_linux_manage": False, "cassandra_cqlsh_python_manage": False, "cassandra_service_unit_manage": False}


def test_kept_setup_on_a_blank_host():
    # a host rebuilt under an imported node's name, its host_vars left as they were
    out = cassandra_new_node_kept_setup(KEPT, installed=False, imported=True)
    assert len(out["problems"]) == 1
    assert out["problems"][0].startswith("cassandra_linux_manage, cassandra_cqlsh_python_manage, cassandra_service_unit_manage"
                                         " are false on a host with the host_vars import_cluster wrote")
    assert "no OS tuning, no Python for cqlsh, no systemd unit" in out["problems"][0]
    assert "remove these lines and cassandra_imported_host from its host_vars" in out["problems"][0]
    one = cassandra_new_node_kept_setup({"cassandra_service_unit_manage": "false"}, installed=False, imported="true")
    assert one["problems"][0].startswith("cassandra_service_unit_manage is false on a host with the host_vars")


def test_kept_setup_accepted():
    # Cassandra there already, or the operator says the host is set up another way
    assert cassandra_new_node_kept_setup(KEPT, installed=True, imported=True)["problems"] == []
    out = cassandra_new_node_kept_setup(KEPT, installed=False, imported=True, allow="yes")
    assert out["problems"] == [] and out["info"][0].startswith("left as it is on this host")


def test_switches_of_the_operator_not_refused():
    # set on purpose (e.g. group_vars, OS tuned by other tooling): no import marker, the operator's choice
    out = cassandra_new_node_kept_setup(KEPT, installed=False)
    assert out["problems"] == [] and out["info"][0].startswith("left as it is on this host")


def test_no_kept_setup():
    on = {"cassandra_linux_manage": True, "cassandra_cqlsh_python_manage": "true", "cassandra_service_unit_manage": True}
    assert cassandra_new_node_kept_setup(on, installed=False, imported=True) == {"problems": [], "warnings": [], "info": []}
    # cassandra_repository_manage false: the package checks cover it
    assert cassandra_new_node_kept_setup({"cassandra_repository_manage": False}, installed=False, imported=True)["problems"] == []
