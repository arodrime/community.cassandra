# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters behind import_cluster's look at the OS tuning of a node, and what cassandra_linux would change there.

cassandra_os_import: what import_cluster's read-only script printed on a node
    (kernel settings, limits, THP, disks, swap, tuned, time sync, firewall),
    and what the roles would set -> the report lines, and the variables that
    give the nodes added later the same tuning.
cassandra_sysctl_write_only: the write-only kernel keys of cassandra_linux_sysctl
    (cassandra_linux's os.yml).
cassandra_sysctl_todo: the keys of cassandra_linux_sysctl that ansible.posix.sysctl
    would change, set or removed (cassandra_linux's os.yml: only they are run).

The script prints one record per line, "kind|field|...": the files as they
are (comments and blank lines left out), and the live values.

Its second half ("what cassandra_linux would change on a node") is the model the import's self-check
(cassandra_import_check) compares the node with: cassandra_linux_changes and cassandra_firewall_changes (what
cassandra_linux and cassandra_firewall would change there), firewall_touched and thp_unit. Those are no filters
(not registered), despite the cassandra_ prefix of the first two: Python only, imported by cassandra_import_check.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import base64
import hashlib
import os
import re

import jinja2

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.filter.cassandra_disks import (
    cassandra_disk_changes, cassandra_disk_tuned_conflict)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_disks import plan

# Read in this order by sysctl --system (and systemd-sysctl): a file name
# found in several dirs is read from the first one, all of them sorted by file
# name, then /etc/sysctl.conf, last
SYSCTL_DIRS = ["/etc/sysctl.d", "/run/sysctl.d", "/usr/local/lib/sysctl.d", "/usr/lib/sysctl.d", "/lib/sysctl.d"]
SYSCTL_CONF = "/etc/sysctl.conf"
LIMITS_CONF = "/etc/security/limits.conf"
LIMITS_OWN = "/etc/security/limits.d/cassandra.conf"  # cassandra_linux's
# set on the node it was found on only (host_vars), not given to nodes added later
NODE_ONLY = ("cassandra_data_disk_tuning",)
# cassandra_linux's (61-: before it also tuned md arrays)
OWN_UDEV_RULES = ("/etc/udev/rules.d/64-cassandra-data-disk.rules", "/etc/udev/rules.d/61-cassandra-data-disk.rules")
# systemd unit setting -> cassandra_service variable
UNIT_LIMITS = [("LimitNOFILE", "nofile"), ("LimitNPROC", "nproc"), ("LimitMEMLOCK", "memlock"), ("LimitAS", "as")]
UNIT_ITEM = dict(UNIT_LIMITS)
PROC_LIMITS = {"Max open files": "nofile", "Max processes": "nproc", "Max locked memory": "memlock",
               "Max address space": "as"}
READAHEAD = re.compile(r'ATTRS?\{(?:queue|bdi)/read_ahead_kb\}\s*=\s*"(\d+)"')
SETRA = re.compile(r"blockdev\s+--setra\s+(\d+)")  # 512-byte sectors
SCHEDULER = re.compile(r'ATTRS?\{queue/scheduler\}\s*=\s*"([\w-]+)"')
# chrony and systemd-timesyncd are what cassandra_linux starts; the others
# conflict with chronyd (its unit stops them)
OWN_TIMESYNC = ("chronyd", "chrony", "systemd-timesyncd")
THP_ARG = re.compile(r"(?:^|\s)transparent_hugepage=(\S+)")


def _records(text):
    out = []
    for line in (text or "").splitlines():
        if "|" in line:
            kind, rest = line.split("|", 1)
            out.append((kind, rest))
    return out


def _fields(rest, n):
    """rest split into n fields, the last one taking what is left."""
    parts = rest.split("|", n - 1)
    return parts + [""] * (n - len(parts))


def _norm(value):
    return " ".join(str(value).split())


def _typed(value):
    """A value as the roles' defaults write it: a number as an int."""
    return int(value) if re.match(r"^(0|[1-9]\d*)$", value) else value


def _vs(value, role, name):
    """ (<name>: <role value>), or (same as <name>)."""
    if role is None:
        return ""
    if _norm(value) == _norm(role):
        return " (same as %s)" % name
    return " (%s: %s)" % (name, _norm(role))


# --- sysctl ---

# the kernel's write-only keys: a trigger (writing it flushes or drops something), never read back, no setting
# (vm.stat_refresh: mode 0600, a read gives nothing, mm/vmstat.c vmstat_refresh; 0600 on EL 9 and 7.0 kernels)
WRITE_ONLY_SYSCTL = ("net.ipv4.route.flush", "net.ipv6.route.flush", "vm.drop_caches", "vm.compact_memory",
                     "vm.stat_refresh")


def _sysctl_key(key):
    key = key.strip()
    # systemd: a key whose first separator is a slash has its slashes and dots swapped
    if "/" in key and ("." not in key or key.index("/") < key.index(".")):
        key = key.translate(str.maketrans("/.", "./"))
    return key


def _sysctl_line(line):
    """(key, value) of a sysctl.d line, or None."""
    line = line.strip()
    if not line or line[0] in "#;" or "=" not in line:
        return None
    if line.startswith("-"):  # "-key = value": errors ignored
        line = line[1:]
    key, value = line.split("=", 1)
    key = _sysctl_key(key)
    if not key or "*" in key:  # a glob (systemd 245+): not followed
        return None
    return key, _norm(value)


def _sysctl_order(paths, link=None, masked=()):
    """The files in the order they are read, a name found in several dirs
    once (the first dir's), a masked name (a link to /dev/null) not at all.
    /etc/sysctl.conf is read at the place of its link in /etc/sysctl.d
    (systemd-sysctl), else last (sysctl --system)."""
    by_name = {}
    for path in paths:
        if path == SYSCTL_CONF:
            continue
        d, name = os.path.split(path)
        rank = SYSCTL_DIRS.index(d) if d in SYSCTL_DIRS else len(SYSCTL_DIRS)
        if name not in by_name or rank < by_name[name][0]:
            by_name[name] = (rank, path)
    if link and SYSCTL_CONF in paths:
        by_name[os.path.basename(link)] = (-1, SYSCTL_CONF)
    order = [by_name[name][1] for name in sorted(by_name) if name not in masked or by_name[name][0] < 0]
    return order + ([SYSCTL_CONF] if SYSCTL_CONF in paths and not link else [])


def _owners(recs):
    """{file under /etc: the package it comes from}."""
    return dict(_fields(rest, 2) for kind, rest in recs if kind == "owner")


def _admin(path, owners):
    """A file of the admin: under /etc, and not a package's (a distro's
    default), except the two files admins traditionally edit."""
    return path.startswith("/etc/") and (path not in owners or path in (SYSCTL_CONF, LIMITS_CONF))


def _label(path, owners):
    return "%s (package %s)" % (path, owners[path]) if path in owners and not _admin(path, owners) else path


def _sysctl(recs, wanted, own):
    """The sysctl records, the role's settings (wanted), whether the role set the node up (own) -> (the report's
    lines, the cassandra_linux_sysctl* variables to write, the notes: what is carried, and what is NOT
    carried and why)."""
    role = dict((k, v) for k, v in (wanted.get("sysctl") or {}).items())
    role_file = wanted.get("sysctl_file") or "/etc/sysctl.d/60-cassandra.conf"
    files, live, link, order, effective = _sysctl_read(recs)
    owners = _owners(recs)
    # a node the role set up keeps its own file's values, and only them (the role run again changes nothing)
    own_values = dict(files.get(role_file, [])) if own else None
    relevant = _sysctl_relevant(files, order, role, owners, own_values)
    lines = _sysctl_lines(files, order, effective, live, link, role, relevant, owners)
    carried, won_by = _sysctl_carried(role, relevant, effective, owners, own, own_values, role_file)
    if not won_by and not own:
        out, notes = _sysctl_none_set(role)
        return lines, out, notes
    target = _sysctl_target(won_by, order, link, own, own_values, role_file)
    notes, others = [], []
    if not (own and own_values) and won_by:
        carried, others = _sysctl_in_target(files, target, won_by)
        if others:
            notes.append("NOT carried: %s, set in other files than %s (the role would move them there)"
                         % (", ".join(others), target))
    carried, set_again = _sysctl_set_again(carried, effective, target)
    for later in sorted(set_again):
        notes.append("NOT carried: %s, set again by %s read after %s: its line stays there as it is (the role would"
                     " set that file's value live)" % (", ".join(set_again[later]), _label(later, owners), target))
    # (the sysctl module could never read one back: a change at each run; its line stays in the node's file, the
    # role writing its own keys line by line)
    triggers = sorted(k for k in carried if k in WRITE_ONLY_SYSCTL)
    if triggers:
        carried = dict((k, v) for k, v in carried.items() if k not in WRITE_ONLY_SYSCTL)
        notes.append("NOT carried: %s, write-only (an action, not a setting: never read back); its line stays in the"
                     " node's file as it is, and runs again whenever that file is loaded (at boot, sysctl --system;"
                     " cassandra_linux sets its own keys alone)" % ", ".join(triggers))
    # (in the file but not carried: the NOT carried notes above say why)
    dropped = set(k for keys in set_again.values() for k in keys) | set(triggers) | set(others)
    out, more = _sysctl_vars(carried, role, target, role_file, triggers, dropped)
    return lines, out, notes + more


def _sysctl_read(recs):
    """The sysctl records -> (files: {path: [(key, value)]} in the order read, live: {key: value}, link: the link
    to /etc/sysctl.conf in /etc/sysctl.d (None), order: the files in the order read, effective: {key: (value,
    the file it comes from)})."""
    files = {}
    for kind, rest in recs:
        if kind == "sysctl":
            path, line = _fields(rest, 2)
            kv = _sysctl_line(line)
            if kv:
                files.setdefault(path, []).append(kv)
    live = dict((_sysctl_key(k), v) for k, v in (_fields(rest, 2) for kind, rest in recs if kind == "sysctl_live"))
    link = next((rest for kind, rest in recs if kind == "sysctl_link"), None)
    masked = {os.path.basename(rest) for kind, rest in recs if kind == "masked" and "/sysctl.d/" in rest}
    order = _sysctl_order(list(files), link, masked)
    files = dict((p, files[p]) for p in order)
    effective = {}  # key -> (value, path)
    for path in order:
        for key, value in files[path]:
            effective[key] = (value, path)
    return files, live, link, order, effective


def _sysctl_relevant(files, order, role, owners, own_values):
    """The keys the report looks at: the role's, then the other keys of the role's own file on a node it set up
    (own_values: that file's, None when not such a node), else of the admin's files named for Cassandra (such a file
    holds Cassandra's settings, all of them)."""
    if own_values is not None:
        return list(role) + sorted(set(own_values) - set(role))
    cassandra_files = [p for p in order if "cassandra" in os.path.basename(p).lower() and _admin(p, owners)]
    return list(role) + sorted({k for p in cassandra_files for k, dummy in files[p]} - set(role))


def _sysctl_lines(files, order, effective, live, link, role, relevant, owners):
    """The report's lines: each relevant setting of each file (against the role's, the file it loses to), a live
    value the files do not give, /etc/sysctl.conf not read at boot."""
    lines = []
    for path in order:
        for key, value in files[path]:
            if key not in relevant:
                continue
            won = effective[key][1]
            lines.append("sysctl %s: %s = %s%s%s" % (
                _label(path, owners), key, value, _vs(value, role.get(key), "cassandra_linux"),
                "" if won == path else ", overridden by %s" % won))
    for key in relevant:
        value = live.get(key, "")
        if value == "":
            continue
        ref = effective[key][0] if key in effective else role.get(key)
        if ref is not None and _norm(value) != _norm(ref):
            lines.append("sysctl live: %s = %s (%s)" % (
                key, value, ("files say %s" % effective[key][0]) if key in effective
                else "no file sets it; cassandra_linux: %s" % _norm(role[key])))
    if SYSCTL_CONF in order and not link and any(k in relevant for k, dummy in files[SYSCTL_CONF]):
        lines.append("sysctl %s: no link to it in /etc/sysctl.d, systemd-sysctl does not read it at boot" % SYSCTL_CONF)
    return lines


def _sysctl_carried(role, relevant, effective, owners, own, own_values, role_file):
    """-> (the values the node gets from the admin's files, {key: the file each one comes from})."""
    if own and own_values:  # exactly its own file (a key it lacks would be added)
        carried = dict((k, _typed(v)) for k, v in own_values.items())
        return carried, dict.fromkeys(carried, role_file)
    if own:
        # nothing in the role's file (another cassandra_linux_sysctl_file, an older role): the role's
        # keys an admin file sets, in that file; the others not written (the role's defaults would be)
        carried, won_by = {}, {}
        keys = role
    else:
        carried, won_by = dict(role), {}
        keys = relevant
    for key in keys:
        if key in effective and _admin(effective[key][1], owners):
            carried[key] = _typed(effective[key][0])
            won_by[key] = effective[key][1]
    return carried, won_by


def _sysctl_none_set(role):
    """No file of these nodes sets the role's keys: the role's keys would be added, a change -> none written, as on
    these nodes (out, notes)."""
    if not role:
        return {}, []
    return {"cassandra_linux_sysctl": {}}, [
        "cassandra_linux_sysctl: {}, no file of these nodes sets the role's kernel settings: cassandra_linux sets none"]


def _sysctl_target(won_by, order, link, own, own_values, role_file):
    """The file the role is to write its keys in: its own on a node it set up, else the file that sets most of
    them (the last read on a tie; not /etc/sysctl.conf when systemd-sysctl does not read it)."""
    if own and (own_values or not won_by):
        return role_file
    counts = {}
    for path in won_by.values():
        counts[path] = counts.get(path, 0) + 1
    candidates = [p for p in counts if link or p != SYSCTL_CONF] or [role_file]
    return max(candidates, key=lambda p: (counts.get(p, 0), order.index(p) if p in order else -1))


def _sysctl_in_target(files, target, won_by):
    """The file the role is to write in, its keys exactly (a key of another file would be added there, and a
    /etc/sysctl.conf line removed) -> (carried, the keys other files set: not carried)."""
    carried = dict((k, _typed(v)) for k, v in files.get(target, []))
    others = sorted(k for k, path in won_by.items()
                    if path != target and k not in carried and k not in WRITE_ONLY_SYSCTL)
    return carried, others


def _sysctl_set_again(carried, effective, target):
    """A key of target that a file read later sets again: the role would set target's value live, the node having
    the later one; its line stays in target as it is (the role writes its keys line by line) -> (carried without
    them, {the later file: [its keys]})."""
    set_again = {}
    for key in sorted(carried):
        if effective.get(key, (None, target))[1] != target:
            set_again.setdefault(effective[key][1], []).append(key)
    gone = set(k for keys in set_again.values() for k in keys)
    return dict((k, v) for k, v in carried.items() if k not in gone), set_again


def _sysctl_vars(carried, role, target, role_file, triggers, dropped):
    """The variables that give new nodes the kernel settings carried, written in target, and their notes (triggers:
    the write-only keys left out; dropped: the role's keys in the file but not carried, each said above)."""
    changed = sorted(k for k in carried if k not in role or _norm(carried[k]) != _norm(role[k]))
    missing = [k for k in role if k not in carried]
    only_triggers = bool(triggers) and not carried  # (its file has write-only keys alone: nothing to write there)
    out, notes = {}, []
    if not carried and role and only_triggers:
        out["cassandra_linux_sysctl"] = {}
        notes.append("cassandra_linux_sysctl: {}, the file these nodes set kernel keys in, %s, has write-only ones"
                     " alone (%s): cassandra_linux sets none" % (target, ", ".join(triggers)))
    elif not carried and role:
        out["cassandra_linux_sysctl"] = {}
        notes.append("cassandra_linux_sysctl: {}, no file of these nodes sets the role's kernel settings where it"
                     " writes them: cassandra_linux sets none")
    elif changed or missing:
        out["cassandra_linux_sysctl"] = carried
        notes.append(_sysctl_values_note(carried, changed, missing, dropped))
    if target != role_file and not only_triggers:  # (no file to write in with no key: no override)
        out["cassandra_linux_sysctl_file"] = target
        notes.append("cassandra_linux_sysctl_file: %s, the file these nodes set them in: the role writes its keys"
                     " there (line by line, the file's other lines kept) instead of a second file that %s"
                     % (target, "this one would override" if os.path.basename(target) > os.path.basename(role_file)
                        else "could override it"))
    return out, notes


def _sysctl_values_note(carried, changed, missing, dropped):
    """cassandra_linux_sysctl's note: the values that differ from the role's, the role's keys missing (absent from
    the file, or dropped: NOT carried)."""
    absent = [k for k in missing if k not in dropped]
    left = [k for k in missing if k in dropped]
    return "cassandra_linux_sysctl: %s, as these nodes have them%s" % (
        ", ".join("%s %s" % (k, _norm(carried[k])) for k in changed) or "the same values",
        (" (%s)" % "; ".join(["without %s, %s" % (", ".join(keys), why) for keys, why in (
            (absent, "as in their file"), (left, "NOT carried")) if keys])) if missing
        else " (the role's other keys at their default)")


# --- limits ---

def _groups(recs):
    """The groups of the cassandra user (the "groups" records)."""
    groups = set()
    for kind, rest in recs:
        if kind == "groups":
            groups.update(rest.split())
    return groups


def _limits(recs, wanted, own):
    role = wanted.get("limits") or {}
    groups = _groups(recs)
    entries = [_fields(rest, 2) for kind, rest in recs if kind == "limits"]
    owners = _owners(recs)
    # pam_limits: limits.conf, then limits.d/*.conf by name; a user entry wins
    # over a group one, which wins over "*"; the last of the same kind wins
    entries.sort(key=lambda e: (e[0] != LIMITS_CONF, os.path.basename(e[0])))
    effective = _limits_effective(entries, groups)
    items = list(role) + sorted({i for (i, dummy), e in effective.items() if e[0] < 3} - set(role))
    lines = _limits_overridden(entries, effective, groups, owners)
    carried, notes, skipped = dict(role), [], []
    for item in items:
        soft, hard = effective.get((item, "soft")), effective.get((item, "hard"))
        if not soft and not hard:
            continue
        lines.append("limits " + _limit_text(item, soft, hard, role, owners))
        if own:
            continue
        if soft and hard and soft[0] < 3 and hard[0] < 3:
            if soft[1] == hard[1]:
                carried[item] = _typed(soft[1])
            else:
                skipped.append(item)
    if not own:
        # a file the role writes would be a change on most nodes (e.g. the package's limits.d/cassandra.conf
        # rewritten): none written, the limits stay what the node's files give
        out = {"cassandra_linux_limits": {}} if role else {}
        notes = ["cassandra_linux_limits: {}, the cassandra user's limits come from the files of these nodes:"
                 " cassandra_linux writes none"] if role else []
        nproc = _nproc_all(entries, wanted)
        if nproc:
            out["cassandra_linux_nproc_all"] = False
        return lines, out, notes + nproc
    carried = _limits_own(entries, effective, items, skipped)
    changed = sorted(k for k in carried if k not in role or _norm(carried[k]) != _norm(role[k]))
    out = {}
    if [(k, _norm(v)) for k, v in carried.items()] != [(k, _norm(v)) for k, v in role.items()]:
        changed = changed or sorted(carried)
        out["cassandra_linux_limits"] = carried
        notes.append("cassandra_linux_limits: %s, as these nodes give the cassandra user"
                     % (", ".join("%s %s" % (k, carried[k]) for k in changed) or "{} (none set)"))
    if skipped:
        notes.append("NOT carried: the cassandra user's %s (soft and hard differ; the role sets both to one value)"
                     % ", ".join(skipped))
    nproc = _nproc_all(entries, wanted)
    if nproc:
        out["cassandra_linux_nproc_all"] = False
    return lines, out, notes + nproc


def _nproc_all(entries, wanted):
    """RedHat without the role's "* - nproc 32768" (90-nproc.conf) -> [the note] (cassandra_linux_nproc_all false)."""
    if wanted.get("os_family") != "RedHat" or [e for e in entries if e[0] == NPROC_FILE and e[1] == NPROC_LINE]:
        return []
    return ["cassandra_linux_nproc_all: false, no \"%s\" in %s on these nodes (the role would add it for every"
            " user)" % (NPROC_LINE, NPROC_FILE)]


def _limits_effective(entries, groups):
    """The limits files' entries (in the order pam_limits reads them) -> {(item, soft|hard): (rank, value, path,
    domain)}: the entry in effect for the cassandra user (rank 0: its own, 1: a group of it, 3: "*")."""
    effective = {}
    for path, line in entries:
        parts = line.split()
        if len(parts) < 4 or parts[0].startswith("#"):
            continue
        domain, kind, item, value = parts[:4]
        if domain == "cassandra":
            rank = 0
        elif domain.startswith("@") and domain[1:] in groups:
            rank = 1
        elif domain == "*":
            rank = 3
        else:
            continue
        for k in (["soft", "hard"] if kind == "-" else [kind] if kind in ("soft", "hard") else []):
            if (item, k) not in effective or rank <= effective[(item, k)][0]:
                effective[(item, k)] = (rank, value, path, domain)
    return effective


def _limits_overridden(entries, effective, groups, owners):
    """The cassandra user's lines (or a group's of it) another file overrides."""
    lines = []
    for path, line in entries:
        parts = line.split()
        if len(parts) < 4 or not (parts[0] == "cassandra" or parts[0].startswith("@") and parts[0][1:] in groups):
            continue
        won = [effective[(parts[2], k)][2] for k in ("soft", "hard") if (parts[2], k) in effective]
        if won and path not in won:
            lines.append("limits %s: %s %s %s = %s, overridden by %s" % (
                _label(path, owners), parts[0], parts[1], parts[2], parts[3], _label(won[-1], owners)))
    return lines


def _limit_text(item, soft, hard, role, owners):
    """A limit in effect: one text when soft and hard are the same entry's, else each one."""
    if soft and hard and soft[1:] == hard[1:]:
        return "%s: %s %s = %s%s" % (_label(soft[2], owners), soft[3], item, soft[1], _vs(soft[1], role.get(item), "cassandra_linux"))
    text = "; ".join("%s: %s %s %s = %s" % (_label(e[2], owners), e[3], k, item, e[1])
                     for k, e in (("soft", soft), ("hard", hard)) if e)
    return text + (" (cassandra_linux: %s)" % role[item] if item in role else "")


def _limits_own(entries, effective, items, skipped):
    """A node the role set up keeps exactly its own file, in its order (the role writes it whole); not in the
    role's file (another name, an older role): the values in effect (skipped gets the ones soft and hard differ)."""
    carried = {}
    for path, line in entries:
        parts = line.split()
        if path == LIMITS_OWN and len(parts) >= 4 and parts[0] == "cassandra" and parts[1] == "-":
            carried[parts[2]] = _typed(parts[3])
    if not carried:
        for item in items:
            soft, hard = effective.get((item, "soft")), effective.get((item, "hard"))
            if soft and hard and soft[0] < 3 and hard[0] < 3 and soft[1] == hard[1]:
                carried[item] = _typed(soft[1])
            elif soft or hard:
                skipped.append(item)
    return carried


def _unit_limits(recs, wanted):
    role = wanted.get("service_limits") or {}
    # the unit cassandra_service wrote keeps the values of its own file (not the drop-ins, left as they are)
    own = boolean(wanted.get("own_unit", False), strict=False)
    found, main = {}, None
    for kind, rest in recs:
        if kind == "unit":
            path, line = _fields(rest, 2)
            main = main or path  # systemctl cat: the unit file first, then its drop-ins
            name, dummy, value = line.strip().partition("=")
            if name not in dict(UNIT_LIMITS) or (own and path != main):
                continue
            if value.strip():
                found[name] = (value.strip(), path)  # the last one wins
            else:
                found.pop(name, None)  # "LimitNOFILE=": back to systemd's default
    lines, out, notes = [], {}, []
    for kind, rest in recs:
        if kind == "unit":
            path, line = _fields(rest, 2)
            name, dummy, value = line.strip().partition("=")
            if name in dict(UNIT_LIMITS):
                item = dict(UNIT_LIMITS)[name]
                lines.append("unit %s: %s=%s%s" % (path, name, value.strip(),
                                                   _vs(value.strip(), role.get(item), "cassandra_service")))
    for name, item in UNIT_LIMITS:
        if name in found and item in role and _norm(found[name][0]) != _norm(role[item]):
            out["cassandra_service_limit_" + item] = _typed(found[name][0])
    if out:
        notes.append("%s: as in the unit these nodes start Cassandra with (cassandra_service writes them in the"
                     " unit of new nodes)" % ", ".join("%s %s" % (k, v) for k, v in sorted(out.items())))
    proc = {}
    for kind, rest in recs:
        if kind == "proc_limit":
            name, soft, hard = _fields(rest, 3)
            if name in PROC_LIMITS:
                proc[PROC_LIMITS[name]] = (soft, hard)
    unit = dict((UNIT_ITEM[n], v[0]) for n, v in found.items())
    for item in [i for dummy, i in UNIT_LIMITS if i in proc]:
        soft, hard = proc[item]
        want = str(unit.get(item, role.get(item, ""))).replace("infinity", "unlimited")
        # a unit value with a size suffix (8M) or soft:hard: not compared
        if want and re.match(r"^(\d+|unlimited)$", want) and (soft != want or hard != want):
            lines.append("running Cassandra: %s soft %s, hard %s (%s: %s)" % (
                item, soft, hard, "its unit" if item in unit else "cassandra_service", unit.get(item, role.get(item))))
    return lines, out, notes


# --- THP, swap, disks, tuned ---

def _bracketed(raw):
    m = re.search(r"\[(\w+)\]", raw)
    return m.group(1) if m else _norm(raw)


def _thp(recs, wanted):
    """-> lines, carried vars, carried lines: THP as the node has it now, when something sets it at boot (the role's
    unit, another, a kernel argument, grub, tuned); else kernel (nothing set)."""
    now = dict(_fields(rest, 2) for kind, rest in recs if kind == "thp")
    if not now:
        return [], {}, []
    enabled, defrag = _bracketed(now.get("enabled", "")), _bracketed(now.get("defrag", ""))
    by = []
    for kind, rest in recs:
        if kind == "cmdline":
            m = THP_ARG.search(rest)
            if m:
                by.append("kernel command line transparent_hugepage=%s" % m.group(1))
        elif kind == "grub":
            path, line = _fields(rest, 2)
            m = THP_ARG.search(line.replace('"', " "))
            if m:
                by.append("%s transparent_hugepage=%s (next boots)" % (path, m.group(1)))
        elif kind == "thp_unit":
            path, state = _fields(rest, 2)
            by.append("%s (%s)" % (path, state or "?"))
    both = "%s %s" % (enabled, defrag)
    lines = ["THP now: enabled %s, defrag %s%s" % (enabled, defrag, _vs(both, "never never", "cassandra_linux"))]
    lines += ["THP set by %s" % b for b in by]
    role = dict((k, str(wanted.get("thp_" + k, "never"))) for k in ("enabled", "defrag"))
    if by or _tuned_thp(recs):
        # its values, which the role's unit then writes (as they are now: no change)
        out = dict(("cassandra_linux_thp_" + k, v) for k, v in (("enabled", enabled), ("defrag", defrag))
                   if v and v != role[k])
        notes = ["%s: as these nodes have them now (cassandra_linux's unit writes them at boot)"
                 % ", ".join("%s %s" % kv for kv in sorted(out.items()))] if out else []
        return lines, out, notes
    out = {"cassandra_linux_thp_enabled": "kernel", "cassandra_linux_thp_defrag": "kernel"}
    return lines, out, ["cassandra_linux_thp_enabled, cassandra_linux_thp_defrag: kernel, nothing found on these nodes"
                        " that sets THP (no kernel argument, grub line, unit or tuned setting): cassandra_linux sets"
                        " none (now: enabled %s, defrag %s)" % (enabled, defrag)]


def _tuned_thp(recs):
    """A tuned profile of the node sets THP (its [vm], [sysfs] or [bootloader] section)."""
    section = ""
    for kind, rest in recs:
        if kind != "tuned_conf":
            continue
        line = _fields(rest, 2)[1].strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1).strip()
        elif "transparent_huge" in line and section in ("vm", "sysfs", "bootloader"):
            return True
    return False


def _swap(recs):
    fstab = [rest.strip() for kind, rest in recs if kind == "swap_fstab"]
    active = [rest.strip() for kind, rest in recs if kind == "swap_on"]
    if not fstab and not active:
        return ["swap: none active, none in /etc/fstab (same as cassandra_linux)"], {}, []
    lines = (["swap active: %s (cassandra_linux: none)" % a for a in active]
             + ["swap in /etc/fstab: %s (cassandra_linux removes it)" % f for f in fstab])
    return lines, {"cassandra_linux_swap_off": False}, [
        "cassandra_linux_swap_off: false, these nodes have swap: cassandra_linux leaves it as it is"]


def _disks(recs, wanted):
    """The devices under the data and commitlog dirs, as cassandra_linux finds
    them (records: lsblk|<lsblk -J>, lvs|<lvs line>, mnt|<dir>|<findmnt -J>),
    with their read-ahead and scheduler now."""
    role_ra = wanted.get("readahead_kb")
    lines, live = [], []
    found = _disk_plan(recs)
    lines += ["disks: %s" % s for s in found["stacks"] + found["warnings"] + found["info"] + found["notes"]]
    for d in found["devices"]:
        live.append(d["ra"])
        lines.append(_device_line(d, role_ra))
    udev_lines, rules = _udev_rules(recs, role_ra)
    lines += udev_lines
    out, notes = {}, []
    values = set().union(*rules.values()) if rules else set()
    own_rule = next((rules[r] for r in OWN_UDEV_RULES if r in rules), None) \
        if boolean(wanted.get("own", False), strict=False) else None
    if own_rule is not None:
        values = set(own_rule)  # a node the role set up: its own rule (the others are left as they are)
    if len(values) == 1:
        value = values.pop()
        if live and str(value) not in live:
            notes.append("NOT carried: read_ahead_kb %d of the udev rules, the data disks have %s now"
                         % (value, ", ".join(sorted(set(live)))))
        elif role_ra is None or str(value) != str(role_ra):
            out["cassandra_data_readahead_kb"] = value
            notes.append("cassandra_data_readahead_kb: %d, as the udev rules of these nodes set it (the role writes"
                         " its own rule, 64-cassandra-data-disk.rules, for the data devices only)" % value)
    elif len(values) > 1:
        notes.append("NOT carried: the udev rules set several read-aheads (%s), set cassandra_data_readahead_kb"
                     % ", ".join(str(v) for v in sorted(values)))
    elif found["devices"] and str(role_ra) != "kernel":
        out["cassandra_data_readahead_kb"] = "kernel"
        notes.append("cassandra_data_readahead_kb: kernel, no udev rule of these nodes sets a read-ahead: cassandra_linux"
                     " sets none")
    ssd = [d["label"] for d in found["devices"] if d["kind"] == "disk" and d["rota"] == "0"
           and d["sched"] not in ("", "none")]
    if ssd and wanted.get("scheduler", "none") != "keep":
        out["cassandra_data_scheduler"] = "keep"
        notes.append("cassandra_data_scheduler: keep, the SSDs of these nodes run another scheduler than none (%s):"
                     " cassandra_linux leaves it" % ", ".join(sorted(set(
                         d["sched"] for d in found["devices"] if d["label"] in ssd))))
    auto = [d["label"] for d in found["devices"] if d["kind"] == "lvm" and not d.get("os") and d["lv_read_ahead"] == "auto"]
    if auto and wanted.get("lvm_readahead", "") != "auto":
        out["cassandra_data_lvm_readahead"] = "auto"
        notes.append("cassandra_data_lvm_readahead: auto, as the LVM volumes of these nodes have it")
    # what cassandra_linux would still change on the devices (a value no variable gives): left as they are on this
    # node only (host_vars), so that running the roles after the import changes nothing
    linux = {"readahead_kb": out.get("cassandra_data_readahead_kb", role_ra),
             "scheduler": out.get("cassandra_data_scheduler", wanted.get("scheduler", "none")),
             "lvm_readahead": out.get("cassandra_data_lvm_readahead", wanted.get("lvm_readahead", ""))}
    changes = [c["what"] for c in _data_device_changes(recs, linux, True, True) if c["effect"] == CHANGE] \
        if role_ra is not None else []
    if changes:
        out["cassandra_data_disk_tuning"] = False
        notes.append("cassandra_data_disk_tuning: false on this node (host_vars), its data devices left as they are"
                     " (cassandra_linux would change %s): remove it to have them tuned" % "; ".join(changes))
    return lines, out, notes


def _disk_plan(recs, tune_os=True):
    """plan() of the devices under the data and commitlog dirs, from the records."""
    text = next((rest for kind, rest in recs if kind == "lsblk"), "")
    mounts = []
    for kind, rest in recs:
        if kind == "mnt":
            what, d, out = _fields(rest, 3)
            mounts.append({"item": {"dir": d, "tune": what != "other", "commitlog": what == "commitlog"},
                           "rc": 0 if out.strip() else 1, "stdout": out})
    lvs = [rest for kind, rest in recs if kind == "lvs"]
    return plan(text, mounts, lvs=lvs, tune_os=tune_os)


def _device_line(d, role_ra):
    """A tuned device of plan()'s as it is now: read-ahead, scheduler, LVM read-ahead, against the role's."""
    ra, rot, sched = d["ra"], d["rota"], d["sched"]
    role_sched = "none" if rot == "0" else None
    line = "%s %s (%s, %s): read_ahead_kb %s%s, scheduler %s%s" % (
        "disk" if d["kind"] == "disk" else "device", d["label"], d["where"], "SSD" if rot == "0" else "rotational",
        ra or "?", _vs(ra, role_ra, "cassandra_linux"),
        sched or "none", _vs(sched, role_sched, "cassandra_linux") if role_sched else "")
    if d["kind"] == "lvm":
        want = int(role_ra) * 2 if role_ra is not None else None
        line += ", LVM read-ahead %s%s" % (
            d["lv_read_ahead"], _vs(d["lv_read_ahead"], want, "cassandra_linux") if want is not None else "")
    return line


def _udev_rules(recs, role_ra):
    """The udev rules setting a read-ahead or a scheduler (a masked one left out) -> (their lines, {the admin's
    rule file: the read-aheads it sets})."""
    lines, rules, owners = [], {}, _owners(recs)
    masked = {os.path.basename(rest) for kind, rest in recs if kind == "masked" and "/udev/" in rest}
    for kind, rest in recs:
        if kind != "udev":
            continue
        path, line = _fields(rest, 2)
        if os.path.basename(path) in masked:
            continue
        values = [int(v) for v in READAHEAD.findall(line)] + [int(v) // 2 for v in SETRA.findall(line)]
        scheds = SCHEDULER.findall(line)
        what = ", ".join(["read_ahead_kb %d%s" % (v, _vs(v, role_ra, "cassandra_linux")) for v in values]
                         + ["scheduler %s" % s for s in scheds])
        if what:
            lines.append("udev %s: %s  [%s]" % (_label(path, owners), what, line.strip()))
            if _admin(path, owners):
                rules.setdefault(path, set()).update(values)
    return lines, rules


def _old_rules(recs):
    """The udev rules of earlier versions of cassandra_linux on the node (it removes them)."""
    rules = set(rest.split("|", 1)[0] for kind, rest in recs if kind == "udev")
    return [old for old in OWN_UDEV_RULES[1:] + ("/etc/udev/rules.d/60-cassandra-data-disk.rules",) if old in rules]


def _tuned_switch(recs, devices, kb):
    """cassandra_linux would add its cassandra-disks profile to the ones tuned runs."""
    # tuned: the records of its profiles, as cassandra_linux reads them
    lines, profiles = [], {}
    for kind, rest in recs:
        if kind == "tuned":
            active, state = _fields(rest, 2)
            lines += ["state|" + state.strip(), "active|" + active, "version|tuned-adm 2.10"]
    for kind, rest in recs:
        if kind != "tuned_conf":
            continue
        path, line = _fields(rest, 2)
        name = os.path.basename(os.path.dirname(path))
        line = re.sub(r"\s+#.*$", "", line).strip()
        m = re.match(r"^\[(.+)\]", line)
        if m:
            profiles[name] = m.group(1).strip()
        elif "=" in line:
            key, value = line.split("=", 1)
            lines.append("opt|%s|%s|%s|%s" % (name, profiles.get(name, ""), key.strip(), value.strip()))
    state = dict((d["kname"], {"rot": d["rota"], "choices": "2" if d["kind"] == "disk" else "1"}) for d in devices)
    active = next((line[len("active|"):].split() for line in lines if line.startswith("active|")), [])
    # the role switches tuned only when it runs, and not again once it runs its profile (whose
    # udev ids the import can't match here)
    return bool("state|active" in lines and active and active[-1] != "cassandra-disks"
                and cassandra_disk_tuned_conflict(lines, kb, devices, state, None, managed=False))


TUNED_KEYS = {"vm": re.compile(r"^transparent_huge_?pages?$"), "disk": re.compile(r"^(readahead|elevator)$"),
              "bootloader": re.compile(r"^cmdline")}


def _tuned(recs, wanted):
    role_keys = set(wanted.get("sysctl") or {})
    active = [_fields(rest, 2) for kind, rest in recs if kind == "tuned"]
    lines = []
    for profile, state in active:
        if profile:
            lines.append("tuned: profile %s (tuned %s)" % (profile, state or "?"))
    section, relevant = "", False
    for kind, rest in recs:
        if kind != "tuned_conf":
            continue
        path, line = _fields(rest, 2)
        line = line.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1).strip()
            continue
        key = line.split("=", 1)[0].strip()
        if (section == "sysctl" and _sysctl_key(key) in role_keys) or \
                (section in TUNED_KEYS and TUNED_KEYS[section].search(key)
                 and (section != "bootloader" or "transparent_hugepage" in line)) or \
                (section == "sysfs" and re.search(r"transparent_hugepage|read_ahead_kb|scheduler", key)):
            lines.append("tuned %s [%s]: %s" % (path, section, line))
            relevant = True
    if relevant and any(state == "active" for dummy, state in active):
        lines.append("tuned applies these when it starts: on a node with the same profile it may undo what"
                     " cassandra_linux sets (sysctl, read-ahead, THP)")
    return lines


def _timesync(recs, wanted):
    active = [_fields(rest, 3) for kind, rest in recs if kind == "timesync"]
    names = []
    for name, state, boot in active:
        if state == "active":
            names.append(name)
    lines = ["time sync: %s active (at boot: %s)" % (n, b or "?") for n, s, b in active if s == "active"]
    servers = [_fields(rest, 2) for kind, rest in recs if kind == "timesync_server"]
    lines += ["time sync %s: %s" % (path, _norm(line)) for path, line in servers]
    out, notes = {}, []
    if not boolean(wanted.get("timesync", True), strict=False):
        return lines, out, notes
    # the service cassandra_linux picks (timesync.yml): running and started at boot already, or the role changes it
    family = wanted.get("os_family", "")
    chrony = any(kind == "chrony_installed" for kind, dummy in recs)
    svc = "chronyd" if family != "Debian" else "chrony" if chrony else "systemd-timesyncd"
    own = dict((name, (state, boot)) for name, state, boot in
               (_fields(rest, 3) for kind, rest in recs if kind == "timesync_own"))
    state, boot = own.get(svc, ("", ""))
    if not names:
        lines.append("time sync: none active (cassandra_linux starts chrony, or systemd-timesyncd on Debian/Ubuntu)")
        out["cassandra_linux_timesync"] = False
        notes.append("cassandra_linux_timesync: false, no time sync runs on these nodes (the role would install and"
                     " start one): set it to true to have it, Cassandra needs synchronized clocks")
    elif not any(n in OWN_TIMESYNC for n in names):
        out["cassandra_linux_timesync"] = False
        notes.append("cassandra_linux_timesync: false, these nodes keep their time with %s: new nodes must get it the"
                     " same way (your image) instead of chrony, whose unit stops it" % ", ".join(names))
    elif family and own and (state != "active" or boot not in ENABLED):
        out["cassandra_linux_timesync"] = False
        notes.append("cassandra_linux_timesync: false, these nodes run %s, cassandra_linux would %s %s" % (
            ", ".join(names), "start" if state != "active" else "enable at boot", svc))
    elif svc in ("chronyd", "chrony"):
        conf = CHRONY_CONF.get(family, "/etc/chrony.conf")
        mine = [line.strip() for path, line in servers if path == conf and CHRONY_SERVER.match(line)]
        elsewhere = [path for path, line in servers if path != conf and CHRONY_SERVER.match(line)]
        if mine and not elsewhere:
            out["cassandra_linux_timesync_servers"] = mine
            notes.append("cassandra_linux_timesync_servers: the server lines of %s on these nodes" % conf)
        elif elsewhere:
            notes.append("NOT carried: the time servers in %s (cassandra_linux writes them in %s only), new nodes need"
                         " them from your image" % (", ".join(sorted(set(elsewhere))), conf))
    elif servers:
        notes.append("time sync servers: cassandra_linux does not write them, new nodes need them from your image")
    return lines, out, notes


# --- the firewall: what cassandra_firewall writes, read back (_firewall_read says the rule) ---
# the forms the role writes: firewalld's ports of the default zone, and its rich rule for one source
# (cassandra_firewall_port_sources); ufw's port open to anyone, and its "allow from X to any port P proto P"
FW_PORTS = re.compile(r"^(permanent )?ports\s+(.*)$")
FW_RICH = re.compile(r"^(permanent )?rich rule (.*)$")
FW_SOURCE = re.compile(r'^rule family="(ipv4|ipv6)" source address="([0-9a-fA-F.:/]+)"'
                       r' port port="(\d+)" protocol="(tcp|udp)" accept$')
UFW_ANYONE = re.compile(r"^(\d+/(?:tcp|udp))(\s+\(v6\))?\s+ALLOW(?:\s+IN)?\s+Anywhere(?:\s+\(v6\))?$")
UFW_SOURCE = re.compile(r"^(\d+/(?:tcp|udp))(?:\s+\(v6\))?\s+ALLOW(?:\s+IN)?\s+([0-9a-fA-F.:/]+)$")
# any line of ufw status: To (a port list, an app profile, Anywhere; "on <interface>"), the action and its direction,
# From (an address, Anywhere; a source port, an app profile)
UFW_RULE = re.compile(r"^(?P<to>.+?)(?: \(v6\))?(?: on (?P<on>\S+))? (?P<action>ALLOW|DENY|REJECT|LIMIT)"
                      r"(?: (?P<dir>IN|OUT|FWD))? (?P<src>.+?)(?: \(v6\))?(?: on \S+)?$")

# one port parser: a port or a range (7000-7001, 7000:7001, reversed too), with its protocol or not; as a whole value
# (PORT_SPEC), or in the text of a rule (PORT_IN_TEXT: not a part of an address, 10.0.9.0/24, 2001:db8::1)
_PORT_NUMBERS = r"(\d{1,5})(?:([-:])(\d{1,5}))?"
PORT_SPEC = re.compile(r"^%s(?:/(tcp|udp|sctp|dccp))?$" % _PORT_NUMBERS)
PORT_IN_TEXT = re.compile(r"(?<![\w.:/])%s(?![\w.])(?!:[0-9a-fA-F]*:)" % _PORT_NUMBERS)


def _range(lo, hi):
    """The bounds of a port or a range, in order; None out of the port numbers."""
    a, b = sorted((int(lo), int(hi or lo)))
    return (a, b) if b <= 65535 else None


def _port(value):
    """A port spec -> (lo, hi, {protocols}, the range's separator) (no protocol: tcp and udp), or None: a name, an
    empty field, a number out of range or written with a leading zero."""
    m = PORT_SPEC.match(str(value).strip())
    # (as firewalld and ufw write a port: no leading zero)
    leading_zero = m and any(re.match(r"^0\d", g or "") for g in (m.group(1), m.group(3)))
    bounds = _range(m.group(1), m.group(3)) if m and not leading_zero else None
    if not bounds:
        return None
    return bounds[0], bounds[1], {m.group(4)} if m.group(4) else {"tcp", "udp"}, m.group(2) or ""


def _is_port(value):
    return _port(value) is not None


def _ports(values):
    """The port specs among values (_port), the others (a value of the inventory, say) left out."""
    return [p for p in (_port(v) for v in values) if p]


def _in_order(value):
    """A port spec with its range in order (firewalld 7000-7001, ufw 7000:7001); as given when it is not one."""
    p = _port(value)
    if not p or not p[3]:
        return str(value)
    proto = str(value).partition("/")[2]
    return "%d%s%d%s" % (p[0], p[3], p[1], "/" + proto if proto else "")


def _ufw_ports(to):
    """ufw's To as port specs: "7000,9042/tcp" (the protocol of a list applies to each), "7000:7001/tcp", "22"."""
    numbers, slash, proto = to.partition("/")
    return ["%s%s%s" % (n, slash, proto) for n in numbers.split(",")]


def _names_port(text, ports):
    """text names one of the ports (port/proto, a range too), alone or in a range of its own; or a number out of the
    port numbers where a port goes (what it means is not known: it may)."""
    spans = _ports(ports)
    named = [_range(lo, hi) for lo, dummy, hi in PORT_IN_TEXT.findall(text)]
    return any(b is None for b in named) or any(lo <= b and a <= hi for lo, hi in named for a, b, dummy, dummy2 in spans)


def _opens_only(ports, open_):
    """ports (port specs, a range too) are all inside ports open to everyone (open_), protocol by protocol; False
    when one is not a port spec."""
    wanted = [_port(p) for p in ports]
    spans = _ports(open_)
    return bool(wanted) and all(wanted) and all(
        any(a <= lo and hi <= b and proto in protos for a, b, protos, dummy in spans)
        for lo, hi, asked, dummy in wanted for proto in asked)


def firewall_touched(cassandra_ports, exporter_port="", open_ports=(), port_sources=None):
    """The ports cassandra_firewall would touch (port/proto): the Cassandra ports, the Prometheus exporter's (where it
    runs: its port, else ''), open_ports, the keys of cassandra_firewall_port_sources. The import and its self-check
    read the rules on these (and on the ports open to everyone, carried into open_ports)."""
    ports = [str(p) for p in cassandra_ports] + (["%s/tcp" % exporter_port] if str(exporter_port or "") else [])
    ports += [str(p) for p in open_ports] + [str(p) for p in (port_sources or {})]
    return list(dict.fromkeys(ports))


def _firewalld_role_form(line):
    """A firewalld line of the default zone in a form the role writes -> [(port, or (port, source)), the set: now,
    later], or None."""
    ports = FW_PORTS.match(line)
    if ports and all(_is_port(p) for p in ports.group(2).split()):
        return [(_in_order(p), "later" if ports.group(1) else "now") for p in ports.group(2).split()]
    rich = FW_RICH.match(line)
    source = FW_SOURCE.match(rich.group(2)) if rich else None
    # (the family the role gives that address, a port)
    if source and (source.group(1) == "ipv6") == (":" in source.group(2)) and _is_port(source.group(3)):
        return [(("%s/%s" % (source.group(3), source.group(4)), source.group(2)), "later" if rich.group(1) else "now")]
    return None


def _ufw_role_form(line):
    """A ufw status line in a form the role writes -> [(port, or (port, source)), the family: v4, v6], or None."""
    anyone = UFW_ANYONE.match(line)
    if anyone and _is_port(anyone.group(1)):
        return [(_in_order(anyone.group(1)), "v6" if anyone.group(2) else "v4")]
    source = UFW_SOURCE.match(line)
    if source and _is_port(source.group(1)):
        # (one family: the source's own)
        key = (_in_order(source.group(1)), source.group(2))
        return [(key, "v4"), (key, "v6")]
    return None


# RHEL's firewalld policy allow-host-ipv6 as shipped: ICMPv6 to this host, its rich rules
STOCK_ALLOW_HOST_IPV6 = set('rule family="ipv6" icmp-type name="%s" accept' % t for t in (
    "neighbour-advertisement", "neighbour-solicitation", "router-advertisement", "redirect", "mld-listener-done",
    "mld-listener-query", "mld-listener-report", "mld2-listener-report"))
# what --info-policy lists that lets nothing in by itself: any other setting with a value (a port, a service, a
# helper, a field of a later firewalld) may
_POLICY_INERT = {"priority": None, "target": None, "ingress-zones": None, "egress-zones": None, "masquerade": "no",
                 "disable": None, "icmp-block-inversion": "no"}


def _policies(recs):
    """The policies firewall-cmd --info-policy printed ("fw_policy" records: "[permanent ]name", a line) ->
    {"[permanent ]name": {setting: value}} (the lines under a setting, a tab first, are its values: rich rules
    [rule], forward ports)."""
    out = {}
    for name, line in (_fields(r, 2) for kind, r in recs if kind == "fw_policy"):
        policy = out.setdefault(name, {"rich rules": []})
        setting = re.match(r"^\s+([a-z -]+):\s*(.*)$", line)
        last = policy.get("_last")
        if line.startswith("\t") and last == "rich rules":
            policy["rich rules"].append(_norm(line))
        elif line.startswith("\t") and last:  # (one per line: forward ports)
            policy[last] = _norm("%s %s" % (policy.get(last, ""), line))
        elif setting:
            policy["_last"] = setting.group(1)
            if setting.group(1) != "rich rules":
                policy[setting.group(1)] = _norm(setting.group(2))
            elif setting.group(2).strip():
                policy["rich rules"].append(_norm(setting.group(2)))
    return out


def _policy_lets_in(name, policy):
    """A policy to this host (egress HOST) that may let traffic in: not an empty CONTINUE one, nor RHEL's
    allow-host-ipv6 as shipped (not modelled: the import stops on it, quoted)."""
    if "HOST" not in policy.get("egress-zones", "").split():
        return False

    def inert(but=()):
        return all(not v or (k in _POLICY_INERT and _POLICY_INERT[k] in (None, v))
                   for k, v in policy.items() if not k.startswith("_") and k not in but)
    if policy.get("target") == "CONTINUE" and inert():
        return False
    stock = name.split()[-1] == "allow-host-ipv6" and policy.get("target") == "CONTINUE" \
        and policy.get("ingress-zones") == "ANY" and set(policy["rich rules"]) == STOCK_ALLOW_HOST_IPV6 \
        and inert(but=("rich rules",))
    return not stock


def _firewall_read(recs, touched=()):
    """The firewall records -> {name: the firewall running (firewalld, ufw, ''), zone: firewalld's default zone,
    state: the state read of the one running, states: the "fw" records, rules: (where, rule) the rules read,
    open: [port/proto open to everyone as the role opens them, in both sets], sources: {port/proto: [source]} (the
    same, to some sources), redundant: [(port, source)] a source rule on a port open to everyone (it changes nothing),
    other: [(where, rule)] the rules on a touched port the role does not write, or in one set only,
    unread: [(where, what)] the lists firewall-cmd or ufw could not give,
    no_effect: [(where, rule)] a rule of another form that opens only ports open to everyone (e.g. the ssh service),
    every_port: [(where, rule)] a rule or setting for a source on every port (said, not compared)}.
    touched: firewall_touched's (the ports open to everyone are touched too).

    One rule: a line is carried only when it is in a form the role writes, the same in both sets (firewalld: now and
    after a reload; ufw: IPv4 and IPv6 for a port open to anyone, IPv4 alone with IPV6=no); any other line on a
    touched port is "other" (the self-check stops), quoted as it is."""
    states = [_fields(rest, 3) for kind, rest in recs if kind == "fw"]
    rules = [(where, _norm(line)) for where, line in (_fields(rest, 2) for kind, rest in recs if kind == "fw_rule")]
    name, zone = _firewall_running(states)
    home = "zone %s" % zone if name == "firewalld" else "ufw"
    ipv6 = not any(n == "ufw" and extra == "IPV6=no" for n, state, extra in states)
    # (a list firewall-cmd or ufw could not give: not known, never "no rule"; the sets there are not compared)
    unread = [tuple(_fields(r, 2)) for kind, r in recs if kind == "fw_unread"]
    home_unread = any(where in (home, name) for where, what in unread)
    # the ports of the services firewalld rich rules name ("[permanent ]<name>"), of ufw's app profiles ("ufw app
    # <name>")
    services = dict(_fields(r, 2) for kind, r in recs if kind == "fw_svc")
    found, other_forms = _role_form_sets(rules, name, home)
    in_both_sets, open_, redundant = _in_both_sets(found, name, ipv6)
    ports = list(touched) + open_
    other = [] if home_unread else _in_one_set_only(found, in_both_sets + redundant, home, ports)
    forms = _other_forms_sorted(other_forms, name, home, ports, open_, services)
    other += forms["other"]
    # (iptables' own INPUT policy other than ACCEPT: a rule of its own)
    other += [(n, state) for n, state, extra in states if n not in ("firewalld", "ufw")]
    other += _policies_letting_in(recs)
    return {"name": name, "zone": zone, "state": next((s for n, s, extra in states if n == name), ""),
            "states": states, "rules": rules, "open": open_, "sources": _source_rules(in_both_sets, redundant, ports),
            "redundant": redundant, "other": other, "unread": unread, "no_effect": forms["no_effect"],
            "every_port": forms["every_port"]}


def _firewall_running(states):
    """The "fw" records -> (the firewall running: firewalld, ufw or "", firewalld's default zone or "")."""
    name = next((n for n, state, extra in states if n in ("firewalld", "ufw")), "")
    zone = next((extra[len("default zone "):] for n, state, extra in states if n == "firewalld"
                 and extra.startswith("default zone ")), "")
    return name, zone


def _role_form_sets(rules, name, home):
    """The rules read -> (found: {port, or (port, source): the sets it is in} for the lines of home in a form the
    role writes; [(where, rule)] the other lines)."""
    role_form = {"firewalld": _firewalld_role_form, "ufw": _ufw_role_form}.get(name)
    found, other_forms = {}, []
    for where, line in rules:
        form = role_form(line) if role_form and where == home else None
        if form is None:
            other_forms.append((where, line))
        for key, in_set in form or []:
            found.setdefault(key, set()).add(in_set)
    return found, other_forms


def _in_both_sets(found, name, ipv6):
    """found (_role_form_sets') -> (its keys in both sets: firewalld now and after a reload, ufw IPv4 and IPv6 (IPv4
    alone with IPV6=no); the ports among them open to everyone; the source rules on one of those, in one set or both:
    redundant, they change nothing)."""
    both = ("now", "later") if name == "firewalld" else ("v4", "v6") if ipv6 else ("v4",)
    in_both_sets = [k for k, sets in found.items() if all(s in sets for s in both)]
    open_ = [k for k in in_both_sets if isinstance(k, str)]
    redundant = [k for k in found if isinstance(k, tuple) and k[0] in open_]
    return in_both_sets, open_, redundant


def _in_one_set_only(found, carried, home, ports):
    """The role's form found in one set only (found's keys not in carried), on one of ports -> [(home, what)]."""
    other = []
    for k, sets in found.items():
        port = k if isinstance(k, str) else k[0]
        if k in carried or not _names_port(port, ports):
            continue
        what = ("port %s open to everyone" % k) if isinstance(k, str) else ("port %s open to %s" % k)
        when = {"now": "now", "later": "from the next reload", "v4": "for IPv4", "v6": "for IPv6"}[sorted(sets)[0]]
        other.append((home, "%s %s only" % (what, when)))
    return other


def _other_forms_sorted(other_forms, name, home, ports, open_, services):
    """The lines of no form the role writes -> {"other", "no_effect", "every_port": [(where, rule)]} as
    _other_form_kind sorts them (a line on no touched port in none), a rule read alike now and after a reload once."""
    kinds = {}
    for where, line in other_forms:
        try:
            kinds[(where, line)] = _other_form_kind(where, line, name, home, ports, open_, services)
        except Exception:  # pylint: disable=broad-except  # a safety net: a line the checks above miss is "other"
            kinds[(where, line)] = "other"
    out = {"other": [], "no_effect": [], "every_port": []}
    for (where, line), kind in kinds.items():
        if line.startswith("permanent ") and kinds.get((where, line[len("permanent "):])) == kind:
            continue  # (the same rule now and after a reload, read alike: said once)
        out.get(kind, []).append((where, line))
    return out


def _policies_letting_in(recs):
    """firewalld's policies to this host: not modelled, each one that may let traffic in -> (where, what) as read."""
    return [("policy %s" % name.split()[-1], "%spolicy to this host: %s" % (
        "permanent " if name.startswith("permanent ") else "",
        "; ".join("%s %s" % (k, ", ".join(v) if isinstance(v, list) else v) for k, v in policy.items()
                  if not k.startswith("_") and v)))
            for name, policy in _policies(recs).items() if _policy_lets_in(name, policy)]


def _source_rules(in_both_sets, redundant, ports):
    """{port/proto: [source]} the ports of ports open to some sources in both sets (a redundant rule left out)."""
    sources = {}
    for k in in_both_sets:
        if isinstance(k, tuple) and k not in redundant and _names_port(k[0], ports):
            sources.setdefault(k[0], []).append(k[1])
    return sources


def _ufw_other_form_kind(line, ports, open_, services):
    """_other_form_kind of a ufw status line."""
    default = re.match(r"^default incoming (\S+)$", line)
    if default:
        return "every_port" if default.group(1) == "allow" else ""  # (allow: every port comes in)
    m = UFW_RULE.match(line)
    if not m:
        return "other"
    lets_in = m.group("action") == "ALLOW" and m.group("dir") in (None, "IN") and not m.group("on")
    if m.group("to") == "Anywhere":
        # a source let in on every port (said); one kept out, a source port, an app profile as From: "other"
        return "every_port" if lets_in and re.match(r"^[0-9a-fA-F.:/]+$", m.group("src")) else "other"
    # (an app profile: its ports, as ufw app info gives them)
    app = services.get("ufw app %s" % m.group("to"))
    specs = [p for token in app.split() for p in _ufw_ports(token)] if app else _ufw_ports(m.group("to"))
    if not all(_is_port(spec) for spec in specs):
        return "other"  # (an app profile not read, a name, an empty field)
    if not _names_port(" ".join(specs), ports) and not _names_port(m.group("src"), ports):
        return ""
    return "no_effect" if lets_in and m.group("src") == "Anywhere" and _opens_only(specs, open_) else "other"


def _firewalld_other_form_kind(where, line, home, ports, open_, services):
    """_other_form_kind of a firewalld line (of any zone)."""
    # the settings of a zone: an ACCEPT target, a whole protocol, a source port (traffic from it reaches every port) let
    # every port in: said; a forward port on a touched port: "other"
    zone_setting = re.match(r"^(?:permanent )?(target|protocols|source-ports|forward-ports) (.*)$", line)
    if zone_setting and zone_setting.group(1) == "target":
        return "every_port" if zone_setting.group(2) == "ACCEPT" else ""
    if zone_setting and zone_setting.group(1) in ("protocols", "source-ports"):
        return "every_port"
    if zone_setting:
        return "other" if _names_port(zone_setting.group(2), ports) else ""
    # what else a service opens: a whole protocol, a source port (every port, said); services it includes (not read
    # here: "other", whatever their ports)
    extra = re.match(r"^(?:permanent )?service \S+ (protocols|source-ports|includes): (.*)$", line)
    if extra and extra.group(1) in ("protocols", "source-ports"):
        return "every_port"
    if extra:
        return "other"
    service = re.match(r"^(permanent )?service [^:]+:\s*(.*)$", line)
    if service:
        specs = service.group(2).split()
        if not all(_is_port(spec) for spec in specs):
            return "other"  # (a name, an empty field)
        if not specs or not _names_port(" ".join(specs), ports):
            return ""
        return "no_effect" if where == home and _opens_only(specs, open_) else "other"
    rich = FW_RICH.match(line)
    if rich and re.match(r'^rule(?: family="ipv[46]")?(?: source (?:NOT )?address="[^"]+")? icmp-(?:type|block)'
                         r' name="[^"]+"(?: (?:accept|drop|reject))?$', rich.group(2)):
        return ""  # (ICMP: no port)
    named = re.search(r' service name="([^"]*)"', rich.group(2)) if rich else None
    if rich and not named and not re.search(r'(?<![\w-])port port=|forward-port ', rich.group(2)):
        # no destination port in it (none, or a source port): it reaches every port, Cassandra's too; letting in is
        # said, anything else stops
        return "every_port" if re.search(r" accept$", rich.group(2)) else "other"
    if named:
        # (the ports of that service as the read listed them in that set: on a touched port, or not known: "other")
        known = services.get((rich.group(1) or "") + named.group(1))
        if known is None or not all(_is_port(spec) for spec in known.split()):
            return "other"
        return "other" if _names_port(known, ports) else ""
    return "other" if _names_port(line, ports) else ""


def _other_form_kind(where, line, name, home, ports, open_, services):
    """A rule line of no form the role writes -> "other" (not carried: the self-check stops), "no_effect" (it opens
    only ports open to everyone, protocol by protocol), "every_port" (a source, or every source, let in on every port:
    said, not compared), "" (on no port the role touches). A whitelist: what is not matched is "other" when it may
    concern a touched port."""
    if name == "ufw" and where == "ufw":
        return _ufw_other_form_kind(line, ports, open_, services)
    if name == "firewalld" or where.startswith("zone "):
        return _firewalld_other_form_kind(where, line, home, ports, open_, services)
    return "other" if _names_port(line, ports) else ""


UNREAD_WAY_OUT = ("import again once %s answers, or keep the firewall as the nodes have it (-e"
                  " import_keep_hand_edits=firewall)")


def _firewall(recs, wanted):
    ports = [str(p) for p in wanted.get("ports") or []]
    exporter_port = str(wanted.get("exporter_port") or "")
    exporter = ("%s/tcp" % exporter_port) if exporter_port else ""
    fw = _firewall_read(recs, firewall_touched(ports, exporter_port))
    if not fw["states"] and not fw["rules"]:
        return (["firewall: none active (firewalld, ufw), no iptables/nftables rule for the Cassandra ports"],
                {"cassandra_firewall_enabled": False},
                ["cassandra_firewall_enabled: false, these nodes run no firewall: cassandra_firewall installs none;"
                 " with cassandra_manage_firewall, it stops and disables the one a node added later has (RHEL starts"
                 " firewalld by default)"])
    lines = ["firewall: %s %s%s" % (name, state, (", " + extra) if extra else "")
             for name, state, extra in fw["states"]]
    lines += ["firewall %s: %s" % rule for rule in fw["rules"]]
    # (as carried: the forms cassandra_firewall writes)
    to_all = [p for p in ports if _opens_only([p], fw["open"])]
    to_some = [p for p in ports if p not in to_all and p in fw["sources"]]
    lines.append("firewall: Cassandra ports open to everyone: %s; to some sources only: %s; neither: %s" % (
        ", ".join(to_all) or "none", ", ".join(to_some) or "none",
        ", ".join(p for p in ports if p not in to_all + to_some) or "none"))
    out, notes = {}, []
    if fw["name"]:
        where = "the default zone %s" % fw["zone"] if fw["name"] == "firewalld" else "ufw"
        out["open_ports"] = fw["open"]
        notes.append("open_ports: %s, open to everyone in %s on these nodes (cassandra_firewall opens them there)"
                     % (", ".join(fw["open"]) or "none", where))
        mine = dict((p, a) for p, a in fw["sources"].items() if p in ports)
        if mine:
            out["cassandra_firewall_port_sources"] = mine
            notes.append("cassandra_firewall_port_sources: %s, open to these sources only in %s on these nodes"
                         % ("; ".join("%s from %s" % (p, ", ".join(a)) for p, a in mine.items()), where))
        if exporter in fw["sources"]:
            out["cassandra_prometheus_exporter_sources"] = fw["sources"][exporter]
            notes.append("cassandra_prometheus_exporter_sources: %s, the Prometheus exporter's port %s open to them"
                         " only in %s on these nodes" % (", ".join(fw["sources"][exporter]), exporter, where))
        notes += ["port %s open to everyone: its rule for %s changes nothing, not carried" % k for k in fw["redundant"]]
        notes += ["%s: %s opens only ports open to everyone, changes nothing (not carried)" % rule
                  for rule in fw["no_effect"]]
    else:
        out["cassandra_firewall_enabled"] = False
        notes.append("cassandra_firewall_enabled: false, these nodes run neither firewalld nor ufw; with"
                     " cassandra_manage_firewall, cassandra_firewall stops and disables the one a node added later"
                     " has (RHEL starts firewalld by default)")
    notes += ["NOT carried: %s %s (cassandra_firewall does not write it; new nodes would not get it)" % rule
              for rule in fw["other"]]
    notes += ["NOT read: %s %s could not be listed: its rules are not known, not carried; %s"
              % (where, what, UNREAD_WAY_OUT % ("ufw" if where == "ufw" else "firewall-cmd"))
              for where, what in fw["unread"]]
    touched_now = firewall_touched(ports, exporter_port, fw["open"])
    notes += ["firewalld direct rule naming a port cassandra_firewall opens, not compared (it writes none): %s"
              % _norm(rest) for kind, rest in recs if kind == "fw_direct" and _names_port(rest, touched_now)]
    # (a zone bound to sources, a rule for a source on every port, Cassandra's too: not compared)
    notes += ["firewalld %s: that source is handled by the zone's own rules, not compared (cassandra_firewall writes"
              " none)" % _norm(rest).replace("|", ": ", 1) for kind, rest in recs if kind == "fw_zone"]
    notes += ["%s %s: %s, for that source on every port, Cassandra's too; not compared (cassandra_firewall writes none)"
              % (fw["name"], where, line) for where, line in fw["every_port"]]
    return lines, out, notes


def cassandra_firewall_changes(text, fw, family="", cassandra_ports=()):
    """What cassandra_firewall, run on the node, would change there, and the rules it does not write (new nodes would
    not get them): text, the import's records; fw: {manage, enabled, open_ports, port_sources, exporter_port} as the
    role reads them (port_sources: the Prometheus exporter's port to its sources included; exporter_port: its port
    where it runs, else ''); family: the node's os_family; cassandra_ports: its storage, native and JMX ports
    (port/proto). -> [{what, now, would, effect}] (as cassandra_linux_changes)."""
    if not boolean(fw.get("manage", True), strict=False):
        return []
    recs = _records(text)
    open_ports = [str(p) for p in fw.get("open_ports") or []]
    sources = dict((str(p), [str(a) for a in a_list or []]) for p, a_list in (fw.get("port_sources") or {}).items())
    read = _firewall_read(recs, firewall_touched(cassandra_ports, fw.get("exporter_port") or "", open_ports, sources))
    svc = "firewalld" if family == "RedHat" else "ufw" if family == "Debian" else read["name"] or "firewalld"
    items = _firewall_kept_items(read)
    unit_active, unit_boot, running, at_boot = _firewall_service_state(recs, svc, read)
    if not boolean(fw.get("enabled", True), strict=False):
        return items + _firewall_disabled_items(svc, read, unit_active, unit_boot, running, at_boot)
    if read["name"] != svc:
        items.append(_item("firewall %s" % svc, ("%s active" % read["name"]) if read["name"] else "none",
                           "installed and started"))
        return items
    if unit_boot not in ENABLED:
        items.append(_item("firewall %s at boot" % svc, unit_boot or "?", "enabled"))
    return items + _firewall_port_items(read, open_ports, sources)


def _firewall_kept_items(read):
    """The rules cassandra_firewall does not write (new nodes would not get them), the lists not read."""
    items = [_item("firewall rule (not carried)", "%s: %s" % rule, "not written by the roles")
             for rule in read["other"]]
    items += [_item("firewall rules (not read)", "%s: %s could not be listed" % (where, what),
                    "not known: the roles may change them; "
                    + UNREAD_WAY_OUT % ("ufw" if where == "ufw" else "firewall-cmd"))
              for where, what in read["unread"]]
    return items


def _firewall_service_state(recs, svc, read):
    """firewalld: its unit, running and at boot. ufw: its rules active (ufw status) and ufw.conf's ENABLED at boot,
    and its unit (the one the role starts and enables) -> (the unit's state, at boot, running, started at boot)."""
    state = dict((n, (active, boot))
                 for n, active, boot in (_fields(rest, 3) for kind, rest in recs if kind == "fw_state"))
    unit_active, unit_boot = state.get(svc, ("", ""))
    rules_active, rules_boot = state.get("ufw-rules", ("", "")) if svc == "ufw" else (unit_active, unit_boot)
    running = rules_active == "active" or read["name"] == svc
    at_boot = unit_boot in ENABLED and (svc != "ufw" or rules_boot == "yes")
    return unit_active, unit_boot, running, at_boot


def _firewall_disabled_items(svc, read, unit_active, unit_boot, running, at_boot):
    """cassandra_firewall_enabled false: ufw disabled where it is active, firewalld stopped and disabled."""
    if svc == "ufw" and running:
        return [_item("firewall ufw", read["state"] or "active", "disabled (ufw disable)")]
    if svc == "ufw" and at_boot:
        return [_item("firewall ufw", "inactive, active from the next boot (ufw.conf ENABLED=yes)",
                      "left as it is: cassandra_firewall disables ufw only where it is active")]
    if svc == "firewalld" and (running or at_boot):
        return [_item("firewall firewalld", "%s, at boot %s" % (unit_active or "active", unit_boot or "?"),
                      "stopped, disabled")]
    return []


def _firewall_port_items(read, open_ports, sources):
    """The ports opened to everyone, and its tasks for cassandra_firewall_port_sources (sources): each port closed to
    everyone, opened to each source. It removes no source rule: one it does not write stays, new nodes would not get
    it; on a port it leaves open to everyone, it changes nothing."""
    items = [_item("port %s" % p, "closed", "open") for p in open_ports if p not in sources and p not in read["open"]]
    for p, wanted in sources.items():
        if p in read["open"]:
            items.append(_item("port %s" % p, "open to everyone",
                               "closed to everyone (open to %s only)" % ", ".join(wanted)))
        have = read["sources"].get(p, []) + [a for q, a in read["redundant"] if q == p]
        items += [_item("port %s from %s" % (p, a), "closed", "open") for a in wanted if a not in have]
    items += [_item("firewall rule (not carried)", "port %s open to %s" % (p, a), "not written by the roles")
              for p, a_list in read["sources"].items() for a in a_list if a not in sources.get(p, [])]
    return items


# --- what cassandra_linux would change on a node (the import's self-check) ---

ROLE_THP_UNIT = "/etc/systemd/system/disable-thp.service"
NPROC_FILE = "/etc/security/limits.d/90-nproc.conf"
NPROC_LINE = "* - nproc 32768"
NPROC_MATCH = re.compile(r"^\* - nproc 32768")  # cassandra_linux's lineinfile regexp
# what systemctl is-enabled says of a unit that starts at boot (or cannot be enabled: static)
ENABLED = ("enabled", "enabled-runtime", "static", "alias", "indirect", "generated")
CHANGE, NO_EFFECT = "change", "no effect"
# the end of the self-check's note for such a file (the report lists them apart)
NO_EFFECT_NOTE = ", no effect (the node keeps the values it has now)"


def _item(what, now, would, effect=CHANGE):
    return {"what": what, "now": _norm(now), "would": _norm(would), "effect": effect}


def thp_unit(values):
    """The role's disable-thp.service writing values ({enabled, defrag}: the ones set), as its template writes it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "roles", "cassandra_linux", "templates",
                        "disable-thp.service.j2")
    with open(path, encoding="utf-8") as f:
        template = f.read()
    # (Ansible's template module: trim_blocks, the file's last newline kept)
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(template).render(_cassandra_linux_thp_set=values)


def _thp_sum(values):
    return hashlib.sha1(thp_unit(values).encode("utf-8")).hexdigest()


def _live_boot(linux, facts):
    """(kernel settings applied live, what runs at boot set up), as cassandra_linux decides them (os.yml)."""
    container = "container" in (facts.get("virtualization_tech_guest") or []) \
        or facts.get("virtualization_type", "") in (linux.get("container_types") or [])
    mode = str(linux.get("apply_live", "auto")).lower()
    live = not container if mode == "auto" else boolean(mode, strict=False)
    return live, live or not container


def _sysctl_changes(recs, linux, live):
    """cassandra_linux's sysctl module: each key in its file (and set live, alone), the same keys out of
    /etc/sysctl.conf.
    A file line is no effect when the value read at boot stays the one the node has now."""
    wanted = [(_sysctl_key(str(k)), _norm(v)) for k, v in (linux.get("sysctl") or {}).items()]
    target = linux.get("sysctl_file") or "/etc/sysctl.d/60-cassandra.conf"
    files, now, link, dummy, dummy2 = _sysctl_read(recs)
    masked = {os.path.basename(rest) for kind, rest in recs if kind == "masked" and "/sysctl.d/" in rest}
    after, edits = _sysctl_edits(files, wanted, target)
    at_boot = {}
    for path in _sysctl_order(list(after), link, masked):
        for k, v in after[path]:
            at_boot[k] = v
    items = _sysctl_live_items(wanted, now) if live else []
    for key, path, before, value in edits:
        items.append(_item("sysctl %s (%s)" % (key, path), "not set" if before is None else before,
                           "removed" if value is None else value, _sysctl_edit_effect(key, value, now, at_boot)))
    return items


def _sysctl_edits(files, wanted, target):
    """The role's keys written in target, the same keys removed from /etc/sysctl.conf -> (the files after, [(key,
    file, its value there now, its value after: None removed)])."""
    after = dict((p, list(lines)) for p, lines in files.items())
    edits = []
    for key, value in wanted:
        lines = after.setdefault(target, [])
        has = [v for k, v in lines if k == key]
        if not has or has[-1] != value:
            edits.append((key, target, has[-1] if has else None, value))
            after[target] = [(k, value if k == key else v) for k, v in lines] + ([] if has else [(key, value)])
        if target != SYSCTL_CONF and any(k == key for k, dummy in after.get(SYSCTL_CONF, [])):
            edits.append((key, SYSCTL_CONF, [v for k, v in after[SYSCTL_CONF] if k == key][-1], None))
            after[SYSCTL_CONF] = [(k, v) for k, v in after[SYSCTL_CONF] if k != key]
    return after, edits


def _sysctl_live_items(wanted, now):
    """The module sets each of its keys live, one by one (never its whole file again: its other keys stay); a
    write-only key: written at each run (cassandra_linux refuses it)."""
    items = []
    for key, value in wanted:
        if key in WRITE_ONLY_SYSCTL:
            items.append(_item("sysctl %s (live)" % key, "not read (write-only)", "written at each run: an action"))
        elif _norm(now.get(key, "")) != value:
            items.append(_item("sysctl %s (live)" % key, now.get(key) or "not read", value))
    return items


def _sysctl_edit_effect(key, value, now, at_boot):
    """A file line's effect: no effect when the value read at boot stays the one the node has now; a write-only key
    the role would write is an action at each load of the file, a change; one removed: no effect."""
    if key in WRITE_ONLY_SYSCTL:
        return CHANGE if value is not None else NO_EFFECT
    return NO_EFFECT if key in now and _norm(at_boot.get(key, "")) == _norm(now[key]) else CHANGE


def _swap_changes(recs, linux, live):
    """swapoff -a (live), the swap lines of /etc/fstab removed (cassandra_linux_swap_off)."""
    if not boolean(linux.get("swap_off", True), strict=False):
        return []
    items = []
    active = [rest.split()[0] for kind, rest in recs if kind == "swap_on" and rest.split()]
    if live and active:
        items.append(_item("swap (live)", "on: %s" % ", ".join(active), "off (swapoff -a)"))
    items += [_item("swap in /etc/fstab", rest, "removed") for kind, rest in recs if kind == "swap_fstab"]
    return items


def _limit_value(effective, item):
    soft, hard = effective.get((item, "soft")), effective.get((item, "hard"))
    if not soft and not hard:
        return "not set"
    if soft and hard and soft[1:3] == hard[1:3]:
        return "%s (%s)" % (soft[1], soft[2])
    return ", ".join("%s %s (%s)" % (k, e[1], e[2]) for k, e in (("soft", soft), ("hard", hard)) if e)


def _limits_changes(recs, linux, own, family):
    """cassandra_linux's limits file (its mark: own), the block earlier versions wrote in limits.conf, RedHat's
    90-nproc.conf line: no effect when the limits the cassandra user gets stay the same."""
    role = linux.get("limits") or {}
    groups = _groups(recs)
    entries = [_fields(rest, 2) for kind, rest in recs if kind == "limits"]
    after, files = _limits_file_written(entries, role, linux.get("limits_file") or LIMITS_OWN, own)
    if role and any(kind == "limits_block_found" for kind, dummy in recs):
        after, removed = _limits_block_removed(recs, after)
        files.append(removed)
    if family == "RedHat" and boolean(linux.get("nproc_all", True), strict=False):
        after, nproc = _nproc_line_set(after)
        files += nproc
    before, then = _user_limits(entries, groups), _user_limits(after, groups)
    changed = _limits_differing(before, then)
    if not changed:
        return [dict(f, effect=NO_EFFECT) for f in files]
    return [_item("limits of the cassandra user: %s (its logins)" % item, _limit_value(before, item),
                  _limit_value(then, item)) for item in changed]


def _limits_file_written(entries, role, target, own):
    """The role's limits file (target) written with its lines, unless it has its mark (own: the default file of a
    node it set up, with them) or role is {} (no file written) -> (the limits entries after, [its item])."""
    wanted = ["cassandra - %s %s" % (k, v) for k, v in role.items()]
    there = [line for path, line in entries if path == target]
    if not role or (own and target == LIMITS_OWN and there == wanted):
        return list(entries), []
    after = [e for e in entries if e[0] != target] + [[target, line] for line in wanted]
    return after, [_item(target, "; ".join(there) or "absent", "; ".join(wanted))]


def _limits_block_removed(recs, after):
    """The block earlier versions wrote in limits.conf removed -> (the entries after, its item)."""
    block = [rest for kind, rest in recs if kind == "limits_block"]
    after = list(after)
    for line in block:
        gone = next((e for e in after if e[0] == LIMITS_CONF and e[1] == line), None)
        if gone:
            after.remove(gone)
    return after, _item("the block earlier versions wrote in %s" % LIMITS_CONF, "; ".join(block) or "empty", "removed")


def _nproc_line_set(after):
    """RedHat's 90-nproc.conf line made the role's, as lineinfile does (the last line its regexp finds replaced, else
    the line added) -> (the entries after, [its item])."""
    matches = [e for e in after if e[0] == NPROC_FILE and NPROC_MATCH.match(e[1])]
    if not matches:
        return after + [[NPROC_FILE, NPROC_LINE]], [_item(NPROC_FILE, "no line %s" % NPROC_LINE,
                                                          "%s added" % NPROC_LINE)]
    if matches[-1][1] == NPROC_LINE:
        return after, []
    after = list(after)
    after[after.index(matches[-1])] = [NPROC_FILE, NPROC_LINE]
    return after, [_item(NPROC_FILE, matches[-1][1], NPROC_LINE)]


def _user_limits(entries, groups):
    """The limits the cassandra user gets from entries, read in pam_limits' order (limits.conf, then limits.d by
    name)."""
    return _limits_effective(sorted(entries, key=lambda e: (e[0] != LIMITS_CONF, os.path.basename(e[0]))), groups)


def _limits_differing(before, then):
    """The items whose soft or hard value differs between before and then (_user_limits'), sorted."""
    changed = []
    for item, dummy in sorted(set(before) | set(then)):
        if item not in changed and any((before.get((item, k)) or (None, None))[1] != (then.get((item, k)) or (None, None))[1]
                                       for k in ("soft", "hard")):
            changed.append(item)
    return changed


def _thp_changes(recs, linux, live, boot):
    """disable-thp.service writing the values set (cassandra_linux_thp_enabled, _defrag), enabled at boot, started
    while THP differs; removed when both are kernel; nothing when neither is set. Written or enabled: no effect when
    the node has these values now (or no THP)."""
    now = dict(_fields(rest, 2) for kind, rest in recs if kind == "thp")
    have = {"enabled": _bracketed(now.get("enabled", "")), "defrag": _bracketed(now.get("defrag", ""))}
    wanted = dict((k, str(linux.get("thp_" + k, "never"))) for k in ("enabled", "defrag"))
    values = dict((k, v) for k, v in wanted.items() if v not in ("kernel", "keep"))
    unit = dict(_fields(rest, 2) for kind, rest in recs if kind == "thp_own")
    items = []
    if not values:
        if list(wanted.values()) == ["kernel", "kernel"] and (unit.get("sum") or unit.get("boot") not in ("", "not-found")):
            # (THP at the next boot: the kernel's, which the import cannot read)
            items.append(_item(ROLE_THP_UNIT, unit.get("boot") or "present", "removed (THP left to the kernel at boot)"))
        return items
    differ = [k for k, v in values.items() if have["enabled"] and have[k] != v]
    at_boot = CHANGE if differ else NO_EFFECT
    order = dict((k, values[k]) for k in ("enabled", "defrag") if k in values)
    if unit.get("sum") != _thp_sum(order):
        items.append(_item(ROLE_THP_UNIT, "another content" if unit.get("sum") else "absent",
                           "the role's (THP %s at boot)" % ", ".join("%s %s" % kv for kv in order.items()), at_boot))
    if boot and unit.get("boot") not in ENABLED:
        items.append(_item("disable-thp.service at boot", unit.get("boot") or "not found", "enabled", at_boot))
    if live:
        items += [_item("transparent_hugepage %s (live)" % k, have[k], values[k]) for k in ("enabled", "defrag")
                  if k in differ]
    return items


def _timesync_changes(recs, linux, family):
    """The time sync service cassandra_linux picks (timesync.yml), installed, started and enabled; chrony's servers
    (cassandra_linux_timesync_servers)."""
    if not boolean(linux.get("timesync", True), strict=False):
        return []
    chrony = any(kind == "chrony_installed" for kind, dummy in recs)
    svc = "chronyd" if family != "Debian" else "chrony" if chrony else "systemd-timesyncd"
    state = dict((name, (active, boot)) for name, active, boot in
                 (_fields(rest, 3) for kind, rest in recs if kind == "timesync_own"))
    active, boot = state.get(svc, ("", ""))
    missing = boot in ("", "not-found")
    if boolean(linux.get("offline", False), strict=False) and missing:
        return []  # nothing downloaded: a warning only
    items = []
    if active != "active":
        items.append(_item("time sync %s" % svc, (active or "inactive") + (", not installed" if missing else ""),
                           "installed and started" if missing else "started"))
    if boot not in ENABLED:
        items.append(_item("time sync %s at boot" % svc, boot or "not found", "enabled"))
    return items + _chrony_server_changes(recs, linux, family)


CHRONY_CONF = {"Debian": "/etc/chrony/chrony.conf"}
CHRONY_SERVER = re.compile(r"^\s*(server|pool|peer)\s")


def _chrony_server_changes(recs, linux, family):
    """chrony.conf's server, pool and peer lines made the ones listed: the others commented out, the missing added."""
    wanted = [_norm(line) for line in linux.get("timesync_servers") or []]
    if not wanted:
        return []
    conf = CHRONY_CONF.get(family, "/etc/chrony.conf")
    there = [_norm(line) for path, line in (_fields(rest, 2) for kind, rest in recs if kind == "timesync_server")
             if path == conf and CHRONY_SERVER.match(line)]
    return ([_item("chrony server line", line, "commented out") for line in there if line not in wanted]
            + [_item("chrony server line", "absent", line) for line in wanted if line not in there])


def _rule_lines_wanted(devices, kb, scheduler, lv_target):
    """The role's udev rule has lines: a read-ahead for a device LVM does not keep at it, or a scheduler line."""
    for d in devices:
        by_lvm = d["kind"] == "lvm" and not d.get("os") and lv_target not in (None, "auto") and d["lv_read_ahead"] == lv_target
        if (kb is not None and not by_lvm) or (scheduler != "keep" and d["kind"] == "disk"):
            return True
    return False


def _data_device_changes(recs, linux, live, boot):
    """The data devices (disks_apply.yml): read-ahead, scheduler and LVM read-ahead (live), the udev rule (no
    effect when the devices keep what they have now), tuned's profiles."""
    ra = str(linux.get("readahead_kb", 4))
    scheduler, lvm = str(linux.get("scheduler", "none")), str(linux.get("lvm_readahead", "") or "")
    if not boolean(linux.get("disk_tuning", True), strict=False) or not boot \
            or (ra == "kernel" and scheduler == "keep" and lvm != "auto"):
        return []
    if linux.get("block_devices") or linux.get("block_device"):
        return [_item("data devices", "set by cassandra_data_block_device(s)",
                      "tuned (the import does not work out which devices)")]
    devices = _disk_plan(recs, boolean(linux.get("tune_os_volumes", True), strict=False))["devices"]
    kb = None if ra == "kernel" else int(ra)
    items = _disk_live_items(devices, ra, scheduler, lvm) if live else []
    rule = NO_EFFECT if not items else CHANGE
    items += [_item(old, "present", "removed", rule) for old in _old_rules(recs)]
    items += _udev_rule_items(recs, devices, kb, scheduler, lvm, rule)
    if live and kb is not None and boolean(linux.get("tuned_manage", True), strict=False) \
            and _tuned_switch(recs, devices, kb):
        active = next((_fields(rest, 2)[0] for kind, rest in recs if kind == "tuned"), "")
        items.append(_item("tuned profiles", active, "%s cassandra-disks (applied again, Cassandra stopped)" % active))
    return items


def _disk_live_items(devices, ra, scheduler, lvm):
    """The read-ahead, scheduler and LVM read-ahead set live on the data devices (cassandra_disk_changes)."""
    state = dict((d["kname"], {"ra": d["ra"] or None, "rot": d["rota"], "sched": d["sched"] or "none",
                               "choices": "2" if d["kind"] == "disk" else "1"}) for d in devices)
    return [_item(c["item"] + (" (lvchange, Cassandra stopped)" if c["what"] == "lvm" else " (live)"),
                  c["before"], c["after"])
            for c in cassandra_disk_changes(devices, state, ra, lvm=True, scheduler=scheduler, lvm_readahead=lvm)]


def _udev_rule_items(recs, devices, kb, scheduler, lvm, rule):
    """The role's udev rule written when it has lines (_rule_lines_wanted), removed when it has none, its read-ahead
    made kb (rule: the effect, no effect when the devices keep what they have now)."""
    lv_target = "auto" if lvm == "auto" else str(kb * 2 or "none") if kb is not None else None
    rules = set(rest.split("|", 1)[0] for kind, rest in recs if kind == "udev")
    wanted = bool(devices) and _rule_lines_wanted(devices, kb, scheduler, lv_target)
    if wanted and OWN_UDEV_RULES[0] not in rules:
        return [_item(OWN_UDEV_RULES[0], "absent", "written", rule)]
    if not wanted and OWN_UDEV_RULES[0] in rules and devices:
        return [_item(OWN_UDEV_RULES[0], "present", "removed", rule)]
    if not wanted:
        return []
    set_by_it = set(int(v) for kind, rest in recs if kind == "udev" and rest.startswith(OWN_UDEV_RULES[0] + "|")
                    for v in READAHEAD.findall(rest))
    if kb is not None and set_by_it - set([kb]):
        return [_item(OWN_UDEV_RULES[0], "read_ahead_kb %s" % ", ".join(str(v) for v in sorted(set_by_it)),
                      "read_ahead_kb %d" % kb, rule)]
    return []


def cassandra_linux_changes(text, linux, facts=None, own=False):
    """What cassandra_linux, run on a node, would change there: text, the records import_cluster's script printed on
    it; linux: the role's variables for that node {sysctl, sysctl_file, limits, limits_file, nproc_all, swap_off,
    thp_enabled, thp_defrag, timesync, timesync_servers, offline, apply_live, container_types, disk_tuning,
    readahead_kb, scheduler, lvm_readahead, block_device, block_devices, tuned_manage, tune_os_volumes} (a missing
    one: the role's default); facts: its ansible_facts (os_family, virtualization); own: its limits file has
    the role's mark. -> [{what, now, would, effect}], effect "change" or "no effect" (a file the role writes that
    leaves the node's values as they are now). e.g. THP madvise on a node the role set up -> [{what:
    "transparent_hugepage enabled (live)", now: "madvise", would: "never", effect: "change"}]."""
    recs = _records(text)
    facts = facts or {}
    live, boot = _live_boot(linux, facts)
    family = facts.get("os_family", "")
    return (_sysctl_changes(recs, linux, live) + _swap_changes(recs, linux, live)
            + _limits_changes(recs, linux, own, family) + _thp_changes(recs, linux, live, boot)
            + _timesync_changes(recs, linux, family)
            + _data_device_changes(recs, linux, live, boot))


def cassandra_os_import(text, wanted):
    """text: the records import_cluster's script printed on a node; wanted:
    {sysctl, sysctl_file, limits, readahead_kb, scheduler, lvm_readahead, thp_enabled, thp_defrag, service_limits,
    timesync, ports, own, own_unit, os_family} (the roles' defaults; own: the node has cassandra_linux's own files).
    -> {'lines': [what was found, compared with the roles], 'vars': {the
    variables that give new nodes the same}, 'carried': [what they are, and
    what is not carried and why], 'keep': {what the roles leave as found on
    this node only (host_vars)}}."""
    recs = _records(text)
    wanted = wanted or {}
    lines, out, notes = [], {}, []
    own = boolean(wanted.get("own", False), strict=False)
    for part in (_sysctl(recs, wanted, own), _limits(recs, wanted, own), _unit_limits(recs, wanted),
                 _thp(recs, wanted), _disks(recs, wanted), (_tuned(recs, wanted), {}, []), _swap(recs),
                 _timesync(recs, wanted), _firewall(recs, wanted)):
        lines += part[0]
        out.update(part[1])
        notes += part[2]
    keep = dict((k, out.pop(k)) for k in list(out) if k in NODE_ONLY)
    return {"lines": lines, "vars": out, "carried": notes, "keep": keep}


def cassandra_sysctl_write_only(keys):
    """Kernel keys (cassandra_linux_sysctl's) -> the write-only ones among them (WRITE_ONLY_SYSCTL; a key written
    another way as sysctl reads it: vm/drop_caches), as given, sorted."""
    return sorted(str(k) for k in (keys or {}) if _sysctl_key(str(k)) in WRITE_ONLY_SYSCTL)


# ansible.posix.sysctl's reading of a value (its _parse_value, after its argument spec made it a string)
SYSCTL_TRUE = ("y", "yes", "on", "1", "true", "t")
SYSCTL_FALSE = ("n", "no", "off", "0", "false", "f")
# a key it runs as is, as the shell of os.yml gives it (else: not judged, run)
SYSCTL_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def _sysctl_module_value(value):
    value = str(value)
    return "1" if value.lower() in SYSCTL_TRUE else "0" if value.lower() in SYSCTL_FALSE else value.strip()


def _sysctl_module_file(record):
    """A "file|<path>|<real path>|<base64 or ->" record -> (real path, {key: value} as ansible.posix.sysctl reads
    the file, the last line of a key winning), None when not read or when a key has several lines (a write
    of another key keeps only its first one: what the next item reads is not what was read here)."""
    path, real, content = (record.split("|", 3) + ["", "", ""])[1:4]
    if content == "-":
        return None
    try:
        text = base64.b64decode(content, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    values = {}
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in values:
            return None
        values[key.strip()] = value.strip()
    return (real, values) if real else None  # (no real path: not told from /etc/sysctl.conf)


def cassandra_sysctl_todo(wanted, lines, live):
    """cassandra_linux_sysctl, the lines of os.yml's read (a file record for the role's file then /etc/sysctl.conf,
    a live record per key: "live|<key>|<base64 of sysctl -e -n>", - when it failed), whether keys are set live ->
    {'set': [...], 'remove': [...]}: the dict2items items whose ansible.posix.sysctl task would change something,
    setting it (state present) or removing it from /etc/sysctl.conf (state absent). Every item when a file was not
    read (or, set live, a value); every removal when both files are the same one (the set loop writes it first). Set live,
    every key after one that runs: a key written live may change another one (net.ipv4.ip_forward the forwarding
    of every interface, vm.dirty_bytes vm.dirty_ratio...), which the module reads again at its item."""
    items = [{"key": k, "value": v} for k, v in (wanted or {}).items()]
    files = [_sysctl_module_file(r) for r in lines or [] if r.startswith("file|")]
    procs = dict(r.split("|", 2)[1:3] for r in lines or [] if r.startswith("live|") and r.count("|") >= 2)
    keys = [str(i["key"]).strip() for i in items]
    # (a key the module would write otherwise, twice, or a value that writes other lines or that templating turns
    # into another value: what a later item reads is not what was read here)
    if (len(files) != 2 or None in files or len(set(keys)) != len(keys) or not all(SYSCTL_PLAIN_KEY.match(k) for k in keys)
            or any(c in _sysctl_module_value(i["value"]) for i in items for c in "\r\n")
            or any(str(i["value"]).strip()[:1] in ("[", "{") for i in items)):
        return {"set": items, "remove": items}
    (own_path, own), (conf_path, conf) = files

    def proc(key):
        if procs.get(key, "-") == "-":
            return None
        try:
            return base64.b64decode(procs[key], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    live = boolean(live, strict=False)

    def set_todo(item):
        key, value = str(item["key"]).strip(), _sysctl_module_value(item["value"])
        if item["value"] is None or value == "" or own.get(key) != value:
            return True  # (no value: the module refuses it)
        return live and (proc(key) is None or proc(key).split() != value.split())

    def remove_todo(item):
        key = str(item["key"]).strip()
        return own_path == conf_path or bool(conf.get(key))

    todo = []
    for item in items:
        if (todo and live) or set_todo(item):
            todo.append(item)
    return {"set": todo, "remove": [i for i in items if remove_todo(i)]}


class FilterModule(object):
    def filters(self):
        return {"cassandra_os_import": cassandra_os_import, "cassandra_sysctl_write_only": cassandra_sysctl_write_only,
                "cassandra_sysctl_todo": cassandra_sysctl_todo}
