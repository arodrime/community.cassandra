# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_snapshot_names: the snapshots nodetool listsnapshots lists, by name;
cassandra_snapshot_dates: when each was taken, where the listing says (4.1+)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

# the lines of nodetool listsnapshots that are not a snapshot row
_NOT_A_ROW = ("Snapshot Details", "Snapshot name", "Total TrueDiskSpaceUsed", "There are no snapshots")


def cassandra_snapshot_names(output, prefix=""):
    """The names of the snapshots in nodetool listsnapshots' output (a row per table, the snapshot name its first
    column), each once, in order; prefix keeps the ones starting with it. Names with a space are cut at it.
    "Snapshot Details:\\nSnapshot name Keyspace name ...\\npre-upgrade-4.1.5 ks1 t1 0 bytes 1 KiB\\n...", "pre-upgrade-"
    -> ["pre-upgrade-4.1.5"]; "There are no snapshots" -> []."""
    names = []
    for line in str(output or "").splitlines():
        words = line.split()
        if not words or line.startswith(_NOT_A_ROW):
            continue
        if words[0].startswith(prefix) and words[0] not in names:
            names.append(words[0])
    return names


def cassandra_snapshot_dates(output, prefix=""):
    """When the listing has a Creation time column (4.1+; the values are aligned under their header), the creation
    time of each snapshot as printed, by name; prefix keeps the ones starting with it. 4.0 has no column: {}.
    "...Size on disk Creation time ...\npre-upgrade-4.1.5 ks1 t1 0 bytes 1 KiB 2026-10-02T09:12:41.317Z ...", "pre-upgrade-"
    -> {"pre-upgrade-4.1.5": "2026-10-02T09:12:41.317Z"}."""
    dates = {}
    start = -1
    for line in str(output or "").splitlines():
        if line.startswith("Snapshot name"):
            start = line.find("Creation time")
            continue
        words = line.split()
        if start < 0 or not words or line.startswith(_NOT_A_ROW):
            continue
        if words[0].startswith(prefix) and words[0] not in dates and line[start:].split():
            dates[words[0]] = line[start:].split()[0]
    return dates


class FilterModule(object):
    def filters(self):
        return {"cassandra_snapshot_names": cassandra_snapshot_names, "cassandra_snapshot_dates": cassandra_snapshot_dates}
