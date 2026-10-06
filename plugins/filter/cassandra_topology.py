# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The plan of the playbook topology: the inventory is the desired state.

cassandra_topology_plan: the hosts of the cluster's group (the ones marked
    cassandra_node_state: absent too) and the ring -> what to add, what to
    remove, in which order, and why not (the refusals).
cassandra_topology_screen: the plan -> the screen (filter cassandra_screen).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import (
    cassandra_decommission_screen, cassandra_reset_warnings, cassandra_screen_title)
from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import cassandra_add_node_plan


def _ring_entries(ring):
    """{address: (dc, entry)} of cassandra_status' cluster_status."""
    out = {}
    for dc, info in sorted((ring or {}).items()):
        for entry in (info or {}).get("nodes") or []:
            out[entry.get("address")] = (dc, entry)
    return out


def _plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def cassandra_topology_plan(hosts, ring=None, max_removals=2, allow_large=False, token_auto="false"):
    """hosts: every host of the cluster's group in the inventory's order:
    [{name, absent, address (the one the ring shows, '' when unknown), dc, rack,
      seed, reachable, running, state, refused, single, token, reset}]. single:
    one token per node; token: its cassandra_initial_token; reset: its reset
    plan (cassandra_add_node_reset), for the screen. state: for a host to
    add, new_node_state.yml's (new, joining, joined); for a host marked absent,
    leaving_node_state.yml's (normal, leaving, decommissioned); refused: why
    a check refused the host. ring: cassandra_status' cluster_status.
    max_removals, allow_large: the guard (cassandra_topology_max_removals,
    cassandra_topology_allow_large_removal); token_auto: cassandra_token_auto
    (one token per node).
    Returns {add, remove: [names] in the order of the run (adds first; a
    bootstrap or decommission an earlier run started first of its kind),
    finish: the hosts to remove only stopped and disabled (out of the ring
    already), gone: hosts marked absent already out of the ring and stopped,
    silent: hosts marked absent out of the ring that do not answer, unknown:
    [ring entries no host has], down: present hosts down in the ring,
    in_ring: the hosts to remove still in the ring, nodes_left: {dc: nodes
    once done}, problems, warnings}."""
    entries = _ring_entries(ring)
    present = [h for h in hosts if not h.get("absent")]
    absent = [h for h in hosts if h.get("absent")]
    known = set(h.get("address") for h in hosts if h.get("address"))
    problems, warnings = [], []
    joining, new, leaving, normal, finish, gone, silent, down = [], [], [], [], [], [], [], []

    for h in present:
        name, address = h["name"], h.get("address") or ""
        entry = entries.get(address, (None, None))[1]
        if h.get("refused"):
            problems.append("%s: %s" % (name, h["refused"]))
        elif h.get("state") == "joining" or (entry and entry["status"] + entry["state"] == "UJ"):
            joining.append(name)
        elif h.get("state") == "joined" or entry:
            if entry and entry["status"] == "D":
                down.append(name)
        elif h.get("seed"):
            problems.append("%s: in cassandra_seeds, and not in the ring yet: seeds don't bootstrap, it would join"
                            " without its data. Add it as a regular node, then make it a seed with change_seeds." % name)
        else:
            new.append(name)

    for h in absent:
        name, address = h["name"], h.get("address") or ""
        dc_entry = entries.get(address)
        entry = dc_entry[1] if dc_entry else None
        where = ("%s, %s%s" % (dc_entry[0], entry["status"], entry["state"])) if entry else ""
        if not h.get("reachable"):
            if entry:
                problems.append(
                    "%s (%s) is marked absent and still in the ring (%s) but does not answer: bring it back and run"
                    " topology again (it is then decommissioned), or, if it is dead for good, remove_dead_node"
                    " -e cassandra_dead_node_address=%s" % (name, address, where, address))
            elif not address and any(a not in known for a in entries):
                problems.append(
                    "%s is marked absent, does not answer and the inventory gives no address for it, while the ring has"
                    " nodes the inventory does not know: it may be one of them. Give its address"
                    " (cassandra_listen_address or ansible_host), or bring it back." % name)
            else:
                silent.append(name)
        elif h.get("refused"):
            problems.append("%s: %s" % (name, h["refused"]))
        elif entry and entry["status"] == "D":
            problems.append(
                "%s (%s) is marked absent and down in the ring (%s): a dead node is not decommissioned. Start it and"
                " run topology again, or remove it with remove_dead_node -e cassandra_dead_node_address=%s"
                % (name, address, where, address))
        elif entry and h.get("seed"):
            problems.append(
                "%s is marked absent but is in cassandra_seeds: take it out of the seeds and apply it with"
                " change_seeds first, or the other nodes keep contacting a node that is gone." % name)
        elif entry and entry["state"] in ("J", "M"):
            problems.append("%s (%s) is marked absent and %s in the ring (%s): Cassandra decommissions a node up and"
                            " normal only. Run topology again once it is UN." % (
                                name, address, "joining" if entry["state"] == "J" else "moving", where))
        elif entry:
            (leaving if h.get("state") == "leaving" or entry["state"] == "L" else normal).append(name)
        elif h.get("state") in ("decommissioned", "leaving"):
            finish.append(name)  # out of the ring, Cassandra still running: stopped and disabled
        elif h.get("running"):
            problems.append(
                "%s is marked absent and runs Cassandra, but is not in the ring (another cluster's node, or one"
                " started alone?): topology leaves it as it is. Stop it by hand, or check its address (%s)."
                % (name, address or "unknown"))
        else:
            gone.append(name)

    # one address, two hosts: the plan can't tell which one the ring means
    by_address = {}
    for h in hosts:
        if h.get("address"):
            by_address.setdefault(h["address"], []).append(h["name"] + (" (marked absent)" if h.get("absent") else ""))
    for address, names in sorted(by_address.items()):
        if len(names) > 1:
            problems.append("%s have the same address %s: fix the inventory (an address reused?) before adding or"
                            " removing any of them." % (" and ".join(names), address))

    add = joining + new
    remove = leaving + finish + normal
    in_ring = leaving + normal
    unknown = ["%s (%s / %s, %s%s)" % (address, dc, entry.get("rack") or "?", entry["status"], entry["state"])
               for address, (dc, entry) in sorted(entries.items()) if address not in known]

    if leaving and (new or joining):
        problems.append("%s still leaving (a decommission an earlier run started): Cassandra adds no node while"
                        " another one leaves. Run topology again once it is out of the ring (or follow it with"
                        " decommission_node -e cassandra_leaving_nodes=%s)." % (", ".join(leaving), ",".join(leaving)))
    if unknown and (add or remove):
        problems.append("the ring has nodes no host of the inventory has: %s. Each step checks the ring has the"
                        " inventory's nodes, so the run would stop at the first one: fix the inventory first (an"
                        " address mistyped, a host missing), or remove a dead node with remove_dead_node."
                        % ", ".join(unknown))

    # the guard: removing many nodes at once is rarely meant
    if not allow_large:
        if len(in_ring) > int(max_removals):
            problems.append("the plan removes %d nodes (%s), more than cassandra_topology_max_removals (%d): check"
                            " the inventory (a group var marking hosts absent?). Raise it, or set"
                            " cassandra_topology_allow_large_removal: true, if it is meant."
                            % (len(in_ring), ", ".join(in_ring), int(max_removals)))
        by_dc = {}
        for name in in_ring:
            h = next(x for x in absent if x["name"] == name)
            by_dc.setdefault(entries[h["address"]][0], []).append(name)
        for dc, names in sorted(by_dc.items()):
            # the datacenter with the nodes this run adds first
            size = len([e for e in entries.values() if e[0] == dc]) + len([h for h in present if h["name"] in new and h.get("dc") == dc])
            if len(names) * 2 > size:
                problems.append("the plan removes %d of the %d nodes of %s (%s): more than half of the datacenter."
                                " A datacenter goes with remove_datacenter; else set"
                                " cassandra_topology_allow_large_removal: true." % (len(names), size, dc, ", ".join(names)))

    # one token per node: add_node needs a token per new node, or cassandra_token_auto
    auto = str(token_auto).strip().lower()
    no_token = [h["name"] for h in present if h["name"] in new and h.get("single") and not h.get("token")]
    if new and in_ring and any(h.get("single") for h in present):
        problems.append("one token per node: adding %s and removing %s in one run would place the new tokens in a ring"
                        " that changes right after. Mark the hosts absent once the adds are done (topology again), then"
                        " even out the ring with move_node." % (", ".join(new), ", ".join(in_ring)))
    if auto == "true" and new and any(h.get("single") for h in present):
        problems.append("cassandra_token_auto=true asks a second question (bisect or balanced): choose one on the"
                        " command line instead (-e cassandra_token_auto=bisect or balanced).")
    elif auto == "false" and no_token:
        problems.append("one token per node: %s %s no cassandra_initial_token. Set it, or -e cassandra_token_auto=bisect"
                        " (no node moves) or balanced (an even ring)." % (", ".join(no_token), "has" if len(no_token) == 1 else "have"))

    nodes_left = {}
    for h in present:
        nodes_left[h.get("dc")] = nodes_left.get(h.get("dc"), 0) + 1
    for h in absent:
        if h["name"] in in_ring:
            nodes_left.setdefault(h.get("dc"), 0)

    if down:
        warnings.append("%s down in the ring: the check before the first step stops the run unless it is back up."
                        % ", ".join(down))
    return {"add": add, "remove": remove, "finish": finish, "gone": gone, "silent": silent, "unknown": unknown, "down": down,
            "in_ring": in_ring, "joining": joining, "leaving": leaving, "nodes_left": nodes_left,
            "problems": problems, "warnings": warnings}


def cassandra_topology_screen(plan, hosts, ring=None, keyspaces=None, replication_problems=None, force=False,
                              names=None):
    """plan: cassandra_topology_plan's; hosts: its hosts; ring: cluster_status;
    keyspaces: cassandra_keyspaces (None: unknown); replication_problems: the
    datacenters left with fewer nodes than replicas (shown when force:
    cassandra_decommission_force); names: {address: host} of the ring's nodes.
    Returns the spec of
    filter cassandra_screen (operation topology)."""
    by_name = dict((h["name"], h) for h in hosts)
    ring = ring or {}
    blocks, steps = [], 0

    adding = [{"host": h["name"], "address": h.get("address"), "dc": h.get("dc"), "rack": h.get("rack"),
               "in_ring": h["name"] in plan["joining"], "state": "joining" if h["name"] in plan["joining"] else "new"}
              for h in hosts if h["name"] in plan["add"]]
    estimate = {}
    if adding:  # streamed while the nodes to remove are still there
        add_plan = cassandra_add_node_plan(ring, adding, hosts=names or {}, keyspaces=keyspaces)
        estimate = dict((e["host"], e) for e in add_plan["estimate"])
    # the racks once done: with as many replicas as racks, a smaller rack's nodes hold more
    rack_warnings = []
    for dc in sorted(set(by_name[n].get("dc") for n in plan["add"] + plan["remove"])):
        racks = {}
        for h in hosts:
            if not h.get("absent") and h.get("dc") == dc:
                racks[h.get("rack")] = racks.get(h.get("rack"), 0) + 1
        if len(set(racks.values())) > 1:
            rack_warnings.append(
                "%s once done: %s nodes per rack. With NetworkTopologyStrategy and as many replicas as racks, each rack"
                " holds a full copy of the data: the nodes of a smaller rack each hold a bigger share (e.g. 1/%d against"
                " 1/%d), so they carry more data and load." % (
                    dc, ", ".join("%s %d" % (r, c) for r, c in sorted(racks.items())), min(racks.values()), max(racks.values())))
    for name in plan["add"]:
        h = by_name[name]
        steps += 1
        lines = []
        if name in plan["joining"]:
            lines.append("still bootstrapping (UJ, an earlier run): waited for")
        else:
            e = estimate.get(name)
            lines.append("bootstraps (add_node): streams its share of %s%s" % (
                h.get("dc"), (", about %.1f GiB (%s)" % (e["bytes"] / 1073741824.0, e["basis"])) if e else ""))
            if h.get("single"):
                lines.append("token %s" % (h["token"] if h.get("token") else "worked out by add_node (cassandra_token_auto)"))
            if h.get("reset"):
                lines.append("reset first (cassandra_add_node_reset): see the data loss warning")
        lines.append("end state: up and normal (UN) in %s / %s" % (h.get("dc"), h.get("rack")))
        where = {"address": h.get("address"), "cassandra_dc": h.get("dc"), "cassandra_rack": h.get("rack")}
        title = "%d. add %s" % (steps, cassandra_screen_title(name, where))
        blocks.append({"title": title, "lines": lines})

    if plan["remove"]:
        # where the data goes: the screen of decommission_node, as the ring is once the adds are done
        states = [{"name": n, "state": "decommissioned" if n in plan["finish"] else
                   "leaving" if n in plan["leaving"] else "normal"} for n in plan["remove"]]
        nodes = [{"name": h["name"], "address": h.get("address"), "dc": h.get("dc"), "rack": h.get("rack"),
                  "seed": h.get("seed")} for h in hosts]
        ring_after = {}
        for dc, info in ring.items():
            ring_after[dc] = {"nodes": list((info or {}).get("nodes") or [])}
        for a in adding:
            if not a["in_ring"]:
                ring_after.setdefault(a["dc"], {"nodes": []})["nodes"].append(
                    {"address": a["address"], "rack": a["rack"], "status": "U", "state": "N"})
        peers = [h["name"] for h in hosts if not h.get("absent")]
        decommission = cassandra_decommission_screen(states, nodes, ring=ring_after, keyspaces=keyspaces,
                                                     peer=peers[0] if peers else "")
        for block in decommission["blocks"]:
            steps += 1
            blocks.append({"title": "%d. remove %s" % (steps, block["title"]), "lines": block["lines"]})

    what = []
    if plan["add"]:
        what.append("add " + ", ".join(plan["add"]))
    if plan["remove"]:
        what.append("remove " + ", ".join(plan["remove"]))
    intro = ["The inventory is the desired state: the hosts of the cluster's group not in the ring are added, the"
             " hosts marked cassandra_node_state: absent still in it are removed. One node at a time, adds first,"
             " the cluster checked before and after each one; the run stops at the first problem. Each step shows"
             " its own screen as it starts (no other question)."]
    after = []
    if plan["gone"]:
        after.append("Already removed (marked absent, out of the ring, Cassandra stopped): %s." % ", ".join(plan["gone"]))
    if plan["silent"]:
        after.append("Marked absent, out of the ring, not answering (nothing to do): %s." % ", ".join(plan["silent"]))
    sizes = {}
    for h in hosts:
        if not h.get("absent"):
            sizes.setdefault(h.get("dc"), []).append(h["name"])
    for dc in sorted(set(by_name[n].get("dc") for n in plan["add"] + plan["remove"])):
        left = sizes.get(dc, [])
        after.append("Afterwards %s has %s: %s." % (dc, _plural(len(left), "node"), ", ".join(left)) if left
                     else "Afterwards %s has no node left." % dc)
    done = plan["remove"] + plan["gone"] + plan["silent"]
    if done:
        after.append("Then delete %s from the inventory (or leave them marked absent: the playbooks leave them out)"
                     "; wipe their data directories before reusing the hosts." % ", ".join(done))
    if plan["add"]:
        after.append("The nodes that hand data over to the new ones keep it until a cleanup: its command is printed"
                     " after the adds (topology runs none: the removals move data again).")

    warnings = [{"label": "racks", "each": rack_warnings},
                {"label": "unknown", "each": ["%s is in the ring but no host of the inventory has this address: never"
                                              " touched. A typo in an address, a host missing from the inventory, or"
                                              " a dead node (remove_dead_node -e cassandra_dead_node_address=%s)."
                                              % (u, u.split(" ")[0]) for u in plan["unknown"]]},
                {"label": "down", "each": plan["warnings"]}]
    resets = [{"name": h["name"], "plan": h["reset"]} for h in hosts if h["name"] in plan["add"] and h.get("reset")]
    warnings.extend(cassandra_reset_warnings(resets))
    if force and replication_problems:
        warnings.append({"label": "replication",
                         "text": ["cassandra_decommission_force is true: the removal goes on although too few nodes are"
                                  " left for some keyspaces; those replicas are lost and QUORUM can fail:"]
                         + list(replication_problems)})
    return {"operation": "topology", "summary": "; ".join(what) or "nothing to do", "intro": intro, "blocks": blocks,
            "after": after, "warnings": warnings}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_topology_plan": cassandra_topology_plan,
            "cassandra_topology_screen": cassandra_topology_screen,
        }
