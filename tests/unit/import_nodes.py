from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's entry for one ring node (cassandra_import_nodes, as "Match the ring with the hosts" calls it),
# from what was read on its host: node1 at 10.0.0.1, read unless read=False.

import os

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import_nodes import cassandra_import_nodes

with open(os.path.join(os.path.dirname(__file__), "..", "..", "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    WRITE_VARS = next(p for p in yaml.safe_load(f) if p["name"] == "Write the inventory")["vars"]

ADDRESS = "10.0.0.1"
FACTS = {"hostname": "node1", "os_family": "RedHat", "all_ipv4_addresses": [ADDRESS]}
READ = {"import_cluster_config": {"vars": {}}, "import_cluster_conf_dir": "/etc/cassandra/conf",
        "import_cluster_series": "41x", "import_cluster_version": "4.1.5", "import_cluster_java": "11",
        "import_cluster_running": {"rc": 0}}


def node(hv=None, read=True, jmx=None, **options):
    """hv: the host's variables (over a host read with Java 11, RedHat facts); options: host_names,
    set_ansible_host, keep_hand_edits."""
    hv = dict(hv or {})
    hv["ansible_facts"] = dict(FACTS, **hv.get("ansible_facts", {}))
    if read:
        hv = dict(READ, **hv)
    else:
        hv.pop("import_cluster_config", None)
    ring = [{"address": ADDRESS, "dc": "dc1", "rack": "r1"}]
    return cassandra_import_nodes(ring, {"node1": hv}, ["node1"], [], jmx or {}, WRITE_VARS["_java_default"], **options)[0]
