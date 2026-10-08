from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A JAVA_TOOL_OPTIONS in the environment makes the JVM print "Picked up
# JAVA_TOOL_OPTIONS: ..." before nodetool's own output (stderr, or stdout when
# merged). Every nodetool parser gives the same answer with it, and the
# modules' nodetool runs drop it from stdout.

import os

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import (
    NodeToolCmd, NodeToolCommandSimple, strip_jvm_banner)
from ansible_collections.community.cassandra.plugins.module_utils import nodetool_netstats, nodetool_status
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_tokens import parse_ring
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import parse_nodetool_get
from ansible_collections.community.cassandra.plugins.modules.cassandra_status import cluster_up_down
from ansible_collections.community.cassandra.plugins.modules.cassandra_netstats import parse_netstats
from ansible_collections.community.cassandra.plugins.modules.cassandra_move import parse_info_tokens
from ansible_collections.community.cassandra.plugins.modules.cassandra_fullquerylog import parse_getfullquerylog
from ansible_collections.community.cassandra.plugins.modules.cassandra_removenode import leaving_nodes
from ansible_collections.community.cassandra.plugins.modules.cassandra_schema import cluster_schema
from ansible_collections.community.cassandra.plugins.modules.cassandra_invalidatecache import parse_cache_info

BANNERS = [
    "Picked up JAVA_TOOL_OPTIONS: -Dcom.sun.jndi.rmiURLParsing=legacy\n",
    "Picked up _JAVA_OPTIONS: -Xmx512m\nPicked up JAVA_TOOL_OPTIONS: -Dcom.sun.jndi.rmiURLParsing=legacy\n",
    "OpenJDK 64-Bit Server VM warning: Options -Xverify:none and -noverify were deprecated\n",
]
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "modules", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


class FakeModule(object):
    def __init__(self, out, **params):
        self.params = dict(host="127.0.0.1", port=7199, password=None, password_file=None, username=None,
                           nodetool_path=None, nodetool_flags="", debug=False, cassandra_version="5.0")
        self.params.update(params)
        self.out = out

    def fail_json(self, **kwargs):
        raise AssertionError(kwargs["msg"])

    def run_command(self, cmd):
        return 0, self.out, BANNERS[0]


@pytest.mark.parametrize("banner", BANNERS)
def test_strip(banner):
    assert strip_jvm_banner(banner + "Mode: NORMAL\n") == "Mode: NORMAL\n"
    assert strip_jvm_banner("Mode: NORMAL\n") == "Mode: NORMAL\n"
    assert strip_jvm_banner("") == ""
    assert strip_jvm_banner(None) is None


def test_nodetool_runs_drop_it_from_stdout_and_keep_stderr():
    rc, out, err = NodeToolCommandSimple(FakeModule(BANNERS[0] + "Mode: NORMAL\n"), "netstats").run_command()
    assert (rc, out, err) == (0, "Mode: NORMAL\n", BANNERS[0])


@pytest.mark.parametrize("banner", BANNERS)
def test_version_detected(banner):
    module = FakeModule(banner + "ReleaseVersion: 5.0.7\n", cassandra_version=None)
    NodeToolCmd(module)
    assert module.params["cassandra_version"] == "5.0"


@pytest.mark.parametrize("banner", BANNERS)
@pytest.mark.parametrize("name", ["nodetool_status_vnodes.txt", "nodetool_status_multi_dc.txt",
                                  "nodetool_status_token_per_node.txt", "nodetool_status_vnodes_load_unknown.txt"])
def test_status(banner, name):
    out = fixture(name)
    assert cluster_up_down(banner + out) == cluster_up_down(out)
    assert cluster_up_down(out)  # the fixture has nodes
    first = cluster_up_down(out)
    node = list(first.values())[0]["nodes"][0]
    assert nodetool_status.ring_state(banner + out, node["address"]) == node["status"] + node["state"]
    assert nodetool_status.node_state(banner + out, node["host_id"]) == node["status"] + node["state"]
    assert leaving_nodes(banner + out) == leaving_nodes(out)


@pytest.mark.parametrize("banner", BANNERS)
@pytest.mark.parametrize("name", ["nodetool_netstats_normal.txt", "nodetool_netstats_joining.txt",
                                  "nodetool_netstats_50_bootstrap_receiving.txt",
                                  "nodetool_netstats_41_bootstrap_sending.txt"])
def test_netstats(banner, name):
    out = fixture(name)
    assert nodetool_netstats.parse_netstats(banner + out) == nodetool_netstats.parse_netstats(out)
    assert nodetool_netstats.node_mode(banner + out) == nodetool_netstats.node_mode(out) != ""
    assert parse_netstats(banner + out) == parse_netstats(out)


@pytest.mark.parametrize("banner", BANNERS)
def test_gossipinfo(banner):
    out = ("/10.100.100.1\n  generation:1700000000\n  STATUS_WITH_PORT:24:NORMAL,-123\n"
           "/10.100.100.2\n  generation:1700000001\n  STATUS:12:shutdown,true\n")
    assert nodetool_status.gossip_status(banner + out, "10.100.100.1") == "NORMAL"
    assert nodetool_status.gossip_status(banner + out, "10.100.100.2") == "shutdown"
    assert nodetool_status.gossip_status(banner + out, "10.100.100.3") is None


@pytest.mark.parametrize("banner", BANNERS)
def test_ring(banner):
    out = ("\nDatacenter: dc1\n==========\nAddress        Rack   Status State   Load       Owns   Token\n"
           "                                                                 3074457345618258602\n"
           "10.100.100.1   rack1  Up     Normal  1.2 MiB    33.3%  -9223372036854775808\n"
           "10.100.100.2   rack1  Up     Normal  1.1 MiB    33.3%  -3074457345618258603\n")
    assert parse_ring(banner + out) == parse_ring(out)
    assert len(parse_ring(out)["dc1"]) == 2


@pytest.mark.parametrize("banner", BANNERS)
def test_info_tokens(banner):
    out = "ID                     : 5a1c3b2e-8d9f-4a6b-7c8d-9e0f1a2b3c4d\nToken                  : -9223372036854775808\n"
    assert parse_info_tokens(banner + out) == ["-9223372036854775808"]


@pytest.mark.parametrize("banner", BANNERS)
def test_fullquerylog(banner):
    out = "enabled             false\nlog_dir\narchive_command\nroll_cycle          HOURLY\nblock               true\n" \
          "max_log_size        17179869184\nmax_queue_weight    268435456\nmax_archive_retries 10\n"
    assert parse_getfullquerylog(banner + out) == parse_getfullquerylog(out)
    assert "Picked" not in parse_getfullquerylog(banner + out)


@pytest.mark.parametrize("banner", BANNERS)
def test_schema(banner):
    out = ("Cluster Information:\n\tName: my_cluster\n\tSchema versions:\n"
           "\t\td4f18346-f81f-3786-aed4-40e03558b299: [10.100.100.1, 10.100.100.2]\n")
    assert cluster_schema(banner + out) == cluster_schema(out)
    assert list(cluster_schema(out)) == ["d4f18346-f81f-3786-aed4-40e03558b299"]


@pytest.mark.parametrize("banner", BANNERS)
def test_throughput_and_cache_info(banner):
    assert parse_nodetool_get(banner + "Current stream throughput: 24.0 Mb/s\n") == (
        24.0, "Mb/s", "Current stream throughput: 24.0 Mb/s")
    info = ("Key Cache              : entries 12, size 1 KiB, capacity 100 MiB\n"
            "Row Cache              : entries 0, size 0 bytes, capacity 0 bytes\n"
            "Counter Cache          : entries 3, size 0 bytes, capacity 50 MiB\n")
    assert parse_cache_info(banner + info, FakeModule(""), False) == parse_cache_info(info, FakeModule(""), False)
