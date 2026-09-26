# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Replication checks for the topology playbooks.

cassandra_keyspaces: cqlsh output of
    SELECT JSON keyspace_name, replication FROM system_schema.keyspaces;
    -> {keyspace: {'class': short class name, 'rf': {dc: n} or {'*': n}}}
cassandra_replication_problems: keyspaces, {dc: nodes left} -> the keyspaces
    that would have fewer nodes than replicas, as sentences.
cassandra_replication_alter: keyspaces, dc, {keyspace: rf} to add that dc to
    those keyspaces, or {} with remove=True to take it out of every keyspace
    -> ALTER KEYSPACE statements.
cassandra_rack_down_problems: keyspaces, dc, racks in that dc -> why taking one
    rack of that dc down would lose more than one replica of some data, and
    warnings (RF 2: QUORUM fails with one replica down).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json

from ansible.errors import AnsibleFilterError


def cassandra_keyspaces(stdout):
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


def cassandra_replication_problems(keyspaces, nodes_left):
    """nodes_left: {dc: number of nodes the DC keeps}."""
    problems = []
    total = sum(nodes_left.values())
    for name in sorted(keyspaces):
        for dc, rf in sorted(keyspaces[name]["rf"].items()):
            if dc == "*":
                if rf > total:
                    problems.append("%s (SimpleStrategy, RF %d) needs %d nodes, the cluster would keep %d"
                                    % (name, rf, rf, total))
            elif rf > nodes_left.get(dc, 0):
                problems.append("%s needs %d replicas in %s, which would keep %d node(s)"
                                % (name, rf, dc, nodes_left.get(dc, 0)))
    return problems


def cassandra_rack_down_problems(keyspaces, dc, racks):
    """NetworkTopologyStrategy puts a DC's replicas on distinct racks when it has
    at least as many racks as replicas: then one rack down is one replica down."""
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


def _cql_replication(rf):
    return "{'class': 'NetworkTopologyStrategy', %s}" % ", ".join("'%s': %d" % (dc, n) for dc, n in sorted(rf.items()))


def cassandra_replication_alter(keyspaces, dc, add=None, remove=False):
    statements = []
    if remove:
        for name in sorted(keyspaces):
            rf = keyspaces[name]["rf"]
            if dc in rf:
                rest = dict((d, n) for d, n in rf.items() if d != dc)
                if not rest:
                    raise AnsibleFilterError("%s only has replicas in %s: drop it or give it replicas elsewhere first"
                                             % (name, dc))
                statements.append('ALTER KEYSPACE "%s" WITH replication = %s;' % (name, _cql_replication(rest)))
        return statements
    for name, n in sorted((add or {}).items()):
        if name not in keyspaces:
            raise AnsibleFilterError("keyspace %s does not exist" % name)
        if keyspaces[name]["class"] != "NetworkTopologyStrategy":
            raise AnsibleFilterError("%s uses %s: switch it to NetworkTopologyStrategy first (ALTER KEYSPACE, then repair)"
                                     % (name, keyspaces[name]["class"]))
        rf = dict(keyspaces[name]["rf"], **{dc: int(n)})
        if rf != keyspaces[name]["rf"]:
            statements.append('ALTER KEYSPACE "%s" WITH replication = %s;' % (name, _cql_replication(rf)))
    return statements


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_keyspaces": cassandra_keyspaces,
            "cassandra_replication_problems": cassandra_replication_problems,
            "cassandra_rack_down_problems": cassandra_rack_down_problems,
            "cassandra_replication_alter": cassandra_replication_alter,
        }
