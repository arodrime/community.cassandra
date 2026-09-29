# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Checks of a host before add_node or replace_node installs anything on it
(roles/cassandra_service/tasks/new_node_checks.yml). Each filter returns
{'problems': [...], 'warnings': [...], 'info': [...]}, readable sentences.

cassandra_new_node_dirs: the Cassandra directories, their mount and free space,
    and whether they hold anything.
cassandra_new_node_load: the average load of the nodes of the same rack (or
    datacenter) against the free space of the data directories.
cassandra_new_node_packages: packages installed, or available from the
    configured repositories (apt-cache policy or dnf repoquery output).
cassandra_new_node_network: ports of existing nodes reached from the host, and
    its own Cassandra ports free.
cassandra_new_node_urls: package sources reached with their credentials.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import re

from ansible.module_utils.six.moves.urllib.parse import urlsplit, urlunsplit

GIB = 1024 ** 3
UNITS = {"bytes": 1, "b": 1, "kib": 1024, "kb": 1024, "mib": 1024 ** 2, "mb": 1024 ** 2,
         "gib": GIB, "gb": GIB, "tib": 1024 ** 4, "tb": 1024 ** 4, "pib": 1024 ** 5, "pb": 1024 ** 5}
# mount units systemd mounts by itself (at boot, or from /etc/fstab)
UNIT_STATES = ("enabled", "enabled-runtime", "generated", "linked", "linked-runtime")


def _result(problems=None, warnings=None, info=None):
    return {"problems": problems or [], "warnings": warnings or [], "info": info or []}


def _gib(size):
    return "%.1f GiB" % (float(size) / GIB)


def _norm(path):
    return os.path.normpath(path) if path else path


def _under(path, top):
    return top == "/" or path == top or path.startswith(top.rstrip("/") + "/")


def _mount_of(path, mountpoints):
    found = [m for m in mountpoints if _under(path, m)]
    return max(found, key=len) if found else None


def _octal_unescape(text):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _fstab_mounts(fstab):
    """Mount points /etc/fstab mounts at boot: {mount point: 'device (fstab)'}"""
    mounts = {}
    for line in (fstab or "").splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0].startswith("#") or not fields[1].startswith("/"):
            continue
        options = fields[3].split(",") if len(fields) > 3 else []
        if fields[2] in ("swap", "none") or "noauto" in options:
            continue
        mounts[_norm(_octal_unescape(fields[1]))] = "%s in /etc/fstab" % fields[0]
    return mounts


def _unit_path(unit):
    name = unit[:-len(".mount")]
    path = "/" if name == "-" else "/" + name.replace("-", "/")
    return _norm(re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), path))


def _unit_mounts(unit_files):
    """systemctl list-unit-files --type=mount output: {mount point: 'unit'}"""
    mounts = {}
    for line in (unit_files or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].endswith(".mount") and fields[1] in UNIT_STATES:
            mounts[_unit_path(fields[0])] = "%s (systemd)" % fields[0]
    return mounts


def cassandra_new_node_dirs(dirs, mounts, fstab="", unit_files="", found=None, min_free_gb=0):
    """dirs: [{'kind': 'data', 'path', 'real'}] (kinds: data, commitlog, hints,
    saved_caches; real: the link's target when path is a link); mounts: ansible_facts['mounts']; found: the paths found in
    them (find, not recursive). A dir whose expected mount (fstab or enabled
    mount unit) is not mounted would land on the file system below it.
    Adds 'data_free': the bytes free on the data directories' file systems."""
    actual = dict((_norm(m["mount"]), m) for m in mounts or [] if m.get("mount"))
    expected = _unit_mounts(unit_files)
    expected.update(_fstab_mounts(fstab))
    expected.pop("/", None)
    paths = [_norm(d["path"]) for d in dirs]
    problems, warnings, info, data_mounts = [], [], [], {}
    if not actual:
        warnings.append("no mount in the facts: the directories' file systems and free space are not checked")
    found = [_norm(p) for p in found or []]
    for d in dirs:
        path, kind = _norm(d["path"]), d["kind"]
        real = _norm(d.get("real") or path)
        on = _mount_of(real, actual)
        want = _mount_of(real, expected)
        if actual and want and (on is None or len(want) > len(on)):
            problems.append("%s directory %s%s: %s (%s) is not mounted, it would go to %s"
                            % (kind, path, " (%s)" % real if real != path else "", want, expected[want],
                               on or "the root file system"))
        if not actual:
            pass
        elif on is None:
            info.append("%s %s: mount not found" % (kind, path))
        else:
            m = actual[on]
            free = m.get("size_available")
            if kind == "data" and free is not None:
                data_mounts[on] = free
            info.append("%s %s: on %s (%s %s)%s" % (kind, path, on, m.get("fstype", "?"), m.get("device", "?"),
                                                  ", %s free of %s" % (_gib(free), _gib(m.get("size_total", 0)))
                                                  if free is not None else ""))
            if kind == "data" and float(min_free_gb or 0) > 0 and free is not None and free < float(min_free_gb) * GIB:
                problems.append("data directory %s: %s free on %s, cassandra_new_node_min_free_gb asks for %s GiB"
                                % (path, _gib(free), on, min_free_gb))
        # what the directory holds, besides the other Cassandra directories and a new file system's lost+found
        held = [p for p in found if os.path.dirname(p) == path and os.path.basename(p) != "lost+found"
                and not any(_under(other, p) for other in paths if other != path)]
        if held:
            names = sorted(os.path.basename(p) for p in held)
            problems.append("%s directory %s is not empty (%s%s): a new node starts empty. Move the data away, or empty it"
                            " if it is not needed" % (kind, path, ", ".join(names[:5]), "..." if len(names) > 5 else ""))
    result = _result(problems, warnings, info)
    result["data_free"] = sum(data_mounts.values())
    return result


def parse_load(load):
    """nodetool status Load ('66.2 KiB', '1.5 TiB') -> bytes, None when unknown"""
    m = re.match(r"^\s*([0-9.]+)\s*([A-Za-z]+)\s*$", str(load or ""))
    if not m or m.group(2).lower() not in UNITS:
        return None
    return float(m.group(1)) * UNITS[m.group(2).lower()]


def cassandra_new_node_load(cluster_status, dc, rack, free, address=""):
    """cluster_status: cassandra_status result's; free: bytes free for the
    data directories. Warns when the nodes of the same rack (else datacenter)
    hold more than half of it on average: the node gets about as much, and
    compactions need room on top."""
    nodes = [n for n in (cluster_status or {}).get(dc, {}).get("nodes", []) if n.get("address") != address]
    same_rack = [n for n in nodes if n.get("rack") == rack]
    group, where = (same_rack, "rack %s (%s)" % (rack, dc)) if same_rack else (nodes, "datacenter %s" % dc)
    loads = [x for x in (parse_load(n.get("load")) for n in group) if x is not None]
    if not loads:
        return _result(info=["no load known for the nodes of %s" % where])
    avg = sum(loads) / len(loads)
    line = "the nodes of %s hold %s on average (nodetool status), this host has %s free for data" % (where, _gib(avg), _gib(free))
    if avg > float(free) / 2:
        return _result(warnings=["%s: the new node gets about as much, and compactions need room on top "
                                 "(keep about half of the disk free)" % line])
    return _result(info=[line])


def _available(query, os_family):
    """{package: [versions]} from apt-cache policy or dnf repoquery output"""
    found = {}
    if os_family == "Debian":
        name = None
        for line in (query or "").splitlines():
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                name = line.strip()[:-1]
                continue
            m = re.match(r"^\s+Candidate:\s+(\S+)", line)
            if name and m and m.group(1) != "(none)":
                found.setdefault(name, []).append(m.group(1))
            m = re.match(r"^\s+(?:\*\*\*\s+)?(\d\S*)\s+-?\d+\s*$", line)
            if name and m:
                found.setdefault(name, []).append(m.group(1))
    else:
        for line in (query or "").splitlines():
            fields = line.split()
            if len(fields) == 2:
                found.setdefault(fields[0], []).append(fields[1])
    return found


def _version_ok(versions, wanted):
    return not wanted or any(re.sub(r"^(?:\d+:)?(\d+\.\d+\.\d+).*$", r"\1", v) == wanted for v in versions)


def cassandra_new_node_packages(needs, installed, query="", os_family="RedHat", query_ok=True):
    """needs: [{'name', 'why', 'mode', 'version'}], mode 'installed' (offline:
    must be there), 'either' (installed or available) or 'warn' (offline, only
    worth a warning); installed: ansible_facts['packages'] (None: unknown, then only
    warnings); query_ok: false
    when the repositories could not be read (then only a warning)."""
    available = _available(query, os_family)
    problems, warnings, info = [], [], []
    for need in needs:
        name, why, mode, version = need["name"], need["why"], need.get("mode", "either"), need.get("version", "")
        what = "%s%s (%s)" % (name, (" " + version) if version else "", why)
        have = [p.get("version", "") for p in (installed or {}).get(name, [])]
        if installed is None and mode != "either":
            warnings.append("%s: could not read the installed packages (python3-apt missing?)" % what)
        elif have and _version_ok(have, version):
            info.append("%s: installed" % what)
        elif have:
            problems.append("%s: %s installed" % (what, ", ".join(have)))
        elif mode == "installed":
            problems.append("%s is not installed, and cassandra_offline downloads nothing" % what)
        elif mode == "warn":
            warnings.append("%s is not installed (cassandra_offline): install it from your image or mirror" % what)
        elif name in available and _version_ok(available[name], version):
            info.append("%s: available" % what)
        elif need.get("fallback"):
            info.append("%s: not in the configured repositories, comes from %s" % (what, need["fallback"]))
        elif not query_ok:
            warnings.append("%s: could not tell whether the repositories have it" % what)
        else:
            problems.append("%s is not installed nor available from the configured repositories%s"
                            % (what, (" (they have %s)" % ", ".join(sorted(set(available[name])))) if name in available else ""))
    return _result(problems, warnings, info)


def _listening(ss_output):
    ports = set()
    for line in (ss_output or "").splitlines():
        fields = line.split()  # State Recv-Q Send-Q Local:Port Peer:Port
        m = re.search(r":(\d+)$", fields[3]) if len(fields) > 3 and fields[0] == "LISTEN" else None
        if m:
            ports.add(int(m.group(1)))
    return ports


def cassandra_new_node_network(reached, own_ports, ss_output=None, running=False):
    """reached: a wait_for loop result over {'name', 'host', 'port'} items;
    own_ports: [{'name', 'port'}] this host's Cassandra ports; ss_output:
    ss -ltn output (None: could not run it)."""
    problems, warnings, info = [], [], []
    if running:
        problems.append("Cassandra is running here: a new node must not have started yet. If it holds no data you need"
                        " (e.g. the package's own instance), stop it and empty its directories")
    for r in (reached or {}).get("results", []):
        item = r.get("item", {})
        if r.get("msg") and item.get("optional"):
            warnings.append("can't reach %s port %s of %s from here (the nodes don't need it, clients do)"
                            % (item.get("name"), item.get("port"), item.get("host")))
        elif r.get("msg"):  # a timeout (the task's failed_when: false drops 'failed')
            problems.append("can't reach %s port %s of %s from here: check the address and the firewalls between the nodes"
                            % (item.get("name"), item.get("port"), item.get("host")))
        else:
            info.append("%s port %s of %s reached" % (item.get("name"), item.get("port"), item.get("host")))
    if ss_output is None:
        warnings.append("could not list the listening ports (ss)")
    else:
        busy = _listening(ss_output)
        for p in own_ports:
            if int(p["port"]) in busy:
                problems.append("port %s (%s) is already in use here" % (p["port"], p["name"]))
    return _result(problems, warnings, info)


def _shown(url):
    """A URL without its credentials or query (a signature)"""
    try:
        parts = urlsplit(url or "")
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "(the URL)"
    if not parts.netloc:
        return url
    return urlunsplit((parts.scheme, host + (":%d" % port if port else ""), parts.path, "", ""))


def cassandra_new_node_urls(results):
    """results: a uri loop result over {'name', 'url', 'match'} items (match:
    a regex the page must hold, e.g. the version wanted). No answer at all
    only warns: the package managers may go through a proxy of their own."""
    problems, warnings, info = [], [], []
    for r in (results or {}).get("results", []):
        item = r.get("item", {})
        status = int(r.get("status", -1))
        name, url = item.get("name"), _shown(item.get("url"))
        if status <= 0:
            warnings.append("%s: no answer from %s (a proxy set only for the package manager or pip is not used by"
                            " this check)" % (name, url))
        elif not 200 <= status < 400 and status != 405 and item.get("soft"):
            warnings.append("%s: %s answers HTTP %s (pip may use another index, from /etc/pip.conf)" % (name, url, status))
        elif not 200 <= status < 400 and status != 405:  # 405: the method is refused, the server answers
            problems.append("%s: %s answers HTTP %s%s" % (name, url, status,
                                                        ": check the credentials" if status in (401, 403) else ""))
        elif item.get("match") and not re.search(item["match"], r.get("content", "")):
            problems.append("%s: %s has no %s" % (name, url, item.get("what", item["match"])))
        else:
            info.append("%s: %s reached" % (name, url))
    return _result(problems, warnings, info)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_new_node_dirs": cassandra_new_node_dirs,
            "cassandra_new_node_load": cassandra_new_node_load,
            "cassandra_new_node_packages": cassandra_new_node_packages,
            "cassandra_new_node_network": cassandra_new_node_network,
            "cassandra_new_node_urls": cassandra_new_node_urls,
        }
