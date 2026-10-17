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
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def _ring_entries(ring):
    """{address: (dc, entry)} of cassandra_status' cluster_status."""
    out = {}
    for dc, info in sorted((ring or {}).items()):
        for entry in (info or {}).get("nodes") or []:
            out[entry.get("address")] = (dc, entry)
    return out


def _seed_entries(seeds):
    """cassandra_seeds (a list or a comma-separated string) -> its entries."""
    if not seeds:
        return []
    if isinstance(seeds, str):
        seeds = seeds.split(",")
    return [str(s).strip() for s in seeds if str(s).strip()]


def _auto_entry(h, wanted, hosts):
    """The seed entry of host h, written as the entries of cassandra_seeds are (wanted): its inventory name
    when they are all inventory names, else its address (a name the nodes may not resolve otherwise); their
    port when they all have one."""
    bare = [re.sub(r":[0-9]+$", "", e) for e in wanted]
    names = [x["name"] for x in hosts]
    by_name = bool(wanted) and all(b in names for b in bare)
    entry = h["name"] if by_name else (h.get("entry") or h.get("address") or h["name"])
    ports = [re.search(r":([0-9]+)$", e) for e in wanted]
    if wanted and all(ports) and len(set(m.group(1) for m in ports)) == 1 and not re.search(r":[0-9]+$", entry):
        entry = "%s:%s" % (entry, ports[0].group(1))
    return entry


def _seed_owner(entry, hosts):
    """A seed entry, the hosts -> the host it names (by a name or an address it answers to), None if none."""
    bare = re.sub(r":[0-9]+$", "", entry)  # the port left out, as seed_facts.yml compares
    return next((h for h in hosts if bare in (h.get("names") or []) or bare == h["name"]), None)


def _seed_key(entry, hosts):
    """A seed entry -> the name of the host it names, else the entry itself."""
    h = _seed_owner(entry, hosts)
    return h["name"] if h else entry


def _seed_label(entry, hosts):
    """A seed entry -> "name (dc/rack)" of the host it names, else the entry itself."""
    h = _seed_owner(entry, hosts)
    return "%s (%s/%s)" % (h["name"], h.get("dc") or "?", h.get("rack") or "?") if h else entry


def _live_seeds(present):
    """The hosts staying -> the entries of the seed lists they run with, each once, in order."""
    old = []
    for h in present:
        for entry in _seed_entries(h.get("live")):
            if entry not in old:
                old.append(entry)
    return old


def _seeds_gone(old, wanted, hosts):
    """The live seed entries, cassandra_seeds' entries, the hosts -> the hosts of the live seeds cassandra_seeds
    leaves out."""
    return [_seed_owner(e, hosts) for e in old
            if _seed_key(e, hosts) not in [_seed_key(w, hosts) for w in wanted] and _seed_owner(e, hosts)]


def _no_seed(dc, names):
    """A datacenter, the seeds leaving it -> the words of it left with no seed."""
    return "%s would be left with no seed (%s %s the seed list)" % (
        dc, ", ".join(names), "leaves" if len(names) == 1 else "leave")


def _pick_dc_seeds(members, old_keys):
    """The hosts of a datacenter staying, the hosts of the live seed lists -> the seeds the seed rule picks among
    the ones up (the ones it runs with as seeds first: a rerun picks the same), in the inventory's order."""
    racks = {}
    for h in members:
        if h.get("up"):
            racks.setdefault(h.get("rack"), []).append(dict(h, seed=h["name"] in old_keys))
    picked = out.pick_seeds(racks, out.seed_target(len(members), len(set(h.get("rack") for h in members))))
    order = [h["name"] for h in members]
    picked.sort(key=lambda h: order.index(h["name"]))  # in the inventory's order: a rerun writes the same line
    return picked


def _auto_seed_warning(dc, leaving, picked, labels, old_keys):
    """A datacenter given seeds of its own, the seeds leaving it, the hosts picked and their labels, the hosts of
    the live seed lists -> the warning: the seeds kept or made there, live only."""
    names = [h["name"] for h in picked]
    dropped = [n for n in leaving if n not in names]
    if dropped:
        why = _no_seed(dc, dropped)
    else:  # a rerun: the seeds picked before, still live
        why = "cassandra_seeds has no node of %s" % dc
    kept = [lab for h, lab in zip(picked, labels) if h["name"] in old_keys]
    made = [lab for h, lab in zip(picked, labels) if h["name"] not in old_keys]
    what = []
    if kept:
        what.append("%s kept as its seed%s" % (", ".join(kept), "" if len(kept) == 1 else "s"))
    if made:
        what.append("%s made its seed%s" % (", ".join(made), "" if len(made) == 1 else "s"))
    return ("%s: %s, live only: put %s in cassandra_seeds (apply_config would put the inventory's list"
            " back)" % (why, "; ".join(what), "it" if len(picked) == 1 else "them"))


def _dc_seeds(dc, members, leaving, wanted, hosts, old_keys):
    """A datacenter left with no seed, its hosts staying, the seeds leaving it, cassandra_seeds' entries, the
    hosts, the hosts of the live seed lists -> ({dc, hosts, labels, entries} of the seeds picked there, the
    warning), or (None, the problem) when none of its nodes is up to be one."""
    picked = _pick_dc_seeds(members, old_keys)
    if not picked:
        return None, ("%s and none of its nodes is up to be one: put a node of %s in cassandra_seeds."
                      % (_no_seed(dc, leaving), dc))
    entries = [_auto_entry(h, wanted, hosts) for h in picked]
    labels = ["%s (%s/%s)" % (h["name"], dc, h.get("rack") or "?") for h in picked]
    return ({"dc": dc, "hosts": [h["name"] for h in picked], "labels": labels, "entries": entries},
            _auto_seed_warning(dc, leaving, picked, labels, old_keys))


def cassandra_seed_change(hosts, seeds):
    """hosts: [{name, absent, dc, rack, names: the names and addresses it
    answers to, live: the seed list of its cassandra.yaml ('' when unknown),
    up: up and normal in the ring (a seed it may get), address (or entry: its
    seed entry)}]; absent: leaving (its own list does not count). seeds:
    cassandra_seeds, the seed list wanted. A datacenter that keeps nodes but
    would be left with no seed gets seeds of its own, picked by the seed rule
    (2 or 3 a datacenter, on different racks; the nodes up, the ones it runs
    with as seeds first): applied live, not in the inventory (auto, warnings);
    with no node up there it is a problem. Returns {new: the line
    cassandra.yaml gets (as change_seeds writes it), wanted: cassandra_seeds'
    line, old: [the entries the other hosts run with], old_hosts: [the hosts
    in it], removed, added: [labels "name (dc/rack)", an entry no host has as
    is], differ: [hosts whose list is not the new one], step: something to
    apply, auto: [{dc, hosts, labels, entries}] the seeds picked, warnings,
    problems: a datacenter left with no seed and no node up there, no seed at
    all}.
    e.g. (node1, node3 marked absent, both running with "10.0.0.1,10.0.0.3"; seeds "10.0.0.1") -> {new:
    "10.0.0.1", old: ["10.0.0.1", "10.0.0.3"], removed: ["node3 (dc1/r1)"], added: [], differ: ["node1"], step: True,
    ...}."""
    wanted = _seed_entries(seeds)
    wanted_line = seeds if isinstance(seeds, str) else ",".join(wanted)
    present = [h for h in hosts if not h.get("absent")]
    old = _live_seeds(present)
    old_keys = [_seed_key(e, hosts) for e in old]
    problems, warnings, auto = [], [], []
    if not wanted:
        problems.append("cassandra_seeds is empty: list a node of each datacenter (one per rack, two or three per"
                        " datacenter).")
    new_entries = list(wanted)
    gone = _seeds_gone(old, wanted, hosts)
    for dc in sorted(set(h.get("dc") for h in gone)) if wanted else []:
        members = [h for h in present if h.get("dc") == dc]
        if not members or [w for w in wanted if _seed_key(w, hosts) in [h["name"] for h in members]]:
            continue
        picked, said = _dc_seeds(dc, members, [h["name"] for h in gone if h.get("dc") == dc], wanted, hosts, old_keys)
        if not picked:
            problems.append(said)
            continue
        new_entries += picked["entries"]
        auto.append(picked)
        warnings.append(said)
    new_line = ",".join(new_entries) if auto else wanted_line
    new_keys = [_seed_key(e, hosts) for e in new_entries]
    removed = [_seed_label(e, hosts) for e in old if _seed_key(e, hosts) not in new_keys]
    added = [_seed_label(e, hosts) for e in new_entries if _seed_key(e, hosts) not in old_keys]
    differ = [h["name"] for h in present if h.get("live") and h["live"] != new_line]
    return {"new": new_line, "wanted": wanted_line, "old": old,
            "old_hosts": [k for k in old_keys if k in [h["name"] for h in hosts]],
            "removed": removed, "added": added, "differ": differ,
            "step": bool(removed or added or differ), "auto": auto, "warnings": warnings, "problems": problems}


def _present_hosts(present, entries):
    """The hosts staying, the ring's entries -> (problems: the hosts a check refused, joining: a bootstrap an
    earlier run started, new: the hosts to add, down: the ones down in the ring)."""
    problems, joining, new, down = [], [], [], []
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
    return problems, joining, new, down


def _absent_unreachable(h, entry, where, entries, known):
    """A host marked absent that does not answer, its ring entry (or None) -> ("silent", "") when nothing is
    to do, else (None, the problem)."""
    name, address = h["name"], h.get("address") or ""
    if entry:
        return None, ("%s (%s) is marked absent and still in the ring (%s) but does not answer: bring it back and run"
                      " topology again (it is then decommissioned), or, if it is dead for good, remove_dead_node"
                      " -e cassandra_target_nodes=%s" % (name, address, where, address))
    if not address and any(a not in known for a in entries):
        return None, ("%s is marked absent, does not answer and the inventory gives no address for it, while the ring"
                      " has nodes the inventory does not know: it may be one of them. Give its address"
                      " (cassandra_listen_address or ansible_host), or bring it back." % name)
    return "silent", ""


def _absent_host(h, entries, known):
    """A host marked absent, the ring's entries, the inventory's addresses -> (what the plan does with it:
    "leaving", "normal" (decommissioned), "finish" (only stopped), "gone", "silent", or None; the problem, '')."""
    name, address = h["name"], h.get("address") or ""
    dc_entry = entries.get(address)
    entry = dc_entry[1] if dc_entry else None
    where = ("%s, %s%s" % (dc_entry[0], entry["status"], entry["state"])) if entry else ""
    if not h.get("reachable"):
        return _absent_unreachable(h, entry, where, entries, known)
    if h.get("refused"):
        return None, "%s: %s" % (name, h["refused"])
    if entry and entry["status"] == "D":
        return None, ("%s (%s) is marked absent and down in the ring (%s): a dead node is not decommissioned. Start it"
                      " and run topology again, or remove it with remove_dead_node -e cassandra_target_nodes=%s"
                      % (name, address, where, address))
    if entry and h.get("seed"):
        return None, ("%s is marked absent but is still in cassandra_seeds: take it out of cassandra_seeds in the"
                      " inventory; topology then applies the new seed list on the other nodes before it removes it."
                      % name)
    if entry and entry["state"] in ("J", "M"):
        return None, ("%s (%s) is marked absent and %s in the ring (%s): Cassandra decommissions a node up and"
                      " normal only. Run topology again once it is UN." % (
                          name, address, "joining" if entry["state"] == "J" else "moving", where))
    if entry:
        return ("leaving" if h.get("state") == "leaving" or entry["state"] == "L" else "normal"), ""
    if h.get("state") in ("decommissioned", "leaving"):
        return "finish", ""  # out of the ring, Cassandra still running: stopped and disabled
    if h.get("running"):
        return None, ("%s is marked absent and runs Cassandra, but is not in the ring (another cluster's node, or one"
                      " started alone?): topology leaves it as it is. Stop it by hand, or check its address (%s)."
                      % (name, address or "unknown"))
    return "gone", ""


def _same_address(hosts):
    """The hosts -> the problems of one address given to two hosts: the plan can't tell which one the ring means."""
    by_address, problems = {}, []
    for h in hosts:
        if h.get("address"):
            by_address.setdefault(h["address"], []).append(h["name"] + (" (marked absent)" if h.get("absent") else ""))
    for address, names in sorted(by_address.items()):
        if len(names) > 1:
            problems.append("%s have the same address %s: fix the inventory (an address reused?) before adding or"
                            " removing any of them." % (" and ".join(names), address))
    return problems


def _large_removals(in_ring, absent, present, new, entries):
    """The hosts to remove still in the ring, the hosts marked absent and staying, the hosts to add, the ring's
    entries -> a line per datacenter losing more than half of its nodes (the replicas it keeps are the guard)."""
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
                                  % (len(names), out.plural(size, "node"), dc, out.nodes(names),
                                     out.plural(size - len(names), "node")))
    return large_removals


def _plan_seeds(hosts, new, entries, seeds):
    """The hosts, the hosts to add, the ring's entries, cassandra_seeds -> cassandra_seed_change's result: applied
    live on every node once the adds are done, before the removals."""
    # a host to add may have a cassandra.yaml of its own (a package's): not a list the cluster runs with
    # (a seed it may get when a datacenter is left with none: up and normal in the ring, staying)
    un = [a for a, (dc, e) in entries.items() if e.get("status") == "U" and e.get("state") == "N"]
    seed_hosts = [dict(h, live="", up=False) if h["name"] in new
                  else dict(h, up=not h.get("absent") and h.get("address") in un) for h in hosts]
    return cassandra_seed_change(seed_hosts, seeds) if seeds is not None else {"step": False, "problems": []}


def _token_problems(present, new, in_ring, token_auto):
    """The hosts staying, the hosts to add, the hosts to remove still in the ring, cassandra_token_auto -> the
    problems of one token per node: add_node needs a token per new node, or cassandra_token_auto."""
    problems = []
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
    return problems


def _nodes_left(present, absent, in_ring):
    """The hosts staying, the hosts marked absent, the hosts to remove still in the ring -> {dc: nodes once done}."""
    nodes_left = {}
    for h in present:
        nodes_left[h.get("dc")] = nodes_left.get(h.get("dc"), 0) + 1
    for h in absent:
        if h["name"] in in_ring:
            nodes_left.setdefault(h.get("dc"), 0)
    return nodes_left


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
    of its nodes], problems, warnings}.
    e.g. (node1 to node3 UN, node3 marked absent, node4 new; seeds "10.0.0.1") -> {add: ["node4"], remove:
    ["node3"], problems: [], warnings: [], ...}."""
    entries = _ring_entries(ring)
    present = [h for h in hosts if not h.get("absent")]
    absent = [h for h in hosts if h.get("absent")]
    known = set(h.get("address") for h in hosts if h.get("address"))
    problems, joining, new, down = _present_hosts(present, entries)
    warnings = []
    kinds = {"leaving": [], "normal": [], "finish": [], "gone": [], "silent": []}
    for h in absent:
        kind, problem = _absent_host(h, entries, known)
        if kind:
            kinds[kind].append(h["name"])
        else:
            problems.append(problem)
    leaving, normal, finish, gone, silent = (kinds[k] for k in ("leaving", "normal", "finish", "gone", "silent"))
    problems.extend(_same_address(hosts))

    add = joining + new
    remove = leaving + finish + normal
    in_ring = leaving + normal
    unknown = ["%s (%s / %s, %s%s)" % (address, dc, entry.get("rack") or "?", entry["status"], entry["state"])
               for address, (dc, entry) in sorted(entries.items()) if address not in known]

    if leaving and (new or joining):
        problems.append("%s still leaving (a decommission an earlier run started): Cassandra adds no node while"
                        " another one leaves. Run topology again once it is out of the ring (or follow it with"
                        " decommission_node -e cassandra_target_nodes=%s)." % (", ".join(leaving), ",".join(leaving)))
    if unknown and (add or remove):
        problems.append("the ring has nodes no host of the inventory has: %s. Each step checks the ring has the"
                        " inventory's nodes, so the run would stop at the first one: fix the inventory first (an"
                        " address mistyped, a host missing), or remove a dead node with remove_dead_node."
                        % ", ".join(unknown))

    # more than half of a datacenter removed: said on the screen (the replicas it keeps are the guard)
    large_removals = _large_removals(in_ring, absent, present, new, entries)
    seed_change = _plan_seeds(hosts, new, entries, seeds)
    if seed_change["step"]:
        problems.extend(seed_change["problems"])
    warnings.extend(seed_change.get("warnings") or [])
    problems.extend(_token_problems(present, new, in_ring, token_auto))
    if down:
        warnings.append("%s down in the ring: the check before the first step stops the run unless it is back up."
                        % ", ".join(down))
    return {"add": add, "remove": remove, "seeds": seed_change, "finish": finish, "gone": gone, "silent": silent, "unknown": unknown, "down": down,
            "in_ring": in_ring, "joining": joining, "leaving": leaving, "nodes_left": _nodes_left(present, absent, in_ring),
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
        picked = "".join("; %s gets %s" % (a["dc"], ", ".join(a["labels"])) for a in seeds.get("auto") or [])
        steps.append({"node": "seeds",
                      "text": "%s -> %s%s" % (",".join(seeds.get("old") or []) or "(none)", seeds.get("new"), picked)})
    for name in plan["remove"]:
        h = by_name.get(name) or {}
        steps.append({"node": "decommission " + name, "dc": h.get("dc") or "?", "rack": h.get("rack"), "text": ""})
    return steps


def _adding(hosts, plan):
    """The hosts, the plan -> the hosts to add as cassandra_add_node_plan takes them, in order."""
    return [{"host": h["name"], "address": h.get("address"), "dc": h.get("dc"), "rack": h.get("rack"),
             "in_ring": h["name"] in plan["joining"], "state": "joining" if h["name"] in plan["joining"] else "new"}
            for h in hosts if h["name"] in plan["add"]]


def _add_text(name, h, plan, estimate, seeds):
    """A host to add, the plan, {host: what it streams}, the seed change -> the text of its step."""
    if name in plan["joining"]:
        return "still bootstrapping (UJ, an earlier run): waited for"
    e = estimate.get(name)
    parts = ["bootstrap, ~%s to stream (%s)" % (out.size(e["bytes"]), e["basis"]) if e else "bootstrap"]
    if h.get("single"):
        parts.append("token %s" % (h["token"] if h.get("token") else "from cassandra_token_auto"))
    if h.get("reset"):  # add_node's line for it: "<node>: has data (...) - will be reset"
        line = str(h["reset"].get("line") or "")
        parts.append(line.split(": ", 1)[1] if ": has data " in line else "will be reset first")
    if seeds.get("step") and name in [a.split(" ")[0] for a in seeds.get("added") or []]:
        parts.append("joins as a regular node, a seed at the seed step")
    return "; ".join(parts)


def _remove_texts(plan, hosts, ring, adding, keyspaces, seeds):
    """The plan, the hosts, the ring, the hosts to add, cassandra_keyspaces, the seed change -> the texts of the
    removal steps, in order: where the data goes, as the ring is once the adds are done."""
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
    return [block["step"]["text"] for block in decommission["blocks"]]


def _dc_facts(plan, hosts, by_name, keyspaces):
    """The plan, the hosts (and by name), cassandra_keyspaces -> (the facts of each datacenter the plan touches,
    once done: its nodes, racks, highest RF; the warnings of its racks left uneven)."""
    facts, rack_warnings = [], []
    touched = sorted(set(by_name[n].get("dc") for n in plan["add"] + plan["remove"]))
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
    return facts, rack_warnings


def _left_facts(plan, seeds):
    """The plan, the seed change -> the facts of what is left: the seeds written again, the hosts already out,
    what to do by hand once done, the cleanup."""
    facts = []
    if seeds.get("step") and not (seeds.get("removed") or seeds.get("added")):
        facts.append(["seeds", "written again on %s, the same seeds: %s" % (out.nodes(seeds.get("differ")), seeds["new"])])
    if plan["gone"]:
        facts.append(["already removed", "%s (marked absent, out of the ring, Cassandra stopped)" % out.nodes(plan["gone"])])
    if plan["silent"]:
        facts.append(["not answering", "%s (marked absent, out of the ring): nothing to do" % out.nodes(plan["silent"])])
    done = plan["remove"] + plan["gone"] + plan["silent"]
    if done:
        facts.append(["then", "empty %s (reset_node) before reusing the host%s, re-import the cluster to drop %s from"
                              " the inventory: the commands at the end" % (
                                  out.nodes(done, keep_order=True), "" if len(done) == 1 else "s",
                                  "it" if len(done) == 1 else "them")])
    if plan["add"]:
        facts.append(["cleanup", "of the nodes that hand data over: its command is printed after the adds (topology"
                                 " runs none, the removals move data again)"])
    return facts


def _grouped(hosts, plan, key):
    """The hosts, the plan, a key of the hosts' (info, checks) -> [(text, hosts)] of the hosts to add, in order,
    each text once."""
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


def _screen_warnings(plan, hosts, seeds, kept, rack_warnings, force, replication_problems):
    """The plan, the hosts, the seed change, the checks' notes, the racks' warnings, cassandra_decommission_force,
    the datacenters left with too few nodes -> the WARNING lines of the screen, in order (not deduplicated)."""
    warnings = [out.warning_text(n) for n in kept if out.is_warning(n)]
    warnings.extend("%s: %s" % (out.nodes(names_of), text) for text, names_of in _grouped(hosts, plan, "checks"))
    if seeds.get("removed") or seeds.get("added"):  # a seed change topology applies on its own
        warnings.append("the seeds will change on every node: %s -> %s (from cassandra_seeds in the inventory%s)"
                        % (",".join(seeds["old"]) or "(none)", seeds["new"],
                           ", and the seeds picked below" if seeds.get("auto") else ""))
    warnings.extend(plan.get("large_removals") or [])
    warnings.extend(rack_warnings)
    warnings.extend("%s is in the ring but in no host of the inventory: never touched (an address mistyped, a host"
                    " missing, or a dead node: remove_dead_node -e cassandra_target_nodes=%s)" % (u, u.split(" ")[0])
                    for u in plan["unknown"])
    warnings.extend(w.rstrip(".") for w in plan["warnings"])
    if force and replication_problems:
        warnings.append("cassandra_decommission_force is true: the removal goes on although too few nodes are left,"
                        " those replicas are lost and QUORUM can fail: %s" % "; ".join(replication_problems))
    return warnings


def _real_run(hosts, plan, session):
    """The hosts, the plan, the tmux/screen warning -> [(label, warning)] only a real run gives: the resets of the
    hosts to add, not inside tmux or screen."""
    real_run = []
    resets = [{"name": h["name"], "plan": h["reset"]} for h in hosts if h["name"] in plan["add"] and h.get("reset")]
    for reset in cassandra_reset_warnings(resets):
        text = reset["text"]
        first, rest = (text[0], text[1:]) if isinstance(text, list) else (text, [])
        dirs = [line for item in rest for line in (item.get("pre") or [] if isinstance(item, dict) else [item])]
        real_run.append((reset["label"], "\n".join(["%s: %s" % (reset["label"], first)] + ["  " + d for d in dirs])))
    if str(session or "").strip():
        real_run.append(("not inside tmux or screen", str(session).strip()))
    return real_run


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
    lines the checks kept for the plan (note.yml, "WARNING - ..." or a note).
    The checks of the hosts to add (hosts' info, checks: new_node_checks.yml)
    are NOTE and WARNING lines too, one per text, with the hosts it is about.
    Every note is on one line, said once.
    e.g. (that plan) -> ["PLAN  topology  my_cluster  2 steps, one node at a time", "  1.  add node4           dc1/r1
    bootstrap, ~768.0 MiB to stream (the load of dc1 / 4 nodes)", "  2.  decommission node3  dc1/r1  load 1 GiB -> node1,
    node2, node4", "", "dc1 after:  3 nodes: node1, node2, node4", ...]."""
    by_name = dict((h["name"], h) for h in hosts)
    ring = ring or {}
    steps = cassandra_topology_steps(plan, hosts)
    seeds = plan.get("seeds") or {}

    adding = _adding(hosts, plan)
    estimate = {}
    if adding:  # streamed while the nodes to remove are still there
        add_plan = cassandra_add_node_plan(ring, adding, hosts=names or {}, keyspaces=keyspaces)
        estimate = dict((e["host"], e) for e in add_plan["estimate"])
    for step, name in zip(steps, plan["add"]):
        step["text"] = _add_text(name, by_name[name], plan, estimate, seeds)
    if seeds.get("step"):
        seed_step = steps[len(plan["add"])]
        seed_step["text"] += "  written and reloaded live on every node, no restart"
    if plan["remove"]:
        texts = _remove_texts(plan, hosts, ring, adding, keyspaces, seeds)
        for step, text in zip(steps[len(steps) - len(plan["remove"]):], texts):
            step["text"] = text

    # the facts: each datacenter once done, then what is left to do by hand
    facts, rack_warnings = _dc_facts(plan, hosts, by_name, keyspaces)
    facts += _left_facts(plan, seeds)
    for text, names_of in _grouped(hosts, plan, "info"):
        facts.append("NOTE  %s: %s" % (out.nodes(names_of), text))
    kept = [str(n).strip() for n in notes or [] if str(n).strip()]
    facts.extend("NOTE  " + n for n in kept if not out.is_warning(n))

    # the warnings, just above the question
    warnings = _screen_warnings(plan, hosts, seeds, kept, rack_warnings, force, replication_problems)
    real_run = _real_run(hosts, plan, session)
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
    return lines  # (a warning on several lines: plan() puts the next ones under its text)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_topology_plan": cassandra_topology_plan,
            "cassandra_topology_screen": cassandra_topology_screen,
            "cassandra_seed_change": cassandra_seed_change,
        }
