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
directly. So the whole stack is tuned, the top device and every device under
it, whatever else they hold: Cassandra is taken as alone on its storage
(a disk under it that also holds an OS filesystem is left alone only when
the OS volumes are, tune_os false).

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
# (a -layer one also for what lvs lists, with its volume's name before it: vg-pool-tpool, not a volume vg-real)
LVM_INTERNAL = re.compile(r"((?<!-)-(tpool|real|cow|vpool)$|_(tdata|tmeta|tierdata|rimage_\d+|rmeta_\d+|mimage_\d+|mlog|"
                          r"corig|cdata|cmeta|cpool|cvol|vdata|vorigin|wcorig|iorig|imeta|pmspare)(-|$))")
# the filesystems of the OS: tuned too when Cassandra's data is on them (unless
# tune_os is false; not for the commitlog, hints or saved caches alone), with a
# note; their LVM volumes are not reloaded (lvchange)
OS_MOUNTS = ("/", "/usr", "/var", "/var/log", "/var/lib", "/var/tmp", "/home", "/opt", "/tmp", "/boot", "/boot/efi")
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
        # LVM's own layers (a pool's -tpool, a snapshot's -real...), also when
        # lvs gives a volume their numbers (a thin pool's): never reloaded
        name = node.get("name", "")
        internal = node.get("type") == "lvm" and LVM_INTERNAL.search(name)
        if internal and len(re.split(r"(?<!-)-(?!-)", name)) >= 3:
            return "lvm-internal"
        lv = lvs.get(node.get("maj:min", ""))
        if lv:
            return "lvm-internal" if lv["hidden"] else "lvm"
        # the other layers lvs doesn't list; any other device it misses: tuned as a dm device
        return "lvm-internal" if internal else "dm"
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
    loop, see plan()), but the OS's."""
    out = []
    for r in loop_results(mounts):
        item = r.get("item") or {}
        target = _str(_lookup(r)[0].get("target"))
        if item.get("tune") and target and target not in OS_MOUNTS and target not in out:
            out.append(target)
    return out


def _name(node):
    """A device as the default view names it: a device-mapper one by its name
    (vg-lv, the mapper name: chosen by the admin), not dm-N."""
    if node["kname"].startswith("dm-") and node.get("name"):
        return node["name"]
    return node["kname"]


def _dirs(dirs):
    """Directories, short: the ones of one parent as parent/{a,b}."""
    groups = OrderedDict()   # {(parent, True): [names]}, {(directory, False): []} for one with no parent to share
    for d in dirs:
        parent, sep, base = d.rpartition("/")
        if sep and parent and base:
            groups.setdefault((parent, True), []).append(base)
        else:
            groups.setdefault((d, False), [])
    return ", ".join("%s/%s" % (p, bases[0] if len(bases) == 1 else "{%s}" % ",".join(bases)) if grouped else p
                     for (p, grouped), bases in groups.items())


# filesystems with no block device that no one means to tune (a container, a test): said with -v only
QUIET_FSTYPES = ("overlay", "tmpfs", "squashfs")


def plan(lsblk, mounts, explicit=None, lvs=None, contents=None, tune_os=True):
    """Which devices to tune, and why.

    lsblk: lsblk -J -o NAME,KNAME,MAJ:MIN,TYPE,MOUNTPOINT,FSTYPE (the whole
    tree). mounts: the results of findmnt -J -o MAJ:MIN,SOURCE,FSTYPE,TARGET
    --target looped over {dir, tune} (tune: false for the directories that
    only count as Cassandra's: hints, saved caches). explicit: the results of
    readlink -f looped over the devices set by hand (they replace the
    directories' devices). lvs: the lines of lvs -a --noheadings
    --separator '|' -o lv_kernel_major,lv_kernel_minor,vg_name,lv_name,
    lv_read_ahead. tune_os: also tune a filesystem of the OS (/, /var...)
    Cassandra's directories are on, and a disk under the Cassandra devices
    that also holds one. contents: the results of ls -A looped over
    mount_targets(), to say when a tuned filesystem holds more than
    Cassandra's directories.

    -> {devices: [{kname, label, kind, lv, lv_read_ahead, top, where, os}] to
    tune, top devices first; brief: [a line per filesystem, short: its
    directories, then its devices level by level, by name]; stacks: [the
    same in full: dm-N, and what is worth knowing about it]; warnings: [what
    is not tuned and should be looked at]; info: [what is not tuned yet, and
    when it will be]; notes: [what is left alone on purpose, and why];
    unusable: [devices set by hand that can't be tuned]; os_skipped:
    [directories or devices left alone as the OS's, tune_os false]}."""
    nodes = parse_lsblk(lsblk)
    if not _is_json(lsblk) and loop_results(mounts) and not loop_results(explicit):
        return {"devices": [], "stacks": [], "brief": [], "unusable": [], "os_skipped": [], "notes": [], "info": [],
                "warnings": ["not tuned: lsblk -J printed no devices (util-linux older than 2.27?)"]}
    planner = _Planner(nodes, parse_lvs(lvs), loop_results(explicit), tune_os)
    planner.read_mounts(mounts)
    planner.read_explicit()
    planner.plan_stacks(dict((r.get("item"), r.get("stdout_lines") or []) for r in loop_results(contents)))
    planner.check_explicit_under_others()
    return planner.result()


class _Planner(object):
    """plan()'s stages, on what they share: the device tree (nodes), the LVM volumes (lvs), the devices set by
    hand (explicit); devices, what is said of them (stacks, brief, warnings, info, notes), unusable, os_skipped."""

    def __init__(self, nodes, lvs, explicit, tune_os):
        self.nodes, self.lvs, self.explicit, self.tune_os = nodes, lvs, explicit, tune_os
        self.notes, self.warnings, self.info = [], [], []
        self.devices, self.stacks, self.brief, self.unusable, self.os_skipped, self.index = [], [], [], [], [], {}
        self.by_majmin = dict((n.get("maj:min"), k) for k, n in nodes.items() if n.get("maj:min"))
        # ours: the maj:min of the Cassandra filesystems; tops: {kname: [its dirs]}; targets: {kname: mount point}
        self.ours, self.tops, self.targets, self.all_dirs = set(), OrderedDict(), {}, []
        self.mounted, self.commitlog = set(), set()

    def kind(self, k):
        return _kind(self.nodes[k], self.lvs)

    def add(self, k, top, where, os_fs=False):
        if k in self.index:
            return
        n = self.nodes.get(k) or {"kname": k}
        k_kind = self.kind(k) if k in self.nodes else ("dm" if k.startswith("dm-") else "md" if k.startswith("md") else "disk")
        lv = self.lvs.get(n.get("maj:min", ""), {}) if k_kind == "lvm" else {}
        self.index[k] = len(self.devices)
        self.devices.append({"kname": k, "label": _label(n), "kind": k_kind, "top": top, "where": where, "os": os_fs,
                             "lv": "%s/%s" % (lv["vg"], lv["lv"]) if lv else "", "lv_read_ahead": lv.get("read_ahead", ""),
                             # what lsblk says now (import_cluster's report; the role reads sysfs)
                             "ra": n.get("ra", ""), "rota": n.get("rota", ""), "sched": n.get("sched", "")})

    def disk_of(self, k):
        """A partition reads ahead through its disk."""
        return self.nodes[k]["parents"][0] if self.kind(k) == "part" and self.nodes[k]["parents"] else k

    def os_uses(self, k, ours=frozenset()):
        """The OS filesystems on top of k."""
        return [u for u in _other_uses(self.nodes, k, set(ours)) if u in OS_MOUNTS]

    def read_mounts(self, mounts):
        """The filesystems of the Cassandra directories (the findmnt loop)."""
        for r in loop_results(mounts):
            item = r.get("item") or {}
            d, tune = item.get("dir"), item.get("tune", True)
            if item.get("commitlog"):
                self.commitlog.add(d)
            self.all_dirs.append(d)
            fs, missing = _lookup(r)
            if not fs:
                if tune and not self.explicit:
                    self.warnings.append("%s: not tuned, not found (not created yet?)" % d)
                continue
            majmin, target = _str(fs.get("maj:min")), _str(fs.get("target"))
            if missing and target in OS_MOUNTS:
                # its own filesystem may not be mounted yet: never an OS one by mistake
                if tune and not self.explicit:
                    self.info.append("%s: not created yet (it would be on %s, an OS filesystem): tuned by a run after"
                                     " it is" % (d, target))
                continue
            self.mounted.add(majmin)
            if not tune or self.explicit:
                # hints or saved caches on an OS filesystem (also bound) don't make its disks Cassandra's
                k = self.by_majmin.get(majmin)
                if target not in OS_MOUNTS and not (k and self.os_uses(k)):
                    self.ours.add(majmin)
                continue
            kname = self.by_majmin.get(majmin)
            if majmin.startswith("0:") or not kname:
                fstype, source = _str(fs.get("fstype")), _str(fs.get("source"))
                why = NOT_BLOCK.get(fstype) or "%s on %s, not a block device" % (fstype or "?", source or "?")
                (self.notes if fstype in QUIET_FSTYPES else self.warnings).append("%s: not tuned, %s" % (d, why))
                continue
            self.targets[kname] = target
            self.tops.setdefault(kname, []).append(d)

    def read_explicit(self):
        """The devices set by hand (they replace the directories' devices)."""
        for r in self.explicit:
            given = r.get("item")
            resolved = (_str(r.get("stdout")) if r.get("rc", 1) == 0 else "") or given
            k = resolved.rsplit("/", 1)[-1]
            if k not in self.nodes:   # /dev/mapper/<name>
                name = given.rsplit("/", 1)[-1]
                k = next((x for x, n in sorted(self.nodes.items()) if n.get("name") == name), k)
            if k not in self.nodes:
                self.add(k, True, given)   # lsblk doesn't see it (no udev, a test tree): taken as a disk, if sysfs has it
                self.stacks.append("%s%s%s" % (given, ARROW, k))
                self.brief.append("%s%s%s" % (given, ARROW, k))
                continue
            if not self.os_uses(k):
                self.ours.add(self.nodes[k].get("maj:min"))   # a device set by hand is Cassandra's, but the OS's
            self.tops.setdefault(k, []).append(given)

    def plan_stacks(self, contents):
        """Each Cassandra filesystem's stack, the data's first; contents: {mount point: its entries}."""
        # what the Cassandra filesystems sit on (an OS filesystem is not: the disks under it stay the OS's)
        self.ours.update(self.nodes[k].get("maj:min") for k in self.tops if self.targets.get(k) not in OS_MOUNTS
                         and not self.os_uses(k))
        # the devices the data is read through (a commitlog partition on such a disk reads with it too)
        data_disks = set(self.disk_of(k) for k, dirs in self.tops.items() if not set(dirs) <= self.commitlog)
        # the data's stacks first: a disk under the data and under the commitlog alone is the data's
        for kname, dirs in sorted(self.tops.items(), key=lambda t: set(t[1]) <= self.commitlog):
            self._stack(kname, dirs, data_disks, contents)

    def _stack(self, kname, dirs, data_disks, contents):
        """The stack of one filesystem (or device set by hand) kname holding dirs."""
        nodes = self.nodes
        top = self.disk_of(kname)
        target = self.targets.get(kname)
        # an OS filesystem: where it is mounted (also through a bind mount), or
        # what else its disk holds for a partition
        os_fs = sorted(set(([target] if target in OS_MOUNTS else []) + self.os_uses(kname)))
        os_disk = sorted(set(self.os_uses(top, self.ours))) if kname != top else []
        if (os_fs or os_disk) and not self.tune_os:
            self.notes.append("%s: not tuned, on %s, an OS filesystem (cassandra_data_tune_os_volumes: false)"
                              % (", ".join(dirs), ", ".join(sorted(set(os_fs + os_disk)))))
            self.os_skipped.extend(dirs)
            return
        if (os_fs or os_disk) and set(dirs) <= self.commitlog and top not in data_disks:
            # read whole, sequentially: an OS filesystem is tuned for the data only
            self.notes.append("%s: not tuned, the commitlog alone on %s, an OS filesystem"
                              % (", ".join(dirs), ", ".join(sorted(set(os_fs + os_disk)))))
            return
        if self.kind(top) in ("loop", "lvm-internal"):
            if self.explicit:
                self.unusable.extend(dirs)
            else:
                (self.notes if self.kind(top) == "loop" else self.warnings).append(
                    "%s: not tuned, on %s (%s)" % (", ".join(dirs), _label(nodes[top]), nodes[top].get("type")))
            return
        on_os = bool(os_fs or os_disk)
        self.add(top, True, ", ".join(dirs), on_os)
        shown, short, also = self._levels(kname, top, dirs, on_os)
        line = ", ".join(dirs) + ARROW + ARROW.join(shown)
        if kname != top:
            uses = _other_uses(nodes, top, self.ours)
            if uses:
                line += " (%s is read through its disk %s, which also holds %s)" % (kname, top, ", ".join(uses))
        line += "".join(" (%s)" % a for a in also)
        if os_fs:
            line += (" (%s: of the OS, its read-ahead set for Cassandra; its other files read with the same)"
                     % ", ".join(os_fs))
        elif os_disk:
            line += " (%s: of the OS, on that disk too: read with the same read-ahead)" % ", ".join(os_disk)
        if self.kind(top) == "dm" and any(self.disk_of(p) not in self.index for p in nodes[top]["parents"]):
            line += (" (%s may get back the read-ahead of the device under it when it is activated again)"
                     % _label(nodes[top]))
        if target and self._shared(target, contents):
            line += " (%s is shared: its other files get the same read-ahead)" % target
        self.stacks.append(line)
        self.brief.append(_dirs(dirs) + ARROW + ARROW.join(short))

    def _levels(self, kname, top, dirs, on_os):
        """The devices under kname, level by level, added to the tuned ones but where they are not to be ->
        (each level's labels in full, short, what else the tuned ones hold)."""
        nodes = self.nodes
        shown, short, also = [], [], []
        mine = self.ours | set([nodes[kname].get("maj:min")])   # an OS filesystem the data is on: not another use here
        for level in _levels(nodes, kname):
            names, short_names = [], []
            for k in level:
                label, short_label = _label(nodes[k]), _name(nodes[k])
                if k != top and self.kind(k) not in ("part", "loop", "lvm-internal") and k not in self.index:
                    # loop, lvm-internal: their read-ahead is not the one files are read with
                    label, short_label = self._under(k, top, dirs, on_os, mine, label, short_label, also)
                names.append(label)
                short_names.append(short_label)
            shown.append(", ".join(names))
            short.append(", ".join(short_names))
        return shown, short, also

    def _under(self, k, top, dirs, on_os, mine, label, short_label, also):
        """A device k under the top one: tuned (added), or left alone for the OS filesystem it also holds ->
        its labels, in full and short; also gets what else a tuned one holds."""
        nodes = self.nodes
        uses = _other_uses(nodes, k, mine)
        os_uses = [u for u in uses if u in OS_MOUNTS]
        if os_uses and not self.tune_os:
            self.notes.append("%s: not tuned, it also holds %s, an OS filesystem"
                              " (cassandra_data_tune_os_volumes: false)" % (_label(nodes[k]), ", ".join(os_uses)))
            return label + " (not tuned: also %s)" % ", ".join(os_uses), _name(nodes[k]) + " (not tuned)"
        if os_uses and set(dirs) <= self.commitlog:
            self.notes.append("%s: not tuned, under the commitlog alone, it also holds %s, an OS filesystem"
                              % (_label(nodes[k]), ", ".join(os_uses)))
            return label + " (not tuned: also %s)" % ", ".join(os_uses), _name(nodes[k]) + " (not tuned)"
        if os_uses:   # the OS reads with it too: said in the default view
            short_label += " (also %s)" % ", ".join(os_uses)
        # under an OS filesystem: of the OS too (an LVM volume never reloaded)
        self.add(k, False, "under " + _label(nodes[top]), on_os or bool(os_uses))
        if uses:
            also.append("%s also holds %s: tuned all the same" % (_label(nodes[k]), ", ".join(uses)))
        return label, short_label

    def _shared(self, target, contents):
        """Whether the filesystem mounted at target holds other files than the Cassandra directories."""
        mine = set(d[len(target):].lstrip("/").split("/")[0] for d in self.all_dirs
                   if d == target or d.startswith(target.rstrip("/") + "/"))
        others = [e for e in contents.get(target) or [] if e not in mine and e != "lost+found"]
        return bool(others) and "" not in mine   # "": a directory is the mount point itself

    def check_explicit_under_others(self):
        """The warnings of the end: LVM volumes lvs did not list, a device set by hand under a Cassandra
        filesystem (the device above it is the one read with)."""
        if not self.lvs and any(self.nodes.get(d["kname"], {}).get("type") == "lvm" for d in self.devices):
            self.warnings.append("lvs listed no LVM volume: they are tuned as device-mapper devices, kept by the udev"
                                 " rule (LVM may set them back when it activates them)")
        for r in self.explicit:
            if r.get("item") in self.unusable:
                continue
            k = (_str(r.get("stdout")) if r.get("rc", 1) == 0 else r.get("item") or "").rsplit("/", 1)[-1]
            for u in _uppermost(self.nodes, k) if k in self.nodes else []:
                if u not in self.index and self.nodes[u].get("maj:min") in self.mounted:
                    self.warnings.append("%s: not tuned, though the Cassandra files on it are read with its read-ahead,"
                                         " not with the one of %s under it: set it in cassandra_data_block_devices, or"
                                         " leave them empty" % (_label(self.nodes[u]), k))

    def result(self):
        return {"devices": self.devices, "stacks": self.stacks, "brief": self.brief,
                "warnings": _dedupe(self.warnings), "info": _dedupe(self.info), "notes": _dedupe(self.notes),
                "unusable": self.unusable, "os_skipped": self.os_skipped}


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
