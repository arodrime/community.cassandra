# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The block devices under Cassandra's directories, and which to tune.

Read-ahead is a page cache setting of the device a filesystem is mounted
from: the kernel takes it from that device's bdi (file_ra_state_init(),
inode_to_bdi()), for read() and for the read-around of mmap faults alike.
For a filesystem on an LVM volume, an md array, dm-crypt or multipath, that
is the top device (dm-N, mdN), not the disks under it: the bios are only
mapped down, nothing is read ahead again there. The devices under it still
matter: LVM's "auto" read-ahead and cryptsetup take theirs as a minimum when
they activate the device above, and they serve the reads issued to them
directly. So the top device is always tuned, and the devices under it too
when nothing else sits on them (a disk shared with the OS keeps its own).

A partition has no read-ahead of its own: its disk's is used.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import re
from collections import OrderedDict

ARROW = u" \u2192 "
# lsblk's columns read (ra, rota, sched: by import_cluster only, the role reads sysfs)
FIELDS = ("name", "type", "maj:min", "mountpoint", "fstype", "ra", "rota", "sched")
# the dm names of LVM's own layers: pools, snapshots, raid, mirror, cache, vdo, integrity, writecache
# (only for a volume lvs doesn't list; a volume named "real" itself would be taken as one: left alone)
LVM_INTERNAL = re.compile(r"((?<!-)-(tpool|real|cow|vpool)$|_(tdata|tmeta|tierdata|rimage_\d+|rmeta_\d+|mimage_\d+|mlog|"
                          r"corig|cdata|cmeta|cpool|cvol|vdata|vorigin|wcorig|iorig|imeta|pmspare)(-|$))")
# characters udev reads as a pattern in a match: no rule matches such an id
UDEV_UNSAFE = set('*?[]|"\\')


def _str(value):
    if value is None:
        return ""
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value).strip()


def parse_lsblk(text):
    """lsblk -J (the whole tree, top-down) -> {kname: node}. A device that sits
    on several others (an LVM volume on two disks, an md array) is listed under
    each of them: its parents are all of them. Old lsblk (2.32) prints every
    value as a string, newer ones numbers and booleans."""
    try:
        data = json.loads(text) if isinstance(text, str) else (text or {})
    except ValueError:
        return {}
    nodes = {}

    def walk(dev, parent):
        kname = _str(dev.get("kname") or dev.get("name"))
        if not kname:
            return
        node = nodes.setdefault(kname, {"kname": kname, "parents": [], "children": []})
        for key in FIELDS:
            if _str(dev.get(key)) and not node.get(key):
                node[key] = _str(dev.get(key))
        for key in FIELDS:
            node.setdefault(key, "")
        if parent and parent not in node["parents"]:
            node["parents"].append(parent)
            nodes[parent]["children"].append(kname)
        for child in dev.get("children") or []:
            walk(child, kname)

    for dev in (data or {}).get("blockdevices") or []:
        walk(dev, None)
    return nodes


def parse_lvs(lines):
    """lvs -a --noheadings --separator '|' -o lv_kernel_major,lv_kernel_minor,
    vg_name,lv_name,lv_read_ahead -> {"major:minor": {vg, lv, read_ahead,
    hidden}}. Hidden volumes (pool data, raid images...) are in brackets."""
    out = {}
    for line in lines or []:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue  # not active (-1)
        lv, ra = parts[3], parts[4].lower()
        try:
            ra = str(int(float(ra)))   # sectors (--units s --nosuffix)
        except ValueError:
            pass   # auto
        out["%s:%s" % (parts[0], parts[1])] = {
            "vg": parts[2], "lv": lv.strip("[]"), "hidden": lv.startswith("["), "read_ahead": ra}
    return out


def _findmnt(stdout):
    try:
        fs = json.loads(stdout or "{}").get("filesystems") or []
    except ValueError:
        return {}
    return fs[0] if fs else {}


def _kind(node, lvs):
    """disk, part, md, loop, dm, lvm (a volume lvchange can set), or
    lvm-internal (the data or metadata of a pool, raid images...: LVM's own
    parts, never read through their page cache)."""
    kname = node["kname"]
    if kname.startswith("dm-"):
        lv = lvs.get(node.get("maj:min", ""))
        if lv:
            return "lvm-internal" if lv["hidden"] else "lvm"
        # LVM's own layers lvs doesn't list as volumes (a pool's -tpool, a
        # snapshot's -real...); any other device lvs misses is tuned as a dm device
        if node.get("type") == "lvm" and LVM_INTERNAL.search(node.get("name", "")):
            return "lvm-internal"
        return "dm"
    if node.get("type") == "part":
        return "part"
    if kname.startswith("md"):
        return "md"
    if node.get("type") == "loop" or kname.startswith("loop"):
        return "loop"
    return "disk"


def _label(node):
    if node["kname"].startswith("dm-") and node.get("name") and node["name"] != node["kname"]:
        return "%s (%s)" % (node["name"], node["kname"])
    return node["kname"]


def _uppermost(nodes, kname):
    """What sits at the top of everything built on kname (itself if nothing)."""
    seen, out, todo = set(), [], [kname]
    while todo:
        k = todo.pop()
        if k in seen or k not in nodes:
            continue
        seen.add(k)
        children = nodes[k]["children"]
        if children:
            todo.extend(children)
        else:
            out.append(k)
    return sorted(out)


def _other_uses(nodes, kname, ours):
    """The uses of kname that are not Cassandra's: mounted or formatted
    devices on top of it (an empty partition is no use)."""
    out = []
    for k in _uppermost(nodes, kname):
        u = nodes[k]
        if u.get("maj:min") in ours:
            continue
        if u.get("mountpoint"):
            out.append(u["mountpoint"])
        elif u.get("fstype"):
            out.append("%s (%s, not mounted)" % (_label(u), u["fstype"]))
    return sorted(set(out))


def _levels(nodes, kname):
    """kname, then the devices under it, level by level."""
    levels, seen, level = [], set(), [kname]
    while level:
        level = [k for k in level if k in nodes and k not in seen]
        seen.update(level)
        if level:
            levels.append(level)
        nxt = []
        for k in level:
            nxt += [p for p in nodes[k]["parents"] if p not in nxt]
        level = nxt
    return levels


NOT_BLOCK = {
    "btrfs": "btrfs reads ahead through its own bdi, not its disks' (/sys/fs/btrfs/<uuid>/bdi/read_ahead_kb)",
    "zfs": "ZFS prefetches on its own (zfs_prefetch_disable), not through the disks' read-ahead",
}


def _dedupe(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _is_json(text):
    try:
        return isinstance(json.loads(text), dict)
    except (TypeError, ValueError):
        return False


def loop_results(loop):
    """A registered loop ({results: [...]}) or its results, as a list."""
    if isinstance(loop, dict):
        loop = loop.get("results") or []
    return [r for r in loop or [] if isinstance(r, dict) and not r.get("skipped")]


def _lookup(result):
    """A result of the findmnt loop -> (its filesystem, whether the directory
    was missing and looked up through a parent: "#missing" first)."""
    out = (result.get("stdout") or "") if result.get("rc", 1) == 0 else ""
    missing = out.startswith("#missing")
    return _findmnt(out[len("#missing"):].strip() if missing else out), missing


def mount_targets(mounts):
    """The mount points of the tuned directories (the results of the findmnt
    loop, see plan()), but /."""
    out = []
    for r in loop_results(mounts):
        item = r.get("item") or {}
        target = _str(_lookup(r)[0].get("target"))
        if item.get("tune") and target and target != "/" and target not in out:
            out.append(target)
    return out


def plan(lsblk, mounts, explicit=None, lvs=None, tune_shared=False, contents=None):
    """Which devices to tune, and why.

    lsblk: lsblk -J -o NAME,KNAME,MAJ:MIN,TYPE,MOUNTPOINT,FSTYPE (the whole
    tree). mounts: the results of findmnt -J -o MAJ:MIN,SOURCE,FSTYPE,TARGET
    --target looped over {dir, tune} (tune: false for the directories that
    only count as Cassandra's: hints, saved caches). explicit: the results of
    readlink -f looped over the devices set by hand (they replace the
    directories' devices). lvs: the lines of lvs -a --noheadings
    --separator '|' -o lv_kernel_major,lv_kernel_minor,vg_name,lv_name,
    lv_read_ahead. tune_shared: also tune the devices under them that hold
    other things. contents: the results of ls -A looped over mount_targets(),
    to say when a tuned filesystem holds more than Cassandra's directories.

    -> {devices: [{kname, label, kind, lv, lv_read_ahead, top, where}] to tune, top
    devices first; stacks: [a line per filesystem: its directories, then its
    devices level by level]; notes: [what is not tuned, and why]; unusable:
    [devices set by hand that can't be tuned]}."""
    nodes, notes = parse_lsblk(lsblk), []
    if not _is_json(lsblk) and loop_results(mounts) and not loop_results(explicit):
        return {"devices": [], "stacks": [], "unusable": [],
                "notes": ["not tuned: lsblk -J printed no devices (util-linux older than 2.27?)"]}
    lvs = parse_lvs(lvs)
    explicit = loop_results(explicit)
    contents = dict((r.get("item"), r.get("stdout_lines") or []) for r in loop_results(contents))
    by_majmin = dict((n.get("maj:min"), k) for k, n in nodes.items() if n.get("maj:min"))
    devices, stacks, unusable, index = [], [], [], {}

    def kind(k):
        return _kind(nodes[k], lvs)

    def add(k, top, where):
        if k in index:
            return
        n = nodes.get(k) or {"kname": k}
        k_kind = kind(k) if k in nodes else ("dm" if k.startswith("dm-") else "md" if k.startswith("md") else "disk")
        lv = lvs.get(n.get("maj:min", ""), {}) if k_kind == "lvm" else {}
        index[k] = len(devices)
        devices.append({"kname": k, "label": _label(n), "kind": k_kind, "top": top, "where": where,
                        "lv": "%s/%s" % (lv["vg"], lv["lv"]) if lv else "", "lv_read_ahead": lv.get("read_ahead", ""),
                        # what lsblk says now (import_cluster's report; the role reads sysfs)
                        "ra": n.get("ra", ""), "rota": n.get("rota", ""), "sched": n.get("sched", "")})

    def disk_of(k):
        """A partition reads ahead through its disk."""
        return nodes[k]["parents"][0] if kind(k) == "part" and nodes[k]["parents"] else k

    # the filesystems of the Cassandra directories
    ours, tops, targets, all_dirs = set(), OrderedDict(), {}, []   # ours: their maj:min; tops: {kname: [dirs]}
    mounted, commitlog = set(), set()
    for r in loop_results(mounts):
        item = r.get("item") or {}
        d, tune = item.get("dir"), item.get("tune", True)
        if item.get("commitlog"):
            commitlog.add(d)
        all_dirs.append(d)
        fs, missing = _lookup(r)
        if not fs:
            if tune and not explicit:
                notes.append("%s: not tuned, not found (not created yet?)" % d)
            continue
        majmin, target = _str(fs.get("maj:min")), _str(fs.get("target"))
        if missing and target == "/":
            # its own filesystem may not be mounted yet: never the OS one by mistake
            if tune and not explicit:
                notes.append("%s: not tuned, not created yet (it would be on the root filesystem)" % d)
            continue
        mounted.add(majmin)
        if not tune or explicit:
            if target != "/":   # hints or saved caches on the OS filesystem don't make its disks Cassandra's
                ours.add(majmin)
            continue
        kname = by_majmin.get(majmin)
        if majmin.startswith("0:") or not kname:
            fstype, source = _str(fs.get("fstype")), _str(fs.get("source"))
            why = NOT_BLOCK.get(fstype) or "%s on %s, not a block device" % (fstype or "?", source or "?")
            notes.append("%s: not tuned, %s" % (d, why))
            continue
        targets[kname] = target
        tops.setdefault(kname, []).append(d)

    for r in explicit:
        given = r.get("item")
        resolved = (_str(r.get("stdout")) if r.get("rc", 1) == 0 else "") or given
        k = resolved.rsplit("/", 1)[-1]
        if k not in nodes:   # /dev/mapper/<name>
            name = given.rsplit("/", 1)[-1]
            k = next((x for x, n in sorted(nodes.items()) if n.get("name") == name), k)
        if k not in nodes:
            add(k, True, given)   # lsblk doesn't see it (no udev, a test tree): taken as a disk, if sysfs has it
            stacks.append("%s%s%s" % (given, ARROW, k))
            continue
        ours.add(nodes[k].get("maj:min"))   # a device set by hand is Cassandra's
        tops.setdefault(k, []).append(given)

    # what the Cassandra filesystems sit on, before telling a device under them shared
    ours.update(nodes[k].get("maj:min") for k, dirs in tops.items()
                if not (targets.get(k) == "/" and set(dirs) <= commitlog))
    for kname, dirs in tops.items():
        top = disk_of(kname)
        if targets.get(kname) == "/" and set(dirs) <= commitlog:
            # read only at a restart, whole: no use tuning the OS for it
            notes.append("%s: not tuned, the commitlog alone on the root filesystem" % ", ".join(dirs))
            continue
        if kind(top) in ("loop", "lvm-internal"):
            if explicit:
                unusable.extend(dirs)
            else:
                notes.append("%s: not tuned, on %s (%s)" % (", ".join(dirs), _label(nodes[top]), nodes[top].get("type")))
            continue
        add(top, True, ", ".join(dirs))
        shown = []
        for level in _levels(nodes, kname):
            names = []
            for k in level:
                label = _label(nodes[k])
                if k != top and kind(k) not in ("part", "loop", "lvm-internal") and k not in index:
                    # loop, lvm-internal: their read-ahead is not the one files are read with
                    uses = _other_uses(nodes, k, ours)
                    if uses and not tune_shared:
                        label += " (not tuned: also %s)" % ", ".join(uses)
                        notes.append("%s: not tuned, it also holds %s (the devices above it are; "
                                     "cassandra_data_tune_shared_devices: true tunes it too)" % (_label(nodes[k]), ", ".join(uses)))
                    else:
                        add(k, False, "under " + _label(nodes[top]))
                names.append(label)
            shown.append(", ".join(names))
        line = ", ".join(dirs) + ARROW + ARROW.join(shown)
        if kname != top:
            uses = _other_uses(nodes, top, ours)
            if uses:
                line += " (%s is read through its disk %s, which also holds %s)" % (kname, top, ", ".join(uses))
        if kind(top) == "dm" and any(disk_of(p) not in index for p in nodes[top]["parents"]):
            line += (" (%s may get back the read-ahead of the device under it when it is activated again)"
                     % _label(nodes[top]))
        target = targets.get(kname)
        if target == "/":
            line += " (the root filesystem: every file on it gets this read-ahead)"
        elif target:
            mine = set(d[len(target):].lstrip("/").split("/")[0] for d in all_dirs
                       if d == target or d.startswith(target.rstrip("/") + "/"))
            others = [e for e in contents.get(target) or [] if e not in mine and e != "lost+found"]
            if others and "" not in mine:   # "": a directory is the mount point itself
                line += " (%s is shared: its other files get the same read-ahead)" % target
        stacks.append(line)
    if not lvs and any(nodes.get(d["kname"], {}).get("type") == "lvm" for d in devices):
        notes.append("lvs listed no LVM volume: they are tuned as device-mapper devices, kept by the udev rule"
                     " (LVM may set them back when it activates them)")
    # a device set by hand under a Cassandra filesystem: the device above it is the one read with
    for r in explicit:
        k = (_str(r.get("stdout")) if r.get("rc", 1) == 0 else r.get("item") or "").rsplit("/", 1)[-1]
        for u in _uppermost(nodes, k) if k in nodes else []:
            if u not in index and nodes[u].get("maj:min") in mounted:
                notes.append("%s: not tuned, though the Cassandra files on it are read with its read-ahead, not with the"
                             " one of %s under it: set it in cassandra_data_block_devices, or leave them empty"
                             % (_label(nodes[u]), k))
    return {"devices": devices, "stacks": stacks, "notes": _dedupe(notes), "unusable": unusable}


def _udev_value(value):
    return value and not (set(value) & UDEV_UNSAFE)


def udev_match(device, props, sysfs, by_name=False):
    """The stable match of a device for a udev rule, or "" when there is none.
    device: an entry of plan()'s devices; props: its udevadm properties
    {KEY: value}; sysfs: {dm_uuid, dm_name, md_uuid} read under /sys/block.
    Disks: their first stable id (WWN with extension, WWN, serial, path).
    dm devices: their uuid (LVM-..., CRYPT-..., mpath-...), else their name
    (chosen by the admin, unlike dm-N). md arrays: mdadm's uuid. by_name: a
    disk with no id is matched by its kernel name (a test tree only: on a
    host, sdX may be another disk after a reboot)."""
    kind = device.get("kind")
    if kind in ("dm", "lvm"):
        if _udev_value(sysfs.get("dm_uuid")):
            return 'KERNEL=="dm-*", ATTR{dm/uuid}=="%s"' % sysfs["dm_uuid"]
        if _udev_value(sysfs.get("dm_name")):
            return 'KERNEL=="dm-*", ATTR{dm/name}=="%s"' % sysfs["dm_name"]
        return ""
    if kind == "md":
        if _udev_value(props.get("MD_UUID")):
            return 'KERNEL=="md*", ENV{MD_UUID}=="%s"' % props["MD_UUID"]
        if _udev_value(sysfs.get("md_uuid")):
            return 'KERNEL=="md*", ATTR{md/uuid}=="%s"' % sysfs["md_uuid"]
        return ""
    # WWN first: an NVMe serial is shared by the namespaces of a drive; on
    # SCSI, ID_WWN is only the NAA prefix, shared by the LUNs of an array
    for key in ("ID_WWN_WITH_EXTENSION", "ID_WWN", "ID_SERIAL", "ID_PATH"):
        if _udev_value(props.get(key)):
            return 'ENV{%s}=="%s"' % (key, props[key])
    return 'KERNEL=="%s"' % device["kname"] if by_name and _udev_value(device.get("kname")) else ""
