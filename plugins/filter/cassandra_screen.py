# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The screen an operation playbook shows before it changes anything.

cassandra_screen: spec -> the text, the same layout for every operation:
    a header line (operation, cluster, Cassandra version, what it does), the
    intro, one block per node, what comes after, the warnings together, each
    one labelled ("WARNING - replication: ..."), then, in the question's
    place, what --check or cassandra_operation_confirm false means for this
    run. One blank line between two blocks, none for an empty one.
    spec: {operation, cluster, version, summary: str,
           intro: [item], blocks: [{title, lines: [item]}], sections: [[item]]
           (each one a block of its own, not indented), after: [item],
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
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import textwrap

from collections.abc import Mapping

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


def _items(items, indent="", hang="", paragraphs=False):
    """paragraphs: each item a paragraph (a blank line between two), the
    item after one ending with ":" (what it introduces) in the same one."""
    parts, leads = [], False
    for item in items or []:
        if isinstance(item, Mapping):
            lines = [indent + str(line) for line in item.get("pre") or []]
        elif str(item).strip():
            lines = _wrap(item, indent, indent + hang)
        else:
            continue
        if not lines:
            continue  # an empty table: the lead-in keeps what comes next
        if parts and (leads or not paragraphs):
            parts[-1] = parts[-1] + lines
        else:
            parts.append(lines)
        leads = lines[-1].rstrip().endswith(":")
    return out.spaced(parts)


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
    cassandra_operation_confirm is false (said at the end); session: the
    tmux/screen warning, if any; asked_by: the playbook that showed the
    whole plan and asked already (topology), said at the end.
    Input: spec (see the module's doc); output: the text, one string. e.g. ({operation: reset_node, cluster:
    my_cluster, summary: "empty node7", blocks: [{title: "node7  10.0.0.7", lines: ["Cassandra stopped"]}], warnings:
    [{label: "data loss", text: "node7: 12 entries DELETED", real_run: True}]}) -> the lines "reset_node on cluster
    'my_cluster': empty node7", "", "  node7  10.0.0.7", "    Cassandra stopped", "", "WARNING - data loss: node7: 12
    entries DELETED", joined; under check, "(A real run would also warn about: data loss.)" and the --check line
    instead of the warning."""
    spec = spec or {}
    header = str(spec.get("operation") or "")
    if spec.get("cluster"):
        header += " on cluster '%s'" % spec["cluster"]
    if spec.get("version"):
        header += " (Cassandra %s)" % spec["version"]
    if spec.get("summary"):
        header += ": %s" % spec["summary"]
    sections = [_wrap(header, "", "  ")]
    sections.append(_items(spec.get("intro"), paragraphs=True))
    for block in spec.get("blocks") or []:
        sections.append(_wrap(block.get("title") or "", "  ", "    ") + _items(block.get("lines"), "    ", "  "))
    for section in spec.get("sections") or []:
        sections.append(_items(section, paragraphs=True))
    sections.append(_items(spec.get("after"), paragraphs=True))

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
    skipped, warned = [], []
    for warning in warnings:
        if check and warning.get("real_run"):
            if warning.get("label") not in skipped:
                skipped.append(warning.get("label"))
            continue
        warned.append(_warning(warning))
    if skipped:
        warned.append(["(A real run would also warn about: %s.)" % ", ".join(str(s) for s in skipped)])
    sections.append(out.spaced(warned))  # the warnings together, a blank line apart, just above the question
    # in the question's place (screen.yml asks it right below)
    if check:
        sections.append(["--check: nothing will be changed (the plan only, no question)."])
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


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_screen": cassandra_screen,
            "cassandra_screen_title": cassandra_screen_title,
        }
