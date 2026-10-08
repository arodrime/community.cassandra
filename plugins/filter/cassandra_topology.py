# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The plan of the playbook topology: the inventory is the desired state.

cassandra_topology_plan: the hosts of the cluster's group (the ones marked
    cassandra_node_state: absent too) and the ring -> what to add, what to
    remove, in which order, and why not (the refusals).
cassandra_topology_screen: the plan -> the lines of the plan screen.
cassandra_seed_change: the hosts' live seed lists and cassandra_seeds -> the
    seed change to apply (topology, add_node, decommission_node).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import (
    cassandra_decommission_screen, cassandra_reset_warnings)
from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import cassandra_add_node_plan
from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out


def _ring_entries(ring):
    """{address: (dc, entry)} of cassandra_status' cluster_status."""
    out = {}
    for dc, info in sorted((ring or {}).items()):
        for entry in (info or {}).get("nodes") or []:
            out[entry.get("address")] = (dc, entry)
    return out


def _plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _seed_entries(seeds):
    """cassandra_seeds (a list or a comma-separated string) -> its entries."""
    if not seeds:
        return []
    if isinstance(seeds, str):
        seeds = seeds.split(",")
    return [str(s).strip() for s in seeds if str(s).strip()]


def cassandra_seed_change(hosts, seeds):
    """hosts: [{name, absent, dc, rack, names: the names and addresses it
    answers to, live: the seed list of its cassandra.yaml ('' when unknown)}];
    absent: leaving (its own list does not count). seeds: cassandra_seeds,
    the seed list wanted. Returns {new: the line cassandra.yaml gets (as
    change_seeds writes it), old: [the entries the other hosts run with],
    old_hosts: [the hosts in it], removed, added: [labels
    "name (dc/rack)", an entry no host has as is], differ: [hosts whose list
    is not the new one], step: something to apply, problems: a datacenter
    left with no seed, no seed at all}."""
    new_entries = _seed_entries(seeds)
    new_line = seeds if isinstance(seeds, str) else ",".join(new_entries)
    present = [h for h in hosts if not h.get("absent")]
    old = []
    for h in present:
        for entry in _seed_entries(h.get("live")):
            if entry not in old:
                old.append(entry)

    def owner(entry):
        bare = re.sub(r":[0-9]+$", "", entry)  # the port left out, as seed_facts.yml compares
        return next((h for h in hosts if bare in (h.get("names") or []) or bare == h["name"]), None)

    def key(entry):
        h = owner(entry)
        return h["name"] if h else entry

    def label(entry):
        h = owner(entry)
        return "%s (%s/%s)" % (h["name"], h.get("dc") or "?", h.get("rack") or "?") if h else entry

    old_keys = [key(e) for e in old]
    new_keys = [key(e) for e in new_entries]
    removed = [label(e) for e in old if key(e) not in new_keys]
    added = [label(e) for e in new_entries if key(e) not in old_keys]
    differ = [h["name"] for h in present if h.get("live") and h["live"] != new_line]
    problems = []
    if not new_entries:
        problems.append("cassandra_seeds is empty: list a node of each datacenter (one per rack, two or three per"
                        " datacenter).")
    gone = [owner(e) for e in old if key(e) not in new_keys and owner(e)]
    for dc in sorted(set(h.get("dc") for h in gone)):
        left = [h["name"] for h in present if h.get("dc") == dc]
        if left and not [n for n in new_keys if n in left]:
            problems.append("%s would be left with no seed (%s %s the seed list): put a node of %s in cassandra_seeds."
                            % (dc, ", ".join(h["name"] for h in gone if h.get("dc") == dc),
                               "leaves" if len([h for h in gone if h.get("dc") == dc]) == 1 else "leave", dc))
    return {"new": new_line, "old": old, "old_hosts": [k for k in old_keys if k in [h["name"] for h in hosts]],
            "removed": removed, "added": added, "differ": differ,
            "step": bool(removed or added or differ), "problems": problems}


def cassandra_topology_plan(hosts, ring=None, token_auto="false", seeds=None):
    """hosts: every host of the cluster's group in the inventory's order:
    [{name, absent, address (the one the ring shows, '' when unknown), dc, rack,
      seed, reachable, running, state, refused, single, token, reset, names,
      live}]. seed: in cassandra_seeds; names, live: see cassandra_seed_change. single:
    one token per node; token: its cassandra_initial_token; reset: its reset
    plan (cassandra_add_node_reset), for the screen. state: for a host to
    add, new_node_state.yml's (new, joining, joined); for a host marked absent,
    leaving_node_state.yml's (normal, leaving, decommissioned); refused: why
    a check refused the host. ring: cassandra_status' cluster_status.
    token_auto: cassandra_token_auto (one token per node); seeds:
    cassandra_seeds. No cap on the number of removals: the guards are the
    replicas each datacenter keeps (checked by the playbook) and its seeds;
    more than half of a datacenter removed is a warning (large_removals).
    Returns {add, remove: [names] in the order of the run (adds first; a
    bootstrap or decommission an earlier run started first of its kind),
    seeds: cassandra_seed_change's (applied between the adds and the removals),
    finish: the hosts to remove only stopped and disabled (out of the ring
    already), gone: hosts marked absent already out of the ring and stopped,
    silent: hosts marked absent out of the ring that do not answer, unknown:
    [ring entries no host has], down: present hosts down in the ring,
    in_ring: the hosts to remove still in the ring, nodes_left: {dc: nodes
    once done}, large_removals: [a line per datacenter losing more than half
    of its nodes], problems, warnings}."""
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
        else:  # in cassandra_seeds too: it joins with the others as its seeds, and is made one once up
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
                "%s is marked absent but is still in cassandra_seeds: take it out of cassandra_seeds in the"
                " inventory; topology then applies the new seed list on the other nodes before it removes it."
                % name)
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

    # more than half of a datacenter removed: said on the screen (the replicas it keeps are the guard)
    by_dc, large_removals = {}, []
    for name in in_ring:
        h = next(x for x in absent if x["name"] == name)
        by_dc.setdefault(entries[h["address"]][0], []).append(name)
    for dc, names in sorted(by_dc.items()):
        # the datacenter with the nodes this run adds first
        size = len([e for e in entries.values() if e[0] == dc]) + len([h for h in present if h["name"] in new and h.get("dc") == dc])
        if len(names) == size:
            large_removals.append("%s removed whole (%s): no node left there" % (dc, out.nodes(names)))
        elif len(names) * 2 > size:
            large_removals.append("%d of %s of %s removed (%s): %s left to hold their data"
                                  % (len(names), _plural(size, "node"), dc, out.nodes(names),
                                     _plural(size - len(names), "node")))

    # the seeds: cassandra_seeds applied live on every node once the adds are done, before the removals
    # a host to add may have a cassandra.yaml of its own (a package's): not a list the cluster runs with
    seed_hosts = [dict(h, live="") if h["name"] in new else h for h in hosts]
    seed_change = cassandra_seed_change(seed_hosts, seeds) if seeds is not None else {"step": False, "problems": []}
    if seed_change["step"]:
        problems.extend(seed_change["problems"])

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
    return {"add": add, "remove": remove, "seeds": seed_change, "finish": finish, "gone": gone, "silent": silent, "unknown": unknown, "down": down,
            "in_ring": in_ring, "joining": joining, "leaving": leaving, "nodes_left": nodes_left,
            "large_removals": large_removals, "problems": problems, "warnings": warnings}


def cassandra_topology_steps(plan, hosts):
    """The steps of the plan in the order of the run, for module_utils
    cassandra_output.plan: [{node: "add node5", dc, rack, text}] (no text:
    see cassandra_topology_screen), the seed step {node: "seeds", text}."""
    by_name = dict((h["name"], h) for h in hosts)
    steps = []
    for name in plan["add"]:
        h = by_name.get(name) or {}
        steps.append({"node": "add " + name, "dc": h.get("dc") or "?", "rack": h.get("rack"), "text": ""})
    seeds = plan.get("seeds") or {}
    if seeds.get("step"):
        steps.append({"node": "seeds", "text": "%s -> %s" % (",".join(seeds.get("old") or []) or "(none)", seeds.get("new"))})
    for name in plan["remove"]:
        h = by_name.get(name) or {}
        steps.append({"node": "decommission " + name, "dc": h.get("dc") or "?", "rack": h.get("rack"), "text": ""})
    return steps


def cassandra_topology_screen(plan, hosts, ring=None, keyspaces=None, replication_problems=None, force=False,
                              names=None, cluster="", version="", check=False, session="", confirm=True, notes=None):
    """The plan screen (OUTPUT_UX Q5): "PLAN  topology  cluster (Cassandra x)
    N steps, one node at a time", the steps in order, the facts (each
    datacenter once done, what is left to do by hand), the WARNING lines,
    then the question line (none when confirm: confirm.yml's prompt comes
    right after; --check: nothing will be changed). Returns the lines.
    plan: cassandra_topology_plan's; hosts: its hosts; ring: cluster_status;
    keyspaces: cassandra_keyspaces (None: unknown); replication_problems: the
    datacenters left with fewer nodes than replicas (shown when force:
    cassandra_decommission_force); names: {address: host} of the ring's
    nodes; session: the tmux/screen warning (a real run only); notes: the
    lines the checks kept for the plan (note.yml, "WARNING  ..." or a note).
    The checks of the hosts to add (hosts' info, checks: new_node_checks.yml)
    are NOTE and WARNING lines too, one per text, with the hosts it is about.
    Every note is on one line, said once."""
    by_name = dict((h["name"], h) for h in hosts)
    ring = ring or {}
    steps = cassandra_topology_steps(plan, hosts)
    seeds = plan.get("seeds") or {}

    adding = [{"host": h["name"], "address": h.get("address"), "dc": h.get("dc"), "rack": h.get("rack"),
               "in_ring": h["name"] in plan["joining"], "state": "joining" if h["name"] in plan["joining"] else "new"}
              for h in hosts if h["name"] in plan["add"]]
    estimate = {}
    if adding:  # streamed while the nodes to remove are still there
        add_plan = cassandra_add_node_plan(ring, adding, hosts=names or {}, keyspaces=keyspaces)
        estimate = dict((e["host"], e) for e in add_plan["estimate"])
    for step, name in zip(steps, plan["add"]):
        h = by_name[name]
        if name in plan["joining"]:
            parts = ["still bootstrapping (UJ, an earlier run): waited for"]
        else:
            e = estimate.get(name)
            parts = ["bootstrap, ~%s to stream (%s)" % (out.size(e["bytes"]), e["basis"]) if e else "bootstrap"]
            if h.get("single"):
                parts.append("token %s" % (h["token"] if h.get("token") else "from cassandra_token_auto"))
            if h.get("reset"):
                parts.append("reset first (cassandra_add_node_reset)")
            if seeds.get("step") and name in [a.split(" ")[0] for a in seeds.get("added") or []]:
                parts.append("joins as a regular node, a seed at the seed step")
        step["text"] = "; ".join(parts)
    if seeds.get("step"):
        seed_step = steps[len(plan["add"])]
        seed_step["text"] += "  written and reloaded live on every node, no restart"

    if plan["remove"]:
        # where the data goes, as the ring is once the adds are done
        states = [{"name": n, "state": "decommissioned" if n in plan["finish"] else
                   "leaving" if n in plan["leaving"] else "normal"} for n in plan["remove"]]
        nodes = [{"name": h["name"], "address": h.get("address"), "dc": h.get("dc"), "rack": h.get("rack"),
                  "seed": h["name"] in seeds.get("old_hosts", [])} for h in hosts]
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
        for step, block in zip(steps[len(steps) - len(plan["remove"]):], decommission["blocks"]):
            step["text"] = block["step"]["text"]

    # the facts: each datacenter once done, then what is left to do by hand
    facts = []
    touched = sorted(set(by_name[n].get("dc") for n in plan["add"] + plan["remove"]))
    rack_warnings = []
    for dc in touched:
        left = [h["name"] for h in hosts if not h.get("absent") and h.get("dc") == dc]
        racks = {}
        for name in left:
            racks[by_name[name].get("rack")] = racks.get(by_name[name].get("rack"), 0) + 1
        # more than 5: nodes() counts them itself
        text = (out.nodes(left) if len(left) > 5 else "%s: %s" % (out.plural(len(left), "node"), out.nodes(left))) \
            if left else "no node left"
        if len(racks) > 1:
            text += "   " + "  ".join("%s %d" % (r, c) for r, c in sorted(racks.items()))
        rfs = sorted(((v.get("rf") or {}).get(dc, 0), k) for k, v in (keyspaces or {}).items()
                     if (v.get("rf") or {}).get(dc))
        if left and rfs:
            text += "   highest RF %d (%s)" % (rfs[-1][0], rfs[-1][1])
        facts.append(["%s after" % dc, text])
        if len(set(racks.values())) > 1:
            rack_warnings.append(
                "%s once done: %s nodes per rack: with as many replicas as racks, a node of a smaller rack holds a"
                " bigger share (1/%d against 1/%d)" % (
                    dc, ", ".join("%s %d" % (r, c) for r, c in sorted(racks.items())), min(racks.values()),
                    max(racks.values())))
    if seeds.get("step") and not (seeds.get("removed") or seeds.get("added")):
        facts.append(["seeds", "written again on %s, the same seeds: %s" % (out.nodes(seeds.get("differ")), seeds["new"])])
    if plan["gone"]:
        facts.append(["already removed", "%s (marked absent, out of the ring, Cassandra stopped)" % out.nodes(plan["gone"])])
    if plan["silent"]:
        facts.append(["not answering", "%s (marked absent, out of the ring): nothing to do" % out.nodes(plan["silent"])])
    done = plan["remove"] + plan["gone"] + plan["silent"]
    if done:
        facts.append(["then", "delete %s from the inventory (or leave %s marked absent); wipe %s data directories"
                              " before reusing the host%s" % (out.nodes(done, keep_order=True), "it" if len(done) == 1 else "them",
                                                              "its" if len(done) == 1 else "their",
                                                              "" if len(done) == 1 else "s")])

    def grouped(key):
        """[(text, hosts)] of the hosts to add, in order, each text once."""
        found = []
        for h in hosts:
            if h["name"] in plan["add"]:
                for text in h.get(key) or []:
                    same = [f for f in found if f[0] == text]
                    if same:
                        same[0][1].append(h["name"])
                    else:
                        found.append((text, [h["name"]]))
        return found

    if plan["add"]:
        facts.append(["cleanup", "of the nodes that hand data over: its command is printed after the adds (topology"
                                 " runs none, the removals move data again)"])

    for text, names_of in grouped("info"):
        facts.append("NOTE  %s: %s" % (out.nodes(names_of), text))
    kept = [str(n).strip() for n in notes or [] if str(n).strip()]
    facts.extend("NOTE  " + n for n in kept if not n.startswith("WARNING"))

    # the warnings, just above the question
    warnings = [n[len("WARNING"):].strip() for n in kept if n.startswith("WARNING")]
    warnings.extend("%s: %s" % (out.nodes(names_of), text) for text, names_of in grouped("checks"))
    if seeds.get("removed") or seeds.get("added"):  # a seed change topology applies on its own
        warnings.append("the seeds will change on every node: %s -> %s (from cassandra_seeds in the inventory)"
                        % (",".join(seeds["old"]) or "(none)", seeds["new"]))
    warnings.extend(plan.get("large_removals") or [])
    warnings.extend(rack_warnings)
    warnings.extend("%s is in the ring but in no host of the inventory: never touched (an address mistyped, a host"
                    " missing, or a dead node: remove_dead_node -e cassandra_dead_node_address=%s)" % (u, u.split(" ")[0])
                    for u in plan["unknown"])
    warnings.extend(w.rstrip(".") for w in plan["warnings"])
    if force and replication_problems:
        warnings.append("cassandra_decommission_force is true: the removal goes on although too few nodes are left,"
                        " those replicas are lost and QUORUM can fail: %s" % "; ".join(replication_problems))
    real_run = []
    resets = [{"name": h["name"], "plan": h["reset"]} for h in hosts if h["name"] in plan["add"] and h.get("reset")]
    for reset in cassandra_reset_warnings(resets):
        text = reset["text"]
        first, rest = (text[0], text[1:]) if isinstance(text, list) else (text, [])
        dirs = [line for item in rest for line in (item.get("pre") or [] if isinstance(item, dict) else [item])]
        real_run.append((reset["label"], "\n".join(["%s: %s" % (reset["label"], first)] + ["  " + d for d in dirs])))
    if str(session or "").strip():
        real_run.append(("not inside tmux or screen", str(session).strip()))
    if check:
        if real_run:
            facts.append(["real run", "would also warn about: %s" % ", ".join(sorted(set(r[0] for r in real_run)))])
    else:
        warnings.extend(r[1] for r in real_run)

    said = []
    for w in warnings:  # each once
        if w not in said:
            said.append(w)
    warnings = said
    question = "" if confirm else "cassandra_operation_confirm is false: no question, the run goes on."
    lines = out.plan("topology", cluster=cluster, version=version,
                     summary="%s, one node at a time" % out.plural(len(steps), "step"),
                     steps=steps, facts=facts, warnings=warnings, question=question, check=check)
    # a warning on several lines: the next ones under its text
    return [part if i == 0 else " " * len("WARNING  ") + part
            for line in lines for i, part in enumerate(str(line).split("\n"))]


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_topology_plan": cassandra_topology_plan,
            "cassandra_topology_screen": cassandra_topology_screen,
            "cassandra_seed_change": cassandra_seed_change,
        }
