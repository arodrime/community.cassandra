# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters behind cassandra_linux's disk tuning (see module_utils/cassandra_disks).

cassandra_disk_plan: the devices under the Cassandra directories, and which to tune.
cassandra_disk_mount_targets: the mount points of the tuned directories.
cassandra_disk_state: what the role read of each device under sysfs.
cassandra_disk_changes: what is to change on them.
cassandra_disk_udev_rules: the lines of the role's udev rule.
cassandra_disk_lv_read_ahead: the LVM read-ahead (lvchange --readahead) of a read-ahead in KB.
cassandra_disk_tuned_conflict: a warning when the active tuned profile sets another read-ahead."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.module_utils.parsing.convert_bool import boolean

from ansible_collections.community.cassandra.plugins.module_utils.cassandra_disks import (
    loop_results,
    mount_targets,
    plan,
    udev_match,
)


def cassandra_disk_plan(lsblk, mounts, explicit=None, lvs=None, tune_shared=False, contents=None):
    # "false" from -e is a string: it must not tune the OS disk
    return plan(lsblk, mounts, explicit=explicit, lvs=lvs, tune_shared=boolean(tune_shared, strict=False),
                contents=contents)


def cassandra_disk_state(results):
    """The results of the role's read of each device (key=value lines: ra,
    sched, choices, rot, dm_uuid, dm_name, md_uuid) -> {kname: {key: value}}."""
    out = {}
    for r in loop_results(results):
        state = {}
        for line in r.get("stdout_lines") or []:
            key, sep, value = line.partition("=")
            if sep:
                state[key] = value.strip()
        out[r.get("item")] = state
    return out


def cassandra_disk_lv_read_ahead(kb):
    """LVM keeps its read-ahead in 512-byte sectors; 0 is "none"."""
    sectors = int(kb) * 2
    return str(sectors) if sectors else "none"


def _has_scheduler(state):
    """A device with a scheduler to choose (a request queue: disks,
    multipath), not a device-mapper or md device (only "none")."""
    choices = state.get("choices", "0")
    return choices.isdigit() and int(choices) > 1


def cassandra_disk_changes(devices, state, read_back, lvm=True):
    """What is to change on the devices to tune: [{kname, what (lvm, ra or
    scheduler), item, before, after}], also the change report's items.
    read_back: the read-ahead in KB as the kernel keeps it (whole pages);
    lvm: LVM volumes are set with lvchange."""
    out = []
    read_back = int(read_back)
    for d in devices or []:
        s = state.get(d["kname"]) or {}
        by_lvm = d.get("kind") == "lvm" and boolean(lvm, strict=False) and d.get("lv_read_ahead") != str(read_back * 2)
        if by_lvm:
            out.append({"kname": d["kname"], "what": "lvm", "lv": d["lv"], "item": "LVM read-ahead %s (sectors)" % d["lv"],
                        "before": d.get("lv_read_ahead"), "after": cassandra_disk_lv_read_ahead(read_back)})
        if s.get("ra") not in (None, str(read_back)):
            # also written when lvchange sets it (in case it fails), reported once
            out.append({"kname": d["kname"], "what": "ra", "item": "read_ahead_kb %s" % d["label"],
                        "before": s["ra"], "after": str(read_back), "report": not by_lvm})
        if s.get("rot", "1") == "0" and _has_scheduler(s) and s.get("sched", "none") != "none":
            out.append({"kname": d["kname"], "what": "scheduler", "item": "IO scheduler %s" % d["label"],
                        "before": s["sched"], "after": "none"})
    return out


def cassandra_disk_udev_rules(devices, state, udev_info, readahead_kb, by_lvm=None, by_name=False):
    """The rule lines for the devices to tune but the LVM volumes LVM keeps
    the read-ahead of (by_lvm: their knames): the read-ahead, and the
    scheduler none when not rotational, for a disk or a dm device with a
    scheduler to choose (multipath). A device with no stable id gets a comment, no rule. Paths of
    a multipath device sharing one id give one rule. by_name: see udev_match.
    A device-mapper device is left alone while suspended (a scheduler
    written then would block until it resumes), as 10-dm.rules asks."""
    props = dict((r.get("item"), dict(line.split("=", 1) for line in r.get("stdout_lines") or [] if "=" in line))
                 for r in loop_results(udev_info))
    lines, by_lvm = [], by_lvm or []
    for d in devices or []:
        if d["kname"] in by_lvm:
            continue
        s = state.get(d["kname"]) or {}
        match = udev_match(d, props.get(d["kname"]) or {}, s, by_name=boolean(by_name, strict=False))
        if not match:
            lines.append("# %s: no stable id to match it by, not kept across reboots" % (d.get("label") or d["kname"]))
            continue
        head = 'ACTION=="add|change", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", %s' % match
        if match.startswith('KERNEL=="dm-*"'):
            head += ', ENV{DM_UDEV_DISABLE_OTHER_RULES_FLAG}!="1", ENV{DM_SUSPENDED}!="1"'
        for line in ['%s, ATTR{queue/read_ahead_kb}="%d"' % (head, int(readahead_kb))] + (
                ['%s, ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="none"' % head]
                if d.get("kind") == "disk" or _has_scheduler(s) else []):
            if line not in lines:
                lines.append(line)
    return lines


def _tuned_disk_sections(lines):
    """The [disk] sections of the active tuned profiles, merged as tuned does:
    an included profile first, then the one including it; the active profiles
    in their order, the last one winning. lines: "active|<profiles>" and
    "opt|<profile>|<section>|<key>|<value>"."""
    opts, active = {}, []
    for line in lines or []:
        kind, dummy, rest = line.partition("|")
        if kind == "active":
            active = rest.split()
        elif kind == "opt":
            parts = rest.split("|", 3)
            if len(parts) == 4:
                opts.setdefault(parts[0], {}).setdefault(parts[1], {})[parts[2]] = parts[3]

    def merge(dst, src):
        for section, kv in src.items():
            dst.setdefault(section, {}).update(kv)

    def profile(name, depth=0):
        out = {}
        if depth > 10 or name not in opts:
            return out
        for inc in opts[name].get("main", {}).get("include", "").replace(",", " ").split():
            merge(out, profile(inc, depth + 1))
        merge(out, opts[name])
        return out

    total = {}
    for name in active:
        merge(total, profile(name))
    return dict((s, kv) for s, kv in total.items()
                if s != "main" and kv.get("type", s) == "disk"
                and kv.get("enabled", "true").strip().lower() not in ("false", "0", "no", "off"))


def cassandra_disk_tuned_conflict(lines, kb):
    """lines: what the role read of the active tuned profiles (see
    _tuned_disk_sections); kb: the read-ahead set here. tuned's value is in
    KB (" s": sectors); ">" only raises it, "<" only lowers it. A section
    limited to some devices (devices=, devices_udev_regex=) is taken as kept
    off the Cassandra ones. -> a warning, or "" when tuned leaves them as set
    here."""
    kb = int(kb)
    active = next((line.partition("|")[2] for line in lines or [] if line.startswith("active|")), "")
    for section, kv in sorted(_tuned_disk_sections(lines).items()):
        value = kv.get("readahead", "").strip()
        m = re.match(r"^([<>]?)\s*(\d+)\s*(s)?", value)
        if not m or kv.get("devices") or kv.get("devices_udev_regex"):
            continue
        op, theirs = m.group(1), int(m.group(2)) // (2 if m.group(3) else 1)
        if (op == ">" and theirs > kb) or (op == "<" and theirs < kb) or (not op and theirs != kb):
            return ("WARNING: the active tuned profile (%s: [%s] readahead=%s) sets the read-ahead of the disks, and"
                    " of the device-mapper and md devices, to %d KB when tuned starts (at boot) or sees a new device,"
                    " instead of the %d KB set here. Use a tuned profile that leaves them alone (see the"
                    " cassandra_linux README)." % (active.strip(), section, value, theirs, kb))
    return ""


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_disk_plan": cassandra_disk_plan,
            "cassandra_disk_mount_targets": mount_targets,
            "cassandra_disk_state": cassandra_disk_state,
            "cassandra_disk_changes": cassandra_disk_changes,
            "cassandra_disk_udev_rules": cassandra_disk_udev_rules,
            "cassandra_disk_lv_read_ahead": cassandra_disk_lv_read_ahead,
            "cassandra_disk_tuned_conflict": cassandra_disk_tuned_conflict,
        }
