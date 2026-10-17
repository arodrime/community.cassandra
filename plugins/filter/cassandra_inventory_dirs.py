# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_inventory_dirs: the Cassandra directories the inventory gives a node, each with its kind (what a
reset empties, node_standing_dirs.yml)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


def cassandra_inventory_dirs(data_dirs, commitlog, saved_caches, hints, extra=None, cdc_raw="", cdc_enabled=False,
                             storage_dir=""):
    """data_dirs: cassandra_data_file_directories; commitlog, saved_caches, hints: their cassandra_*_dir; extra:
    cassandra_extra_settings; cdc_raw: cassandra_cdc_raw_dir; cdc_enabled: cassandra_cdc_enabled; storage_dir:
    cassandra_config_storage_dir. -> [[kind, dir]]: data, commitlog, saved_caches, hints, the
    local_system_data_file_directory of cassandra_extra_settings (data), and cdc_raw: cassandra_cdc_raw_dir,
    else the cdc_raw_directory of cassandra_extra_settings (an inventory from before the variable), else with CDC
    on, Cassandra's default under the storage dir."""
    extra = extra or {}
    out = [["data", d] for d in data_dirs]
    out += [["commitlog", commitlog], ["saved_caches", saved_caches], ["hints", hints]]
    if "local_system_data_file_directory" in extra:
        out.append(["data", extra["local_system_data_file_directory"]])
    if cdc_raw or "cdc_raw_directory" in extra:
        out.append(["cdc_raw", cdc_raw or extra["cdc_raw_directory"]])
    elif cdc_enabled:
        top = (storage_dir or "/var/lib/cassandra")
        out.append(["cdc_raw", (top.rstrip("/") or top[:1]) + "/cdc_raw"])
    return out


class FilterModule(object):
    def filters(self):
        return {"cassandra_inventory_dirs": cassandra_inventory_dirs}
