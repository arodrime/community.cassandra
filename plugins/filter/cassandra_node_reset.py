# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The Cassandra directories of a node, as a reset would empty them: where a
node stands (node_standing_dirs.yml) is read from what they hold.

cassandra_inventory_dirs: the Cassandra directories the inventory gives a
    node, each with its kind.
cassandra_node_reset_dirs: those directories and the live cassandra.yaml's,
    and the paths refused as too risky to empty.
cassandra_node_reset_real: the same checks where the directories really
    are, once links are resolved.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import posixpath
import re

import yaml

from ansible_collections.community.cassandra.plugins.filter import cassandra_node_standing as standing
# cassandra.yaml keys of the directories a reset empties, their kind, and the
# directory under the package's storage dir (/var/lib/cassandra) a missing key
# stands for
LIVE_KEYS = (("data_file_directories", "data", "data"),
             ("local_system_data_file_directory", "data", None),
             ("commitlog_directory", "commitlog", "commitlog"),
             ("saved_caches_directory", "saved_caches", "saved_caches"),
             ("hints_directory", "hints", "hints"),
             ("cdc_raw_directory", "cdc_raw", "cdc_raw"))
STORAGE_DIR = "/var/lib/cassandra"

# Never emptied, whatever the inventory or cassandra.yaml say: the system
# trees (and anything under them) and the usual parents of real data
SYSTEM_TREES = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32", "/proc", "/root", "/run",
                "/sbin", "/snap", "/sys", "/usr")
SYSTEM_DIRS = ("/home", "/media", "/mnt", "/opt", "/srv", "/tmp", "/var", "/var/cache", "/var/lib", "/var/local",
               "/var/log", "/var/mail", "/var/opt", "/var/spool", "/var/tmp")
# the cluster name of the package's own cassandra.yaml (and Cassandra's default)
STOCK_CLUSTER = standing.STOCK_CLUSTER
# cassandra.yaml settings with this node's addresses
LIVE_ADDRESSES = ("listen_address", "broadcast_address", "rpc_address", "broadcast_rpc_address")


def _mount_points(mounts):
    """Mount points from ansible_facts['mounts'] (dicts) and /proc/self/mounts
    lines (strings: the facts leave out e.g. ZFS datasets and tmpfs)."""
    out = []
    for m in mounts or []:
        if isinstance(m, dict):
            out.append(m.get("mount"))
        elif isinstance(m, str) and len(m.split()) > 1:
            out.append(re.sub(r"\\([0-7]{3})", lambda x: chr(int(x.group(1), 8)), m.split()[1]))
    return [p for p in out if p]


def _under(path, top):
    return top == "/" or path == top or path.startswith(top.rstrip("/") + "/")


def _clean(path):
    """A path without its trailing slashes and doubled slashes (not resolved)."""
    path = posixpath.normpath(path)
    return "/" + path.lstrip("/") if path.startswith("/") else path


def reset_path_problem(path, mounts=(), protected=()):
    """Why a path must not be emptied ('' when it may be): empty, relative,
    with . or .. in it, the root, a top-level or system directory, a home,
    a directory that holds another mount point or one of the protected paths
    (config, logs, the Cassandra install)."""
    if not isinstance(path, str) or not path.strip():
        return "an empty path"
    if path != path.strip():
        return "%r: spaces around the path" % path
    if not path.startswith("/"):
        return "%s: a relative path" % path
    if any(part in (".", "..") for part in path.split("/")):
        return "%s: . or .. in the path" % path
    clean = _clean(path)
    if clean == "/":
        return "/: the root of the file system"
    if any(_under(clean, t) for t in SYSTEM_TREES):
        return "%s: a system directory" % clean
    if clean in SYSTEM_DIRS:
        return "%s: a system directory, it holds more than Cassandra's data" % clean
    if clean == STORAGE_DIR:
        return "%s: the package's storage root, it holds the other directories (empty each of them)" % clean
    if clean.count("/") == 1 and clean not in [_clean(m) for m in mounts if m]:
        return "%s: a top-level directory that is not a mount point, too broad to empty (use a directory below it)" % clean
    parts = clean.split("/")
    if len(parts) > 3 and parts[1:3] == ["var", "lib"] and "cassandra" not in parts[3].lower():
        return "%s: under /var/lib/%s, another program's state" % (clean, parts[3])
    if _under(clean, "/home") and clean.count("/") == 2:
        return "%s: a home directory" % clean
    inner = sorted(m for m in (_clean(m) for m in mounts if m) if m != clean and _under(m, clean))
    if inner:
        return "%s: holds the mount point %s (another file system)" % (clean, ", ".join(inner))
    held = sorted(p for p in (_clean(p) for p in protected if p) if _under(p, clean))
    if held:
        return "%s: holds %s" % (clean, ", ".join(held))
    return ""


def _live_dirs(text):
    """[(kind, path)] from the text of a live cassandra.yaml, its cluster name
    and addresses, or a problem."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return None, None, "the live cassandra.yaml is not valid YAML (%s): its directories are unknown" % str(e).split("\n", 1)[0]
    if not isinstance(data, dict):
        return None, None, "the live cassandra.yaml is not a mapping: its directories are unknown"
    out = []
    cdc = str(data.get("cdc_enabled", "")).lower() == "true"
    for key, kind, default in LIVE_KEYS:
        value = data.get(key)
        if value is None or value == []:
            # Cassandra's own default, under the package's storage dir; cdc_raw only when CDC is on
            if default and (kind != "cdc_raw" or cdc):
                out.append((kind, posixpath.join(STORAGE_DIR, default)))
            continue
        for v in value if isinstance(value, list) else [value]:
            out.append((kind, v if isinstance(v, str) else str(v)))
    node = {"cluster_name": str(data.get("cluster_name") or STOCK_CLUSTER),  # Cassandra's default
            "addresses": [str(data[k]) for k in LIVE_ADDRESSES if data.get(k) not in (None, "")]}
    return out, node, ""


def cassandra_node_reset_dirs(inventory, live_yaml=None, mounts=None, protected=None):
    """inventory: [[kind, path]] from the inventory (kinds: data, commitlog,
    saved_caches, hints, cdc_raw); live_yaml: the live cassandra.yaml text
    (None or '': no such file); mounts: ansible_facts['mounts'] and/or
    /proc/self/mounts lines; protected: paths
    a directory to empty must not hold (config, logs, the Cassandra install).
    Returns {'dirs': [{'path', 'kinds', 'from'}], 'problems': [...],
    'addresses': [...], 'cluster_name': ...}: the directories to empty
    (inventory and live file merged), the reasons not to touch any of them,
    the addresses and the cluster name (None: no live file, or unreadable)
    the live file gives this node.
    Example: [["data", "/var/lib/cassandra/data"]] -> {"dirs": [{"path": "/var/lib/cassandra/data", ...}], ...}."""
    mount_points = _mount_points(mounts)
    found, problems, addresses, live_cluster = [], [], [], None
    for kind, path in inventory or []:
        found.append((kind, path, "inventory"))
    if live_yaml:  # None or '' (Ansible before 2.19 may turn a none into ''): no live file
        live, node, problem = _live_dirs(live_yaml)
        if problem:
            problems.append(problem)
        if node:
            addresses = node["addresses"]
            live_cluster = node["cluster_name"]
        for kind, path in live or []:
            found.append((kind, path, "cassandra.yaml"))
    dirs = {}
    for kind, path, source in found:
        why = reset_path_problem(path, mount_points, protected or [])
        if why:
            problems.append("%s directory from %s, %s" % (kind, source, why))
            continue
        d = dirs.setdefault(_clean(path), {"path": _clean(path), "kinds": [], "from": []})
        for key, value in (("kinds", kind), ("from", source)):
            if value not in d[key]:
                d[key].append(value)
    # emptying one would empty the other with it (another kind of data, or a mount below)
    for a in sorted(dirs):
        for b in sorted(dirs):
            if a != b and _under(b, a):
                problems.append("%s (%s) is inside %s (%s): set them apart"
                                % (b, ", ".join(dirs[b]["kinds"]), a, ", ".join(dirs[a]["kinds"])))
    return {"dirs": [dirs[p] for p in sorted(dirs)], "problems": problems, "addresses": addresses,
            "cluster_name": live_cluster}


def cassandra_node_reset_real(result, real, mounts=None, protected=None):
    """result: cassandra_node_reset_dirs' output; real: {path: the path with
    its links resolved (realpath)}. A directory that is a link, or below one,
    is checked again where it really is, and so is the nesting. Each
    directory gets 'real': where it really is (its path when not a link).
    -> result, each directory with its real path and the checks done again there."""
    mount_points = _mount_points(mounts)
    problems = list(result.get("problems") or [])
    dirs = []
    for d in result.get("dirs") or []:
        d = dict(d)
        target = _clean(str(real.get(d["path"]) or d["path"]))
        d["real"] = target
        if target != d["path"]:
            why = reset_path_problem(target, mount_points, protected or [])
            if why:
                problems.append("%s directory %s is a link (or below one) to %s" % (", ".join(d["kinds"]), d["path"], why))
        dirs.append(d)
    for a in dirs:
        for b in dirs:
            ra, rb = a["real"], b["real"]
            if a is b or not _under(rb, ra) or _under(b["path"], a["path"]) or (ra == rb and a["path"] > b["path"]):
                continue
            problems.append("%s (%s) is %s %s (%s) once links are resolved: set them apart"
                            % (b["path"], rb, "the same directory as" if ra == rb else "inside", a["path"], ra))
    return dict(result, dirs=dirs, problems=problems)


def cassandra_inventory_dirs(data_dirs, commitlog, saved_caches, hints, extra=None, cdc_raw="", cdc_enabled=False,
                             storage_dir=""):
    """data_dirs: cassandra_data_file_directories; commitlog, saved_caches, hints: their cassandra_*_dir; extra:
    cassandra_extra_settings; cdc_raw: cassandra_cdc_raw_dir; cdc_enabled: cassandra_cdc_enabled; storage_dir:
    cassandra_config_storage_dir. -> [[kind, dir]]: data, commitlog, saved_caches, hints, the
    local_system_data_file_directory of cassandra_extra_settings (data), and cdc_raw: cassandra_cdc_raw_dir,
    else the cdc_raw_directory of cassandra_extra_settings (an inventory from before the variable), else with CDC
    on, Cassandra's default under the storage dir."""
    extra = extra or {}
    out = [["data", d] for d in data_dirs]
    out += [["commitlog", commitlog], ["saved_caches", saved_caches], ["hints", hints]]
    if "local_system_data_file_directory" in extra:
        out.append(["data", extra["local_system_data_file_directory"]])
    if cdc_raw or "cdc_raw_directory" in extra:
        out.append(["cdc_raw", cdc_raw or extra["cdc_raw_directory"]])
    elif cdc_enabled:
        top = (storage_dir or "/var/lib/cassandra")
        out.append(["cdc_raw", (top.rstrip("/") or top[:1]) + "/cdc_raw"])
    return out


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_node_reset_dirs": cassandra_node_reset_dirs,
            "cassandra_node_reset_real": cassandra_node_reset_real,
            "cassandra_inventory_dirs": cassandra_inventory_dirs,
        }
