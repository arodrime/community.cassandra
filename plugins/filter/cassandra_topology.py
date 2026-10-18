# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_seed_host, cassandra_seed_port: a seed entry's host and port
    ("[2001:db8::4]:7000" -> "2001:db8::4", "7000"), as the roles compare an
    entry with a node's names.

cassandra_seed_change: the hosts' live seed lists and cassandra_seeds -> the
    seed change to apply (change_seeds).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def _seed_entries(seeds):
    """cassandra_seeds (a list or a comma-separated string) -> its entries."""
    if not seeds:
        return []
    if isinstance(seeds, str):
        seeds = seeds.split(",")
    return [str(s).strip() for s in seeds if str(s).strip()]


def _host_port(entry):
    """A seed entry -> (its host, its port or None): "10.0.0.1:7000", "node1", "[2001:db8::4]:7000" (an IPv6
    address with a port is written in brackets), "2001:db8::4" (an IPv6 address alone: no port)."""
    bracketed = re.match(r"^\[(.*)\](?::([0-9]+))?$", entry)
    if bracketed:
        return bracketed.group(1), bracketed.group(2)
    if entry.count(":") > 1:
        return entry, None
    host, sep, port = entry.rpartition(":")
    if sep and port.isdigit():
        return host, port
    return entry, None


def cassandra_seed_host(entry):
    """A seed entry -> its host: "10.0.0.1:7000" -> "10.0.0.1", "[2001:db8::4]:7000" -> "2001:db8::4",
    "2001:db8::4" as is (an IPv6 address alone is not cut at its last colon)."""
    return _host_port(str(entry).strip())[0]


def cassandra_seed_port(entry):
    """A seed entry -> its port, None when it has none: "10.0.0.1:7000" -> "7000", "2001:db8::4" -> None."""
    return _host_port(str(entry).strip())[1]


def _auto_entry(h, wanted, hosts):
    """The seed entry of host h, written as the entries of cassandra_seeds are (wanted): its inventory name
    when they are all inventory names, else its address (a name the nodes may not resolve otherwise); their
    port when they all have one."""
    bare = [_host_port(e)[0] for e in wanted]
    names = [x["name"] for x in hosts]
    by_name = bool(wanted) and all(b in names for b in bare)
    entry = h["name"] if by_name else (h.get("entry") or h.get("address") or h["name"])
    ports = [_host_port(e)[1] for e in wanted]
    if wanted and all(ports) and len(set(ports)) == 1 and _host_port(entry)[1] is None:
        entry = "%s:%s" % ("[%s]" % entry if ":" in entry else entry, ports[0])
    return entry


def _seed_owner(entry, hosts):
    """A seed entry, the hosts -> the host it names (by a name or an address it answers to), None if none."""
    bare = _host_port(entry)[0]  # the port left out, as seed_facts.yml compares
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
    for dc in sorted(set(h.get("dc") or "" for h in gone)) if wanted else []:
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


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_seed_change": cassandra_seed_change,
            "cassandra_seed_host": cassandra_seed_host,
            "cassandra_seed_port": cassandra_seed_port,
        }
