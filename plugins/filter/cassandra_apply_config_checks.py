# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What apply_config finds besides the config files (plugin_utils cassandra_apply_config_coverage: the
categories).

cassandra_apply_config_version: the inventory's version and the one a node runs -> the change, if any, and its
    category (an upgrade: a procedure of its own; older: refused).
cassandra_apply_config_ring: the nodes of the run, the hosts marked absent, the ring -> the hosts topology has to add
    or remove (a procedure of its own), the ring's nodes the inventory does not have (said).
cassandra_apply_config_token: a joined node's inventory token, its cassandra.yaml one and the ring's -> why it is
    refused ('' when it is not).
cassandra_apply_config_unit: what the running Cassandra got from its unit and what the role's unit gives -> the
    differences (a restart for them).
cassandra_apply_config_stops: the nodes' stops -> the lines of the refusal (why, then the commands).
cassandra_described_versions: nodetool describecluster's output -> {address: version}.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.module_utils.parsing.convert_bool import boolean
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_unit_environment
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as output
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_patch_notes as patch_notes
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_apply_config_coverage import (
    PROCEDURE, RESTART)

# The category of a version change, by its kind: a newer release of the same series is a package and a restart (B:
# in the node's one restart, plugin_utils cassandra_patch_notes says what it refuses to start with); a new series is
# an upgrade (sstables, phases: C); an older release a procedure too (Cassandra does not go back by itself: C)
VERSION_CATEGORY = {"series": PROCEDURE, "release": RESTART, "older": PROCEDURE}


def cassandra_apply_config_version(series, package_version, running, settings=None, allow_unknown=False):
    """The inventory's series (cassandra_version, '50x'), its version (cassandra_package_version, '' for any of the
    series) and the version the node runs ('' unknown: nothing said) -> {} when it is the same, else {kind: series
    (another series), release (a newer release of the series), older (below the one it runs), category
    (VERSION_CATEGORY), running, wanted}; a release also has refused (why the new release would not start with the
    node's settings: cassandra_patch_notes.refusals, settings as it takes them), unknown (newer than the notes know,
    and not allow_unknown: -e cassandra_allow_unknown_patch=true) and notes (the releases crossed).
    e.g. ("50x", "", "4.1.5") -> {kind: "series", category: "C", running: "4.1.5", wanted: "5.0"};
    ("41x", "4.1.12", "4.1.11") -> {kind: "release", category: "B", running: "4.1.11", wanted: "4.1.12", refused: [],
    unknown: False, notes: []}."""
    now = patch_notes.numbers(running, partial=True)
    if not now:
        return {}
    want = patch_notes.numbers(package_version, partial=True)
    m = re.match(r"^(\d)(\d)x$", str(series or ""))
    wanted_series = (int(m.group(1)), int(m.group(2))) if m else None
    shown = str(package_version).strip() if want else ("%d.%d" % wanted_series if wanted_series else str(series))
    kind = ""
    if want and want < now or (not want and wanted_series and wanted_series < now[:2]):
        kind = "older"
    elif wanted_series and wanted_series != now[:2]:
        kind = "series"
    elif want and want != now:
        kind = "release"
    if not kind:
        return {}
    out = {"kind": kind, "category": VERSION_CATEGORY[kind], "running": str(running).strip(), "wanted": shown}
    if kind == "release":
        out.update(refused=patch_notes.refusals(series, shown, settings or {}),
                   unknown=not patch_notes.known(series, shown) and not boolean(allow_unknown, strict=False),
                   notes=patch_notes.notes(series, running, shown))
    return out


def cassandra_described_versions(text):
    """nodetool describecluster's output -> {address: version} from its "Database versions:" section
    ("    4.1.5: [10.0.0.1:7000, 10.0.0.2:7000]"); {} when it has none (an older nodetool)."""
    section = (str(text or "") + "\n").split("Database versions:", 1)
    out = {}
    if len(section) < 2:
        return out
    for line in section[1].split("\n\n")[0].splitlines():
        m = re.match(r"^\s*([0-9][^:\s]*):\s*\[(.*)\]\s*$", line)
        if not m:
            continue
        for node in m.group(2).split(","):
            node = node.strip()  # (an address with its port: "10.0.0.1:7000", "[2001:db8::1]:7000")
            address = node.rsplit(":", 1)[0] if node.count(":") == 1 or node.startswith("[") else node
            address = address.strip("[]")
            if address:
                out[address] = m.group(1)
    return out


def cassandra_apply_config_ring(nodes, absent, known, ring):
    """nodes: the nodes of the run [{name, address}] (address: the one the ring shows); absent: the hosts marked
    cassandra_node_state: absent [{name, addresses}] (any address it may have); known: every address of the
    inventory's hosts; ring: cassandra_status' cluster_status (none: no node running, nothing judged) ->
    {add: the nodes not in the ring, remove: the hosts marked absent still in it, unknown: the ring's addresses no
    host has}. Adding or removing a node is topology's (a procedure of its own); a ring node the inventory lacks is
    said, as topology says it (it refuses only when it has steps to do)."""
    if not ring:
        return {"add": [], "remove": [], "unknown": []}
    addresses = set()
    for info in ring.values():
        addresses.update(str(n.get("address")) for n in (info or {}).get("nodes") or [])
    add = [str(n["name"]) for n in nodes or [] if str(n.get("address") or "") not in addresses]
    remove = [str(h["name"]) for h in absent or [] if set(str(a) for a in h.get("addresses") or []) & addresses]
    known = set(str(a) for a in known or [])
    return {"add": add, "remove": remove, "unknown": sorted(a for a in addresses if a not in known)}


def _tokens(value):
    return sorted(t.strip() for t in str(value or "").split(",") if t.strip())


def cassandra_apply_config_token(inventory, live, owned):
    """A joined node: its cassandra_initial_token in the inventory, the one of its cassandra.yaml, the tokens it owns
    in the ring ([]: not in it; None: the ring read and failed) -> '' when apply_config may write it, else why it is
    refused. Cassandra reads
    initial_token at a node's first start only: writing the tokens it owns (after move_node) or nothing new is fine;
    another token would be a wrong record, and moving a node is move_node's."""
    want = _tokens(inventory)
    if not want or want == _tokens(live):
        return ""
    if owned is None:  # the ring read and failed: not known to be the token it owns
        now = ",".join(_tokens(live))
        return ("cassandra_initial_token %s is not the one of its cassandra.yaml (%s), and the ring's tokens could not be"
                " read to tell whether it owns it: Cassandra reads initial_token at a node's first start only. Run"
                " again once nodetool ring answers, or set cassandra_initial_token to %s"
                % (",".join(want), now or "none", now or "''"))
    if not owned or want == _tokens(",".join(owned)):
        return ""
    shown = ", ".join(owned) if len(owned) <= 3 else "%d tokens" % len(owned)
    return ("cassandra_initial_token %s is not the token it owns in the ring (%s): Cassandra reads initial_token at a"
            " node's first start only. To move it: move_node; else set cassandra_initial_token to its token, or remove"
            " it" % (",".join(want), shown))


_LIMITS = {"nofile": "Max open files", "nproc": "Max processes", "memlock": "Max locked memory",
           "as": "Max address space"}
_UNITS = {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}


def _limit(value, in_bytes):
    """A limit as systemd takes it (infinity, 65536, 64K, '1024:2048') or /proc/<pid>/limits shows it (unlimited)
    -> (soft, hard) comparable: 'unlimited' or an integer."""
    def one(v):
        v = str(v).strip()
        if v.lower() in ("infinity", "unlimited"):
            return "unlimited"
        m = re.match(r"^(\d+)([KMGTPE])?$", v, re.I)
        if not m:
            return v
        return int(m.group(1)) * (1024 ** _UNITS[m.group(2).upper()] if m.group(2) and in_bytes else 1)
    parts = str(value).split(":", 1)
    return (one(parts[0]), one(parts[-1]))


def _proc_limits(text):
    """/proc/<pid>/limits -> {name: (soft, hard)} of the limits the unit sets."""
    out = {}
    for key, label in _LIMITS.items():
        m = re.search(r"(?m)^%s\s+(\S+)\s+(\S+)" % re.escape(label), str(text or ""))
        if m:
            out[key] = ("unlimited" if m.group(1) == "unlimited" else int(m.group(1)) if m.group(1).isdigit() else m.group(1),
                        "unlimited" if m.group(2) == "unlimited" else int(m.group(2)) if m.group(2).isdigit() else m.group(2))
    return out


# what bin/cassandra (and cassandra.in.sh) set or extend before the JVM starts: compared as the unit has them
REWRITTEN = ("JVM_OPTS", "CLASSPATH", "CASSANDRA_CONF", "CASSANDRA_HOME")


def _lower(value, cap):
    """The lower of a limit and the cap over it ('unlimited' above any number)."""
    if cap == "unlimited" or not isinstance(cap, int):
        return value
    return cap if value == "unlimited" or (isinstance(value, int) and value > cap) else value


def cassandra_apply_config_unit(running, wanted):
    """What the running Cassandra got from its unit and what the role's unit gives at its next start -> [{item,
    before, after}] of what differs (empty: the unit changes nothing for it, whatever its file says).
    running: {user, group, limits: /proc/<pid>/limits, environment: systemctl show -p Environment --value, environ:
    /proc/<pid>/environ (NUL-separated: the values the JVM runs with, but REWRITTEN's), ceiling:
    /proc/1/limits in a container, the most its systemd may give (a limit above it compared at it)} (read on the
    node; none read: nothing compared); wanted: {user, group, limits: {nofile, nproc, memlock, as} (none: the
    unit sets none, the package's init script), environment: {name: value}}. The environment compared is the unit's
    (what the unit sets), not all of the JVM's (bin/cassandra adds its own)."""
    running, wanted = dict(running or {}), dict(wanted or {})
    if not running:
        return []
    changes = []
    for key in ("user", "group"):
        have, want = str(running.get(key) or ""), str(wanted.get(key) or "")
        if have and want and have != want:
            changes.append({"item": "unit " + ("User" if key == "user" else "Group"), "before": have, "after": want})
    limits = _proc_limits(running.get("limits"))
    ceiling = _proc_limits(running.get("ceiling"))  # (what systemd itself may give: a container's cap)
    for key, value in sorted((wanted.get("limits") or {}).items()):
        if key in limits and value not in (None, ""):
            want = _limit(value, key in ("memlock", "as"))
            capped = tuple(_lower(w, ceiling[key][1]) for w in want) if key in ceiling else want
            if limits[key] not in (want, capped):  # (a privileged container's systemd raises past its own cap)
                shown = "%s" % limits[key][0] if limits[key][0] == limits[key][1] else "%s:%s" % limits[key]
                cap = " (%s: the host's cap)" % ceiling[key][1] if capped != want else ""
                changes.append({"item": "unit Limit%s" % key.upper(), "before": shown, "after": str(value) + cap})
    have = cassandra_unit_environment(running.get("environment") or "")
    want = dict((str(k), str(v)) for k, v in (wanted.get("environment") or {}).items())
    if running.get("environ"):  # the JVM's own: a unit reloaded since its start (daemon-reload) is not what it runs
        jvm = dict(e.split("=", 1) for e in str(running["environ"]).split("\0") if "=" in e)
        for name in set(have) | set(want):
            if name not in REWRITTEN:
                if name in jvm:
                    have[name] = jvm[name]
                else:
                    have.pop(name, None)
    for name in sorted(set(have) | set(want)):
        if have.get(name) != want.get(name):
            changes.append(dict({"item": "unit Environment %s" % name}, **_env_shown(name, have.get(name), want.get(name))))
    return changes


def _env_shown(name, before, after):
    """A unit Environment variable's two values (None: absent) as the plan and the run report show them: a secret
    masked (its name, or one inside its value: JVM_EXTRA_OPTS=-D...Password=), two secrets that differ said so."""
    shown = dict((side, "absent" if value is None else output.shown(name, value))
                 for side, value in (("before", before), ("after", after)))
    if shown["before"] == shown["after"] == output.MASK:
        shown["after"] = output.MASK + " (another)"
    return shown


def cassandra_apply_config_stops(nodes, ring=None, commands=None):
    """nodes: [{name, series, version: cassandra_apply_config_version's, token: cassandra_apply_config_token's}]; ring:
    cassandra_apply_config_ring's; commands: {upgrade, topology_check, topology} (the full command lines) -> the
    lines of the refusal (one fact per line, then the commands, each on a line of its own), [] when nothing stops
    the run. A procedure (C) gives the command to run; an impossible change (D) says why."""
    commands = commands or {}
    lines = []
    by_change = {}
    for n in nodes or []:
        v = n.get("version") or {}
        if v.get("kind"):
            by_change.setdefault((v["kind"], v.get("running"), v.get("wanted")), []).append(n)
    upgrade = unknown = False
    for (kind, running, wanted), group in sorted(by_change.items(), key=lambda x: x[0]):
        names = ", ".join(str(n["name"]) for n in group)
        v = group[0]["version"]
        if kind == "older":
            lines.append("%s: runs Cassandra %s, the inventory asks for %s: Cassandra does not go back to an older"
                         " release by itself (what the newer one wrote may not be read). Set cassandra_package_version"
                         " to %s, the release it runs." % (names, running, wanted, running))
        elif kind == "series":
            lines.append("%s: runs Cassandra %s, the inventory asks for %s: another series is an upgrade, a procedure"
                         " of its own (one node at a time, its own checks)." % (names, running, wanted))
            upgrade = True
        else:
            for why in sorted(set(w for n in group for w in n["version"].get("refused") or [])):
                lines.append("%s: %s (now %s)." % (names, why, running))
            if v.get("unknown"):
                lines.append("%s: Cassandra %s is newer than the releases the collection's notes cover (up to %s): read"
                             " its NEWS.txt (what it refuses to start with, the defaults it changes)."
                             % (names, wanted, patch_notes.KNOWN.get(group[0].get("series"), "?")))
                unknown = True
    ring = ring or {}
    if ring.get("add"):
        lines.append("%s: in the inventory, not in the ring: adding a node is topology's (it bootstraps it)."
                     % ", ".join(ring["add"]))
    if ring.get("remove"):
        lines.append("%s: marked cassandra_node_state: absent, still in the ring: removing a node is topology's (it"
                     " decommissions it)." % ", ".join(ring["remove"]))
    for n in nodes or []:
        if n.get("token"):
            lines.append("%s: %s." % (n["name"], n["token"]))
    if not lines:
        return []
    ways = []
    if upgrade and commands.get("upgrade"):
        ways += ["the upgrade, its first phase (it changes nothing):", "    " + commands["upgrade"]]
    if unknown:
        ways += ["once read, to go on with that release:", "    -e cassandra_allow_unknown_patch=true"]
    if (ring.get("add") or ring.get("remove")) and commands.get("topology"):
        ways += ["the nodes to add or remove, the plan (changing nothing), then the run:",
                 "    " + commands.get("topology_check", ""), "    " + commands["topology"]]
    return lines + ways + ["then run apply_config again."]


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_apply_config_version": cassandra_apply_config_version,
            "cassandra_described_versions": cassandra_described_versions,
            "cassandra_apply_config_ring": cassandra_apply_config_ring,
            "cassandra_apply_config_token": cassandra_apply_config_token,
            "cassandra_apply_config_unit": cassandra_apply_config_unit,
            "cassandra_apply_config_stops": cassandra_apply_config_stops,
        }
