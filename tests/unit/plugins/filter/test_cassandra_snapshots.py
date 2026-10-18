from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The snapshots a node has, from nodetool listsnapshots (upgrade: the pre-upgrade ones removed by cleanup, the
# one prepare takes skipped when an interrupted prepare took it already).

from ansible_collections.community.cassandra.plugins.filter.cassandra_snapshots import cassandra_snapshot_dates, cassandra_snapshot_names

# 4.0: no creation time
LISTED = """\
Snapshot Details:
Snapshot name     Keyspace name Column family name True size Size on disk
pre-upgrade-4.1.5 system_schema tables             0 bytes   13.4 KiB
pre-upgrade-4.1.5 ks1           t1                 0 bytes   1.2 KiB
1697000000000     ks1           t1                 0 bytes   1.2 KiB
pre-upgrade-4.0.1 ks1           t1                 0 bytes   1.2 KiB

Total TrueDiskSpaceUsed: 0 bytes
"""

# 4.1 and 5.0: the creation time (5.0 adds Ephemeral); a snapshot with no TTL has no expiration time
DATED = """\
Snapshot Details:
Snapshot name     Keyspace name Column family name True size Size on disk Creation time            Expiration time Ephemeral
pre-upgrade-4.1.5 system_schema tables             0 bytes   13.4 KiB     2026-10-02T09:12:41.317Z                 false
pre-upgrade-4.1.5 ks1           t1                 0 bytes   1.2 KiB      2026-10-02T09:12:41.317Z                 false
1697000000000     ks1           t1                 0 bytes   1.2 KiB      2023-10-11T05:33:20.000Z                 false

Total TrueDiskSpaceUsed: 0 bytes
"""


def test_each_name_once_in_order():
    assert cassandra_snapshot_names(LISTED) == ["pre-upgrade-4.1.5", "1697000000000", "pre-upgrade-4.0.1"]


def test_prefix_keeps_the_pre_upgrade_ones():
    assert cassandra_snapshot_names(LISTED, "pre-upgrade-") == ["pre-upgrade-4.1.5", "pre-upgrade-4.0.1"]


def test_none():
    assert cassandra_snapshot_names("There are no snapshots\n") == []
    assert cassandra_snapshot_names("") == []
    assert cassandra_snapshot_names(None, "pre-upgrade-") == []


def test_dates_where_the_listing_has_them():
    assert cassandra_snapshot_names(DATED, "pre-upgrade-") == ["pre-upgrade-4.1.5"]
    assert cassandra_snapshot_dates(DATED) == {"pre-upgrade-4.1.5": "2026-10-02T09:12:41.317Z", "1697000000000": "2023-10-11T05:33:20.000Z"}
    assert cassandra_snapshot_dates(DATED, "pre-upgrade-") == {"pre-upgrade-4.1.5": "2026-10-02T09:12:41.317Z"}
    assert cassandra_snapshot_dates(LISTED, "pre-upgrade-") == {}  # 4.0: no column
    assert cassandra_snapshot_dates("There are no snapshots\n") == {}
    assert cassandra_snapshot_dates(None) == {}
