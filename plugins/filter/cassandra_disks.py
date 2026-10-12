# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Filters behind cassandra_linux's disk tuning (see module_utils/cassandra_disks).

cassandra_disk_plan: the devices under the Cassandra directories, and which to tune.
cassandra_disk_mount_targets: the mount points of the tuned directories.
cassandra_disk_state: what the role read of each device under sysfs.
cassandra_disk_changes: what is to change on them.
cassandra_disk_udev_rules: the lines of the role's udev rule.
cassandra_disk_lv_read_ahead: the LVM read-ahead (lvchange --readahead) of a read-ahead in KB.
cassandra_disk_tuned_conflict: a warning when the active tuned profile sets another read-ahead.
cassandra_disk_tuned_profile: the tuned profile that keeps the active one off the Cassandra devices."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import fnmatch
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


# tuned: the profile of the role, added after the active one(s) (tuned merges
# the profiles it runs), takes the Cassandra devices off their [disk] sections
TUNED_PROFILE = "cassandra-disks"
TUNED_MARK = "Managed by Ansible (community.cassandra.cassandra_linux)"
TUNED_ID_KEYS = ("ID_WWN_WITH_EXTENSION", "ID_WWN", "ID_SERIAL", "ID_PATH")
TUNED_TRUE = ("True", "true", "1")   # tuned's own (profiles/unit.py)


def _tuned_read(lines):
    """What the role read of tuned: "state|active", "version|<tuned-adm
    --version>", "active|<profiles>", "opt|<profile>|<section>|<key>|<value>"
    -> (state, version, active, opts)."""
    state, version, active, opts = "", (), [], {}
    for line in lines or []:
        kind, dummy, rest = line.partition("|")
        if kind == "state":
            state = rest.strip()
        elif kind == "version":
            m = re.search(r"tuned-adm\s+(\d+)\.(\d+)", rest)
            version = (int(m.group(1)), int(m.group(2))) if m else ()
        elif kind == "active":
            active = rest.split()
        elif kind == "opt":
            parts = rest.split("|", 3)
            if len(parts) == 4:
                opts.setdefault(parts[0], {}).setdefault(parts[1], {})[parts[2]] = parts[3]
    return state, version, active, opts


def _tuned_unit(section, kv):
    """A section as tuned's Unit reads it."""
    drop = kv.get("drop")
    return {"name": section, "type": kv.get("type", section), "enabled": kv.get("enabled", "True") in TUNED_TRUE,
            "devices": kv.get("devices", "*"), "devices_udev_regex": kv.get("devices_udev_regex"),
            "priority": kv.get("priority"), "replace": kv.get("replace", "") in TUNED_TRUE,
            "drop": re.split(r"\b\s*[,;]\s*", drop) if drop else None,
            "options": dict((k, v) for k, v in kv.items() if k in ("readahead", "elevator"))}


def _tuned_units(profiles, opts):
    """The units of these profiles merged as tuned's Merger does: an included
    profile first, then the one including it; the profiles in their order. A
    unit of the same name gets the type, enabled and devices of the newer one
    (their defaults too), its other options are added (replace: it is
    replaced, drop: options removed)."""
    def merge(dst, src):
        for name, unit in src.items():
            if unit["replace"] or name not in dst:
                dst[name] = dict(unit, options=dict(unit["options"]))
                continue
            old = dst[name]
            old.update(type=unit["type"], enabled=unit["enabled"], devices=unit["devices"])
            for key in ("priority", "devices_udev_regex"):
                if unit[key] is not None:
                    old[key] = unit[key]
            for option in unit["drop"] or []:
                old["options"].pop(option, None)
            old["options"].update(unit["options"])

    def profile(name, depth=0):
        out = {}
        if depth > 10 or name not in opts:
            return out
        for inc in re.split(r"\b\s*[,;]\s*", opts[name].get("main", {}).get("include", "").strip()):
            if inc:
                merge(out, profile(inc, depth + 1))
        merge(out, dict((s, _tuned_unit(s, kv)) for s, kv in opts[name].items() if s != "main"))
        return out

    total = {}
    for name in profiles:
        merge(total, profile(name))
    return total


def _fnmatch(rules, name):
    """tuned's DeviceMatcher: glob rules, "!" ones excluding, all when no other."""

    rules = [r for r in re.split(r"\s|,\s*", rules or "") if r.strip()]
    positive = [r for r in rules if not r.startswith("!")] or ["*"]
    negative = [r[1:] for r in rules if r.startswith("!")]
    return any(fnmatch.fnmatch(name, r) for r in positive) and not any(fnmatch.fnmatch(name, r) for r in negative)


def _tuned_props(devices, state, udev_info):
    """{kname: the udev properties tuned matches a device by}: udevadm's, and
    for device-mapper devices their uuid and name (as 10-dm.rules sets them)."""
    props = dict((r.get("item"), dict(line.split("=", 1) for line in r.get("stdout_lines") or [] if "=" in line))
                 for r in loop_results(udev_info))
    out = {}
    for d in devices or []:
        p, s = dict(props.get(d["kname"]) or {}), state.get(d["kname"]) or {}
        if s.get("dm_uuid"):
            p["DM_UUID"] = s["dm_uuid"]
        if s.get("dm_name"):
            p["DM_NAME"] = s["dm_name"]
        out[d["kname"]] = p
    return out


def _tuned_takes(units, device, props):
    """The [disk] unit tuned gives the device to: the first enabled one, by
    priority, matching it."""
    ordered = sorted(((int(u["priority"]) if u["priority"] not in (None, "") else 0, i, u)
                      for i, u in enumerate(units.values())), key=lambda x: (x[0], x[1]))
    for dummy, dummy2, u in ordered:
        if u["type"] != "disk" or not u["enabled"]:
            continue
        if u["devices_udev_regex"] is not None:
            text = "".join("%s=%s\n" % (k, v) for k, v in sorted(props.items()))
            try:
                if re.search(u["devices_udev_regex"], text, re.MULTILINE):
                    return u
            except re.error:
                continue
        elif _fnmatch(u["devices"], device["kname"]):
            return u
    return None


def _tuned_sets(unit, kb, ssd):
    """What the unit sets that the role doesn't: (value, what), or None.
    tuned's read-ahead is in KB (" s": sectors); ">" only raises it, "<"
    only lowers it. A scheduler counts on an SSD only (the role sets none)."""
    value = unit["options"].get("readahead", "").strip()
    m = re.match(r"^([<>]?)\s*(\d+)\s*(s)?", value)
    if m:
        op, theirs = m.group(1), int(m.group(2)) // (2 if m.group(3) else 1)
        if (op == ">" and theirs > kb) or (op == "<" and theirs < kb) or (not op and theirs != kb):
            return "readahead=" + value, "a read-ahead of %d KB" % theirs
    elevator = unit["options"].get("elevator", "").strip()
    if ssd and elevator and elevator != "none":
        return "elevator=" + elevator, "the IO scheduler " + elevator
    return None


def _tuned_ids(devices, props):
    """The udev properties the tuned profile matches the devices by (as the
    udev rule does), and the devices with none."""
    ids, none = [], []
    for d in devices or []:
        p = props.get(d["kname"]) or {}
        keys = ("DM_UUID", "DM_NAME") if d.get("kind") in ("dm", "lvm") else \
            ("MD_UUID",) if d.get("kind") == "md" else TUNED_ID_KEYS
        # not ones its config file would read otherwise: % (interpolation), # (comment), ${ (variable)
        found = ["%s=%s" % (k, p[k]) for k in keys if p.get(k) and not re.search(r"[%#$\s]", p[k])]
        if found:
            if found[0] not in ids:
                ids.append(found[0])
        else:
            none.append(d.get("label") or d["kname"])
    return ids, none


def _tuned(lines, devices, state, udev_info, kb):
    """Where tuned stands for the Cassandra devices: {conflict: (device,
    profile section, value, what) of a device its active profiles set, or
    None; profile: the one to add (see cassandra_disk_tuned_profile), why:
    why there is none though a conflict, active, base}."""
    st, version, active, opts = _tuned_read(lines)
    base = [p for p in active if p != TUNED_PROFILE]
    props = _tuned_props(devices, state, udev_info)
    units = _tuned_units(active, opts)
    conflict, first = None, False
    for d in devices or []:
        s = state.get(d["kname"]) or {}
        unit = _tuned_takes(units, d, props.get(d["kname"]) or {})
        found = unit and _tuned_sets(unit, int(kb), s.get("rot", "1") == "0"
                                     and (d.get("kind") == "disk" or _has_scheduler(s)))
        if found:
            conflict = (d.get("label") or d["kname"], unit["name"]) + found
            ours = units.get("cassandra_disks")
            # the role's profile matches it, yet another section takes it first
            first = bool(ours and ours["devices_udev_regex"] and _tuned_takes({"x": ours}, d, props.get(d["kname"]) or {}))
            break
    out = {"conflict": conflict, "profile": {}, "why": "", "active": active, "base": base}
    if not conflict:
        return out
    ids, unmatched = _tuned_ids(devices, props)
    if st != "active":
        out["why"] = "tuned is not running"
    elif not version or version < (2, 10):
        out["why"] = ("tuned %d.%d is older than 2.10 (not tested)" % (version[0], version[1]) if version
                      else "tuned-adm --version said no version")
    elif any(r.get("rc", 0) != 0 for r in loop_results(udev_info)):
        out["why"] = "udevadm could not read the ids of all the Cassandra devices"
    elif not ids:
        out["why"] = "the Cassandra devices have no stable id"
    elif not base:
        out["why"] = "no tuned profile is active"
    elif active == base + [TUNED_PROFILE] and first:
        out["why"] = "its [%s] section takes them before the role's profile (a priority number as low or lower)" % conflict[1]
    else:
        # tuned's regex, escaping what a regex reads, nothing else (tuned keeps the backslashes)
        regex = "^(%s)$" % "|".join(re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", i) for i in ids)
        out["profile"] = {"name": TUNED_PROFILE, "base": base, "active": active, "unmatched": unmatched,
                          "profiles": base + [TUNED_PROFILE], "lines": [
                              "# %s: change the role variables, not this file." % TUNED_MARK,
                              "# Run after the other profiles: tuned-adm profile <them> %s" % TUNED_PROFILE,
                              "[main]",
                              "summary=%s: the Cassandra devices taken off the [disk] sections of the profiles"
                              " run before it" % TUNED_MARK,
                              "",
                              "# a lower priority number: given its devices first, it sets nothing on them",
                              "[cassandra_disks]",
                              "type=disk",
                              "priority=-100",
                              "dynamic=0",
                              "devices_udev_regex=%s" % regex]}
    return out


def cassandra_disk_tuned_profile(lines, devices, state, udev_info, kb):
    """The tuned profile the role adds after the active one(s) when they would
    set the read-ahead (or scheduler) of the Cassandra devices: {} when not
    needed or not possible, else {name, base, active, profiles (what tuned is
    to run), unmatched, lines}. tuned merges the profiles it runs; this one
    adds a [disk] instance of a lower priority number (tuned gives each
    device to the first instance that matches it) that takes the Cassandra
    devices, matched by their stable udev ids, and sets nothing on them."""
    return _tuned(lines, devices, state, udev_info, kb)["profile"]


def cassandra_disk_tuned_conflict(lines, kb, devices=None, state=None, udev_info=None, managed=False, live=True):
    """A warning when the active tuned profile(s) set another read-ahead (or
    a scheduler) on a Cassandra device: "" otherwise, or when the role takes
    care of it (managed: cassandra_linux_tuned_manage; live: the role applies
    settings live, cassandra_linux_apply_live)."""
    if devices is None:
        devices = [{"kname": "sdx", "kind": "disk", "label": "the disks"}]
    t = _tuned(lines, devices, state or {}, udev_info, kb)
    managed = boolean(managed, strict=False)
    if not t["conflict"] or (managed and boolean(live, strict=False) and t["profile"]):
        return ""
    device, section, value, what = t["conflict"]
    if not managed:
        hint = "cassandra_linux_tuned_manage: true keeps them off the Cassandra devices."
    elif not boolean(live, strict=False):
        hint = "The role applies no setting live here (cassandra_linux_apply_live): it leaves tuned alone."
    else:
        hint = t["why"] + ": the role can't keep them off."
    instead = "the %d KB set here" % int(kb) if what.startswith("a read-ahead") else "none"
    return ("WARNING: the active tuned profile (%s: [%s] %s) sets %s, and the other devices its [%s] section"
            " takes, to %s when tuned starts (at boot) or sees a new device, instead of %s. %s"
            % (" ".join(t["active"]), section, value, device, section, what, instead, hint))


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
            "cassandra_disk_tuned_profile": cassandra_disk_tuned_profile,
        }
