from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster refuses a node that runs DataStax Enterprise or ScyllaDB: the
# REFUSED block from what the distribution read printed on each node.

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_import_distribution import (
    cassandra_import_unsupported, runs, series_list)

ONLY = ("  This collection drives Apache Cassandra 4.0, 4.1 and 5.0 only (DSE and ScyllaDB have their own packages,"
        " services and configuration files).")
CASSANDRA = "distribution=cassandra\ndistribution_version=\n"


@pytest.mark.parametrize("text, expected", [
    ("distribution=dse\ndistribution_version=6.8.42\n", "DataStax Enterprise 6.8.42"),
    ("distribution=dse\ndistribution_version=\n", "DataStax Enterprise"),  # dse -v failed: the name alone
    ("distribution=scylla\ndistribution_version=5.4.3-0.20240305.c0c3ad4ac5a1\n", "ScyllaDB 5.4.3"),
    # no marker, nodetool answers with four numbers: a DataStax build
    ("distribution=\ndistribution_version=\nrelease=4.0.0.6816\n", "a DataStax Enterprise build (ReleaseVersion 4.0.0.6816)"),
    ("config=/etc/cassandra/cassandra.yaml\nversion=3.11.3.5115\n", "a DataStax Enterprise build (ReleaseVersion 3.11.3.5115)"),
    (CASSANDRA, ""),
    ("config=/etc/cassandra/cassandra.yaml\nversion=4.1.5\n" + CASSANDRA, ""),
    ("distribution=\ndistribution_version=\nrelease=\n", ""),  # stopped, nothing found: not this refusal
    ("", ""),
])
def test_what_a_node_runs(text, expected):
    assert runs(text) == expected


def test_the_series_come_from_the_collection():
    assert series_list() == "4.0, 4.1 and 5.0"


def test_nothing_to_refuse():
    assert cassandra_import_unsupported({"h-n1": CASSANDRA, "h-n2": "version=4.1.5\n" + CASSANDRA}) == []
    assert cassandra_import_unsupported({}) == []


def test_a_dse_node():
    assert cassandra_import_unsupported({"h-n1": "distribution=dse\ndistribution_version=6.8.42\n"}) == [
        "REFUSED  import_cluster  nothing was written",
        "  h-n1 runs DataStax Enterprise 6.8.42: not supported.",
        ONLY]


def test_a_scylla_node():
    assert cassandra_import_unsupported({"h-n1": "distribution=scylla\ndistribution_version=5.4.3-0.20240305.c0c3ad4ac5a1\n"}) == [
        "REFUSED  import_cluster  nothing was written",
        "  h-n1 runs ScyllaDB 5.4.3: not supported.",
        ONLY]


def test_the_nodes_of_one_distribution_on_one_line():
    dse = "distribution=dse\ndistribution_version=6.8.42\n"
    read = {"h-n3": dse, "h-n1": dse, "h-n2": dse, "h-n4": "distribution=scylla\ndistribution_version=5.4.3\n",
            "h-n5": CASSANDRA}
    assert cassandra_import_unsupported(read) == [
        "REFUSED  import_cluster  nothing was written",
        "  h-n1..h-n3 run DataStax Enterprise 6.8.42: not supported.",
        "  h-n4 runs ScyllaDB 5.4.3: not supported.",
        ONLY]
