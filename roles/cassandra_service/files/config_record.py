# -*- coding: utf-8 -*-
# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# The record of the config the running Cassandra started with (the sha1 of each file, by its path, and _jvm: the
# pid and start of that JVM, the only one the record is trusted for), written on the node with its Python
# (python -c, one of these commands in argv). Python 2.7 and 3.6+.
#   start <record> <conf dir> [--same=<JSON>] <unit file>...: after a start, every file of the conf dir (not
#       hidden) and the unit files (the unit, its drop-in) that exist, but the ones written since that JVM started
#       (what it read is unknown); the JVM, when one runs. --same: {file name: {disk, same}}, a file cassandra_config
#       left as it is (the same settings as its rendering, written another way): when the file read has the sha1
#       disk, the sha1 same of that rendering is recorded in _same_settings ({path: sha1}), so that the file written
#       back later as the role renders it is known to hold the settings that start read
#   update <record> <request>: some files recorded as the running Cassandra's, the request (JSON) saying when:
#       jvm, that JVM's {pid, start} (else any JVM the record has); before, {path: sha1} the record has now;
#       files, {path: sha1 on disk, or null for whatever it is} (a file missing: nothing recorded); same, {path:
#       sha1 of the rendering with the same settings} (else none kept for those paths). Prints recorded, or why
#       not
# Paths are expanded (~, $VAR) and normalised as the readers of the record do (config_pending.yml).

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import hashlib
import json
import os
import stat
import sys
import tempfile
import time


def path_of(text):
    return os.path.normpath(os.path.expanduser(os.path.expandvars(text)))


def sha1(path):
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def running_jvm():
    """The running Cassandra JVM, as jvm_started.yml finds it: {pid, start}, None when none."""
    try:
        me = os.readlink("/proc/self/ns/pid")
    except OSError:
        me = None
    for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
        try:
            with open("/proc/%s/comm" % pid) as f:
                if f.read().strip() != "java":  # as jvm_started.yml (pgrep -x java), not a sudo or a grep
                    continue
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                if b"org.apache.cassandra.service.CassandraDaemon" not in f.read().split(b"\0"):
                    continue
            if me is not None and os.readlink("/proc/%s/ns/pid" % pid) != me:  # a container's on this host
                continue
            with open("/proc/%s/stat" % pid) as f:
                ticks = int(f.read().rsplit(")", 1)[1].split()[19])
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            return {"pid": pid, "start": round(time.time() - up + ticks / float(os.sysconf("SC_CLK_TCK")), 2)}
        except (IOError, OSError, IndexError, ValueError):
            continue
    return None


def save(record, data):
    """The record written whole or not at all: a file of its own (never one planted in that directory, which
    Cassandra's account owns), root only (it holds the checksum of the JMX password file), then renamed."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(record) or ".", prefix=".ansible_config_at_start.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=4, sort_keys=True)
        os.rename(tmp, record)
    except BaseException:
        os.unlink(tmp)
        raise


def start(record, conf, units, same=None):
    started = running_jvm()
    out = {}
    names = sorted(os.listdir(conf)) if os.path.isdir(conf) else []
    for path in [os.path.normpath(os.path.join(conf, n)) for n in names if not n.startswith(".")] + units:
        try:
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and (started is None or st.st_ctime <= started["start"]):
                out[path] = sha1(path)
        except (IOError, OSError):  # gone or unreadable meanwhile: left out (pending until a restart)
            continue
    settings = {}
    for name, sums in (same or {}).items():
        path = os.path.normpath(os.path.join(conf, name))
        if out.get(path) and out[path] == sums.get("disk") and sums.get("same"):
            settings[path] = sums["same"]
    if settings:
        out["_same_settings"] = settings
    if started is not None:
        out["_jvm"] = started
    save(record, out)
    return "recorded"


def update(record, request):
    try:
        with open(record) as f:
            data = json.load(f)
    except (IOError, OSError, ValueError):
        return "no record"
    jvm, given = data.get("_jvm") or {}, request.get("jvm")
    if not jvm or given and (str(jvm.get("pid")) != str(given["pid"])
                             or abs(float(jvm.get("start") or 0) - float(given["start"])) >= 2):
        return "the record of another JVM"
    if any(data.get(path_of(p)) != s for p, s in (request.get("before") or {}).items()):
        return "started with other files"
    found = {}
    for p, s in request["files"].items():
        path = path_of(p)
        found[path] = sha1(path) if os.path.isfile(path) else None
        if found[path] is None or s is not None and found[path] != s:
            return "written otherwise"
    data.update(found)
    settings = dict((k, v) for k, v in (data.get("_same_settings") or {}).items() if k not in found)
    for p, s in (request.get("same") or {}).items():
        if s and path_of(p) in found:
            settings[path_of(p)] = s
    data.pop("_same_settings", None)
    if settings:
        data["_same_settings"] = settings
    save(record, data)
    return "recorded"


def main(argv):
    if argv[1] == "start":
        same = [json.loads(a[len("--same="):]) for a in argv[4:] if a.startswith("--same=")]
        return start(path_of(argv[2]), path_of(argv[3]), [path_of(u) for u in argv[4:] if not u.startswith("--same=")],
                     same[0] if same else None)
    return update(path_of(argv[2]), json.loads(argv[3]))


if __name__ == "__main__":
    print(main(sys.argv))
