from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest


from ansible_collections.community.cassandra.plugins.filter import cassandra_node_reset, cassandra_node_standing as standing
from ansible_collections.community.cassandra.plugins.filter.cassandra_node_reset import (
    cassandra_node_reset_dirs, cassandra_node_reset_real, reset_path_problem)


def cassandra_node_reset_ring(answers, addresses, replace_address="", running=False, own=None, host="", known=None,
                              has_data=False, cluster_ring=None):
    """What the reset says of the rings: create_cluster's whole-cluster check, else the ring problems where the node
    stands gives (ring_view, ring_problems), as reset_node refuses on them."""
    if cluster_ring is not None:
        return cassandra_node_reset.cassandra_node_reset_ring(addresses, host=host, has_data=has_data, cluster_ring=cluster_ring)
    view = standing.ring_view(answers, addresses, standing.own_ids(addresses, running=running, own=own), replace_address,
                              known, has_data)
    return {"problems": standing.ring_problems(view, addresses, host or "this node", replace_address, running, own),
            "info": ["%s sees %s down (%s): the node being replaced" % (o, standing.status_address(n.get("address")), standing._seen(n))
                     for o, n in view["replaced"]]}


INVENTORY = [["data", "/var/lib/cassandra/data"], ["commitlog", "/var/lib/cassandra/commitlog"],
             ["saved_caches", "/var/lib/cassandra/saved_caches"], ["hints", "/var/lib/cassandra/hints"]]
MOUNTS = [{"mount": "/"}, {"mount": "/boot"}, {"mount": "/var/lib/cassandra"}]


@pytest.mark.parametrize("path, why", [
    ("", "an empty path"),
    ("   ", "an empty path"),
    (None, "an empty path"),
    (["/data/cassandra"], "an empty path"),
    (" /data/cassandra", "spaces around the path"),
    ("var/lib/cassandra/data", "a relative path"),
    ("./data", "a relative path"),
    ("/data/cassandra/../..", ". or .. in the path"),
    ("/data/./cassandra", ". or .. in the path"),
    ("/", "the root of the file system"),
    ("//", "the root of the file system"),
    ("/var", "a system directory"),
    ("/var/", "a system directory"),
    ("/var/lib", "a system directory"),
    ("//var//lib//", "a system directory"),
    ("/var/log", "a system directory"),
    ("/usr/local", "a system directory"),
    ("/etc/cassandra", "a system directory"),
    ("/boot/efi", "a system directory"),
    ("/proc/1", "a system directory"),
    ("/home", "a system directory"),
    ("/data", "a top-level directory"),
    ("/cassandra", "a top-level directory"),
    ("/home/cassandra", "a home directory"),
    ("/var/lib/cassandra", "the package's storage root"),
    ("/usr/lib/jvm", "a system directory"),
    ("/usr/local/cassandra/data", "a system directory"),
    ("/root/cassandra", "a system directory"),
    ("/snap/core", "a system directory"),
    ("/var/lib/dpkg", "under /var/lib/dpkg, another program's state"),
    ("/var/lib/docker/volumes/x", "under /var/lib/docker, another program's state"),
])
def test_path_refused(path, why):
    assert why in reset_path_problem(path)


@pytest.mark.parametrize("path", [
    "/var/lib/cassandra/data", "/var/lib/cassandra/commitlog/", "/data/cassandra", "/data1/cassandra/data",
    "/home/cassandra/data", "/opt/cassandra/data", "/srv/cassandra/hints", "/var/lib/cassandra/cdc_raw",
])
def test_path_accepted(path):
    assert reset_path_problem(path, [m["mount"] for m in MOUNTS], ["/etc/cassandra", "/var/log/cassandra"]) == ""


def test_top_level_mount_point_accepted():
    # a disk of its own mounted at /data: emptied, not removed
    assert reset_path_problem("/data", ["/", "/data"], []) == ""
    assert "not a mount point" in reset_path_problem("/data", ["/", "/data/disk1"], [])


@pytest.mark.parametrize("path", ["/var/lib/cassandra-2/data", "/var/lib/Cassandra/hints"])
def test_var_lib_cassandra_dirs_accepted(path):
    assert reset_path_problem(path) == ""


def test_path_holding_a_mount_point_refused():
    why = reset_path_problem("/data/cassandra", ["/", "/data/cassandra/disk2/", "/data/cassandra"], [])
    assert why == "/data/cassandra: holds the mount point /data/cassandra/disk2 (another file system)"


def test_path_that_is_a_mount_point_accepted():
    # emptied, not removed: a mount point itself may be emptied
    assert reset_path_problem("/data/cassandra", ["/", "/data/cassandra"], []) == ""


def test_path_holding_a_protected_path_refused():
    why = reset_path_problem("/data/cassandra", [], ["/data/cassandra/logs", "/etc/cassandra"])
    assert why == "/data/cassandra: holds /data/cassandra/logs"
    assert reset_path_problem("/data/cassandra/data", [], ["/data/cassandra"]) == ""


def test_dirs_from_the_inventory_only():
    out = cassandra_node_reset_dirs(INVENTORY, None, MOUNTS)
    assert out["problems"] == []
    assert [(d["path"], d["kinds"], d["from"]) for d in out["dirs"]] == [
        ("/var/lib/cassandra/commitlog", ["commitlog"], ["inventory"]),
        ("/var/lib/cassandra/data", ["data"], ["inventory"]),
        ("/var/lib/cassandra/hints", ["hints"], ["inventory"]),
        ("/var/lib/cassandra/saved_caches", ["saved_caches"], ["inventory"])]


def test_dirs_from_the_live_file_merged():
    live = """
cluster_name: Test Cluster
data_file_directories:
    - /data1/cassandra/data
    - /var/lib/cassandra/data/
commitlog_directory: /var/lib/cassandra/commitlog
# hints_directory: /elsewhere/hints
saved_caches_directory: /data1/cassandra/saved_caches
local_system_data_file_directory: /data1/cassandra/system
server_encryption_options:
  keystore: /etc/cassandra/conf/.keystore
"""
    out = cassandra_node_reset_dirs(INVENTORY, live, MOUNTS)
    assert out["problems"] == []
    got = dict((d["path"], (d["kinds"], d["from"])) for d in out["dirs"])
    assert got == {
        "/data1/cassandra/data": (["data"], ["cassandra.yaml"]),
        "/data1/cassandra/system": (["data"], ["cassandra.yaml"]),
        "/data1/cassandra/saved_caches": (["saved_caches"], ["cassandra.yaml"]),
        "/var/lib/cassandra/data": (["data"], ["inventory", "cassandra.yaml"]),
        "/var/lib/cassandra/commitlog": (["commitlog"], ["inventory", "cassandra.yaml"]),
        # missing key: Cassandra's default under /var/lib/cassandra
        "/var/lib/cassandra/hints": (["hints"], ["inventory", "cassandra.yaml"]),
        "/var/lib/cassandra/saved_caches": (["saved_caches"], ["inventory"])}


STOCK = ["/var/lib/cassandra/commitlog", "/var/lib/cassandra/data", "/var/lib/cassandra/hints",
         "/var/lib/cassandra/saved_caches"]


def test_cdc_raw_only_when_configured():
    paths = [d["path"] for d in cassandra_node_reset_dirs([], "cdc_enabled: false\n")["dirs"]]
    assert paths == STOCK
    paths = [d["path"] for d in cassandra_node_reset_dirs([], "cdc_enabled: true\n")["dirs"]]
    assert paths == sorted(STOCK + ["/var/lib/cassandra/cdc_raw"])
    paths = [d["path"] for d in cassandra_node_reset_dirs([], "cdc_raw_directory: /data/cdc/raw\n")["dirs"]]
    assert paths == sorted(STOCK + ["/data/cdc/raw"])
    out = cassandra_node_reset_dirs([["cdc_raw", "/data/cassandra/cdc_raw"]], None)
    assert [d["path"] for d in out["dirs"]] == ["/data/cassandra/cdc_raw"]


def test_live_file_empty_data_list_is_the_default():
    paths = [d["path"] for d in cassandra_node_reset_dirs([], "data_file_directories: []\n")["dirs"]]
    assert paths == STOCK


def test_live_file_cluster_name_and_addresses():
    live = "cluster_name: Billing\nlisten_address: 10.100.100.50\nbroadcast_address: 192.0.2.50\nrpc_address: ''\n"
    # another cluster's: said by where the node stands (its live_cluster), not refused here
    out = cassandra_node_reset_dirs([], live)
    assert out["problems"] == [] and out["cluster_name"] == "Billing"
    assert out["addresses"] == ["10.100.100.50", "192.0.2.50"]
    assert cassandra_node_reset_dirs(INVENTORY, None)["addresses"] == []


def test_live_file_a_stock_one_without_directories():
    paths = [d["path"] for d in cassandra_node_reset_dirs([], "cluster_name: Test Cluster\n")["dirs"]]
    assert paths == STOCK


@pytest.mark.parametrize("live", [None, ""])
def test_no_live_file(live):
    # Ansible before 2.19 turns a none passed through a var into ''
    out = cassandra_node_reset_dirs([["data", "/data/cassandra/data"]], live)
    assert out == {"dirs": [{"path": "/data/cassandra/data", "kinds": ["data"], "from": ["inventory"]}], "problems": [],
                   "addresses": [], "cluster_name": None}


def test_live_file_without_cluster_name_is_test_cluster():
    assert cassandra_node_reset_dirs([], "num_tokens: 16\n")["cluster_name"] == "Test Cluster"


def test_mount_points_from_proc_mounts():
    # ZFS datasets are not in the facts; spaces are escaped in /proc/self/mounts
    proc = ["tank/data /data zfs rw 0 0", "tank/x /srv/cassandra\\040x/disk2 zfs rw 0 0"]
    out = cassandra_node_reset_dirs([["data", "/data"], ["commitlog", "/srv/cassandra x"]], None, [{"mount": "/"}] + proc)
    assert out["problems"] == ["commitlog directory from inventory, /srv/cassandra x: holds the mount point"
                               " /srv/cassandra x/disk2 (another file system)"]
    assert [d["path"] for d in out["dirs"]] == ["/data"]


@pytest.mark.parametrize("live, why", [
    ("data_file_directories: [/a\n  b: :", "not valid YAML"),
    ("- just\n- a list\n", "not a mapping"),
])
def test_live_file_unreadable_refused(live, why):
    out = cassandra_node_reset_dirs(INVENTORY, live)
    assert len(out["problems"]) == 1 and why in out["problems"][0]  # the YAML error text varies with PyYAML


def test_wrong_paths_reported_with_their_source():
    live = "data_file_directories: [/var/lib]\ncommitlog_directory: commitlog\nhints_directory: ''\n"
    out = cassandra_node_reset_dirs([["data", "/"], ["hints", ""]], live)
    assert out["problems"] == [
        "data directory from inventory, /: the root of the file system",
        "hints directory from inventory, an empty path",
        "data directory from cassandra.yaml, /var/lib: a system directory, it holds more than Cassandra's data",
        "commitlog directory from cassandra.yaml, commitlog: a relative path",
        "hints directory from cassandra.yaml, an empty path"]
    # the good ones are still listed (shown in the refusal), none is emptied by the task on a problem
    assert [d["path"] for d in out["dirs"]] == ["/var/lib/cassandra/saved_caches"]


def test_nested_dirs_refused():
    out = cassandra_node_reset_dirs([["data", "/data/cassandra"], ["commitlog", "/data/cassandra/commitlog"]], None)
    assert out["problems"] == ["/data/cassandra/commitlog (commitlog) is inside /data/cassandra (data): set them apart"]


def test_non_string_live_values():
    out = cassandra_node_reset_dirs([], "data_file_directories: [1234]\ncommitlog_directory: 42\n")
    assert out["problems"] == ["data directory from cassandra.yaml, 1234: a relative path",
                               "commitlog directory from cassandra.yaml, 42: a relative path"]


def test_links_resolved():
    dirs = cassandra_node_reset_dirs(INVENTORY, None)
    real = {"/var/lib/cassandra/data": "/data1/cassandra/data", "/var/lib/cassandra/commitlog": "/var/lib/cassandra/commitlog",
            "/var/lib/cassandra/hints": "/var/lib/cassandra/hints"}  # saved_caches: no answer, kept as it is
    out = cassandra_node_reset_real(dirs, real, [{"mount": "/"}, {"mount": "/data1"}], ["/etc/cassandra"])
    assert out["problems"] == []
    assert [(d["path"], d["real"]) for d in out["dirs"]] == [
        ("/var/lib/cassandra/commitlog", "/var/lib/cassandra/commitlog"), ("/var/lib/cassandra/data", "/data1/cassandra/data"),
        ("/var/lib/cassandra/hints", "/var/lib/cassandra/hints"), ("/var/lib/cassandra/saved_caches", "/var/lib/cassandra/saved_caches")]


@pytest.mark.parametrize("target, why", [
    ("/", "data directory /var/lib/cassandra/data is a link (or below one) to /: the root of the file system"),
    ("/var/lib/", "data directory /var/lib/cassandra/data is a link (or below one) to /var/lib: a system directory,"
                  " it holds more than Cassandra's data"),
    ("/srv/c", "data directory /var/lib/cassandra/data is a link (or below one) to /srv/c: holds the mount point /srv/c/disk"
               " (another file system)"),
    ("/var/log/cassandra/..", "data directory /var/lib/cassandra/data is a link (or below one) to /var/log: a system"
                              " directory, it holds more than Cassandra's data"),
])
def test_links_to_wrong_places_refused(target, why):
    dirs = cassandra_node_reset_dirs([["data", "/var/lib/cassandra/data"]], None)
    out = cassandra_node_reset_real(dirs, {"/var/lib/cassandra/data": target}, [{"mount": "/srv/c/disk"}])
    assert out["problems"] == [why]


def test_links_to_the_same_or_a_nested_place_refused():
    dirs = cassandra_node_reset_dirs([["data", "/var/lib/cassandra/data"], ["hints", "/srv/cassandra/hints"],
                                      ["commitlog", "/srv/cassandra/commitlog"]], None)
    real = {"/var/lib/cassandra/data": "/srv/cassandra", "/srv/cassandra/hints": "/srv/cassandra/hints",
            "/srv/cassandra/commitlog": "/srv/cassandra/hints"}
    out = cassandra_node_reset_real(dirs, real)
    assert out["problems"] == [
        "/srv/cassandra/hints (/srv/cassandra/hints) is the same directory as /srv/cassandra/commitlog (/srv/cassandra/hints)"
        " once links are resolved: set them apart",
        "/srv/cassandra/commitlog (/srv/cassandra/hints) is inside /var/lib/cassandra/data (/srv/cassandra) once links are"
        " resolved: set them apart",
        "/srv/cassandra/hints (/srv/cassandra/hints) is inside /var/lib/cassandra/data (/srv/cassandra) once links are"
        " resolved: set them apart"]


N1 = ("10.100.100.1", "U", "N", "id-1")
N2 = ("10.100.100.2", "U", "N", "id-2")
ME = ["10.100.100.9", "172.17.0.9"]


HOSTS = {"n1": ["10.100.100.1"], "n2": ["10.100.100.2", "172.17.0.2"], "n3": ["10.100.100.3"]}


def test_live_cluster_name_returned():
    assert cassandra_node_reset_dirs(INVENTORY, "cluster_name: billing\n", MOUNTS)["cluster_name"] == "billing"
    assert cassandra_node_reset_dirs(INVENTORY, "num_tokens: 16\n", MOUNTS)["cluster_name"] == "Test Cluster"
    assert cassandra_node_reset_dirs(INVENTORY, None, MOUNTS)["cluster_name"] is None
