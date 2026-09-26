# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Replication checks for the topology playbooks.

cassandra_keyspaces: cqlsh output of
    SELECT JSON keyspace_name, replication FROM system_schema.keyspaces;
    -> {keyspace: {'class': short class name, 'rf': {dc: n} or {'*': n}}}
cassandra_replication_problems: keyspaces, {dc: nodes left} -> the keyspaces
    that would have fewer nodes than replicas, as sentences.
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


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_keyspaces": cassandra_keyspaces,
            "cassandra_replication_problems": cassandra_replication_problems,
        }
