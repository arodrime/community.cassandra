# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Replication checks before a rack goes down (stop_rack, rolling_restart's rack mode).

cassandra_keyspaces: cqlsh output of
    SELECT JSON keyspace_name, replication FROM system_schema.keyspaces;
    -> {keyspace: {'class': short class name, 'rf': {dc: n} or {'*': n}}}
cassandra_rack_down_problems: keyspaces, dc, racks in that dc -> why taking one
    rack of that dc down would lose more than one replica of some data, and
    warnings (RF 2: QUORUM fails with one replica down).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json

from ansible.errors import AnsibleFilterError


def cassandra_keyspaces(stdout):
    """cqlsh's JSON rows of system_schema.keyspaces -> {keyspace: {class, rf: {dc: replicas}}}: a row of ks,
    NetworkTopologyStrategy dc1 3 -> {"ks": {"class": "NetworkTopologyStrategy", "rf": {"dc1": 3}}}."""
    keyspaces = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise AnsibleFilterError("cassandra_keyspaces: not a JSON row: %s" % line)
        options = dict(row["replication"])
        cls = options.pop("class").rsplit(".", 1)[-1]
        if cls == "SimpleStrategy":
            rf = {"*": int(options.get("replication_factor", 1))}
        elif cls == "NetworkTopologyStrategy":
            rf = dict((dc, int(n)) for dc, n in options.items())
        else:  # LocalStrategy, EverywhereStrategy...: no fixed replica count
            rf = {}
        keyspaces[row["keyspace_name"]] = {"class": cls, "rf": rf}
    return keyspaces


def cassandra_rack_down_problems(keyspaces, dc, racks):
    """NetworkTopologyStrategy puts a DC's replicas on distinct racks when it has
    at least as many racks as replicas: then one rack down is one replica down.
    -> {problems, warnings}: ks 3 replicas
    in dc1 of 2 racks -> problems ["ks has 3 replicas in dc1, which has 2 rack(s): a rack holds more than one of them"]."""
    problems, warnings = [], []
    for name in sorted(keyspaces):
        rf = keyspaces[name]["rf"]
        if "*" in rf and rf["*"] > 1:
            problems.append("%s uses SimpleStrategy with RF %d, which ignores racks: a rack down can hold "
                            "several of its replicas" % (name, rf["*"]))
        elif dc in rf and rf[dc] > racks:
            problems.append("%s has %d replicas in %s, which has %d rack(s): a rack holds more than one of them"
                            % (name, rf[dc], dc, racks))
        elif rf.get(dc) == 2:
            warnings.append("%s has 2 replicas in %s: with one down, (LOCAL_)QUORUM fails" % (name, dc))
    return {"problems": problems, "warnings": warnings}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_keyspaces": cassandra_keyspaces,
            "cassandra_rack_down_problems": cassandra_rack_down_problems,
        }
