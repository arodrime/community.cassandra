# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The screen an operation playbook shows before it changes anything.

cassandra_screen: spec -> the text, the same layout for every operation (the
    plan block of docs/docsite/rst/guide_output.rst): the title, the operation
    in capitals then what it does to whom, where ("ADD NODE  node4, node5
    into dc1 of my_cluster"); the "how" lines under it (the intro); a blank
    line, then the node table (one row per node, two-space columns) and the
    notes under it (after); the warnings together, last, "WARNING  label:
    text", two lines at most each; then, in the question's place, what
    --check or cassandra_operation_confirm false means for this run. The
    cluster and its version are the banner's, not said again.
    spec: {operation, summary: str, intro: [item],
           blocks: [{title, row: str, lines: [item]}] (a block with a row is
           a row of the table: its title's fields then the row; without one,
           its title then its lines indented under it),
           sections: [[item]] (each one a block of its own, indented 2),
           after: [item] (the notes under the table),
           warnings: [{label, text: str or [str, item...], real_run: bool}
                      or {label, each: [str], real_run}: one warning per str]}
    An item is a string (wrapped) or {'pre': [lines]} (kept as is: tables,
    statements). Empty strings are dropped, and a warning with the same
    label and text shown once. A warning with real_run true only
    concerns the act itself (the SSH session, data deleted for good): --check
    names it on one line instead of printing it. The warnings about the
    cluster's state show under --check too.
cassandra_screen_title: name, preflight facts -> a block title,
    "node7  10.0.0.7  dc1 / rack1" (what is known of it).
cassandra_reset_warnings: the warnings of the resets an operation runs
    first (add_node, replace_node with their reset): one "data loss" warning
    per node with something to delete, the directories and what they hold.
cassandra_decommission_screen: the blocks of decommission_node: each node
    to remove with its dc/rack, load and share, where its data goes, where
    it runs and is checked from, and how it ends; its "step": the same on
    one line ({node, dc, rack, text}, the plan of topology), its row.
cassandra_add_node_blocks: the row of each new node on add_node's screen.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import textwrap

from collections.abc import Mapping

from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out

WIDTH = 100


def _wrap(text, indent="", subsequent=None):
    subsequent = indent + "  " if subsequent is None else subsequent
    out = []
    for para in str(text).split("\n"):
        if not para.strip():
            continue
        out.extend(textwrap.wrap(para.strip(), width=WIDTH, initial_indent=indent, subsequent_indent=subsequent,
                                 break_long_words=False, break_on_hyphens=False))
    return out


def _items(items, indent="", hang="  "):
    """The items as lines: a string wrapped (its next lines hang columns further in), a {'pre': [lines]} as it
    is, indented; an empty one left out."""
    lines = []
    for item in items or []:
        if isinstance(item, Mapping):
            lines += [indent + str(line) for line in item.get("pre") or []]
        elif str(item).strip():
            lines += _wrap(item, indent, indent + hang)
    return lines


def _warning(warning):
    """A warning's lines: "WARNING  label: text", the text wrapped under itself (two lines when it fits), the
    items of a text given as a list under it, each on its own line; no label: "WARNING  text"."""
    text = warning.get("text") or ""
    first, rest = (text[0], text[1:]) if isinstance(text, list) else (text, [])
    label = str(warning.get("label") or "").strip()
    under = " " * len(out.WARNING)
    lines = _wrap(out.WARNING + ("%s: %s" % (label, first) if label else str(first)), "", under)
    for item in rest:
        if isinstance(item, Mapping):
            lines.extend(_items([item], under))
        elif str(item).strip():
            lines.extend(_wrap(item, under + "- ", under + "  "))
    return lines


def _table_block(blocks, after):
    """The node table and the notes under it: a block with a row is a row (its title's fields, then the row);
    one without is its title, then its lines indented under it."""
    rows, lines = [], []
    for block in blocks:
        if "row" in block:
            rows.append([f for f in str(block.get("title") or "").split("  ") if f] + [str(block.get("row") or "")])
        else:
            lines += _wrap(block.get("title") or "", "  ", "    ") + _items(block.get("lines"), "    ")
    return out.table(rows, "  ") + lines + _items(after, "  ")


def _warnings(spec, session, notes, check):
    """The warnings of the spec, the session's and the checks' notes (WARNING lines kept for the plan), each once;
    under check, the ones that only concern a real run named on one line instead."""
    given = []
    for warning in spec.get("warnings") or []:  # each: one warning per text, under the same label
        given.extend([dict(warning, text=t) for t in warning["each"]] if "each" in warning else [warning])
    given += [{"label": "", "text": out.warning_text(n)} for n in notes or [] if out.is_warning(n)]
    if str(session or "").strip():
        given.append({"label": "", "text": session, "real_run": True})
    seen, kept = set(), []
    for warning in given:
        key = (warning.get("label"), repr(warning.get("text")))
        if warning.get("text") and key not in seen:
            seen.add(key)
            kept.append(warning)
    skipped, lines = [], []
    for warning in kept:
        if check and warning.get("real_run"):
            name = warning.get("label") or str(warning["text"]).split(":", maxsplit=1)[0]
            if name not in skipped:
                skipped.append(name)
            continue
        lines += _warning(warning)
    if skipped:
        lines += ["(A real run would also warn about: %s.)" % ", ".join(str(s) for s in skipped)]
    return lines


def cassandra_screen(spec, check=False, asks=True, session="", asked_by="", notes=None):
    """check: --check; asks: false when a question would be asked but
    cassandra_operation_confirm is false (said at the end); session: the
    tmux/screen warning, if any; asked_by: the playbook that showed the
    whole plan and asked already (topology), said at the end; notes: the
    lines the checks kept for the plan (note.yml: WARNING lines, said with
    the warnings; the others under the table).
    Input: spec (see the module's doc); output: the text, one string. e.g. ({operation: reset_node, summary: "node7
    of my_cluster", blocks: [{title: "node7  10.0.0.7", lines: ["Cassandra stopped"]}], warnings: [{label: "data
    loss", text: "node7: 12 entries DELETED", real_run: True}]}) -> the lines "RESET NODE  node7 of my_cluster", "",
    "  node7  10.0.0.7", "    Cassandra stopped", "", "WARNING  data loss: node7: 12 entries DELETED", joined;
    under check, "(A real run would also warn about: data loss.)" and the --check line instead of the warning."""
    spec = spec or {}
    plain = [out.warning_text(n) for n in notes or [] if str(n).strip() and not out.is_warning(n)]
    sections = [[out.title(spec.get("operation") or "", spec.get("summary") or "")] + _items(spec.get("intro"), "  ")]
    sections.append(_table_block(spec.get("blocks") or [], list(spec.get("after") or []) + plain))
    for section in spec.get("sections") or []:
        sections.append(_items(section, "  "))
    sections.append(_warnings(spec, session, notes, check))  # the warnings together, last, just above the question
    # in the question's place (screen.yml asks it right below)
    if check:
        sections.append(["--check: nothing will be changed"])
    elif asked_by:
        sections.append(["A step of %s, confirmed on its screen: no question here." % asked_by])
    elif not asks:
        sections.append(["cassandra_operation_confirm is false: no question, the run goes on."])

    lines = []
    for section in sections:
        if section:
            if lines:
                lines.append("")
            lines.extend(section)
    return "\n".join(lines)


def cassandra_screen_title(name, preflight=None):
    """A node's name, its preflight facts (address, cassandra_dc, cassandra_rack) -> the title of its block, what
    is known of it. e.g. ("node7", {address: "10.0.0.7", cassandra_dc: "dc1", cassandra_rack: "rack1"}) ->
    "node7  10.0.0.7  dc1 / rack1"."""
    p = preflight or {}
    parts = [str(name), str(p.get("address") or "")]
    if p.get("cassandra_dc"):
        parts.append("%s / %s" % (p["cassandra_dc"], p.get("cassandra_rack") or "?"))
    return "  ".join(x for x in parts if x)


def cassandra_reset_warnings(plans):
    """plans: [{name, plan}], plan: reset_node_plan.yml's _cassandra_node_reset_plan
    ({stop, disable, delete: [paths], dirs: [lines]}). A node with nothing to
    delete but a Cassandra to stop or disable gets a "reset" warning instead.
    Output: [{label, real_run, text}] for cassandra_screen. e.g. [{name: node7, plan: {stop: True, delete: [2
    paths], dirs: [...]}}, {name: node8, plan: {disable: True}}] -> [{label: "data loss", real_run: True, text: ["node7:
    Cassandra stopped, then 2 entries DELETED for good (no snapshot, no backup), in:", {pre: [...]}]}, {label: "reset",
    real_run: True, text: "node8: kept from starting at boot first (nothing to delete)"}]."""
    warnings = []
    for item in plans or []:
        plan = item.get("plan") or {}
        acts = (["Cassandra stopped"] if _true(plan.get("stop")) else []) \
            + (["kept from starting at boot"] if _true(plan.get("disable")) else [])
        if plan.get("delete"):
            text = "%s: %s%d entries DELETED for good (no snapshot, no backup), in:" % (
                item["name"], ", ".join(acts) + ", then " if acts else "", len(plan["delete"]))
            warnings.append({"label": "data loss", "real_run": True, "text": [text, {"pre": plan.get("dirs") or []}]})
        elif acts:
            warnings.append({"label": "reset", "real_run": True,
                             "text": "%s: %s first (nothing to delete)" % (item["name"], ", ".join(acts))})
    return warnings


def _true(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def _ring_members(ring, nodes):
    """cluster_status, the group's nodes -> {dc: [(name or address, rack, entry)]}: the ring, its nodes named by
    the inventory; the inventory's nodes (no entry) without a ring."""
    by_address = dict((n.get("address"), n["name"]) for n in nodes)
    members = {}
    for dc, info in ring.items():
        for entry in (info or {}).get("nodes") or []:
            members.setdefault(dc, []).append((by_address.get(entry.get("address"), entry.get("address")),
                                               entry.get("rack"), entry))
    if not ring:
        for n in nodes:
            members.setdefault(n["dc"], []).append((n["name"], n["rack"], {}))
    return members


def _load_said(seen, ring, peer):
    """A node's ring entries, the ring, the node it is seen from -> (the lines of its load and share, its load in
    short for the step: "load 1.2 GiB, owns 33.3% ", '' when unknown)."""
    if seen and seen[0]:
        owns = seen[0].get("owns") or "?"
        return (["load %s, %s" % (seen[0].get("load") or "?",
                                  ("owns " + owns) if owns != "?" else "share unknown (the keyspaces replicate differently)")],
                "load %s%s " % (seen[0].get("load") or "?", (", owns " + owns) if owns != "?" else ""))
    if ring:
        return ["not in the ring as %s sees it" % (peer or "the node that stays")], ""
    return [], ""


def _data_goes(name, dc, rack, gone, members, keyspaces, simple):
    """A node to remove, its dc and rack, the nodes removed before it, the ring per dc, cassandra_keyspaces, the
    SimpleStrategy keyspaces -> (the nodes its data goes to, whether by rack: one replica per rack, the line
    that says where it goes, whether a SimpleStrategy keyspace sends it to any node)."""
    receivers = [(m, r) for m, r, e in members.get(dc, []) if m != name and m not in gone]
    racks = sorted(set(r for m, r in receivers) | set([rack]))
    rfs = set(v["rf"][dc] for v in keyspaces.values() if dc in v.get("rf", {}))
    same_rack = [m for m, r in receivers if r == rack]
    by_rack = len(racks) > 1 and rfs == set([len(racks)]) and bool(same_rack)
    if by_rack:
        names = same_rack
        where = "data goes to the other nodes of %s (%d racks in %s, its replication factor): %s" % (
            rack, len(racks), dc, ", ".join(names))
    else:
        names = [m for m, r in receivers]
        where = "data goes to the other nodes of %s: %s" % (dc, ", ".join(names) or "none")
    # SimpleStrategy ignores racks and datacenters
    any_node = bool(simple) and (by_rack or len(members) > 1)
    if any_node:
        where += "; SimpleStrategy keyspaces (%s): any node of the cluster" % ", ".join(simple)
    return names, by_rack, where, any_node


def _leaving_block(index, item, order, by_name, members, ring, keyspaces, simple, peer):
    """The index-th node to remove ({name, state}), the run's order, the nodes by name, the ring per dc, the ring,
    cassandra_keyspaces, the SimpleStrategy keyspaces, the node that stays -> its block {title, lines, step}."""
    name, state = item["name"], item.get("state") or "normal"
    node = by_name.get(name, {"dc": "?", "rack": "?", "address": "?"})
    dc, rack = node.get("dc"), node.get("rack")
    title = cassandra_screen_title(name, {"address": node.get("address"), "cassandra_dc": dc, "cassandra_rack": rack})
    lines = ["a seed until now: the other nodes' seed lists drop it first (cassandra_seeds, live)"
             if node.get("seed") else "not a seed"]
    # the same, on one line (the plan of topology)
    step = {"node": name, "dc": dc, "rack": rack, "text": ""}
    if state == "decommissioned":
        lines.append("already out of the ring (an earlier run): Cassandra only stopped and disabled on it")
        step["text"] = "already out of the ring (an earlier run): Cassandra stopped and disabled only"
        return {"title": title, "lines": lines, "step": step, "row": step["text"]}
    short = []
    if state == "leaving":
        lines.append("still leaving (a decommission an earlier run started): waited for")
        short.append("still leaving (an earlier run): waited for")
    load_lines, load = _load_said([e for m, r, e in members.get(dc, []) if m == name], ring, peer)
    lines += load_lines
    names, by_rack, where, any_node = _data_goes(name, dc, rack, order[:index], members, keyspaces, simple)
    lines.append(where)
    receivers_text = out.nodes(names) or "none (no node left in %s)" % dc
    short.append("%s-> %s%s" % (load, receivers_text, (" (the other nodes of %s)" % rack) if by_rack else ""))
    if any_node:
        short.append("SimpleStrategy keyspaces: any node")
    passed_on = [m for m in names if m in order[index + 1:]]
    if passed_on:
        lines.append("%s %s removed later and hand%s this data on again" % (
            ", ".join(passed_on), "is" if len(passed_on) == 1 else "are", "s" if len(passed_on) == 1 else ""))
        short.append("%s removed later" % out.nodes(passed_on))
    step["text"] = "; ".join(short)
    # a decommission an earlier run started is only followed
    run = ("followed on %s" if state == "leaving" else "runs on %s (nodetool decommission)") % name
    lines.append("%s, the ring checked from %s before and after" % (run, peer or "another node"))
    lines.append("end state: out of the ring, Cassandra stopped and disabled, its data left on disk")
    row = step["text"] + ("; a seed until now, dropped from the seed lists first" if node.get("seed") else "")
    return {"title": title, "lines": lines, "step": step, "row": row}


def _decommission_after(order, by_name, members):
    """The nodes to remove, the nodes by name, the ring per dc -> what each datacenter keeps, then what is left
    to do by hand."""
    after = []
    for dc in sorted(set(by_name[n]["dc"] for n in order if n in by_name)):
        left = [m for m, r, e in members.get(dc, []) if m not in order]
        after.append("Afterwards %s keeps %d node%s: %s." % (dc, len(left), "" if len(left) == 1 else "s", ", ".join(left))
                     if left else "Afterwards %s has no node left." % dc)
    after.append("Then empty %s (reset_node) before reusing the host%s, and re-import the cluster to drop %s from"
                 " the inventory: the commands are printed at the end."
                 % (", ".join(order), "" if len(order) == 1 else "s", "it" if len(order) == 1 else "them"))
    return after


def cassandra_decommission_screen(leaving, nodes, ring=None, keyspaces=None, peer="", replication_problems=None):
    """leaving: [{name, state (normal, leaving, decommissioned)}] in the order
    of the run; nodes: [{name, address, dc, rack, seed}] for every node of the
    group, seed: in the seed lists the nodes run with (dropped before it leaves); ring: cassandra_status' cluster_status, read from a node that
    stays; keyspaces: cassandra_keyspaces; peer: the node that stays, which
    the ring is checked from; replication_problems: the ones cassandra_decommission_force
    lets through.
    Output: a cassandra_screen spec {operation, summary, intro, blocks: [{title, lines, step}], after, warnings}.
    e.g. ([{name: node3, state: normal}], node1 to node3 of dc1/r1, ring: <their loads>, peer="node1") -> {summary:
    "remove node3", blocks: [{title: "node3  10.0.0.3  dc1 / r1", lines: ["not a seed", "load 1 GiB, owns 33.3%", "data
    goes to the other nodes of dc1: node1, node2", ...], step: {node: node3, dc: dc1, rack: r1, text: "load 1 GiB, owns
    33.3% -> node1, node2"}}], after: ["Afterwards dc1 keeps 2 nodes: node1, node2.", ...], ...}."""
    ring = ring or {}
    keyspaces = keyspaces or {}
    by_name = dict((n["name"], n) for n in nodes)
    order = [n["name"] for n in leaving]
    # the ring per dc: [(name or address, rack, entry)]; the inventory without one
    members = _ring_members(ring, nodes)
    simple = sorted(k for k, v in keyspaces.items() if "*" in v.get("rf", {}) and v["rf"]["*"] > 0)
    blocks = [_leaving_block(index, item, order, by_name, members, ring, keyspaces, simple, peer)
              for index, item in enumerate(leaving)]

    if len(order) == 1:
        intro = "One node to remove: %s. It streams its data to the nodes that stay (hours on a big node), then" \
                " Cassandra is stopped and disabled on it." % order[0]
    else:
        intro = "%d nodes to remove, one after the other: first %s, then %s. Each one streams its data to the" \
                " nodes that stay (hours on a big node), then Cassandra is stopped and disabled on it." % (
                    len(order), order[0], ", then ".join(order[1:]))

    warnings = []
    if replication_problems:
        warnings.append({"label": "replication",
                         "text": ["cassandra_decommission_force is true: the removal goes on although too few nodes are"
                                  " left for some keyspaces; those replicas are lost and QUORUM can fail:"]
                         + list(replication_problems)})
    return {"operation": "decommission_node", "summary": "remove %s" % ", ".join(order), "intro": [intro], "blocks": blocks,
            "after": _decommission_after(order, by_name, members), "warnings": warnings}


_SEED = "in cassandra_seeds: joins as a regular node, made a seed once up"


def _medusa_fqdn(name, v):
    if "cassandra_medusa_fqdn" in v:
        fqdn = v["cassandra_medusa_fqdn"]
    elif v.get("cassandra_medusa_fqdn_domain", ""):
        fqdn = "%s.%s" % ((v.get("ansible_facts") or {}).get("hostname", name), v["cassandra_medusa_fqdn_domain"])
    else:
        fqdn = ""
    return fqdn or "(Medusa works it out)"


def cassandra_add_node_blocks(new, hostvars, estimate):
    """new: the hosts to add; hostvars; estimate: cassandra_add_node_plan's ({host, bytes, basis}). -> [{title,
    lines, row}]: each new node's title and what the screen says of it, as a row of the node table (the data to
    stream, a seed made one once up, its Medusa fqdn, its reset)."""
    blocks = []
    for name in new:
        v = hostvars[name]
        lines = ["about %s to stream" % out.size(e["bytes"]) for e in estimate if e["host"] == name]
        if boolean(v.get("_cassandra_service_is_seed", False), strict=False):
            lines.append(_SEED)
        if boolean(v.get("cassandra_medusa_enabled", False), strict=False):
            lines.append("Medusa fqdn " + _medusa_fqdn(name, v))
        reset = (v.get("_cassandra_node_reset_plan") or {}).get("line", "")
        if reset:
            lines.append(reset)
        blocks.append({"title": cassandra_screen_title(name, v["_cassandra_preflight"]), "lines": lines,
                       "row": "; ".join(lines)})
    return blocks


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_screen": cassandra_screen,
            "cassandra_screen_title": cassandra_screen_title,
            "cassandra_decommission_screen": cassandra_decommission_screen,
            "cassandra_reset_warnings": cassandra_reset_warnings,
            "cassandra_add_node_blocks": cassandra_add_node_blocks,
        }
