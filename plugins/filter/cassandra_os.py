# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filter behind import_cluster's look at the OS tuning of a node.

cassandra_os_import: what import_cluster's read-only script printed on a node
    (kernel settings, limits, THP, disks, swap, tuned, time sync, firewall),
    and what the roles would set -> the report lines, and the variables that
    give the nodes added later the same tuning.

The script prints one record per line, "kind|field|...": the files as they
are (comments and blank lines left out), and the live values.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

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
    """-> lines, carried vars, carried lines."""
    role = dict((k, v) for k, v in (wanted.get("sysctl") or {}).items())
    role_file = wanted.get("sysctl_file") or "/etc/sysctl.d/60-cassandra.conf"
    files, live, link, order, effective = _sysctl_read(recs)
    # a file named for Cassandra holds Cassandra's settings, all of them
    owners = _owners(recs)
    cassandra_files = [p for p in order if "cassandra" in os.path.basename(p).lower() and _admin(p, owners)]
    extra = sorted({k for p in cassandra_files for k, dummy in files[p]} - set(role))
    relevant = list(role) + extra
    # a node the role set up keeps its own file's values, and only them (the role run again changes nothing)
    own_values = dict(files.get(role_file, [])) if own else None
    if own:
        relevant = list(role) + sorted(set(own_values) - set(role))
    lines = _sysctl_lines(files, order, effective, live, link, role, relevant, owners)
    carried, won_by = _sysctl_carried(role, relevant, effective, owners, own, own_values, role_file)
    out, notes = {}, []
    if not won_by and not own:
        # (the role's keys would be added, a change: none written, as on these nodes)
        if role:
            out["cassandra_linux_sysctl"] = {}
            notes.append("cassandra_linux_sysctl: {}, no file of these nodes sets the role's kernel settings:"
                         " cassandra_linux sets none")
        return lines, out, notes
    target = _sysctl_target(won_by, order, link, own, own_values, role_file)
    if not (own and own_values) and won_by:
        # the file the role is to write in, its keys exactly (a key of another file would be added there, and a
        # /etc/sysctl.conf line removed)
        carried = dict((k, _typed(v)) for k, v in files.get(target, []))
        others = sorted(k for k, path in won_by.items() if path != target and k not in carried)
        if others:
            notes.append("NOT carried: %s, set in other files than %s (the role would move them there)"
                         % (", ".join(others), target))
    if own:
        notes += _sysctl_lost(carried, effective, target)
    changed = sorted(k for k in carried if k not in role or _norm(carried[k]) != _norm(role[k]))
    missing = [k for k in role if k not in carried]
    if not carried and role:
        out["cassandra_linux_sysctl"] = {}
        notes.append("cassandra_linux_sysctl: {}, no file of these nodes sets the role's kernel settings where it"
                     " writes them: cassandra_linux sets none")
    elif changed or missing:
        out["cassandra_linux_sysctl"] = carried
        notes.append("cassandra_linux_sysctl: %s, as these nodes have them%s" % (
            ", ".join("%s %s" % (k, _norm(carried[k])) for k in changed) or "the same values",
            (" (without %s, as in their file)" % ", ".join(missing)) if missing
            else " (the role's other keys at their default)"))
    if target != role_file:
        out["cassandra_linux_sysctl_file"] = target
        notes.append("cassandra_linux_sysctl_file: %s, the file these nodes set them in: the role writes its keys"
                     " there (line by line, the file's other lines kept) instead of a second file that %s"
                     % (target, "this one would override" if os.path.basename(target) > os.path.basename(role_file)
                        else "could override it"))
    return lines, out, notes


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


def _sysctl_lost(carried, effective, target):
    """A node the role set up whose own file loses to another one: running the role again resets the live value
    (and takes the line out of /etc/sysctl.conf)."""
    notes = []
    for key in sorted(carried):
        won = effective.get(key, (None, None))[1]
        if won and won != target and (won == SYSCTL_CONF or _norm(effective[key][0]) != _norm(carried[key])):
            notes.append("CHANGE on these nodes if the roles run: %s = %s in %s wins over %s (the role sets"
                         " %s live%s): make them the same first" % (
                             key, effective[key][0], won, target, _norm(carried[key]),
                             ", and removes it from %s" % SYSCTL_CONF if won == SYSCTL_CONF else ""))
    return notes


# --- limits ---

def _limits(recs, wanted, own):
    role = wanted.get("limits") or {}
    groups = set()
    for kind, rest in recs:
        if kind == "groups":
            groups.update(rest.split())
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
    changes = [c["what"] for c in _disk_changes_of(recs, linux, True, True) if c["effect"] == CHANGE] \
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


def _firewall(recs, wanted):
    ports = [str(p) for p in wanted.get("ports") or []]
    fws = [_fields(rest, 3) for kind, rest in recs if kind == "fw"]
    rules = [_fields(rest, 2) for kind, rest in recs if kind == "fw_rule"]
    if not fws and not rules:
        return ["firewall: none active (firewalld, ufw), no iptables/nftables rule for the Cassandra ports"]
    lines = ["firewall: %s %s%s" % (name, state, (", " + extra) if extra else "") for name, state, extra in fws]
    lines += ["firewall %s: %s" % (where, _norm(line)) for where, line in rules]
    opened = [p for p in ports
              if any(re.search(r"(?<![\d-])%s(?!\d)" % re.escape(p.split("/")[0]), line) for dummy, line in rules)]
    lines.append("firewall: Cassandra ports open: %s; not seen open: %s (new nodes need the same: from your image,"
                 " or cassandra_manage_firewall: true and open_ports, which cassandra_firewall opens in the default"
                 " zone)" % (", ".join(opened) or "none", ", ".join(p for p in ports if p not in opened) or "none"))
    return lines


# --- what cassandra_linux would change on a node (the import's self-check) ---

ROLE_THP_UNIT = "/etc/systemd/system/disable-thp.service"
NPROC_FILE = "/etc/security/limits.d/90-nproc.conf"
NPROC_LINE = "* - nproc 32768"
NPROC_MATCH = re.compile(r"^\* - nproc 32768")  # cassandra_linux's lineinfile regexp
# what systemctl is-enabled says of a unit that starts at boot (or cannot be enabled: static)
ENABLED = ("enabled", "enabled-runtime", "static", "alias", "indirect", "generated")
CHANGE, NO_EFFECT = "change", "no effect"


def _item(what, now, would, effect=CHANGE):
    return {"what": what, "now": _norm(now), "would": _norm(would), "effect": effect}


THP_VALUES = {"enabled": ("always", "madvise", "never"), "defrag": ("always", "defer", "defer+madvise", "madvise", "never")}


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
    """cassandra_linux's sysctl module: each key in its file (and set live), the same keys out of /etc/sysctl.conf.
    A file line is no effect when the value read at boot stays the one the node has now."""
    wanted = [(_sysctl_key(str(k)), _norm(v)) for k, v in (linux.get("sysctl") or {}).items()]
    target = linux.get("sysctl_file") or "/etc/sysctl.d/60-cassandra.conf"
    files, now, link, dummy, dummy2 = _sysctl_read(recs)
    masked = {os.path.basename(rest) for kind, rest in recs if kind == "masked" and "/sysctl.d/" in rest}
    after = dict((p, list(lines)) for p, lines in files.items())
    edits = []  # (key, file, its value there now, its value after: None removed)
    for key, value in wanted:
        lines = after.setdefault(target, [])
        has = [v for k, v in lines if k == key]
        if not has or has[-1] != value:
            edits.append((key, target, has[-1] if has else None, value))
            after[target] = [(k, value if k == key else v) for k, v in lines] + ([] if has else [(key, value)])
        if target != SYSCTL_CONF and any(k == key for k, dummy in after.get(SYSCTL_CONF, [])):
            edits.append((key, SYSCTL_CONF, [v for k, v in after[SYSCTL_CONF] if k == key][-1], None))
            after[SYSCTL_CONF] = [(k, v) for k, v in after[SYSCTL_CONF] if k != key]
    at_boot = {}
    for path in _sysctl_order(list(after), link, masked):
        for k, v in after[path]:
            at_boot[k] = v
    items = []
    if live:
        # the module sets each key, and loads its whole file again when it changed it
        set_live = dict(after.get(target, [])) if any(path == target for dummy, path, dummy2, dummy3 in edits) else {}
        set_live.update(wanted)
        for key, value in set_live.items():
            if _norm(now.get(key, "")) != value:
                items.append(_item("sysctl %s (live)" % key, now.get(key) or "not read", value))
    for key, path, before, value in edits:
        effect = NO_EFFECT if key in now and _norm(at_boot.get(key, "")) == _norm(now[key]) else CHANGE
        items.append(_item("sysctl %s (%s)" % (key, path), "not set" if before is None else before,
                           "removed" if value is None else value, effect))
    return items


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
    target = linux.get("limits_file") or LIMITS_OWN
    groups = set()
    for kind, rest in recs:
        if kind == "groups":
            groups.update(rest.split())
    entries = [_fields(rest, 2) for kind, rest in recs if kind == "limits"]
    after, files = list(entries), []
    wanted = ["cassandra - %s %s" % (k, v) for k, v in role.items()]
    there = [line for path, line in entries if path == target]
    # (its mark: the default file of a node it set up; {}: no file written)
    if role and not (own and target == LIMITS_OWN and there == wanted):
        after = [e for e in after if e[0] != target] + [[target, line] for line in wanted]
        files.append(_item(target, "; ".join(there) or "absent", "; ".join(wanted)))
    block = [rest for kind, rest in recs if kind == "limits_block"]
    if any(kind == "limits_block_found" for kind, dummy in recs):
        for line in block:
            gone = next((e for e in after if e[0] == LIMITS_CONF and e[1] == line), None)
            if gone:
                after.remove(gone)
        files.append(_item("the block earlier versions wrote in %s" % LIMITS_CONF, "; ".join(block) or "empty",
                           "removed"))
    if family == "RedHat" and boolean(linux.get("nproc_all", True), strict=False):
        matches = [e for e in after if e[0] == NPROC_FILE and NPROC_MATCH.match(e[1])]
        if not matches:
            after.append([NPROC_FILE, NPROC_LINE])
            files.append(_item(NPROC_FILE, "no line %s" % NPROC_LINE, "%s added" % NPROC_LINE))
        elif matches[-1][1] != NPROC_LINE:  # (lineinfile replaces the last line its regexp finds)
            files.append(_item(NPROC_FILE, matches[-1][1], NPROC_LINE))
            after[after.index(matches[-1])] = [NPROC_FILE, NPROC_LINE]

    def effective(es):
        return _limits_effective(sorted(es, key=lambda e: (e[0] != LIMITS_CONF, os.path.basename(e[0]))), groups)

    before, then = effective(entries), effective(after)
    changed = []
    for item, dummy in sorted(set(before) | set(then)):
        if item not in changed and any((before.get((item, k)) or (None, None))[1] != (then.get((item, k)) or (None, None))[1]
                                       for k in ("soft", "hard")):
            changed.append(item)
    if not changed:
        return [dict(f, effect=NO_EFFECT) for f in files]
    return [_item("limits of the cassandra user: %s (its logins)" % item, _limit_value(before, item),
                  _limit_value(then, item)) for item in changed]


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


def _disk_changes_of(recs, linux, live, boot):
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
    lv_target = "auto" if lvm == "auto" else str(kb * 2 or "none") if kb is not None else None
    state = dict((d["kname"], {"ra": d["ra"] or None, "rot": d["rota"], "sched": d["sched"] or "none",
                               "choices": "2" if d["kind"] == "disk" else "1"}) for d in devices)
    items = []
    if live:
        for c in cassandra_disk_changes(devices, state, ra, lvm=True, scheduler=scheduler, lvm_readahead=lvm):
            items.append(_item(c["item"] + (" (lvchange, Cassandra stopped)" if c["what"] == "lvm" else " (live)"),
                               c["before"], c["after"]))
    rule = NO_EFFECT if not items else CHANGE
    items += [_item(old, "present", "removed", rule) for old in _old_rules(recs)]
    rules = set(rest.split("|", 1)[0] for kind, rest in recs if kind == "udev")
    wanted = bool(devices) and _rule_lines_wanted(devices, kb, scheduler, lv_target)
    if wanted and OWN_UDEV_RULES[0] not in rules:
        items.append(_item(OWN_UDEV_RULES[0], "absent", "written", rule))
    elif not wanted and OWN_UDEV_RULES[0] in rules and devices:
        items.append(_item(OWN_UDEV_RULES[0], "present", "removed", rule))
    elif wanted:
        set_by_it = set(int(v) for kind, rest in recs if kind == "udev" and rest.startswith(OWN_UDEV_RULES[0] + "|")
                        for v in READAHEAD.findall(rest))
        if kb is not None and set_by_it - set([kb]):
            items.append(_item(OWN_UDEV_RULES[0], "read_ahead_kb %s" % ", ".join(str(v) for v in sorted(set_by_it)),
                               "read_ahead_kb %d" % kb, rule))
    if live and kb is not None and boolean(linux.get("tuned_manage", True), strict=False) \
            and _tuned_switch(recs, devices, kb):
        active = next((_fields(rest, 2)[0] for kind, rest in recs if kind == "tuned"), "")
        items.append(_item("tuned profiles", active, "%s cassandra-disks (applied again, Cassandra stopped)" % active))
    return items


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
            + _disk_changes_of(recs, linux, live, boot))


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
                 _timesync(recs, wanted), (_firewall(recs, wanted), {}, [])):
        lines += part[0]
        out.update(part[1])
        notes += part[2]
    keep = dict((k, out.pop(k)) for k in list(out) if k in NODE_ONLY)
    return {"lines": lines, "vars": out, "carried": notes, "keep": keep}


class FilterModule(object):
    def filters(self):
        return {"cassandra_os_import": cassandra_os_import}
