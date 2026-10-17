# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_health_problems: the problems found by the cluster health check
(roles/cassandra_operation/tasks/cluster_health.yml), as readable sentences;
cassandra_health_findings: the same as data, for cassandra_health_report:
the health_check playbook's report, a line per problem (each one once,
whatever node saw it), the verdict first.
cassandra_health_wait_line: the progress line of the wait for nodes down to
come back."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import JMX_CAUSES
from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import compressed_address as _ip


def _by_ip(names):
    """{address: name} by one spelling of each address, the first given winning (cassandra_host_addresses
    puts what hosts listen on first)."""
    by_ip = {}
    for address, name in (names or {}).items():
        by_ip.setdefault(_ip(address), name)
    return by_ip


def _nodes(cluster_status):
    return [n for dc in cluster_status.values() for n in dc.get("nodes", [])]


def _error(result):
    msg = result.get("msg", "unreachable")
    stderr = (result.get("stderr") or "").strip()
    # nodetool's own error line, else the last one (not a Java stack trace line)
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    said = [line for line in lines if line.startswith("error:")] or [
        line for line in lines if not line.startswith(("at ", "-- StackTrace --", "..."))][-1:]
    line = said[0] if said else ""
    if msg.startswith(("nodetool gets no answer from JMX", "nodetool can't reach JMX")):
        return msg  # (version_error: nodetool's line said already, in words)
    return "%s (%s)" % (msg, line) if line and line not in msg and line.replace("error:", "").strip() not in msg else msg


def _unasked(result):
    """A check that could not ask nodetool (it failed with a message, no answer)."""
    return "is_up" not in result and bool(result.get("msg"))


def cassandra_health_findings(views, expected, node, gossip=None, binary=None, netstats=None, schema=None, ports=None,
                              down_ok=None, joining_ok=None, leaving_ok=None, names=None):
    """The problems of cassandra_health_problems (same arguments) as
    [{kind, text (its sentence), ...}]: kind state (address, rack, state,
    seen_from), count (ring, expected, seen_from), nodetool (on, error), port
    (name, port, on, host), gossip, cql, streams (on), netstats (on, error),
    schema (msg).
    e.g. (the same) -> [{kind: "state", address: "10.0.0.4", rack: "r1", state: "DN", seen_from: "node1", text:
    "node4 (10.0.0.4, r1) is DN, seen from node1"}]."""
    names = _by_ip(names)
    found = []
    for view in views:
        result = view["result"]
        if not result.get("cluster_status"):
            found.append({"kind": "nodetool", "on": view["from"], "error": _error(result),
                          "text": "nodetool status failed on %s: %s" % (view["from"], _error(result))})
            continue
        nodes = _nodes(result["cluster_status"])
        for n in nodes:
            if n["status"] == "D" and n["address"] in (down_ok or []):
                continue
            if n["status"] + n["state"] == "UJ" and n["address"] in (joining_ok or []):
                continue
            if n["status"] + n["state"] == "UL" and n["address"] in (leaving_ok or []):
                continue
            if n["status"] != "U" or n["state"] != "N":
                # by its inventory name when it has one: "node4 (10.0.0.4, rack1) is DN"
                name = names.get(_ip(n["address"]))
                where = ("%s, %s" % (n["address"], n["rack"])) if name else n["rack"]
                found.append({"kind": "state", "address": n["address"], "rack": n["rack"],
                              "state": n["status"] + n["state"], "seen_from": view["from"],
                              "text": "%s (%s) is %s%s, seen from %s" % (
                                  name or n["address"], where, n["status"], n["state"], view["from"])})
        if len(nodes) != int(expected):
            found.append({"kind": "count", "ring": len(nodes), "expected": int(expected), "seen_from": view["from"],
                          "text": "the ring has %d nodes, the inventory %d (seen from %s): a node is missing from the ring, "
                                  "or the inventory is incomplete" % (len(nodes), int(expected), view["from"])})
    for port in (ports or {}).get("results", []):
        if port.get("failed"):
            item = port["item"]
            found.append({"kind": "port", "name": item["name"], "port": item["port"], "on": node, "host": item["host"],
                          "text": "%s port %s is not answering on %s (%s)" % (item["name"], item["port"], node, item["host"])})
    # a check that could not ask: when nodetool status failed on this node too (e.g. a JMX login refused), that
    # failure says it; else its own failure (not "not running": not known)
    node_failed = any(view["from"] == node and not view["result"].get("cluster_status") for view in views)
    for check, what in ((gossip, "gossip"), (binary, "native transport (CQL)")):
        if check is not None and _unasked(check) and not node_failed:
            found.append({"kind": "check", "what": what, "on": node, "error": _error(check),
                          "text": "the %s check failed on %s: %s" % (what, node, _error(check))})
    if gossip is not None and not gossip.get("is_up") and not _unasked(gossip):
        found.append({"kind": "gossip", "on": node, "text": "gossip is not running on %s" % node})
    if binary is not None and not binary.get("is_up") and not _unasked(binary):
        found.append({"kind": "cql", "on": node, "text": "the native transport (CQL) is not running on %s" % node})
    if netstats is not None:
        if netstats.get("failed") or "streaming" not in netstats:
            found.append({"kind": "netstats", "on": node, "error": _error(netstats),
                          "text": "nodetool netstats failed on %s: %s" % (node, _error(netstats))})
        elif netstats["streaming"]:
            found.append({"kind": "streams", "on": node, "text": "streams in progress on %s (nodetool netstats)" % node})
    if schema is not None and schema.get("failed"):
        found.append({"kind": "schema", "msg": schema.get("msg", ""), "text": "schema disagreement: %s" % schema.get("msg", "")})
    return found


# A nodetool failure a restart explains, as nodetool_cmd_objects.version_error says it: JMX refused (not up
# yet) or not answering in time (too busy). Not the ones that do not go away waiting: a login refused, nodetool
# missing, a JMX over SSL it can't speak, nodetool version failed (and a wrong JMX port: the node is seen up).
_PASSING = re.compile(r"^nodetool gets no answer from JMX on \S* ?\((%s)\):" % "|".join(
    re.escape(cause) for dummy, cause, dummy2 in JMX_CAUSES[:2]))


def cassandra_health_waiting(findings, names=None):
    """The findings a wait may see go (cluster_health.yml waits while there is one): a node down (DN, DL, DJ,
    DM: restarting, stopped meanwhile), a port, gossip or the native transport of this node not up yet (just
    restarted), a nodetool status that failed with JMX not answering (refused, timed out) on a node another
    one sees down (names: {address: inventory name}); each one once, whatever view saw it. The others (a ring
    of another size, streams, a schema disagreement, nodetool failing on a node seen up...) are not waited
    for: alone, they stop the run at once; with one of these, at the end of the wait.
    Input: cassandra_health_findings' result; output: the findings kept. e.g. [<10.0.0.4 DN>, <the ring has 3
    nodes, the inventory 4>] -> [<10.0.0.4 DN>]."""
    findings = findings or []
    down = set((names or {}).get(f.get("address"), f.get("address")) for f in findings
               if f.get("kind") == "state" and str(f.get("state", ""))[:1] == "D")
    kept, seen = [], set()
    for f in findings:
        key = (f.get("kind"), f.get("address") or "%s %s" % (f.get("on"), f.get("port")))
        if key in seen:
            continue
        if (f.get("kind") == "state" and str(f.get("state", ""))[:1] == "D") or f.get("kind") in ("port", "gossip", "cql") or (
                f.get("kind") == "nodetool" and f.get("on") in down and _PASSING.search(str(f.get("error", "")))):
            seen.add(key)
            kept.append(f)
    return kept


def cassandra_health_wait_line(waiting, names=None, waited=0, timeout=0, index=0, steps=0, node="", operation="",
                               done=False):
    """The progress line of the wait for waiting (cassandra_health_waiting's): "[2/5] node2 restart  waiting for
    node4 (10.0.0.4) DN to come back  1m30s/10m"; names: {address: inventory name}; waited, timeout: seconds; done:
    the line once nothing is waited for any more ("[2/5] node2 restart  all back after 1m40s").
    e.g. ([<10.0.0.4 DN>], names={"10.0.0.4": "node4"}, waited=90, timeout=600, index=2, steps=5, node="node2",
    operation="restart") -> "[2/5] node2 restart  waiting for node4 (10.0.0.4) DN to come back  1m30s/10m"."""
    if int(steps or 0):
        head = "[%d/%d] %s %s" % (int(index), int(steps), node, operation)
    else:
        head = ("%s %s" % (node, operation)).strip()
    if done:
        return "  ".join(x for x in [head, "all back after %s" % out.duration(waited)] if x)

    def down(f):
        name = (names or {}).get(f["address"], f["address"])
        return "%s %s" % (f["address"], f["state"]) if name == f["address"] else "%s (%s) %s" % (name, f["address"], f["state"])

    parts = []
    downs = [down(f) for f in waiting or [] if f.get("kind") == "state"]
    if downs:
        parts.append("%s to come back" % ", ".join(downs))
    parts += ["the %s port %s on %s (%s) to answer" % (f["name"], f["port"], f["on"], f["host"])
              for f in waiting or [] if f.get("kind") == "port"]
    parts += ["%s on %s to run" % ("gossip" if f["kind"] == "gossip" else "the native transport (CQL)", f["on"])
              for f in waiting or [] if f.get("kind") in ("gossip", "cql")]
    for f in waiting or []:
        if f.get("kind") == "nodetool":
            cause = _PASSING.search(str(f.get("error", "")))
            parts.append("nodetool status on %s to answer%s" % (f["on"], " (JMX: %s)" % cause.group(1) if cause else ""))
    return "  ".join(x for x in [head, "waiting for " + "; ".join(parts) if parts else "",
                                 "%s/%s" % (out.duration(waited), out.duration(timeout, short=True))] if x)


def cassandra_health_problems(views, expected, node, gossip=None, binary=None, netstats=None, schema=None, ports=None,
                              down_ok=None, joining_ok=None, leaving_ok=None, names=None):
    """views: [{'from': host, 'result': cassandra_status result}]; the others
    are this node's registered results (ports: a wait_for loop over
    {'name', 'host', 'port'} items). down_ok: addresses expected down (a dead
    node being replaced). joining_ok: addresses expected up and joining (a
    bootstrap still running). leaving_ok: addresses expected up and leaving (a
    decommission still running). names: {address: inventory name}
    (cassandra_host_addresses), a node of the ring named by it. Returns a
    list of problems.
    e.g. ([{from: node1, result: <a ring where 10.0.0.4 is DN>}], 2, "node1", names={"10.0.0.4": "node4"}) ->
    ["node4 (10.0.0.4, r1) is DN, seen from node1"]."""
    return [f["text"] for f in cassandra_health_findings(
        views, expected, node, gossip=gossip, binary=binary, netstats=netstats, schema=schema, ports=ports,
        down_ok=down_ok, joining_ok=joining_ok, leaving_ok=leaving_ok, names=names)]


def _lost_guess(names, found, unreachable, ring):
    """{address: name}, the findings of the hosts checked, the hosts not reached, the ring a node saw -> (the
    address, "host?") of an unreachable host known by no address (no facts, no ansible_host, no listen address)
    taken for the one node of the ring no host has, down; None: never a guess between several, nor with a ring
    of another size than the inventory's."""
    unnamed = sorted(set(_ip(n["address"]) for dc in (ring or {}).values() for n in dc.get("nodes", [])
                         if _ip(n["address"]) not in names))
    down = set(_ip(f["address"]) for f in found if f.get("kind") == "state" and f["state"].startswith("D"))
    addressed = set(name for address, name in names.items() if address != name)
    lost = [h for h in unreachable if h not in addressed]
    if len(unnamed) == 1 and unnamed[0] in down and len(lost) == 1 and not any(f.get("kind") == "count" for f in found):
        return unnamed[0], lost[0] + "?"
    return None


def _finding_row(f, names):
    """A finding (cassandra_health_findings'), {address: name} -> (label, key: the same problem seen by several
    nodes has the same, text, who saw it or where: None for none). A text with %s gets the nodes there."""
    kind = f.get("kind")
    if kind == "state":
        name = names.get(_ip(f["address"]))
        return "ring", ("state", f["address"], f["state"]), "%s%s (%s) %s" % (
            (name + " ") if name else "", f["address"], f["rack"], f["state"]), f["seen_from"]
    if kind == "count":
        return ("ring", ("count", f["ring"], f["expected"]), "%d members, inventory %d" % (f["ring"], f["expected"]),
                f["seen_from"])
    if kind == "nodetool":
        return "nodetool", ("nodetool", f["error"]), "status failed on %s: " + str(f["error"]), f["on"]
    if kind == "port":
        return "ports", ("port", f["name"], f["port"]), "%s %s not answering on" % (f["name"], f["port"]), f["on"]
    if kind == "gossip":
        return "gossip", ("gossip",), "not running on", f["on"]
    if kind == "cql":
        return "CQL", ("cql",), "native transport not running on", f["on"]
    if kind == "streams":
        return "streams", ("streams",), "in progress on", f["on"]
    if kind == "netstats":
        return "netstats", ("netstats", f["error"]), "failed on %s: " + str(f["error"]), f["on"]
    if kind == "check":
        return ("nodetool", ("check", f["what"], f["error"]), "%s check failed on %%s: %s" % (f["what"], f["error"]),
                f["on"])
    if kind == "schema":
        return "schema", ("schema", f["msg"]), "disagreement: %s" % f["msg"], None
    return "other", ("other", f.get("text")), f.get("text", ""), None


def _problem_lines(rows, seen):
    """The problems' keys in order, {key: [label, text, who]} -> a line per problem, with the nodes that saw it."""
    lines = []
    width = max([len(seen[k][0]) for k in rows] or [0]) + 1
    for key in rows:
        label, text, who = seen[key]
        if key[0] in ("state", "count"):
            text += "   seen from %s" % out.nodes(who)
        elif key[0] in ("nodetool", "netstats", "check"):
            text = text.replace("%s", out.nodes(who), 1)
        elif who:
            text += " " + out.nodes(who)
        lines.append("  %s  %s" % ((label + ":").ljust(width), text))
    return lines


def _report_todo(rows, names, unreachable, absent, counted, topology_command):
    """The problems' keys, {address: name}, the hosts not reached, the hosts marked absent, whether the ring was
    counted, the command that removes them -> the TO DO items (a node down and not reached: one item)."""
    todo = []
    down = [names.get(_ip(k[1]), k[1]) for k in rows if k[0] == "state" and k[2].startswith("D")]
    # a node down and not reached: one thing to do, its server first
    both = [h + "?" if h + "?" in down else h for h in unreachable if h in down or h + "?" in down]  # a guess said so
    down = [d for d in down if d not in both]
    if down:
        todo.append("start Cassandra on %s (its server first if it is down); a node that can't be recovered: "
                    "replace_node" % ", ".join(down))
    if both:
        todo.append("bring %s back (not reached over SSH, down in the ring): its server first, then Cassandra, then run "
                    "this again; a node that can't be recovered: replace_node" % out.nodes(both))
    if absent and counted and topology_command:
        todo.append({"text": "remove %s from the ring" % out.nodes(absent), "command": topology_command})
    others = [h for h in unreachable if h not in both and h + "?" not in both]
    if others:
        todo.append("reach %s over SSH, then run this again" % out.nodes(others))
    if any(k[0] == "streams" for k in rows):
        todo.append("wait for the streams to end (nodetool netstats), then run this again")
    return todo


def cassandra_health_report(findings, cluster, hosts, unreachable=None, absent=None, names=None, members=None,
                            topology_command="", ring=None):
    """The health_check report. findings: {host: its
    cassandra_health_findings} for the hosts checked; hosts: every host of
    the run; unreachable: the ones Ansible did not reach (or whose check
    did not run); absent: the hosts marked cassandra_node_state: absent;
    names: {address: inventory name}; members: the ring's size as a node
    saw it; topology_command: the command that removes the absent nodes;
    ring: the cluster_status a node saw (an unreachable host known by no
    address is taken for the one node of that ring no host has, when it is
    down: "node3?").
    Each problem once, with every node that saw it. Returns {healthy,
    problems (count), lines}: "HEALTHY  my_cluster  6 nodes  6 UN, schema
    agreed, no streams, ports open" or "NOT HEALTHY  my_cluster  6 nodes
    checked, 2 problems", a line per problem, then a TO DO (a node down
    and not reached: one item).
    e.g. ({node1: [<10.0.0.4 DN>]}, "my_cluster", ["node1", "node4"], unreachable=["node4"]) -> {healthy: False,
    problems: 2, lines: ["NOT HEALTHY  my_cluster  1 node checked, 2 problems", "  ring:  node4 10.0.0.4 (r1) DN   seen
    from node1", "  ssh:   not reached by Ansible: node4", "TO DO", "  1. bring node4 back (not reached over SSH, down
    in the ring): ..."]}."""
    names = _by_ip(names)  # nodetool prints IPv6 uncompressed
    unreachable = [h for h in hosts if h in (unreachable or [])]
    checked = [h for h in hosts if h not in unreachable]
    rows, seen = [], {}
    found = [f for host in checked for f in (findings or {}).get(host) or []]
    guess = _lost_guess(names, found, unreachable, ring)
    if guess:
        names[guess[0]] = guess[1]

    def add(label, key, text, who=None):
        if key not in seen:
            seen[key] = [label, text, []]
            rows.append(key)
        if who and who not in seen[key][2]:
            seen[key][2].append(who)

    for f in found:
        add(*_finding_row(f, names))
    for host in unreachable:
        add("ssh", ("ssh",), "not reached by Ansible:", host)

    lines = _problem_lines(rows, seen)
    counted = any(k[0] == "count" for k in rows)
    absent = list(absent or [])
    if absent and counted:
        lines.append("  not counted, marked cassandra_node_state: absent: %s" % out.nodes(absent))
    if not rows:
        size = members if members is not None else len(hosts)
        return {"healthy": True, "problems": 0, "lines": [
            "HEALTHY  %s  %s  %d UN, schema agreed, no streams, ports open" % (
                cluster, out.plural(len(hosts), "node"), size)]}
    todo = _report_todo(rows, names, unreachable, absent, counted, topology_command)
    head = "NOT HEALTHY  %s  %s checked, %s" % (cluster, out.plural(len(checked), "node"), out.plural(len(rows), "problem"))
    return {"healthy": False, "problems": len(rows), "lines": [head] + lines + out.todo(todo)}


class FilterModule(object):
    def filters(self):
        return {"cassandra_health_problems": cassandra_health_problems,
                "cassandra_health_findings": cassandra_health_findings,
                "cassandra_health_waiting": cassandra_health_waiting,
                "cassandra_health_wait_line": cassandra_health_wait_line,
                "cassandra_health_report": cassandra_health_report}
