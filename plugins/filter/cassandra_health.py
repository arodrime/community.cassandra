# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_health_problems: the problems found by the cluster health check
(roles/cassandra_service/tasks/cluster_health.yml), as readable sentences;
cassandra_health_findings: the same as data, for cassandra_health_report:
the health_check playbook's report, a line per problem (each one once,
whatever node saw it), the verdict first.
cassandra_leaving_state: where a node given to decommission_node stands.
cassandra_removal_state: where a dead node given to remove_dead_node stands.
cassandra_removal_force_target: the node to run removenode force on."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out


def _nodes(cluster_status):
    return [n for dc in cluster_status.values() for n in dc.get("nodes", [])]


def _error(result):
    msg = result.get("msg", "unreachable")
    stderr = (result.get("stderr") or "").strip()
    return "%s (%s)" % (msg, stderr.splitlines()[-1]) if stderr and stderr not in msg else msg


def cassandra_health_findings(views, expected, node, gossip=None, binary=None, netstats=None, schema=None, ports=None,
                              down_ok=None, joining_ok=None, leaving_ok=None):
    """The problems of cassandra_health_problems (same arguments) as
    [{kind, text (its sentence), ...}]: kind state (address, rack, state,
    seen_from), count (ring, expected, seen_from), nodetool (on, error), port
    (name, port, on, host), gossip, cql, streams (on), netstats (on, error),
    schema (msg)."""
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
                found.append({"kind": "state", "address": n["address"], "rack": n["rack"],
                              "state": n["status"] + n["state"], "seen_from": view["from"],
                              "text": "%s (%s) is %s%s, seen from %s" % (n["address"], n["rack"], n["status"], n["state"],
                                                                        view["from"])})
        if len(nodes) != int(expected):
            found.append({"kind": "count", "ring": len(nodes), "expected": int(expected), "seen_from": view["from"],
                          "text": "the ring has %d nodes, the inventory %d (seen from %s): a node is missing from the ring, "
                                  "or the inventory is incomplete" % (len(nodes), int(expected), view["from"])})
    for port in (ports or {}).get("results", []):
        if port.get("failed"):
            item = port["item"]
            found.append({"kind": "port", "name": item["name"], "port": item["port"], "on": node, "host": item["host"],
                          "text": "%s port %s is not answering on %s (%s)" % (item["name"], item["port"], node, item["host"])})
    if gossip is not None and not gossip.get("is_up"):
        found.append({"kind": "gossip", "on": node, "text": "gossip is not running on %s" % node})
    if binary is not None and not binary.get("is_up"):
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


def cassandra_health_problems(views, expected, node, gossip=None, binary=None, netstats=None, schema=None, ports=None,
                              down_ok=None, joining_ok=None, leaving_ok=None):
    """views: [{'from': host, 'result': cassandra_status result}]; the others
    are this node's registered results (ports: a wait_for loop over
    {'name', 'host', 'port'} items). down_ok: addresses expected down (a dead
    node being replaced). joining_ok: addresses expected up and joining (a
    bootstrap still running). leaving_ok: addresses expected up and leaving (a
    decommission still running). Returns a list of problems."""
    return [f["text"] for f in cassandra_health_findings(
        views, expected, node, gossip=gossip, binary=binary, netstats=netstats, schema=schema, ports=ports,
        down_ok=down_ok, joining_ok=joining_ok, leaving_ok=leaving_ok)]


def cassandra_health_report(findings, cluster, hosts, unreachable=None, absent=None, names=None, members=None,
                            topology_command=""):
    """The health_check report. findings: {host: its
    cassandra_health_findings} for the hosts checked; hosts: every host of
    the run; unreachable: the ones Ansible did not reach (or whose check
    did not run); absent: the hosts marked cassandra_node_state: absent;
    names: {address: inventory name}; members: the ring's size as a node
    saw it; topology_command: the command that removes the absent nodes.
    Each problem once, with every node that saw it. Returns {healthy,
    problems (count), lines}: "HEALTHY  my_cluster  6 nodes  6 UN, schema
    agreed, no streams, ports open" or "NOT HEALTHY  my_cluster  6 nodes
    checked, 2 problems", a line per problem, then a TO DO."""
    names = names or {}
    unreachable = [h for h in hosts if h in (unreachable or [])]
    checked = [h for h in hosts if h not in unreachable]
    rows, seen = [], {}

    def add(label, key, text, who=None):
        if key not in seen:
            seen[key] = [label, text, []]
            rows.append(key)
        if who and who not in seen[key][2]:
            seen[key][2].append(who)

    for host in checked:
        for f in (findings or {}).get(host) or []:
            kind = f.get("kind")
            if kind == "state":
                name = names.get(f["address"])
                add("ring", ("state", f["address"], f["state"]), "%s%s (%s) %s" % (
                    (name + " ") if name else "", f["address"], f["rack"], f["state"]), f["seen_from"])
            elif kind == "count":
                add("ring", ("count", f["ring"], f["expected"]), "%d members, inventory %d" % (f["ring"], f["expected"]),
                    f["seen_from"])
            elif kind == "nodetool":
                add("nodetool", ("nodetool", f["on"]), "status failed on %s: %s" % (f["on"], f["error"]))
            elif kind == "port":
                add("ports", ("port", f["name"], f["port"]), "%s %s not answering on" % (f["name"], f["port"]), f["on"])
            elif kind == "gossip":
                add("gossip", ("gossip",), "not running on", f["on"])
            elif kind == "cql":
                add("CQL", ("cql",), "native transport not running on", f["on"])
            elif kind == "streams":
                add("streams", ("streams",), "in progress on", f["on"])
            elif kind == "netstats":
                add("netstats", ("netstats", f["error"]), "failed (%s) on" % f["error"], f["on"])
            elif kind == "schema":
                add("schema", ("schema", f["msg"]), "disagreement: %s" % f["msg"])
            else:
                add("other", ("other", f.get("text")), f.get("text", ""))
    for host in unreachable:
        add("ssh", ("ssh",), "not reached by Ansible:", host)

    lines = []
    width = max([len(seen[k][0]) for k in rows] or [0]) + 1
    for key in rows:
        label, text, who = seen[key]
        if key[0] in ("state", "count"):
            text += "   seen from %s" % out.nodes(who)
        elif who:
            text += " " + out.nodes(who)
        lines.append("  %s  %s" % ((label + ":").ljust(width), text))
    counted = any(k[0] == "count" for k in rows)
    absent = list(absent or [])
    if absent and counted:
        lines.append("  not counted, marked cassandra_node_state: absent: %s" % out.nodes(absent))
    if not rows:
        size = members if members is not None else len(hosts)
        return {"healthy": True, "problems": 0, "lines": [
            "HEALTHY  %s  %s  %d UN, schema agreed, no streams, ports open" % (cluster, out.plural(len(hosts), "node"),
                                                                             size)]}
    todo = []
    down = [names.get(k[1], k[1]) for k in rows if k[0] == "state" and k[2].startswith("D")]
    if down:
        todo.append("start Cassandra on %s (its server first if it is down); a node that can't be recovered: "
                    "replace_node" % ", ".join(down))
    if absent and counted and topology_command:
        todo.append({"text": "remove %s from the ring" % out.nodes(absent), "command": topology_command})
    if unreachable:
        todo.append("reach %s over SSH, then run this again" % out.nodes(unreachable))
    if any(k[0] == "streams" for k in rows):
        todo.append("wait for the streams to end (nodetool netstats), then run this again")
    head = "NOT HEALTHY  %s  %s checked, %s" % (cluster, out.plural(len(checked), "node"), out.plural(len(rows), "problem"))
    return {"healthy": False, "problems": len(rows), "lines": [head] + lines + out.todo(todo)}


def _ring_node(cluster_status, address):
    return next((n for n in _nodes(cluster_status or {}) if n["address"] == address), None)


def cassandra_leaving_state(netstats, ring, address):
    """netstats: this node's cassandra_netstats result; ring: cassandra_status
    result from a node that stays; address: this node's address in the ring.
    Returns {'state', 'in_ring', 'reason'}. state: 'normal' (not leaving: the
    usual checks decide), 'leaving' (a decommission in progress, to wait for;
    in_ring false once it has announced it left), 'decommissioned' (gone from
    the ring, Cassandra running or not: to stop and disable), 'failed' (an
    operator's job, reason says why)."""
    mode = (netstats or {}).get("mode") or ""
    if not (ring or {}).get("cluster_status"):
        return {"state": "normal", "in_ring": True, "reason": ""}  # the health check reports it
    seen = _ring_node(ring["cluster_status"], address)
    in_ring = seen is not None
    if mode == "LEAVING":
        return {"state": "leaving", "in_ring": in_ring, "reason": ""}
    if mode == "DECOMMISSIONED" and not in_ring:
        return {"state": "decommissioned", "in_ring": False, "reason": ""}
    if mode == "DECOMMISSIONED":
        return {"state": "failed", "in_ring": True, "reason": (
            "Mode DECOMMISSIONED but still %s%s in the ring: wait a minute (the other nodes drop it after about 30s)"
            " and run this again; if it stays, check nodetool status and gossipinfo on the other nodes."
            % (seen["status"], seen["state"]))}
    if mode == "DECOMMISSION_FAILED":
        return {"state": "failed", "in_ring": in_ring, "reason": (
            "its decommission failed (Mode DECOMMISSION_FAILED): see why in its system.log. Once fixed, nodetool"
            " decommission on it resumes it (the ranges already streamed are skipped), or restart Cassandra on it"
            " to cancel it (it comes back UN) and run decommission_node again.")}
    stopped = "Connection refused" in "%s %s" % ((netstats or {}).get("msg") or "", (netstats or {}).get("stderr") or "")
    down = [n for n in _nodes(ring["cluster_status"]) if n["status"] == "D"]
    if not in_ring and not mode and stopped and not down:
        # decommissioned and stopped since: what is left is to keep it stopped. Not
        # when a node is down: it may be this one, known by another address
        return {"state": "decommissioned", "in_ring": False, "reason": (
            "not in the ring and its nodetool does not answer (%s)" % _error(netstats or {}))}
    return {"state": "normal", "in_ring": in_ring, "reason": ""}


def _same_address(field, address):
    """True when a nodetool field ("/10.0.0.4", "host/10.0.0.4", "10.0.0.4:7000",
    "[0:0:0:0:0:0:0:4]:7000") is address."""
    peer = field.strip().split("/")[-1].replace("[", "").replace("]", "")
    return re.fullmatch(re.escape(address) + r"(:\d+)?", peer) is not None


def _gossip_removing(gossip, address):
    """True when nodetool gossipinfo shows the node at address being removed
    (STATUS removing,<host id>). The other nodes' nodetool status shows it DN,
    only the coordinator shows it DL."""
    block, found = [], False
    for line in (gossip or "").splitlines():
        if line and not line[0].isspace():  # "/10.0.0.4", "host/10.0.0.4" or "/10.0.0.4:7000"
            if found:
                break
            found = _same_address(line, address)
        elif found:
            block.append(line)
    return any(re.match(r"\s*STATUS(?:_WITH_PORT)?:\d+:removing,", line) for line in block)


def _ring_tokens(ring_text, address):
    """The tokens nodetool ring lists for address: its rows start with the
    address and end with the token."""
    tokens = []
    for line in (ring_text or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and _same_address(fields[0], address):
            tokens.append(fields[-1])
    return tokens


def _hosts(names):
    return "%s %s" % (", ".join(names), "is" if len(names) == 1 else "are")


def cassandra_removal_state(ring, address, removal_status, gossip="", tokens=""):
    """ring: cassandra_status result; address: the dead node's;
    removal_status: {host: stdout of nodetool removenode status on it} for
    every node of the run ('' when it did not answer). Only the node coordinating a
    removal says "Removing token (<one of the removed node's tokens>)" and shows
    the node DL; the others show it DN, their gossip (gossip: nodetool
    gossipinfo output) says "removing". tokens: nodetool ring output, to tell
    whose removal a coordinator runs.
    Returns {'state', 'on', 'reason'}. state: 'absent' (not in the ring:
    nothing to do), 'resume' (one node, 'on', is removing this node: wait for
    it), 'busy' (a node is removing another node, or one it can't tell,
    several are removing, or other nodes are leaving: reason says which),
    'orphan' (being removed but no node of the run is coordinating it:
    elsewhere, or its coordinator restarted), 'unknown' (no ring) or 'start'."""
    if not (ring or {}).get("cluster_status"):
        return {"state": "unknown", "on": "", "reason": ""}
    nodes = _nodes(ring["cluster_status"])
    dead = next((n for n in nodes if n["address"] == address), None)
    if dead is None:
        return {"state": "absent", "on": "", "reason": ""}
    others_leaving = [n["address"] for n in nodes if n["state"] == "L" and n["address"] != address]
    removing = {}
    for host, out in (removal_status or {}).items():
        match = re.search(r"Removing token \(([^)]*)\)", out or "")
        if match:
            removing[host] = match.group(1).strip()
    dead_tokens = set(_ring_tokens(tokens, address))
    ours = sorted(h for h in removing if removing[h] in dead_tokens)
    theirs = sorted(h for h in removing if h not in ours)
    if len(ours) == 1 and not theirs and not others_leaving:
        return {"state": "resume", "on": ours[0], "reason": ""}
    reason = ""
    if theirs and dead_tokens:
        reason = "%s removing another node (token %s, not one of %s's)" % (
            _hosts(theirs), ", ".join(sorted(set(removing[h] for h in theirs))), address)
    elif theirs:
        reason = "%s removing a node, and nodetool ring did not list %s's tokens to tell which" % (_hosts(theirs), address)
    elif len(ours) > 1:
        reason = "%s all removing %s" % (_hosts(ours), address)
    elif ours:
        reason = "%s removing %s, but other nodes are leaving too (%s)" % (_hosts(ours), address, ", ".join(others_leaving))
    if removing:
        return {"state": "busy", "on": ", ".join(sorted(removing)), "reason": reason}
    if dead["status"] == "D" and (dead["state"] == "L" or _gossip_removing(gossip, address)):
        return {"state": "orphan", "on": "", "reason": ""}
    return {"state": "start", "on": "", "reason": ""}


def cassandra_removal_force_target(rings, address, removal):
    """Where remove_dead_node runs nodetool removenode force. It finishes
    every removal or leave the node it runs on knows of (the nodes it shows
    L), and nothing on a node that does not show the dead node DL (only the
    coordinator of a removal does). rings: {host: cassandra_status result read
    on it}, in the order of the run; removal: cassandra_removal_state's.
    Returns {'on': the host, 'reason': ''} or {'on': '', 'reason': why not}."""
    removal = removal or {}
    if removal.get("state") == "busy":
        return {"on": "", "reason": "%s: removenode force would finish that too. Wait for it (nodetool removenode"
                                    " status), then run this again." % removal.get("reason", "")}
    views = []
    for host, result in (rings or {}).items():
        cluster_status = (result or {}).get("cluster_status")
        if cluster_status:
            dead = _ring_node(cluster_status, address)
            others = [n["address"] for n in _nodes(cluster_status) if n["state"] == "L" and n["address"] != address]
            views.append((host, dead, others))
    if removal.get("state") == "resume":
        candidates = [v for v in views if v[0] == removal["on"]]
        if not candidates:
            return {"on": "", "reason": "%s is removing %s, but its ring could not be read (nodetool status):"
                                        " run this again." % (removal["on"], address)}
    else:
        candidates = [v for v in views if v[1] and v[1]["status"] + v[1]["state"] == "DL"]
    if candidates:
        host, dead, others = candidates[0]
        if others:
            return {"on": "", "reason": "Other nodes are leaving as %s sees them (%s): removenode force there would"
                                        " finish those too. Wait for them, then run this again." % (host, ", ".join(others))}
        return {"on": host, "reason": ""}
    seen = sorted(set(v[1]["status"] + v[1]["state"] for v in views if v[1]))
    if "UL" in seen:
        return {"on": "", "reason": "%s is UL: a live node leaving (decommission), not a removal; nothing forced." % address}
    if removal.get("state") == "orphan":
        return {"on": "", "reason": (
            "%s is being removed (gossip), but no node of this run is removing it or shows it DL, and removenode"
            " force finishes only what the node it runs on knows. If its coordinator is outside this run, run"
            " nodetool removenode force there. If the coordinator restarted, the removal is over: start a new one"
            " with -e cassandra_dead_node_new_removal=true (method removenode)." % address)}
    return {"on": "", "reason": "%s is %s: no removal of it is in progress, run removenode first (the default"
                                " method)." % (address, "/".join(seen) or "not readable in the ring")}


class FilterModule(object):
    def filters(self):
        return {"cassandra_health_problems": cassandra_health_problems,
                "cassandra_health_findings": cassandra_health_findings,
                "cassandra_health_report": cassandra_health_report,
                "cassandra_leaving_state": cassandra_leaving_state,
                "cassandra_removal_state": cassandra_removal_state,
                "cassandra_removal_force_target": cassandra_removal_force_target}
