# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The operator output of the collection's filters, written once: every
function returns text lines (or a string) that a debug task prints, nothing
else. Pure Python, no Ansible.

- sizes, durations, counts: size, parse_size, duration, count, plural;
- node lists: nodes (node1..node5, node7), full_list;
- warnings: warning, warning_lines (each once, a blank line between two),
  spaced paragraphs;
- phase lines: phase, check_what;
- what is left to do: command, extra_var, todo;
- the seed rule of preflight: seed_layout."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import os
import re
import shlex
ARROW = u"←"  # the marker of a line that needs a look: "<- down", "<- differs"
# A blank line between two paragraphs of one block: a single space (the
# default callback's yaml format strips it). An empty line "" between two
# blocks of a plan, a screen or a recap is a rule under the ops callback;
# this one stays blank there.
PARAGRAPH = " "
GIB = 1024.0 ** 3
_UNITS = (("TiB", 1024.0 ** 4), ("GiB", GIB), ("MiB", 1024.0 ** 2), ("KiB", 1024.0))

# --- sizes, rates, durations ---------------------------------------------------------------


def _unit(count):
    """The unit count reads at least 1 in: (name, factor), ("B", 1) under
    1 KiB; the larger one when the smaller would round up to 1024.0."""
    for unit, factor in _UNITS:
        if count >= factor or float("%.1f" % (count * 1024 / factor)) >= 1024.0:
            return unit, factor
    return "B", 1


def _figure(count, factor):
    """count in a unit of factor bytes, without the unit: "305.1", "0.0",
    "<0.1" for a non-zero count that would read 0.0, "512" in bytes."""
    if factor == 1:
        return "%d" % count
    text = "%.1f" % (count / factor)
    return "<0.1" if count > 0 and text == "0.0" else text


def size(count, scale=None):
    """count bytes, one decimal: "41.2 GiB", "260.6 MiB", "0 B", in the
    unit it reads at least 1.0 in; scale: the bytes whose unit to use
    instead (the larger size next to it). A non-zero count never reads
    0.0: "<0.1 GiB". A done/total pair goes through amount or sizes."""
    count = max(0.0, float(count or 0))
    unit, factor = _unit(float(scale) if scale else count)
    return "%s %s" % (_figure(count, factor), unit)


_SIZE = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(bytes|B|KiB|KB|MiB|MB|GiB|GB|TiB|TB)\s*$")
_FACTORS = {"bytes": 1, "B": 1, "KiB": 1024, "KB": 1024, "MiB": 1024 ** 2, "MB": 1024 ** 2,
            "GiB": 1024 ** 3, "GB": 1024 ** 3, "TiB": 1024 ** 4, "TB": 1024 ** 4}


def parse_size(text):
    """A size as nodetool prints it ("412.3 GiB", "1,5 GiB" in some locales,
    "100 bytes") in bytes (float), None when unknown ("?", "")."""
    match = _SIZE.match(str(text or ""))
    if not match:
        return None
    return float(match.group(1).replace(",", ".")) * _FACTORS[match.group(2)]


def duration(seconds, short=False):
    """"45s", "9m20s", "1h12m", "2d04h"; short: minutes at most under an
    hour ("9m"), for an ETA."""
    seconds = int(max(0, seconds or 0))
    if seconds >= 86400:
        return "%dd%02dh" % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    if seconds >= 60:
        return ("%dm" % (seconds // 60)) if short else "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%ds" % seconds


def count(number):
    """1240 as "1 240"."""
    return "{0:,}".format(int(number)).replace(",", " ")


def plural(number, word, many=None):
    """"1 node", "3 nodes"; many: the plural when it is not word + s."""
    return "%d %s" % (number, word if number == 1 else (many or word + "s"))


# --- node lists ------------------------------------------------------------------------------

def _natural(name):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(name))]


def _follows(before, name):
    """The index of the one number that goes up by 1 from before to name, the
    rest the same (node9 -> node10, node09 -> node10, rack1-n3 -> rack1-n4),
    else None."""
    a, b = re.split(r"(\d+)", before), re.split(r"(\d+)", name)
    if len(a) != len(b):
        return None
    changed = [i for i in range(len(a)) if a[i] != b[i]]
    if len(changed) != 1 or changed[0] % 2 == 0:
        return None
    x, y = a[changed[0]], b[changed[0]]
    padded = x.startswith("0") or y.startswith("0")
    if int(y) != int(x) + 1 or (padded and len(x) != len(y)):
        return None
    return changed[0]


def full_list(names, keep_order=False):
    """Every name, ", " between them (an import report, -v)."""
    names = [str(n) for n in names or []]
    return ", ".join(names if keep_order else sorted(names, key=_natural))


def nodes(names, keep_order=False, full=False):
    """Node names, short: 3 or more consecutive numbered names as a range
    (node1..node5), 2 listed (node1, node2), a gap breaks the range (never a
    node that is not there), "6 nodes: " first when there are more than 5.
    Sorted naturally unless keep_order (a run's order: ranges only where the
    order follows the numbers). full: every name (see full_list)."""
    names = [str(n) for n in names or []]
    if full:
        return full_list(names, keep_order)
    names = names if keep_order else sorted(set(names), key=_natural)
    text = _ranges(names)
    return ("%d nodes: %s" % (len(names), text)) if len(names) > 5 else text


def _ranges(names):
    """Names in the order given, 3 or more consecutive numbered ones as a range."""
    parts, run, place = [], [], None

    def flush():
        parts.extend(["%s..%s" % (run[0], run[-1])] if len(run) >= 3 else run)
        del run[:]

    for name in names:
        step = _follows(run[-1], name) if run else None
        if step is not None and (len(run) == 1 or step == place):
            run.append(name)
            place = step
            continue
        flush()
        run.append(name)
        place = None
    flush()
    return ", ".join(parts)


# --- secrets -------------------------------------------------------------------------------

# --- a setting and its value per node ------------------------------------------------------


# --- node groups -------------------------------------------------------------------------

# --- plan ----------------------------------------------------------------------------------


# --- warnings --------------------------------------------------------------------------------

# The one mark of a warning, everywhere: "WARNING - what"; the lines of a
# longer one under it, indented
WARNING = "WARNING - "
_MARKED = re.compile(r"^\s*WARNING(?:\s+-\s+|:\s*|\s+)")


def is_warning(line):
    """Whether line is a warning (WARNING - ..., its older spellings too)."""
    return bool(_MARKED.match(str(line)))


def warning_text(line):
    """A warning without its mark ("WARNING - x" -> "x"); another line as is."""
    return _MARKED.sub("", str(line), count=1) if is_warning(line) else str(line)


def warning(text, indent="  ", under=False):
    """A warning's lines: "WARNING - text", its next lines (text a list, or
    lines in it) indented; under: every next line under its text (a plan's,
    their own indent kept after it)."""
    lines = [str(t) for t in text] if isinstance(text, (list, tuple)) else str(text).split("\n")
    lines = [line.rstrip() for line in lines if line.strip()]
    if not lines:
        return []
    if under:
        rest = [" " * len(WARNING) + line for line in lines[1:]]
    else:
        rest = [line if line[:1] in (" ", "\t") else indent + line for line in lines[1:]]
    return [WARNING + warning_text(lines[0]).strip()] + rest


def warning_lines(texts, seen=None, under=False):
    """Several warnings: each one once (seen: the texts already shown, kept
    up to date), a blank line (PARAGRAPH) between two; under: see warning()."""
    seen = set() if seen is None else seen
    blocks = []
    for text in texts or []:
        lines = warning(text, under=under)
        key = "\n".join(lines)
        if lines and key not in seen:
            seen.add(key)
            blocks.append(lines)
    return spaced(blocks)


def spaced(paragraphs):
    """Paragraphs (each a list of lines) as one block, a PARAGRAPH line
    between two, the empty ones left out."""
    lines = []
    for paragraph in paragraphs:
        if paragraph:
            lines += ([PARAGRAPH] if lines else []) + list(paragraph)
    return lines


# --- phases --------------------------------------------------------------------------------

# What a run does now, a line per major phase, ending with "..." (the ops
# callback's phase colour). {nodes}: the node list (nodes()), {count}: "5
# nodes", {node}, {what}: as given.
PHASES = {
    "check": "Checking the cluster ({what})...",  # what: check_what()
    "check_new": "Checking the new nodes ({nodes})...",
    "check_leaving": "Checking the nodes to remove ({nodes})...",
    "ring": "Reading the ring and seeds...",
    "prepare": "Preparing the new nodes ({nodes})...",
    "prepare_cluster": "Preparing the nodes ({nodes})...",
    "join": "Starting {node} and waiting for its bootstrap...",
    "join_again": "Waiting for the bootstrap of {node} (started by an earlier run)...",
    "join_no_stream": "Starting {node}, joining without streaming (auto_bootstrap false)...",
    "replace": "Starting {node} and waiting while it replaces {what}...",
    "decommission": "Decommissioning {node}...",
    "leave_again": "Waiting for the decommission of {node} (started by an earlier run)...",
    "move": "Moving {node}...",
    "reboot": "Rebooting {node}...",
    "update_java": "Updating Java on {node}...",
    "sync_clock": "Setting the clock of {node} (drained, stopped, started again)...",
    "upgrade": "Upgrading {node}...",
    "upgradesstables": "Upgrading the SSTables of {node}...",
    "wait_un": "Waiting for every node to be UN...",
    "seeds": "Applying cassandra_seeds on every node...",
    "compare_config": "Comparing the config with the inventory ({count})...",
    "write_config": "Writing the config ({nodes})...",
    "restart": "Restarting {node}...",
    "restart_rack": "Restarting {what} ({nodes})...",
    "cleanup": "Cleaning up {nodes}...",
    "read_reset": "Reading the data directories of {nodes}...",
    "reset": "Resetting {nodes}...",
    "import_given": "Reading the ring from {nodes}...",
}


def phase(name, names=None, node="", what="", count=None):
    """The phase line PHASES[name], e.g. "Decommissioning node3...". names:
    the nodes (one or a list, shown as nodes() does, in the order given);
    count: how many nodes (the number of names when not given). "" for a
    name PHASES does not have (a step that says nothing)."""
    if name not in PHASES:
        return ""
    names = [names] if isinstance(names, str) else list(names or [])
    number = len(names) if count is None else int(count)
    return PHASES[name].format(nodes=nodes(names, keep_order=True), node=node, what=what, count=plural(number, "node"))


def check_what(present, kind="", targets=None, replacing="", run=None, absent=None):
    """What preflight's check line says of the cluster: its members and the
    change the run makes, "3 nodes; adding node4, node5". present: the
    cluster's hosts not marked absent (lookup cassandra_nodes, the whole
    group, inventory order); kind: what the playbook does with targets
    (_cassandra_preflight_target: add, leave, reset, dead, canary or none;
    topology: plan); targets: the hosts cassandra_target_nodes names (dead: the
    dead node as given); replacing: cassandra_replace_address (replace_node);
    run: the run's hosts, said when --limit leaves some out; absent: the hosts
    marked absent (topology). A host being added or reset is not a member yet,
    one being removed (absent or not) still is; a dead node, out of the
    inventory (or marked absent), is not counted. topology's plan comes from the
    ring, read later: the inventory then."""
    present, targets, absent = list(present or []), list(targets or []), list(absent or [])
    if kind == "plan":
        text = plural(len(present), "node") + " in the inventory"
        if absent:
            text += ", %s marked absent" % nodes(absent, keep_order=True)
        whole = present
    else:
        named = [] if kind == "dead" else targets
        if kind in ("add", "reset"):
            members = [h for h in present if h not in named]
        elif kind == "leave":
            members = present + [h for h in named if h not in present]
        else:
            members = present
        whole = members + [h for h in named if h not in members]
        text = plural(len(members), "node")
        listed = nodes(named, keep_order=True)
        if kind == "add" and named and replacing:
            text += "; replacing %s with %s" % (replacing, listed)
        elif kind == "add" and named:
            text += "; adding " + listed
        elif kind == "leave" and named:
            text += "; removing " + listed
        elif kind == "reset" and named:
            text += "; resetting " + listed
        elif kind == "dead" and targets:
            text += "; removing the dead node %s" % targets[0]
    if run is not None and set(run) != set(whole):
        text += "; --limit: " + nodes(run, keep_order=True)
    return text


# --- the prepare of new nodes ---------------------------------------------------------------

# --- progress ------------------------------------------------------------------------------

# --- end of run ----------------------------------------------------------------------------

# --- what is left to do --------------------------------------------------------------------


def path_from(path, cwd):
    """path relative to cwd when it is under it, else as given."""
    rel = os.path.relpath(path, cwd) if cwd and os.path.isabs(path) else path
    return path if rel.startswith("..") else rel


def extra_var(name, value):
    """-e name=value as the shell and Ansible's key=value parsing both take it (else as JSON)."""
    value = str(value)
    if re.match(r"^[\w.:/@,+-]+$", value):
        return "-e %s=%s" % (name, value)
    return "-e " + shlex.quote(json.dumps({name: value}))


def command(playbook, inventory=None, hosts=None, limit=None, extra=None, options=None, cwd=None):
    """The full command of a collection playbook, ready to paste:
    "ansible-playbook -i inventory.yml community.cassandra.cleanup -e
    cassandra_hosts=my_cluster --limit node1,node2 -e k=v". inventory: the
    sources (a list or a string; none: the -i is left out, ansible.cfg's
    is used); hosts: the cluster's group (none: left out, the playbooks find
    it); limit: the hosts, every one named (no range); extra: {name: value}
    or a list of ready arguments; options: other arguments as given
    (--vault-id...); cwd: paths shown relative to it."""
    sources = [inventory] if isinstance(inventory, str) else list(inventory or [])
    parts = ["ansible-playbook"] + ["-i %s" % shlex.quote(path_from(s, cwd)) for s in sources if s]
    parts += [str(o) for o in options or []]
    name = playbook if "." in playbook else "community.cassandra." + playbook
    parts.append(name)
    if hosts:
        parts.append(extra_var("cassandra_hosts", hosts))
    if limit:
        names = [limit] if isinstance(limit, str) else list(limit)
        parts.append("--limit " + shlex.quote(",".join(str(n) for n in names)))
    if isinstance(extra, dict):
        parts += [extra_var(k, extra[k]) for k in extra]
    else:
        parts += [str(e) for e in extra or []]
    return " ".join(parts)


def todo(items, title="TO DO"):
    """items: a string, or {text, command} (the command on its own line under
    it) -> "TO DO", then "  1. text" numbered; nothing when no item."""
    entries = []
    for item in items or []:
        if not item:
            continue
        text, cmd = (item.get("text", ""), item.get("command", "")) if isinstance(item, dict) else (str(item), "")
        entry = ["  %d. %s%s" % (len(entries) + 1, text, ":" if cmd and text and not text.endswith(":") else "")]
        entries.append(entry + (["     " + cmd] if cmd else []))
    # an item on several lines: a blank line between two
    lines = spaced(entries) if any(len(e) > 1 for e in entries) else [line for e in entries for line in e]
    return [title] + lines if lines else []


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
    (rack_b)  ok", a warning's "WARNING - Seeds  dc2  ..."), dcs: [{dc, level
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
