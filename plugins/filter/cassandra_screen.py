# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The screen an operation playbook shows before it changes anything.

cassandra_screen: spec -> the text, the same layout for every operation:
    a header line (operation, cluster, Cassandra version, what it does), what
    --check or cassandra_operation_confirm false means for this run, the
    intro, one block per node, what comes after, then the warnings, each one
    on its own paragraph and labelled ("WARNING - replication: ...").
    spec: {operation, cluster, version, summary: str,
           intro: [item], blocks: [{title, lines: [item]}], after: [item],
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
    it runs and is checked from, and how it ends.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import textwrap

from collections.abc import Mapping

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


def _items(items, indent="", hang=""):
    out = []
    for item in items or []:
        if isinstance(item, Mapping):
            out.extend(indent + str(line) for line in item.get("pre") or [])
        elif str(item).strip():
            out.extend(_wrap(item, indent, indent + hang))
    return out


def _warning(warning):
    text = warning.get("text") or ""
    first, rest = (text[0], text[1:]) if isinstance(text, list) else (text, [])
    out = _wrap("WARNING - %s: %s" % (warning["label"], first), "", "  ")
    for item in rest:
        if isinstance(item, Mapping):
            out.extend(_items([item], "    "))
        elif str(item).strip():
            out.extend(_wrap(item, "  - ", "    "))
    return out


def cassandra_screen(spec, check=False, asks=True, session="", asked_by=""):
    """check: --check; asks: false when a question would be asked but
    cassandra_operation_confirm is false (said under the header); session:
    the tmux/screen warning, if any; asked_by: the playbook that showed the
    whole plan and asked already (topology), said under the header."""
    spec = spec or {}
    header = str(spec.get("operation") or "")
    if spec.get("cluster"):
        header += " on cluster '%s'" % spec["cluster"]
    if spec.get("version"):
        header += " (Cassandra %s)" % spec["version"]
    if spec.get("summary"):
        header += ": %s" % spec["summary"]
    sections = [_wrap(header, "", "  ")]
    if check:
        sections[0].append("--check: nothing will be changed (the plan only, no question).")
    elif asked_by:
        sections[0].append("A step of %s, confirmed on its screen: no question here." % asked_by)
    elif not asks:
        sections[0].append("cassandra_operation_confirm is false: no question, the run goes on.")

    sections.append(_items(spec.get("intro")))
    for block in spec.get("blocks") or []:
        sections.append(_wrap(block.get("title") or "", "  ", "    ") + _items(block.get("lines"), "    ", "  "))
    sections.append(_items(spec.get("after")))

    warnings, seen = [], set()
    given = []
    for warning in spec.get("warnings") or []:  # each: one warning per text, under the same label
        given.extend([dict(warning, text=t) for t in warning["each"]] if "each" in warning else [warning])
    if str(session or "").strip():
        given.append({"label": "session", "text": session, "real_run": True})
    for warning in given:
        key = (warning.get("label"), repr(warning.get("text")))
        if not warning.get("text") or key in seen:
            continue
        seen.add(key)
        warnings.append(warning)
    skipped = []
    for warning in warnings:
        if check and warning.get("real_run"):
            if warning.get("label") not in skipped:
                skipped.append(warning.get("label"))
            continue
        sections.append(_warning(warning))
    if skipped:
        sections.append(["(A real run would also warn about: %s.)" % ", ".join(str(s) for s in skipped)])

    lines = []
    for section in sections:
        if section:
            if lines:
                lines.append("")
            lines.extend(section)
    return "\n".join(lines)


def cassandra_screen_title(name, preflight=None):
    p = preflight or {}
    parts = [str(name), str(p.get("address") or "")]
    if p.get("cassandra_dc"):
        parts.append("%s / %s" % (p["cassandra_dc"], p.get("cassandra_rack") or "?"))
    return "  ".join(x for x in parts if x)


def cassandra_reset_warnings(plans):
    """plans: [{name, plan}], plan: reset_node_plan.yml's _cassandra_node_reset_plan
    ({stop, disable, delete: [paths], dirs: [lines]}). A node with nothing to
    delete but a Cassandra to stop or disable gets a "reset" warning instead."""
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


def cassandra_decommission_screen(leaving, nodes, ring=None, keyspaces=None, peer="", replication_problems=None):
    """leaving: [{name, state (normal, leaving, decommissioned)}] in the order
    of the run; nodes: [{name, address, dc, rack, seed}] for every node of the
    group, seed: in the seed lists the nodes run with (dropped before it leaves); ring: cassandra_status' cluster_status, read from a node that
    stays; keyspaces: cassandra_keyspaces; peer: the node that stays, which
    the ring is checked from; replication_problems: the ones cassandra_decommission_force
    lets through."""
    ring = ring or {}
    keyspaces = keyspaces or {}
    by_name = dict((n["name"], n) for n in nodes)
    by_address = dict((n.get("address"), n["name"]) for n in nodes)
    order = [n["name"] for n in leaving]

    # the ring per dc: [(name or address, rack, entry)]; the inventory without one
    members = {}
    for dc, info in ring.items():
        for entry in (info or {}).get("nodes") or []:
            members.setdefault(dc, []).append((by_address.get(entry.get("address"), entry.get("address")),
                                               entry.get("rack"), entry))
    if not ring:
        for n in nodes:
            members.setdefault(n["dc"], []).append((n["name"], n["rack"], {}))
    simple = sorted(k for k, v in keyspaces.items() if "*" in v.get("rf", {}) and v["rf"]["*"] > 0)

    blocks = []
    for index, item in enumerate(leaving):
        name, state = item["name"], item.get("state") or "normal"
        node = by_name.get(name, {"dc": "?", "rack": "?", "address": "?"})
        dc, rack = node.get("dc"), node.get("rack")
        title = cassandra_screen_title(name, {"address": node.get("address"), "cassandra_dc": dc, "cassandra_rack": rack})
        lines = ["a seed until now: the other nodes' seed lists drop it first (cassandra_seeds, live)"
                 if node.get("seed") else "not a seed"]
        if state == "decommissioned":
            lines.append("already out of the ring (an earlier run): Cassandra only stopped and disabled on it")
            blocks.append({"title": title, "lines": lines})
            continue
        if state == "leaving":
            lines.append("still leaving (a decommission an earlier run started): waited for")
        seen = [e for m, r, e in members.get(dc, []) if m == name]
        if seen and seen[0]:
            owns = seen[0].get("owns") or "?"
            lines.append("load %s, %s" % (seen[0].get("load") or "?",
                                          ("owns " + owns) if owns != "?" else "share unknown (the keyspaces replicate differently)"))
        elif ring:
            lines.append("not in the ring as %s sees it" % (peer or "the node that stays"))
        gone = order[:index]
        later = order[index + 1:]
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
        if simple and (by_rack or len(members) > 1):
            where += "; SimpleStrategy keyspaces (%s): any node of the cluster" % ", ".join(simple)
        lines.append(where)
        passed_on = [m for m in names if m in later]
        if passed_on:
            lines.append("%s %s removed later and hand%s this data on again" % (
                ", ".join(passed_on), "is" if len(passed_on) == 1 else "are", "s" if len(passed_on) == 1 else ""))
        # a decommission an earlier run started is only followed
        run = ("followed on %s" if state == "leaving" else "runs on %s (nodetool decommission)") % name
        lines.append("%s, the ring checked from %s before and after" % (run, peer or "another node"))
        lines.append("end state: out of the ring, Cassandra stopped and disabled, its data left on disk")
        blocks.append({"title": title, "lines": lines})

    if len(order) == 1:
        intro = "One node to remove: %s. It streams its data to the nodes that stay (hours on a big node), then" \
                " Cassandra is stopped and disabled on it." % order[0]
    else:
        intro = "%d nodes to remove, one after the other: first %s, then %s. Each one streams its data to the" \
                " nodes that stay (hours on a big node), then Cassandra is stopped and disabled on it." % (
                    len(order), order[0], ", then ".join(order[1:]))

    after = []
    for dc in sorted(set(by_name[n]["dc"] for n in order if n in by_name)):
        left = [m for m, r, e in members.get(dc, []) if m not in order]
        after.append("Afterwards %s keeps %d node%s: %s." % (dc, len(left), "" if len(left) == 1 else "s", ", ".join(left))
                     if left else "Afterwards %s has no node left." % dc)
    after.append("Then remove %s from the inventory; wipe the data directories before reusing the host%s."
                 % (", ".join(order), "" if len(order) == 1 else "s"))

    warnings = []
    if replication_problems:
        warnings.append({"label": "replication",
                         "text": ["cassandra_decommission_force is true: the removal goes on although too few nodes are"
                                  " left for some keyspaces; those replicas are lost and QUORUM can fail:"]
                         + list(replication_problems)})
    return {"operation": "decommission_node", "summary": "remove %s" % ", ".join(order), "intro": [intro], "blocks": blocks, "after": after,
            "warnings": warnings}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_screen": cassandra_screen,
            "cassandra_screen_title": cassandra_screen_title,
            "cassandra_decommission_screen": cassandra_decommission_screen,
            "cassandra_reset_warnings": cassandra_reset_warnings,
        }
