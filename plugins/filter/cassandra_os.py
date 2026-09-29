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

import os
import re

from ansible.module_utils.parsing.convert_bool import boolean

# Read in this order by sysctl --system (and systemd-sysctl): a file name
# found in several dirs is read from the first one, all of them sorted by file
# name, then /etc/sysctl.conf, last
SYSCTL_DIRS = ["/etc/sysctl.d", "/run/sysctl.d", "/usr/local/lib/sysctl.d", "/usr/lib/sysctl.d", "/lib/sysctl.d"]
SYSCTL_CONF = "/etc/sysctl.conf"
LIMITS_CONF = "/etc/security/limits.conf"
LIMITS_OWN = "/etc/security/limits.d/cassandra.conf"  # cassandra_linux's
OWN_UDEV_RULE = "/etc/udev/rules.d/61-cassandra-data-disk.rules"  # cassandra_linux's
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
    # a file named for Cassandra holds Cassandra's settings, all of them
    owners = _owners(recs)
    cassandra_files = [p for p in order if "cassandra" in os.path.basename(p).lower() and _admin(p, owners)]
    extra = sorted({k for p in cassandra_files for k, dummy in files[p]} - set(role))
    relevant = list(role) + extra
    # a node the role set up keeps its own file's values, and only them (the role run again changes nothing)
    own_values = dict(files.get(role_file, [])) if own else None
    if own:
        relevant = list(role) + sorted(set(own_values) - set(role))

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

    # Carried: the value the node gets from the admin's files
    if own and own_values:  # exactly its own file (a key it lacks would be added)
        carried = dict((k, _typed(v)) for k, v in own_values.items())
        won_by = dict.fromkeys(carried, role_file)
    elif own:
        # nothing in the role's file (another cassandra_linux_sysctl_file, an older role): the role's
        # keys an admin file sets, in that file; the others not written (the role's defaults would be)
        carried, won_by = {}, {}
        for key in role:
            if key in effective and _admin(effective[key][1], owners):
                carried[key] = _typed(effective[key][0])
                won_by[key] = effective[key][1]
    else:
        carried, won_by = dict(role), {}
        for key in relevant:
            if key in effective and _admin(effective[key][1], owners):
                carried[key] = _typed(effective[key][0])
                won_by[key] = effective[key][1]
    out, notes = {}, []
    if not won_by and not own:
        return lines, out, notes
    counts = {}
    for path in won_by.values():
        counts[path] = counts.get(path, 0) + 1
    # the file that sets most of them (the last read on a tie)
    # (not /etc/sysctl.conf when systemd-sysctl does not read it)
    candidates = [p for p in counts if link or p != SYSCTL_CONF] or [role_file]
    target = role_file if own and (own_values or not won_by) else \
        max(candidates, key=lambda p: (counts.get(p, 0), order.index(p) if p in order else -1))
    if own and not own_values and won_by:
        # the role would add a key to the target file (and drop /etc/sysctl.conf's): only the target's are carried
        others = sorted(k for k, path in won_by.items() if path != target)
        for key in others:
            del carried[key]
        if others:
            notes.append("NOT carried: %s, set in other files than %s (the role would move them there)"
                         % (", ".join(others), target))
    if own:
        # the role's own file loses to another one: running the role again resets the live value (and
        # takes the line out of /etc/sysctl.conf)
        for key in sorted(carried):
            won = effective.get(key, (None, None))[1]
            if won and won != target and (won == SYSCTL_CONF or _norm(effective[key][0]) != _norm(carried[key])):
                notes.append("CHANGE on these nodes if the roles run: %s = %s in %s wins over %s (the role sets"
                             " %s live%s): make them the same first" % (
                                 key, effective[key][0], won, target, _norm(carried[key]),
                                 ", and removes it from %s" % SYSCTL_CONF if won == SYSCTL_CONF else ""))
    changed = sorted(k for k in carried if k not in role or _norm(carried[k]) != _norm(role[k]))
    missing = [k for k in role if k not in carried]
    if changed or missing:
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
    effective = {}  # (item, soft|hard) -> (rank, value, path, domain)
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

    items = list(role) + sorted({i for (i, dummy), e in effective.items() if e[0] < 3} - set(role))
    lines, carried, notes, skipped = [], dict(role), [], []
    # the cassandra user's lines another file overrides
    for path, line in entries:
        parts = line.split()
        if len(parts) < 4 or not (parts[0] == "cassandra" or parts[0].startswith("@") and parts[0][1:] in groups):
            continue
        won = [effective[(parts[2], k)][2] for k in ("soft", "hard") if (parts[2], k) in effective]
        if won and path not in won:
            lines.append("limits %s: %s %s %s = %s, overridden by %s" % (
                _label(path, owners), parts[0], parts[1], parts[2], parts[3], _label(won[-1], owners)))
    for item in items:
        soft, hard = effective.get((item, "soft")), effective.get((item, "hard"))
        if not soft and not hard:
            continue
        same = soft and hard and soft[1:] == hard[1:]
        if same:
            text = "%s: %s %s = %s%s" % (_label(soft[2], owners), soft[3], item, soft[1], _vs(soft[1], role.get(item), "cassandra_linux"))
        else:
            text = "; ".join("%s: %s %s %s = %s" % (_label(e[2], owners), e[3], k, item, e[1])
                             for k, e in (("soft", soft), ("hard", hard)) if e)
            text += " (cassandra_linux: %s)" % role[item] if item in role else ""
        lines.append("limits " + text)
        if own:
            continue
        if soft and hard and soft[0] < 3 and hard[0] < 3:
            if soft[1] == hard[1]:
                carried[item] = _typed(soft[1])
            else:
                skipped.append(item)
    if own:
        # a node the role set up keeps exactly its own file, in its order (the role writes it whole)
        carried = {}
        for path, line in entries:
            parts = line.split()
            if path == LIMITS_OWN and len(parts) >= 4 and parts[0] == "cassandra" and parts[1] == "-":
                carried[parts[2]] = _typed(parts[3])
        if not carried:  # not in the role's file (another name, an older role): the values in effect
            for item in items:
                soft, hard = effective.get((item, "soft")), effective.get((item, "hard"))
                if soft and hard and soft[0] < 3 and hard[0] < 3 and soft[1] == hard[1]:
                    carried[item] = _typed(soft[1])
                elif soft or hard:
                    skipped.append(item)
    changed = sorted(k for k in carried if k not in role or _norm(carried[k]) != _norm(role[k]))
    out = {}
    if [(k, _norm(v)) for k, v in carried.items()] != [(k, _norm(v)) for k, v in role.items()]:
        changed = changed or sorted(carried)
        out["cassandra_linux_limits"] = carried
        notes.append("cassandra_linux_limits: %s, as these nodes give the cassandra user"
                     % ", ".join("%s %s" % (k, carried[k]) for k in changed))
    if skipped:
        notes.append("NOT carried: the cassandra user's %s (soft and hard differ; the role sets both to one value)"
                     % ", ".join(skipped))
    return lines, out, notes


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


def _thp(recs):
    now = dict(_fields(rest, 2) for kind, rest in recs if kind == "thp")
    if not now:
        return []
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
    now = "%s %s" % (enabled, defrag)
    line = "THP now: enabled %s, defrag %s%s" % (enabled, defrag, _vs(now, "never never", "cassandra_linux"))
    return [line] + ["THP set by %s" % b for b in by]


def _swap(recs):
    fstab = [rest.strip() for kind, rest in recs if kind == "swap_fstab"]
    active = [rest.strip() for kind, rest in recs if kind == "swap_on"]
    if not fstab and not active:
        return ["swap: none active, none in /etc/fstab (same as cassandra_linux)"]
    return (["swap active: %s (cassandra_linux: none)" % a for a in active]
            + ["swap in /etc/fstab: %s (cassandra_linux removes it)" % f for f in fstab])


def _disks(recs, wanted):
    role_ra = wanted.get("readahead_kb")
    lines, live = [], []
    for kind, rest in recs:
        if kind != "disk":
            continue
        d, source, dtype, disk, ra, sched, rot = _fields(rest, 7)
        if not source:
            lines.append("disk of %s: not found (no such directory yet?)" % d)
            continue
        if not disk or dtype not in ("disk", "part"):
            lines.append("disk of %s: %s (%s), not a plain disk: its read-ahead is not read" % (d, source or "?", dtype or "?"))
            continue
        live.append(ra)
        role_sched = "none" if rot == "0" else None
        lines.append("disk %s (%s, %s): read_ahead_kb %s%s, scheduler %s%s" % (
            disk, d, "SSD" if rot == "0" else "rotational", ra, _vs(ra, role_ra, "cassandra_linux"),
            sched, _vs(sched, role_sched, "cassandra_linux") if role_sched else ""))
    rules, owners = {}, _owners(recs)
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
    out, notes = {}, []
    values = set().union(*rules.values()) if rules else set()
    own_rule = rules.get(OWN_UDEV_RULE) if boolean(wanted.get("own", False), strict=False) else None
    if own_rule is not None:
        values = set(own_rule)  # a node the role set up: its own rule (the others are left as they are)
    if len(values) == 1:
        value = values.pop()
        if live and any(ra != str(value) for ra in live):
            notes.append("NOT carried: read_ahead_kb %d of the udev rules, the data disks have %s now"
                         % (value, ", ".join(sorted(set(live)))))
        elif role_ra is None or str(value) != str(role_ra):
            out["cassandra_data_readahead_kb"] = value
            notes.append("cassandra_data_readahead_kb: %d, as the udev rules of these nodes set it (the role writes"
                         " its own rule, 61-cassandra-data-disk.rules, for the data disks only)" % value)
    elif len(values) > 1:
        notes.append("NOT carried: the udev rules set several read-aheads (%s), set cassandra_data_readahead_kb"
                     % ", ".join(str(v) for v in sorted(values)))
    return lines, out, notes


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
    if not names:
        lines.append("time sync: none active (cassandra_linux starts chrony, or systemd-timesyncd on Debian/Ubuntu)")
        if boolean(wanted.get("own", False), strict=False) and boolean(wanted.get("timesync", True), strict=False):
            # a node cassandra_linux set up, run again: it would install and start chrony there
            out["cassandra_linux_timesync"] = False
            notes.append("cassandra_linux_timesync: false, no time sync runs on these nodes (the role run again would"
                         " install and start chrony there): set it to true to have it, Cassandra needs synchronized clocks")
    elif not any(n in OWN_TIMESYNC for n in names) and boolean(wanted.get("timesync", True), strict=False):
        out["cassandra_linux_timesync"] = False
        notes.append("cassandra_linux_timesync: false, these nodes keep their time with %s: new nodes must get it the"
                     " same way (your image) instead of chrony, whose unit stops it" % ", ".join(names))
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


def cassandra_os_import(text, wanted):
    """text: the records import_cluster's script printed on a node; wanted:
    {sysctl, sysctl_file, limits, readahead_kb, service_limits, timesync,
    ports, own, own_unit} (own: the node has cassandra_linux's own files).
    -> {'lines': [what was found, compared with the roles], 'vars': {the
    variables that give new nodes the same}, 'carried': [what they are, and
    what is not carried and why]}."""
    recs = _records(text)
    wanted = wanted or {}
    lines, out, notes = [], {}, []
    own = boolean(wanted.get("own", False), strict=False)
    for part in (_sysctl(recs, wanted, own), _limits(recs, wanted, own), _unit_limits(recs, wanted),
                 (_thp(recs), {}, []), _disks(recs, wanted), (_tuned(recs, wanted), {}, []), (_swap(recs), {}, []),
                 _timesync(recs, wanted), (_firewall(recs, wanted), {}, [])):
        lines += part[0]
        out.update(part[1])
        notes += part[2]
    return {"lines": lines, "vars": out, "carried": notes}


class FilterModule(object):
    def filters(self):
        return {"cassandra_os_import": cassandra_os_import}
