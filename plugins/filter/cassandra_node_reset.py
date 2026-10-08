# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Decisions of the node reset (roles/cassandra_service/tasks/reset_node.yml),
which empties a node's Cassandra directories for a fresh start.

cassandra_node_reset_dirs: the directories to empty, from the inventory and
    the live cassandra.yaml, and the paths refused as too risky to empty.
cassandra_node_reset_real: the same checks where the directories really
    are, once links are resolved.
cassandra_cluster_reset_check: create_cluster's reset of a whole cluster, only
    when every node of the ring is a host of the inventory's group.
cassandra_node_reset_ring: whether the node may be reset, from the rings
    other nodes of the cluster see and, when Cassandra runs there, its own.
cassandra_add_node_reset_check: add_node's reset, on by default: only a node
    that is down, in no ring, of this cluster or the stock 'Test Cluster',
    with no user keyspace but a failed bootstrap's of this cluster.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import posixpath
import re

import yaml

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output

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
STOCK_CLUSTER = "Test Cluster"
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


def cassandra_node_reset_dirs(inventory, live_yaml=None, mounts=None, protected=None, cluster_name=""):
    """inventory: [[kind, path]] from the inventory (kinds: data, commitlog,
    saved_caches, hints, cdc_raw); live_yaml: the live cassandra.yaml text
    (None or '': no such file); mounts: ansible_facts['mounts'] and/or
    /proc/self/mounts lines; protected: paths
    a directory to empty must not hold (config, logs, the Cassandra install).
    cluster_name: the inventory's; a live file of another cluster (but the
    package's stock Test Cluster) is refused.
    Returns {'dirs': [{'path', 'kinds', 'from'}], 'problems': [...],
    'addresses': [...], 'cluster_name': ...}: the directories to empty
    (inventory and live file merged), the reasons not to touch any of them,
    the addresses and the cluster name (None: no live file, or unreadable)
    the live file gives this node."""
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
            if cluster_name and node["cluster_name"] not in (cluster_name, STOCK_CLUSTER):
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
    return {"dirs": [dirs[p] for p in sorted(dirs)], "problems": problems, "addresses": addresses,
            "cluster_name": live_cluster}


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
                              has_data=False, cluster_ring=None):
    """answers: [{'host', 'addresses', 'status'}]: the ring (cassandra_status
    cluster_status, None when it did not answer) other nodes of the cluster
    see, and their own addresses; addresses: this node's addresses;
    replace_address: a replace of this node: that address may be in the
    rings, down; running: Cassandra runs here; own: then its own ring (None:
    it did not answer); known: the addresses of the cluster group's hosts;
    has_data: the node holds data (a down node no host of the inventory
    accounts for may be this one under an old address); cluster_ring: the
    whole cluster is reset (create_cluster), these are the addresses of its
    ring: the rings are not read, a node holding data must be in that one.
    Returns {'problems', 'info'}."""
    mine = set(_addr(a) for a in addresses or [] if a and not _loopback(_addr(a)))
    name = host or "this node"
    if cluster_ring is not None:
        if has_data and not mine & set(_addr(a) for a in cluster_ring):
            return {"problems": ["%s holds data but none of its addresses (%s) is in the ring of the cluster: a node of"
                                 " another cluster?" % (name, ", ".join(sorted(mine)))], "info": []}
        return {"problems": [], "info": ["the whole cluster is reset"]}
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


def cassandra_cluster_reset_check(answers, addresses_of, group="", running=None):
    """create_cluster's reset of a whole cluster. answers: [{'host',
    'status'}] the rings the running nodes of the group see (status None: no
    answer); addresses_of: {host: [its addresses]} for every host of the
    group; running: the hosts where Cassandra runs. Returns {'problems',
    'info', 'ring': the addresses in it}: refused unless a node answered, every node of the ring is a host
    of the group (never wipe part of a cluster that keeps running) and every
    running host is in that ring (not in another cluster)."""
    known = dict((_addr(a), h) for h, addrs in (addresses_of or {}).items() for a in addrs or [])
    problems, info = [], []
    answered = [a for a in answers or [] if a.get("status")]
    if not answered:
        problems.append("no node of %s answers nodetool status: the ring can't be checked against the inventory. Start at"
                        " least one node, then run again (if an earlier reset emptied them already, run create_cluster"
                        " without cassandra_create_cluster_reset)" % (group or "the group"))
    ring = {}
    for a in answered:
        for n in _nodes(a.get("status")):
            ring.setdefault(_addr(n.get("address")), set()).add(a.get("host"))
    for address in sorted(ring):
        if address not in known:
            problems.append("%s sees %s in its ring, a node no host of %s has: the inventory must cover the whole cluster"
                            " (a node left out would keep running with its data)"
                            % (", ".join(sorted(ring[address])), address, group or "the group"))
    # nodes of one cluster see the same nodes: another ring is another cluster
    seen = dict((a.get("host"), sorted(set(_addr(n.get("address")) for n in _nodes(a.get("status"))))) for a in answered)
    if len(set(tuple(v) for v in seen.values())) > 1:
        problems.append("the running nodes see different rings (%s): another cluster among them, or one still joining or"
                        " leaving" % "; ".join("%s: %s" % (h, ", ".join(v)) for h, v in sorted(seen.items())))
    for host in running or []:
        mine = set(_addr(a) for a in (addresses_of or {}).get(host) or [])
        if not any(a.get("host") == host for a in answered):
            problems.append("%s runs Cassandra but nodetool status does not answer there: whether it is a node of this"
                            " cluster can't be checked" % host)
        elif answered and not mine & set(ring):
            problems.append("%s runs Cassandra but is not in the ring the others see: a node of another cluster?" % host)
    result_ring = sorted(ring)
    if answered and not problems:
        info.append("the ring (%s) is all in %s" % (", ".join("%s=%s" % (known[a], a) for a in sorted(ring)), group or "the group"))
    return {"problems": problems, "info": info, "ring": result_ring}


def _names(names, most=5):
    names = sorted(names)
    return ", ".join(names[:most]) + (", ..." if len(names) > most else "")


def _user_keyspace(name):
    return not str(name).startswith("system") and name != "lost+found"


def _true_value(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def cassandra_add_node_reset_check(node, cluster_name, live_cluster=None, has_data=False, running=False, size=0,
                                   keyspaces=None, peers=False, cluster_keyspaces=None, ring_problems=None, title=""):
    """add_node's reset of a new node holding data (cassandra_add_node_reset,
    on by default): done only when the node is down, in no ring, of this
    cluster or the package's stock 'Test Cluster', and has no user keyspace
    but the ones of a failed bootstrap of this cluster.
    node: its name; cluster_name: this cluster's; live_cluster: the cluster
    name of its live cassandra.yaml (Cassandra refuses to start with another
    name than its data's; None: no such file, or unreadable); has_data: its
    directories hold something; running: a Cassandra JVM runs there or the
    unit is active; size: bytes in its directories; keyspaces: the keyspace
    directories in its data directories; peers: its system.peers tables hold
    data (it met other nodes); cluster_keyspaces: the cluster's keyspaces
    (None: unknown); ring_problems: cassandra_node_reset_ring's problems (an
    up node of the cluster sees it, no node answered...); title: how the
    lines name it, e.g. "node7 (dc1/rack_b)".
    Returns {'reset': bool, 'problems': [...], 'line': str}: without data,
    nothing to reset (no line); else the line for the plan ("... - will be
    reset") or the refusal, which says what the node holds."""
    title = title or node
    if not _true_value(has_data):
        return {"reset": False, "problems": [], "line": ""}
    running = _true_value(running)
    ring_problems = [ring_problems] if isinstance(ring_problems, str) else list(ring_problems or [])
    users = sorted(set(k for k in keyspaces or [] if _user_keyspace(k)))
    ours = live_cluster is not None and live_cluster == cluster_name
    problems = []
    if running:
        problems.append("Cassandra runs on it: add_node resets only a node where Cassandra is down. Stop it"
                        " (systemctl stop cassandra) if it holds nothing you need, then run add_node again")
    problems.extend(ring_problems)
    if live_cluster is None:
        problems.append("the cluster its data belongs to is unknown (no readable cassandra.yaml)")
    elif live_cluster not in (cluster_name, STOCK_CLUSTER):
        problems.append("its data is of cluster '%s', neither '%s' nor the package's stock '%s'"
                        % (live_cluster, cluster_name, STOCK_CLUSTER))
    elif _true_value(peers) and not ours:
        problems.append("its system.peers lists other nodes: a member of another '%s' ring" % live_cluster)
    if users and live_cluster is not None:
        foreign = [k for k in users if cluster_keyspaces is not None and k not in cluster_keyspaces]
        if not ours:
            problems.append("it holds user keyspaces (%s): only a failed bootstrap of this cluster may" % _names(users))
        elif cluster_keyspaces is None:
            problems.append("it holds user keyspaces (%s) and the cluster's keyspaces could not be read: whether they"
                            " are a failed bootstrap's can't be checked" % _names(users))
        elif foreign:
            problems.append("it holds keyspaces this cluster does not have (%s)" % _names(foreign))
    seen = [p for p in ring_problems if " sees " in p]
    ring = ("in the ring of this cluster" if seen else "ring unknown" if ring_problems
            else "in another ring" if _true_value(peers) and not ours else "not in any ring")
    held = ["%s" % cassandra_output.size(float(size or 0)), "cluster '%s'" % live_cluster if live_cluster is not None
            else "cluster unknown"]
    if users:
        held.append("user keyspaces %s" % _names(users)
                    + (" (a failed bootstrap of this cluster)" if ours and not problems else ""))
    held.extend([ring, "running" if running else "down"])
    holds = "%s: has data (%s)" % (title, ", ".join(held))
    if problems:
        return {"reset": False, "problems": problems,
                "line": "%s: not reset automatically, %s. Nothing was changed: check what it holds; if nothing is"
                        " needed, empty it (reset_node) or remove it from cassandra_new_nodes"
                        % (holds, "; ".join(problems))}
    return {"reset": True, "problems": [], "line": "%s \u2014 will be reset" % holds}


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_add_node_reset_check": cassandra_add_node_reset_check,
            "cassandra_cluster_reset_check": cassandra_cluster_reset_check,
            "cassandra_node_reset_dirs": cassandra_node_reset_dirs,
            "cassandra_node_reset_real": cassandra_node_reset_real,
            "cassandra_node_reset_ring": cassandra_node_reset_ring,
        }
