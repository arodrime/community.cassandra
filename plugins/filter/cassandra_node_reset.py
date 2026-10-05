# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Decisions of the node reset (roles/cassandra_service/tasks/reset_node.yml),
which empties a node's Cassandra directories for a fresh start.

cassandra_node_reset_dirs: the directories to empty, from the inventory and
    the live cassandra.yaml, and the paths refused as too risky to empty.
cassandra_node_reset_real: the same checks where the directories really
    are, once links are resolved.
cassandra_node_reset_ring: whether the node may be reset, from the rings
    other nodes of the cluster see and, when Cassandra runs there, its own.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import posixpath
import re

import yaml

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
        return None, None, "the live cassandra.yaml is not valid YAML (%s): its directories are unknown" % str(e).split("\n")[0]
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
    node = {"cluster_name": str(data.get("cluster_name") or "Test Cluster"),  # Cassandra's default
            "addresses": [str(data[k]) for k in LIVE_ADDRESSES if data.get(k) not in (None, "")]}
    return out, node, ""


def cassandra_node_reset_dirs(inventory, live_yaml=None, mounts=None, protected=None, cluster_name=""):
    """inventory: [[kind, path]] from the inventory (kinds: data, commitlog,
    saved_caches, hints, cdc_raw); live_yaml: the live cassandra.yaml text
    (None or '': no such file); mounts: ansible_facts['mounts'] and/or
    /proc/self/mounts lines; protected: paths
    a directory to empty must not hold (config, logs, the Cassandra install).
    cluster_name: the inventory's; a live file of another cluster (but the
    package's stock Test Cluster) is refused.
    Returns {'dirs': [{'path', 'kinds', 'from'}], 'problems': [...],
    'addresses': [...]}: the directories to empty (inventory and live file
    merged), the reasons not to touch any of them, the addresses the live
    file gives this node."""
    mount_points = _mount_points(mounts)
    found, problems, addresses = [], [], []
    for kind, path in inventory or []:
        found.append((kind, path, "inventory"))
    if live_yaml:  # None or '' (Ansible before 2.19 may turn a none into ''): no live file
        live, node, problem = _live_dirs(live_yaml)
        if problem:
            problems.append(problem)
        if node:
            addresses = node["addresses"]
            if cluster_name and node["cluster_name"] not in (cluster_name, "Test Cluster"):
                problems.append("the live cassandra.yaml is for cluster '%s', not '%s' (nor the package's stock"
                                " 'Test Cluster'): a node of another cluster?" % (node["cluster_name"], cluster_name))
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
    return {"dirs": [dirs[p] for p in sorted(dirs)], "problems": problems, "addresses": addresses}


def cassandra_node_reset_real(result, real, mounts=None, protected=None):
    """result: cassandra_node_reset_dirs' output; real: {path: the path with
    its links resolved (realpath)}. A directory that is a link, or below one,
    is checked again where it really is, and so is the nesting. Each
    directory gets 'real': where it really is (its path when not a link)."""
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


def _addr(address):
    """An address of nodetool status, without a :port (nodetool -pp)."""
    a = str(address or "")
    return a.rsplit(":", 1)[0] if a.count(":") == 1 else a


def _loopback(address):
    return address.startswith("127.") or address in ("::1", "0:0:0:0:0:0:0:1", "localhost")


def _nodes(status):
    return [n for dc in (status or {}).values() if isinstance(dc, dict) for n in dc.get("nodes") or []]


def cassandra_node_reset_ring(answers, addresses, replace_address="", running=False, own=None, host="", known=None,
                              has_data=False):
    """answers: [{'host', 'addresses', 'status'}]: the ring (cassandra_status
    cluster_status, None when it did not answer) other nodes of the cluster
    see, and their own addresses; addresses: this node's addresses;
    replace_address: a replace of this node: that address may be in the
    rings, down; running: Cassandra runs here; own: then its own ring (None:
    it did not answer); known: the addresses of the cluster group's hosts;
    has_data: the node holds data (a down node no host of the inventory
    accounts for may be this one under an old address). Returns
    {'problems', 'info'}."""
    mine = set(_addr(a) for a in addresses or [] if a and not _loopback(_addr(a)))
    name = host or "this node"
    problems, info = [], []
    own_ids = set()
    if running:
        if not own:
            problems.append("Cassandra runs on %s but nodetool status does not answer there: whether it is a member of a"
                            " cluster is unknown. Check it, stop it (systemctl stop cassandra), then run again" % name)
        else:
            # alone in its ring (e.g. the package's stock config on a loopback address): not a member
            others = sorted(set(_addr(n.get("address")) for n in _nodes(own)) - mine)
            if len(_nodes(own)) > 1 and others:
                problems.append("Cassandra runs on %s and its ring has other nodes (%s): it is a member of a cluster,"
                                " never reset a live member" % (name, ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")))
            own_ids = set(n.get("host_id") for n in _nodes(own) if _addr(n.get("address")) in mine and n.get("host_id"))
    usable = []
    for a in answers or []:
        nodes = _nodes(a.get("status"))
        own_addr = set(_addr(x) for x in a.get("addresses") or [])
        if any(_addr(n.get("address")) in own_addr and n.get("status") == "U" and n.get("state") == "N" for n in nodes):
            usable.append((a.get("host"), nodes))
    if not usable:
        tried = ", ".join(str(a.get("host")) for a in answers or []) or "none"
        problems.append("no other node of the cluster answered as up and normal (UN) (asked: %s): whether %s is in the"
                        " ring can't be checked, nothing is reset" % (tried, name))
    for other, nodes in usable:
        for n in nodes:
            address = _addr(n.get("address"))
            if address not in mine and n.get("host_id") not in own_ids:
                if (has_data and n.get("status") == "D" and known is not None and address not in known
                        and address != _addr(replace_address)):
                    problems.append("%s sees %s down (host ID %s), a node no host of the inventory has: if it is %s"
                                    " under an old address, its data would be lost. Remove it first (remove_dead_node),"
                                    " or give the inventory host its address" % (other, address, n.get("host_id") or "?", name))
                continue
            seen = "%s%s, host ID %s" % (n.get("status", "?"), n.get("state", "?"), n.get("host_id") or "?")
            if replace_address and address == _addr(replace_address) and n.get("status") == "D":
                info.append("%s sees %s down (%s): the node being replaced" % (other, address, seen))
            elif replace_address and address == _addr(replace_address):
                problems.append("%s sees %s up (%s): only a dead node is replaced, never reset a live member"
                                % (other, address, seen))
            else:
                problems.append("%s sees %s in its ring (%s): %s is a member of the cluster, never reset it."
                                " A node leaves with decommission_node (remove_dead_node when dead), then can be reset"
                                % (other, address, seen, name))
    return {"problems": sorted(set(problems), key=problems.index), "info": info}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_node_reset_dirs": cassandra_node_reset_dirs,
            "cassandra_node_reset_real": cassandra_node_reset_real,
            "cassandra_node_reset_ring": cassandra_node_reset_ring,
        }
