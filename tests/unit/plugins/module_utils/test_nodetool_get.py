from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import parse_nodetool_get


# Lines printed by the nodetool get* commands of Cassandra 4.0, 4.1 and 5.0.
@pytest.mark.parametrize("out, expected", [
    # getstreamthroughput: 4.0, then 4.1/5.0 with -d (default 24 MiB/s)
    ("Current stream throughput: 200 Mb/s", (200.0, "Mb/s")),
    ("Current stream throughput: 201.326592 Mb/s", (201.326592, "Mb/s")),
    ("Current stream throughput: 500.0 Mb/s\n", (500.0, "Mb/s")),
    # 4.1/5.0 print unlimited for 0
    ("Current stream throughput: unlimited", (0.0, None)),
    ("Current stream throughput: 0 Mb/s", (0.0, "Mb/s")),
    # getinterdcstreamthroughput: 4.0, then 4.1/5.0 with -d
    ("Current inter-datacenter stream throughput: 200 Mb/s", (200.0, "Mb/s")),
    ("Current stream throughput: 200.0 Mb/s", (200.0, "Mb/s")),
    # getcompactionthroughput: 4.0, then 4.1/5.0 with -d
    ("Current compaction throughput: 64 MB/s", (64.0, "MB/s")),
    ("Current compaction throughput: 64.0 MiB/s", (64.0, "MiB/s")),
    ("Current compaction throughput: 0.5 MiB/s", (0.5, "MiB/s")),
    # Java prints large and small doubles with an exponent
    ("Current stream throughput: 1.0E7 Mb/s", (10000000.0, "Mb/s")),
    ("Current trace probability: 1.0E-4", (0.0001, None)),
    ("Current trace probability: 0.0", (0.0, None)),
    ("Batchlog replay throttle: 1024 KB/s", (1024.0, "KB/s")),
    ("Current max hint window: 10800000 ms", (10800000.0, "ms")),
    ("Current timeout for type read: 5000 ms", (5000.0, "ms")),
])
def test_parse(out, expected):
    value, unit, line = parse_nodetool_get(out)
    assert (value, unit) == expected
    assert line == out.strip()


def test_parse_cast_int():
    value, unit, line = parse_nodetool_get("Current max hint window: 10800000 ms", int)
    assert value == 10800000
    assert isinstance(value, int)


def test_parse_unlimited_cast_int():
    value, unit, line = parse_nodetool_get("Current stream throughput: unlimited", int)
    assert value == 0
    assert isinstance(value, int)


def test_parse_skips_other_lines():
    out = "WARN  Some JVM warning\nCurrent stream throughput: 200.0 Mb/s\n"
    assert parse_nodetool_get(out) == (200.0, "Mb/s", "Current stream throughput: 200.0 Mb/s")


@pytest.mark.parametrize("out", [
    "",
    "nodetool: Failed to connect to '127.0.0.1:7199'",
    "Current stream throughput: n/a",
    "Current stream throughput: 200 Mb/s extra",
])
def test_parse_no_value(out):
    assert parse_nodetool_get(out) == (None, None, None)
