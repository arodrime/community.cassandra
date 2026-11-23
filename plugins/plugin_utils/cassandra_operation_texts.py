# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The texts of one operation each, built on the generic blocks of
cassandra_output.py. Pure Python, no Ansible: the filters of
plugins/filter/cassandra_output.py, cassandra_seeds.py, cassandra_help.py and
cassandra_topology.py call it.

- what the prepare of new nodes did (add_node, add_datacenter,
  create_cluster): prepare_lines, left_system_warning;
- what is left to do once seeds were picked or reloaded, or nodes removed
  (topology, decommission_node, change_seeds): seeds_todo,
  seeds_reload_todo, removed_todo;
- the seed rule of preflight, help and topology: seed_layout, seed_target,
  pick_seeds.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import re
import shlex
import textwrap

from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import (
    NBSP, WARNING, command, extra_var, in_git_work_tree, nodes, path_from, plural, table, todo, who)

# --- the prepare of new nodes --------------------------------------------------------------

# The steps of a prepare (cassandra_operation action_add_prepare.yml, create_cluster), in their order
PREPARE_STEPS = ("repository", "install", "java", "os tuning", "data disks", "config", "firewall", "medusa", "seeds")
_FILE_CHANGED = re.compile(r"\d+ line\(s\) changed$")  # cassandra_config's item for a file it writes
_VERSION = re.compile(r"(?:\d+:)?(\d+(?:\.\d+)*)")  # "1:17.0.12+7-1~deb12u1" -> 17.0.12


def _on(value):
    """A flag as Ansible may template it: True, "True", "yes", "on"..."""
    return value is True or str(value).strip().lower() in ("true", "yes", "on", "y", "1")


def _version(text):
    found = _VERSION.match(str(text or ""))
    return found.group(1) if found else str(text or "")


def _grouped(pairs):
    """[(key, name)] -> [(key, [names])], the keys in the order first seen."""
    groups = []
    for key, name in pairs:
        group = next((g for g in groups if g[0] == key), None)
        if group is None:
            groups.append((key, [name]))
        else:
            group[1].append(name)
    return groups


def _package(item):
    """The package a change item of cassandra_install names, without a version pin ("pkg=17.0.12*"), else ""."""
    text = str(item.get("item", ""))
    return re.sub(r"[=<>].*$", "", text[len("package "):]) if text.startswith("package ") else ""


def _package_text(name, version, items, label=None):
    """A package's words: "cassandra 5.0.9"; the version the role installed when it has one (a pinned name
    looked up as is reads "not in configured repos yet": the version read is better), "cassandra (not in
    configured repos yet)" for a candidate not found; label: what names it ("" for its version alone)."""
    new = next((i for i in items if _package(i) == name), None)
    after = (new or {}).get("after") or ""
    if _VERSION.match(str(after)) or not _VERSION.match(str(version or "")):
        version = after or version
    label = name if label is None else label
    shown = _version(version)
    if shown and not _VERSION.match(str(version or "")):
        return ("%s (%s)" % (label, version)).strip()
    return ("%s %s" % (label, shown)).strip()


def _prepare_texts(node):
    """{step: words} for one node of prepare_lines: a step it has nothing for, or that is off or not managed, left
    out; the words say what the node got, in short ("cassandra 5.0.9", "config (3 files)")."""
    texts = {}
    items = list(node.get("packages") or [])
    if node.get("repository") in ("set", "ok"):
        texts["repository"] = "repository"
    ran = list(node.get("ran") or [])
    if "cassandra_install" in ran:
        java = node.get("java") or {}
        others = [_package(i) for i in items if _package(i) not in ("", "cassandra", str(java.get("package", "")))]
        texts["install"] = ", ".join([_package_text("cassandra", node.get("cassandra"), items)] + others)
        if _on(java.get("managed", True)) and java.get("home"):
            texts["java"] = "java %s (%s)" % (java.get("version") or java.get("series", ""), java["home"])
        elif _on(java.get("managed", True)) and java.get("package"):
            texts["java"] = _package_text(java["package"], java.get("version"), items, label="java")
    if "cassandra_linux" in ran:
        changes = int(node.get("os") or 0)
        texts["os tuning"] = "os tuning" + (" (%s)" % plural(changes, "change") if changes else "")
    disks = node.get("disks") or {}
    if disks and (_on(disks.get("changed")) or disks.get("warnings") or disks.get("info")):
        kb = "read-ahead %s KB" % disks["kb"] if str(disks.get("kb", "")) not in ("", "none") else ""
        said = ("disks " + kb).strip() if _on(disks.get("changed")) else "disks"
        if disks.get("warnings"):
            said += " (%s below)" % plural(len(disks["warnings"]), "warning")
        elif disks.get("info"):
            said += " (see below)"
        texts["data disks"] = said
    if "cassandra_config" in ran:
        texts["config"] = _config_text(list(node.get("config") or []))
    for step, value in (("firewall", node.get("firewall")), ("medusa", node.get("medusa"))):
        if isinstance(value, (list, tuple)) and ran:
            texts[step] = _switched_text(step, list(value))
    seeds = node.get("seeds")
    if seeds:
        seeds = [x.strip() for x in seeds.split(",")] if isinstance(seeds, str) else list(seeds)
        texts["seeds"] = "seeds " + ", ".join(seeds)
    return texts


def _config_text(config):
    """The config step: cassandra_config's change items, "config (3 files)"."""
    templated = next((i for i in config if i.get("item") == "config files"), None)
    files = [i for i in config if _FILE_CHANGED.search(str(i.get("after", "")))]
    other = [i for i in config if i is not templated and i not in files]
    parts = []
    if templated is not None:
        parts.append(str(templated.get("after", "")).lower() or "files templated")
    elif files:
        parts.append(plural(len(files), "file"))
    if other:  # owners, modes, the RedHat conf link
        parts.append(plural(len(other), "other change"))
    return "config" + (" (%s)" % ", ".join(parts) if parts else "")


def _switched_text(step, changes):
    """The firewall or medusa step from its change items: "medusa 0.24.1", "firewall (2 changes)"."""
    medusa = next((i for i in changes if i.get("item") == "Medusa"), None)
    if medusa is not None:
        more = len(changes) - 1
        return "medusa %s" % medusa.get("after") + (" (+%s)" % plural(more, "change") if more else "")
    return step + (" (%s)" % plural(len(changes), "change") if changes else "")


def prepare_lines(hosts, check=False):
    """What the prepare of new nodes did (check: would do), one line per group
    of nodes with the same outcome, the steps in their order in short words:
    "node4, node5  prepared  repository, cassandra 5.0.9, java 17.0.12, os
    tuning (2 changes), config (3 files), seeds 10.0.0.1, 10.0.0.2" ("would
    prepare" under check); a step off or not managed is left out. hosts:
    [{name, ran (the roles whose change report ran), repository (off,
    packages, set or ok), packages (cassandra_install's change items),
    cassandra (its version installed), java ({managed, package, version, home,
    series}), os (the count of OS changes), disks (cassandra_linux's
    _cassandra_disk_summary), config (cassandra_config's change items),
    firewall (its change items, off or not managed), medusa (its change items
    or off), seeds (the seeds it joins with)}]."""
    texts = [(h["name"], _prepare_texts(h)) for h in hosts or [] if isinstance(h, dict) and h.get("name")]
    # a step's words kept together (NBSP): a row that goes on past 79 columns, under its text, cut between steps
    said = [(", ".join(words[step].replace(" ", NBSP) for step in PREPARE_STEPS if step in words), name)
            for name, words in texts]
    rows = [[who(names, counted=False), "would prepare" if check else "prepared", text]
            for text, names in _grouped(said) if text]
    return [line.replace(NBSP, " ") for line in table(rows, width=79)]


def _wrapped(text, width=79, indent="  ", first=None):
    return textwrap.wrap(text, width, initial_indent=indent if first is None else first, subsequent_indent=indent,
                         break_on_hyphens=False, break_long_words=False)


def left_system_warning(hosts):
    """The WARNING about an old system keyspace a new node has outside the
    data directories its config uses, each directory once with its nodes:
    why the run goes on, and what to delete by hand. hosts: [{name, left:
    [directories holding a system keyspace], dirs: [the data directories its
    config uses]}]."""
    said = _grouped(((tuple(h["left"]), tuple(h.get("dirs") or [])), h["name"])
                    for h in hosts or [] if isinstance(h, dict) and h.get("left"))
    lines = []
    for (left, dirs), names in said:
        where, shown = ", ".join(d.rstrip("/") or "/" for d in left), who(names, counted=False)
        inside = [d for d in dirs if any(d.startswith(x.rstrip("/") + "/") for x in left)]
        lines += _wrapped(WARNING + "%s: an old system keyspace in %s, not one of the data directories the config"
                          " uses (%s)" % (shown, where, ", ".join(dirs) or "?"), first="")
        lines += _wrapped(
            "Cassandra does not read it: the node starts empty in its own data directories, the run goes on. It is"
            " what an earlier install or config left there: disk space taken, and a config pointing there again"
            " would start the node on that old system keyspace.")
        lines += _wrapped(
            "The way out, once the run is over and if no other Cassandra on %s uses it: delete by hand the keyspace"
            " directories in %s (system, system_schema, system_auth, system_distributed, system_traces and any"
            " other there)%s. reset_node does not: it empties only the directories the inventory and"
            " cassandra.yaml name." % (shown, where, ", keeping " + ", ".join(inside) if inside else ""))
    return lines


# --- what is left to do --------------------------------------------------------------------


def seeds_todo(change, group=""):
    """The TO DO item once seeds were picked for a datacenter its
    cassandra_seeds left with none (cassandra_seed_change's auto): the line
    to put in the inventory, else apply_config would write the inventory's
    list back. None when nothing was picked."""
    if not (change or {}).get("auto"):
        return None
    entries = [e.strip() for e in str(change["new"]).split(",") if e.strip()]
    return {"text": "put the seeds the nodes now run with in the inventory (group_vars/%s/, or a re-import writes"
                    " them), else apply_config puts cassandra_seeds back" % (group or "<cluster group>"),
            "command": "cassandra_seeds: [%s]" % ", ".join('"%s"' % e for e in entries)}


def seeds_reload_todo(failed, command_line):
    """The TO DO item when nodetool reloadseeds failed on nodes after their
    seed list was written (seeds_apply.yml): command_line (change_seeds)
    reloads it once the cause is fixed. None when none failed."""
    failed = list(failed or [])
    if not failed:
        return None
    return {"text": "reload the seed list on %s: nodetool reloadseeds failed (the WARNING above says why; a node that"
                    " cannot load its cassandra.yaml again will not start either: fix the file before any restart),"
                    " then" % nodes(failed, keep_order=True),
            "command": command_line}


def removed_todo(removed, reimport_from, inventory=None, hosts=None, inventory_dir=None, cwd=None, in_git=None,
                 unreachable=None, imported=True, reimport_hosts=None, first=None):
    """The TO DO once nodes left the ring (decommission_node, topology):
    empty them (reset_node), then drop them from the inventory by a
    re-import of the cluster from the inventory itself (its connection
    variables and its logins, vaulted ones too), --check --diff first, then
    the commit, only when the inventory dir is in a git work tree.
    reimport_from: a node that stays (none: no re-import); inventory, hosts:
    the run's (none: left out of the commands); reimport_hosts: the
    cluster's group for the re-import's -e cassandra_hosts ('': the import
    takes the cluster without it, CASSANDRA_CLUSTER or the inventory's only
    cluster; None: hosts); inventory_dir: where the cluster's inventory is
    (import_dir, given when it is not the default inventories);
    unreachable: the ones not answering, left out of the reset (the
    re-import drops them too); imported: the inventory is import_cluster's
    (else: remove them from it by hand, no re-import); first: items said
    before (the seeds to put in the inventory)."""
    removed = list(removed or [])
    first = [i for i in first or [] if i]
    if not removed:
        return todo(first)
    if reimport_hosts is None:
        reimport_hosts = hosts
    names = nodes(removed, keep_order=True)
    reset = [n for n in removed if n not in (unreachable or [])]
    items = first + [{"text": "empty %s before reusing %s (%s data is left in place)" % (
        nodes(reset, keep_order=True), "the host" if len(reset) == 1 else "the hosts", "its" if len(reset) == 1 else "their"),
        "command": command("reset_node", inventory=inventory, hosts=hosts, cwd=cwd,
                           extra={"cassandra_target_nodes": ",".join(reset)})}] if reset else first
    shown_dir = path_from(str(inventory_dir), cwd) if inventory_dir else ""
    reimport = [extra_var("import_force", "true")]
    if shown_dir and os.path.normpath(shown_dir) != "inventories":
        reimport.append(extra_var("import_dir", shown_dir))
    if not imported or not reimport_from:
        items.append("remove %s from the inventory (or leave %s marked cassandra_node_state: absent)" % (
            names, "it" if len(removed) == 1 else "them"))
    else:
        # read with the inventory: its connection variables and logins (secrets.yml) come with it
        items.append({"text": "drop %s from the inventory: re-import the cluster with its inventory, the changes shown"
                              " first" % names,
                      "command": command("import_cluster", inventory=inventory, hosts=reimport_hosts, cwd=cwd,
                                         extra=reimport + ["--check", "--diff"])})
        items.append({"text": "then write them",
                      "command": command("import_cluster", inventory=inventory, hosts=reimport_hosts, cwd=cwd,
                                         extra=reimport)})
    if shown_dir and (in_git if in_git is not None else in_git_work_tree(str(inventory_dir))):
        # only the inventory, whatever else is staged
        items.append({"text": "commit it", "command": "git add %s && git commit -m %s -- %s" % (
            shlex.quote(shown_dir), shlex.quote("Inventory: %s removed" % ", ".join(removed)), shlex.quote(shown_dir))})
    return todo(items)


# --- the seed rule -------------------------------------------------------------------------


SEEDS_MIN, SEEDS_MAX = 2, 3  # per datacenter (OUTPUT Q7)


def seed_target(nodes, racks):
    """The seeds a datacenter of that many nodes and racks gets by the seed rule."""
    return min(nodes, SEEDS_MAX, max(SEEDS_MIN, racks))


def pick_seeds(racks, target):
    """target nodes of a datacenter, one rack after the other, its seeds first
    (racks holding a seed first: well placed seeds stay)."""
    order = sorted(racks, key=lambda r: not any(n["seed"] for n in racks[r]))
    queues = dict((r, sorted(racks[r], key=lambda n: not n["seed"])) for r in order)
    picked = []
    while len(picked) < target and any(queues.values()):
        for rack in order:
            if queues[rack] and len(picked) < target:
                picked.append(queues[rack].pop(0))
    return picked


def seed_layout(nodes):
    """The seed rule (Q7), the same for preflight and help: 2 or 3 seeds per
    datacenter, on different racks when it has several; 1 seed (in a
    datacenter of more than one node) or none is a warning, more than 3 a
    note. nodes: [{name, address, dc, rack, seed}] in inventory order.
    Returns {lines: one per datacenter ("Seeds  dc1  node1 (rack_a), node3
    (rack_b)  ok", a warning's "WARNING  Seeds  dc2  ..."), dcs: [{dc, level
    ok|note|warning, text}], problems: the warnings' texts, notes, suggested: a seed list that follows the rule
    (addresses, every datacenter; empty without a warning)}."""
    dcs = {}
    for n in nodes or []:
        dcs.setdefault(n["dc"], {}).setdefault(n["rack"], []).append(n)
    width = max([len(str(dc)) for dc in dcs] or [0])
    result = {"lines": [], "dcs": [], "problems": [], "notes": [], "suggested": []}
    suggested = []
    for dc, racks in dcs.items():
        members = [n for rack in racks.values() for n in rack]
        seeds = [n for n in members if n["seed"]]
        seed_racks = []
        for n in seeds:
            if n["rack"] not in seed_racks:
                seed_racks.append(n["rack"])
        level, text = "ok", ""
        if not seeds:
            level, text = "warning", "no seed"
        elif len(seeds) == 1 and len(members) > 1:
            level, text = "warning", "1 seed: %d to %d per datacenter%s" % (
                SEEDS_MIN, SEEDS_MAX, ", on different racks" if len(racks) > 1 else "")
        elif len(seed_racks) < min(len(seeds), len(racks)):
            empty = [r for r in racks if r not in seed_racks]
            level, text = "warning", "%s on %s, none on %s: put them on different racks" % (
                plural(len(seeds), "seed"), ", ".join(seed_racks), ", ".join(empty))
        elif len(seeds) > SEEDS_MAX:
            level, text = "note", "%d seeds: %d to %d are enough" % (len(seeds), SEEDS_MIN, SEEDS_MAX)
        listed = ", ".join("%s (%s)" % (n["name"], n["rack"]) for n in seeds) or "none"
        line = "Seeds  %s  %s  %s" % (str(dc).ljust(width), listed, {"ok": "ok", "note": "note: " + text}.get(level, text))
        result["lines"].append(WARNING + line if level == "warning" else line)
        result["dcs"].append({"dc": dc, "level": level, "text": text})
        if level == "warning":
            result["problems"].append("%s: %s" % (dc, text))
        elif level == "note":
            result["notes"].append("%s: %s" % (dc, text))
        keep = seeds if level != "warning" else pick_seeds(racks, seed_target(len(members), len(racks)))
        suggested += [n["address"] for n in keep]
    result["suggested"] = suggested if result["problems"] else []
    return result
