from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_node_reset import (
    cassandra_add_node_reset_check, cassandra_cluster_reset_check, cassandra_node_reset_dirs, cassandra_node_reset_real,
    cassandra_node_reset_ring, reset_path_problem)

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
    out = cassandra_node_reset_dirs([], live, cluster_name="Orders")
    assert out["problems"] == ["the live cassandra.yaml is for cluster 'Billing', not 'Orders' (nor the package's stock"
                               " 'Test Cluster'): a node of another cluster?"]
    assert out["addresses"] == ["10.100.100.50", "192.0.2.50"]
    for name in "Orders", "Test Cluster":
        assert cassandra_node_reset_dirs([], "cluster_name: %s\n" % name, cluster_name="Orders")["problems"] == []
    assert cassandra_node_reset_dirs(INVENTORY, None, cluster_name="Orders")["addresses"] == []


def test_live_file_a_stock_one_without_directories():
    paths = [d["path"] for d in cassandra_node_reset_dirs([], "cluster_name: Test Cluster\n")["dirs"]]
    assert paths == STOCK


@pytest.mark.parametrize("live", [None, ""])
def test_no_live_file(live):
    # Ansible before 2.19 turns a none passed through a var into ''
    out = cassandra_node_reset_dirs([["data", "/data/cassandra/data"]], live, cluster_name="Orders")
    assert out == {"dirs": [{"path": "/data/cassandra/data", "kinds": ["data"], "from": ["inventory"]}], "problems": [],
                   "addresses": [], "cluster_name": None}


def test_live_file_without_cluster_name_is_test_cluster():
    assert cassandra_node_reset_dirs([], "num_tokens: 16\n", cluster_name="Orders")["problems"] == []


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


def _ring(*nodes):
    return {"dc1": {"nodes": [dict(zip(("address", "status", "state", "host_id"), n)) for n in nodes]}}


N1 = ("10.100.100.1", "U", "N", "id-1")
N2 = ("10.100.100.2", "U", "N", "id-2")
ME = ["10.100.100.9", "172.17.0.9"]


def _answer(host, address, status):
    return {"host": host, "addresses": [address], "status": status}


def test_never_joined_node_may_be_reset():
    out = cassandra_node_reset_ring([_answer("n1", "10.100.100.1", _ring(N1, N2))], ME, host="n9")
    assert out == {"problems": [], "info": []}


@pytest.mark.parametrize("seen", [("10.100.100.9", "U", "N", "id-9"), ("10.100.100.9", "D", "N", "id-9"),
                                  ("10.100.100.9", "U", "J", "id-9"), ("172.17.0.9:7000", "D", "J", "id-9")])
def test_node_in_the_ring_refused(seen):
    out = cassandra_node_reset_ring([_answer("n1", "10.100.100.1", _ring(N1, N2, seen))], ME, host="n9")
    assert len(out["problems"]) == 1
    assert out["problems"][0].startswith("n1 sees %s in its ring (%s%s, host ID id-9): n9 is a member"
                                         % (seen[0].split(":")[0], seen[1], seen[2]))


def test_seen_by_host_id_refused():
    # running here with host ID id-7, listed under another address (a changed IP)
    own = _ring(("10.100.100.9", "U", "N", "id-7"))
    out = cassandra_node_reset_ring([_answer("n1", "10.100.100.1", _ring(N1, ("10.100.100.77", "D", "N", "id-7")))],
                                    ME, running=True, own=own, host="n9")
    assert out["problems"] == ["n1 sees 10.100.100.77 in its ring (DN, host ID id-7): n9 is a member of the cluster, never"
                               " reset it. A node leaves with decommission_node (remove_dead_node when dead), then can be reset"]


def test_every_answer_counts():
    answers = [_answer("n1", "10.100.100.1", _ring(N1, N2)),
               _answer("n2", "10.100.100.2", _ring(N1, N2, ("10.100.100.9", "U", "J", "id-9")))]
    out = cassandra_node_reset_ring(answers, ME, host="n9")
    assert out["problems"] == ["n2 sees 10.100.100.9 in its ring (UJ, host ID id-9): n9 is a member of the cluster, never"
                               " reset it. A node leaves with decommission_node (remove_dead_node when dead), then can be reset"]


def test_unknown_down_node_refused_when_holding_data():
    # this node under an old address (a changed IP): down, at an address no inventory host has
    answers = [_answer("n1", "10.100.100.1", _ring(N1, N2, ("10.100.100.50", "D", "N", "id-50")))]
    known = ["10.100.100.1", "10.100.100.2"] + ME
    out = cassandra_node_reset_ring(answers, ME, host="n9", known=known, has_data=True)
    assert out["problems"] == ["n1 sees 10.100.100.50 down (host ID id-50), a node no host of the inventory has: if it is n9"
                               " under an old address, its data would be lost. Remove it first (remove_dead_node), or give"
                               " the inventory host its address"]
    # nothing to lose, the dead node being replaced, an inventory host, or up: not refused
    assert cassandra_node_reset_ring(answers, ME, known=known, has_data=False)["problems"] == []
    assert cassandra_node_reset_ring(answers, ME, known=known, has_data=True, replace_address="10.100.100.50")["problems"] == []
    assert cassandra_node_reset_ring(answers, ME, known=known + ["10.100.100.50"], has_data=True)["problems"] == []
    up = [_answer("n1", "10.100.100.1", _ring(N1, N2, ("10.100.100.50", "U", "N", "id-50")))]
    assert cassandra_node_reset_ring(up, ME, known=known, has_data=True)["problems"] == []


def test_problems_listed_once():
    seen = ("10.100.100.50", "D", "N", "id-50")
    answers = [_answer("n1", "10.100.100.1", _ring(N1, N2, seen)), _answer("n2", "10.100.100.2", _ring(N1, N2, seen))]
    out = cassandra_node_reset_ring(answers, ME, known=["10.100.100.1", "10.100.100.2"], has_data=True)
    assert [p.split(" sees")[0] for p in out["problems"]] == ["n1", "n2"]


def test_only_answers_of_un_nodes_count():
    answers = [_answer("n1", "10.100.100.1", _ring(("10.100.100.1", "U", "J", "id-1"), N2)),
               _answer("n2", "10.100.100.2", None), _answer("n3", "10.100.100.3", _ring(N1, N2))]
    out = cassandra_node_reset_ring(answers, ME, host="n9")
    assert out["problems"] == ["no other node of the cluster answered as up and normal (UN) (asked: n1, n2, n3): whether"
                               " n9 is in the ring can't be checked, nothing is reset"]
    assert cassandra_node_reset_ring([], ME)["problems"][0].startswith("no other node of the cluster answered")


def test_running_node_alone_in_its_ring_may_be_reset():
    # started once by mistake with the stock config: a ring of its own
    own = _ring(("127.0.0.1", "U", "N", "id-x"))
    for mine in ME, ME + ["127.0.0.1"]:
        out = cassandra_node_reset_ring([_answer("n1", "10.100.100.1", _ring(N1, N2))], mine, running=True, own=own)
        assert out["problems"] == []


def test_loopback_entries_of_other_rings_ignored():
    # a node of the group on a loopback address lists itself there, not this node
    out = cassandra_node_reset_ring([_answer("n1", "127.0.0.1", _ring(("127.0.0.1", "U", "N", "id-1")))],
                                    ME + ["127.0.0.1"])
    assert out["problems"] == []


def test_running_with_other_local_instances_refused():
    own = _ring(("127.0.0.1", "U", "N", "id-x"), ("127.0.0.2", "U", "N", "id-y"))
    out = cassandra_node_reset_ring([_answer("n1", "10.100.100.1", _ring(N1, N2))], ME, running=True, own=own, host="n9")
    assert out["problems"] == ["Cassandra runs on n9 and its ring has other nodes (127.0.0.1, 127.0.0.2): it is a member of"
                               " a cluster, never reset a live member"]


def test_running_member_refused():
    own = _ring(N1, ("10.100.100.9", "U", "N", "id-9"))
    out = cassandra_node_reset_ring([_answer("n1", "10.100.100.1", _ring(N1, N2))], ME, running=True, own=own, host="n9")
    assert out["problems"] == ["Cassandra runs on n9 and its ring has other nodes (10.100.100.1): it is a member of a"
                               " cluster, never reset a live member"]


def test_running_node_not_answering_refused():
    out = cassandra_node_reset_ring([_answer("n1", "10.100.100.1", _ring(N1, N2))], ME, running=True, own=None, host="n9")
    assert out["problems"] == ["Cassandra runs on n9 but nodetool status does not answer there: whether it is a member of a"
                               " cluster is unknown. Check it, stop it (systemctl stop cassandra), then run again"]


def test_replace_seen_down_accepted():
    answers = [_answer("n1", "10.100.100.1", _ring(N1, N2, ("10.100.100.9", "D", "N", "id-9")))]
    out = cassandra_node_reset_ring(answers, ME, replace_address="10.100.100.9")
    assert out["problems"] == []
    assert out["info"] == ["n1 sees 10.100.100.9 down (DN, host ID id-9): the node being replaced"]


def test_replace_seen_up_refused():
    answers = [_answer("n1", "10.100.100.1", _ring(N1, N2, ("10.100.100.9", "D", "N", "id-9"))),
               _answer("n2", "10.100.100.2", _ring(N1, N2, ("10.100.100.9", "U", "N", "id-9")))]
    out = cassandra_node_reset_ring(answers, ME, replace_address="10.100.100.9")
    assert out["problems"] == ["n2 sees 10.100.100.9 up (UN, host ID id-9): only a dead node is replaced, never reset a"
                               " live member"]


def test_replace_other_address_of_this_host_refused():
    # the dead node's address is not this host's: a ring entry at this host's address is a member
    answers = [_answer("n1", "10.100.100.1", _ring(N1, N2, ("172.17.0.9", "D", "N", "id-9")))]
    out = cassandra_node_reset_ring(answers, ME, replace_address="10.100.100.9", host="n9")
    assert out["problems"] == ["n1 sees 172.17.0.9 in its ring (DN, host ID id-9): n9 is a member of the cluster, never"
                               " reset it. A node leaves with decommission_node (remove_dead_node when dead), then can be reset"]


HOSTS = {"n1": ["10.100.100.1"], "n2": ["10.100.100.2", "172.17.0.2"], "n3": ["10.100.100.3"]}


def test_cluster_reset_whole_ring_in_the_group():
    ring = _ring(N1, N2, ("10.100.100.3", "D", "N", "id-3"))
    out = cassandra_cluster_reset_check([{"host": "n1", "status": ring}, {"host": "n2", "status": None}], HOSTS, "orders")
    assert out == {"problems": [], "info": ["the ring (n1=10.100.100.1, n2=10.100.100.2, n3=10.100.100.3) is all in orders"],
                   "ring": ["10.100.100.1", "10.100.100.2", "10.100.100.3"]}


def test_cluster_reset_node_outside_the_group_refused():
    ring = _ring(N1, N2, ("10.100.100.4:7000", "U", "N", "id-4"))
    answers = [{"host": "n1", "status": ring}, {"host": "n2", "status": ring}]
    out = cassandra_cluster_reset_check(answers, HOSTS, "orders")
    assert out == {"problems": ["n1, n2 sees 10.100.100.4 in its ring, a node no host of orders has: the inventory must cover"
                                " the whole cluster (a node left out would keep running with its data)"], "info": [],
                   "ring": ["10.100.100.1", "10.100.100.2", "10.100.100.4"]}


def test_cluster_reset_no_answer_refused():
    out = cassandra_cluster_reset_check([{"host": "n1", "status": None}], HOSTS, "orders")
    assert out == {"problems": ["no node of orders answers nodetool status: the ring can't be checked against the"
                                " inventory. Start at least one node, then run again (if an earlier reset emptied them"
                                " already, run create_cluster without cassandra_create_cluster_reset)"], "info": [], "ring": []}
    assert cassandra_cluster_reset_check([], HOSTS)["problems"][0].startswith("no node of the group answers")


def test_cluster_reset_running_host_outside_the_ring_refused():
    # n3 runs in another cluster (same name): its nodetool does not answer, or its ring is not this one
    ring = _ring(N1, N2)
    out = cassandra_cluster_reset_check([{"host": "n1", "status": ring}, {"host": "n3", "status": None}], HOSTS, "orders",
                                        running=["n1", "n3"])
    assert out["problems"] == ["n3 runs Cassandra but nodetool status does not answer there: whether it is a node of this"
                               " cluster can't be checked"]
    other = _ring(("10.100.100.3", "U", "N", "id-3"), ("10.100.100.9", "U", "N", "id-9"))
    HOSTS9 = dict(HOSTS, n9=["10.100.100.9"])
    out = cassandra_cluster_reset_check([{"host": "n1", "status": ring}, {"host": "n3", "status": other}], HOSTS9, "orders",
                                        running=["n1", "n2", "n3"])
    assert out["problems"] == [
        "the running nodes see different rings (n1: 10.100.100.1, 10.100.100.2; n3: 10.100.100.3, 10.100.100.9): another"
        " cluster among them, or one still joining or leaving",
        "n2 runs Cassandra but nodetool status does not answer there: whether it is a node of this cluster can't be checked"]
    # a host that does not run and is not in the ring (new in the rebuild, or holding data: reset_node.yml refuses
    # it then, with the ring returned here): fine here
    out = cassandra_cluster_reset_check([{"host": "n1", "status": ring}], HOSTS, "orders", running=["n1"])
    assert out["problems"] == []


def test_whole_cluster_reset_needs_data_holders_in_the_ring():
    ring = ["10.100.100.1", "10.100.100.2", "10.100.100.9:7000"]
    assert cassandra_node_reset_ring([], ME, has_data=True, cluster_ring=ring) == {
        "problems": [], "info": ["the whole cluster is reset"]}
    out = cassandra_node_reset_ring([], ME, has_data=True, cluster_ring=ring[:2], host="n9")
    assert out == {"problems": ["n9 holds data but none of its addresses (10.100.100.9, 172.17.0.9) is in the ring of the"
                                " cluster: a node of another cluster?"], "info": []}
    # blank (a new host in the rebuild): fine
    assert cassandra_node_reset_ring([], ME, has_data=False, cluster_ring=ring[:2])["problems"] == []


# add_node's reset, on by default (cassandra_add_node_reset): every condition
def _auto(**kwargs):
    args = dict(node="node7", cluster_name="my_cluster", live_cluster="Test Cluster", has_data=True, running=False,
                size=12 * 1024 ** 3, keyspaces=["system", "system_schema"], peers=False, cluster_keyspaces=None,
                ring_problems=[], title="node7 (dc1/rack_b)")
    args.update(kwargs)
    return cassandra_add_node_reset_check(**args)


def test_auto_reset_of_a_stock_node_that_is_down():
    out = _auto()
    assert out == {"reset": True, "problems": [],
                   "line": "node7 (dc1/rack_b): has data (12.0 GiB, cluster 'Test Cluster', not in any ring, down)"
                           " — will be reset"}


def test_auto_reset_nothing_to_do_without_data():
    assert _auto(has_data=False, running=True, live_cluster="other") == {"reset": False, "problems": [], "line": ""}


def test_auto_reset_refused_on_a_running_node():
    out = _auto(running=True)
    assert not out["reset"]
    assert out["problems"] == ["Cassandra runs on it: add_node resets only a node where Cassandra is down. Stop it"
                               " (systemctl stop cassandra) if it holds nothing you need, then run add_node again"]
    assert out["line"].startswith("node7 (dc1/rack_b): has data (12.0 GiB, cluster 'Test Cluster', not in any ring,"
                                  " running): not reset automatically, Cassandra runs on it")


def test_auto_reset_refused_when_in_the_ring():
    seen = "node1 sees 10.100.100.7 in its ring (UN, host ID id-7): node7 is a member of the cluster, never reset it"
    out = _auto(live_cluster="my_cluster", ring_problems=[seen])
    assert out["problems"] == [seen]
    assert "(12.0 GiB, cluster 'my_cluster', in the ring of this cluster, down)" in out["line"]


def test_auto_reset_refused_when_the_ring_can_not_be_read():
    why = "no other node of the cluster answered as up and normal (UN) (asked: node1): whether node7 is in the ring can't be checked"
    out = _auto(ring_problems=[why])
    assert out["problems"] == [why] and "ring unknown" in out["line"]


def test_auto_reset_refused_for_another_cluster():
    out = _auto(live_cluster="billing")
    assert out["problems"] == ["its data is of cluster 'billing', neither 'my_cluster' nor the package's stock 'Test Cluster'"]
    assert "cluster 'billing'" in out["line"]


def test_auto_reset_refused_when_the_cluster_is_unknown():
    out = _auto(live_cluster=None)
    assert out["problems"] == ["the cluster its data belongs to is unknown (no readable cassandra.yaml)"]
    assert "cluster unknown" in out["line"]


def test_auto_reset_refused_for_a_member_of_another_test_cluster_ring():
    out = _auto(peers=True)
    assert out["problems"] == ["its system.peers lists other nodes: a member of another 'Test Cluster' ring"]
    assert "in another ring" in out["line"]


def test_auto_reset_refused_for_user_keyspaces_of_test_cluster():
    out = _auto(keyspaces=["system", "shop", "users", "system_auth"])
    assert out["problems"] == ["it holds user keyspaces (shop, users): only a failed bootstrap of this cluster may"]
    assert "user keyspaces shop, users" in out["line"]


def test_auto_reset_of_a_failed_bootstrap_of_this_cluster():
    out = _auto(live_cluster="my_cluster", keyspaces=["system", "shop"], peers=True, cluster_keyspaces={"shop": {}})
    assert out["reset"] and out["problems"] == []
    assert out["line"] == ("node7 (dc1/rack_b): has data (12.0 GiB, cluster 'my_cluster', user keyspaces shop (a failed"
                           " bootstrap of this cluster), not in any ring, down) — will be reset")


def test_auto_reset_refused_for_user_keyspaces_when_the_cluster_keyspaces_are_unknown():
    # no CQL answer: the same name is not enough (a clone of the cluster, a cassandra.yaml rewritten)
    out = _auto(live_cluster="my_cluster", keyspaces=["shop"])
    assert not out["reset"]
    assert out["problems"] == ["it holds user keyspaces (shop) and the cluster's keyspaces could not be read: whether"
                               " they are a failed bootstrap's can't be checked"]
    # without user keyspaces it does not matter
    assert _auto(live_cluster="my_cluster", keyspaces=["system"])["reset"]


def test_auto_reset_ring_problems_as_a_string():
    assert _auto(ring_problems="no other node answered")["problems"] == ["no other node answered"]


def test_auto_reset_refused_for_keyspaces_this_cluster_does_not_have():
    out = _auto(live_cluster="my_cluster", keyspaces=["shop", "legacy"], cluster_keyspaces={"shop": {}})
    assert out["problems"] == ["it holds keyspaces this cluster does not have (legacy)"]


def test_auto_reset_every_reason_at_once():
    out = _auto(running=True, live_cluster="billing", keyspaces=["orders"])
    assert len(out["problems"]) == 3
    assert out["line"].endswith("Nothing was changed: check what it holds; if nothing is needed, empty it (reset_node)"
                                " or remove it from cassandra_new_nodes")


def test_live_cluster_name_returned():
    assert cassandra_node_reset_dirs(INVENTORY, "cluster_name: billing\n", MOUNTS)["cluster_name"] == "billing"
    assert cassandra_node_reset_dirs(INVENTORY, "num_tokens: 16\n", MOUNTS)["cluster_name"] == "Test Cluster"
    assert cassandra_node_reset_dirs(INVENTORY, None, MOUNTS)["cluster_name"] is None


def test_auto_reset_a_cluster_with_the_stock_name_does_not_own_stock_nodes():
    # this cluster kept the name 'Test Cluster': a stock node that met other nodes, or holds user keyspaces,
    # may be of any other stock-named ring
    out = _auto(cluster_name="Test Cluster", peers=True)
    assert out["problems"] == ["its system.peers lists other nodes: a member of another 'Test Cluster' ring"]
    out = _auto(cluster_name="Test Cluster", keyspaces=["system", "app"], cluster_keyspaces=["app"])
    assert out["problems"] == ["it holds user keyspaces (app): only a failed bootstrap of this cluster may"]
    assert _auto(cluster_name="Test Cluster")["reset"]  # blank of user data, never met a node: reset
