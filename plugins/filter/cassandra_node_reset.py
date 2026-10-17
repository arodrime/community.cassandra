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
(add_node's own reset, on by default, decides from the node's state:
cassandra_node_standing.py, cassandra_add_node_reset_check.)
cassandra_add_node_refusal: add_node's refusal of the new nodes it may not
    add or reset, one block, then what to do.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import posixpath
import re

import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter import cassandra_node_standing as standing
from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_command as _command
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output

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
    (A live file of another cluster: refused from where the node stands,
    cassandra_node_reset_refusal.)
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


_addr = standing.status_address
_nodes = standing.status_nodes


def cassandra_node_reset_ring(addresses, host="", has_data=False, cluster_ring=None):
    """create_cluster's reset of the whole cluster (reset_node.yml with
    _cassandra_node_reset_whole_cluster): the rings are not read again, the
    caller checked them (cassandra_cluster_reset_check); a node holding data
    must be in that ring (cluster_ring: its addresses). addresses: this
    node's. Returns {'problems', 'info'}. Any other reset decides from where
    the node stands (cassandra_node_standing, cassandra_node_reset_refusal)."""
    name = host or "this node"
    me = standing.mine(addresses)
    if has_data and not me & set(_addr(a) for a in cluster_ring or []):
        return {"problems": ["%s holds data but none of its addresses (%s) is in the ring of the cluster: a node of"
                             " another cluster?" % (name, ", ".join(sorted(me)))], "info": []}
    return {"problems": [], "info": ["the whole cluster is reset"]}


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


def cassandra_reset_choice(value):
    """cassandra_add_node_reset as one word: 'auto' (the default: only a
    node that can't hold anything of value), 'true' (given: also a former
    member of this cluster) or 'false' (refuse every node with data).
    "yes" -> "true", "" -> "auto"."""
    word = str("auto" if value is None else value).strip().lower()
    if word in ("false", "no", "off", "0", "n", "f"):
        return "false"
    if word in ("true", "yes", "on", "1", "y", "t"):
        return "true"
    if word in ("auto", ""):
        return "auto"
    # a typo must not reset a node the operator meant to keep
    raise AnsibleFilterError("cassandra_add_node_reset must be auto (the default), true or false, not %r: nothing was"
                             " changed" % (value,))


def cassandra_add_node_refusal(refused, cluster="", operation="add_node", targets=None, inventory=None, hosts=None,
                               cwd=None, default_inventory=None, asked_by=""):
    """add_node's refusal of new nodes, one block for all of them (the ops
    callback rules it as a screen, its verdict line red), then what to do
    with the commands. refused: [{'name', 'title': "node7 (dc1/rack_b)",
    'holds': what it holds (cassandra_add_node_reset_check's, '' when not
    known), 'why': why it is refused, 'way': what to do about it ('': its
    why says it), 'former': only a former member of this cluster (reset_node
    or -e cassandra_add_node_reset=true empties it)}];
    targets: cassandra_target_nodes (a list, or names with commas);
    inventory, hosts, cwd, default_inventory: as cassandra_command takes
    them; asked_by: the playbook that runs add_node's plays (topology: run
    again as is, no target list). [] when nothing is refused.
    -> lines: the refusal block, then TO DO with the commands."""
    refused = [r for r in refused or [] if r]
    if not refused:
        return []
    op = asked_by or operation
    names = targets.split(",") if isinstance(targets, str) else list(targets or [])
    names = [str(n).strip() for n in names if str(n).strip()]
    former = [r["name"] for r in refused if r.get("former")]
    others = [r["name"] for r in refused if not r.get("former")]
    rest = [n for n in names if n not in others]  # the targets without the nodes refused for another reason

    def command(playbook, extra):
        return _command(playbook, inventory=inventory, hosts=hosts, cwd=cwd, default_inventory=default_inventory,
                        extra=extra)

    def add(nodes, reset=False):
        extra = {} if asked_by else {"cassandra_target_nodes": ",".join(nodes)}
        if reset:
            extra["cassandra_add_node_reset"] = "true"
        return command(op, extra)

    def them(nodes):
        return "it" if len(nodes) == 1 else "them"

    lines = ["  ".join(x for x in ["REFUSED", op, cluster, "nothing was changed"] if x), ""]
    lines += ["%s: %s" % (r["title"], " \u2014 ".join(x for x in (r.get("holds"), r.get("why")) if x)) for r in refused]
    shown = cassandra_output.nodes
    without = ((" without %s" % shown(others, keep_order=True)) if others and rest and not asked_by
               else (", once %s %s dealt with" % (shown(others, keep_order=True), "is" if len(others) == 1 else "are"))
               if others else "")
    items = []
    if former:
        items.append({"text": "if what %s %s is not needed (snapshot or copy it first if in doubt), empty %s" % (
            shown(former, keep_order=True), "holds" if len(former) == 1 else "hold", them(former)),
            "command": command("reset_node", {"cassandra_target_nodes": ",".join(former)})})
        items.append({"text": "then run %s again%s" % (op, without), "command": add(rest or names)})
        items.append({"text": "or both in one run (%s empties %s first)" % (op, them(former)),
                      "command": add(rest or names, reset=True)})
    for r in refused:
        if not r.get("former"):
            items.append({"text": ("%s: %s; then run %s again" % (r["name"], r["way"], op)) if r.get("way")
                          else "%s: do what its line above says, then run %s again if still needed" % (r["name"], op),
                          "command": add(names)})
    if others and not former and rest and not asked_by:
        items.append({"text": "or add the other nodes now%s" % without, "command": add(rest)})
    return lines + [""] + cassandra_output.todo(items)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_add_node_refusal": cassandra_add_node_refusal,
            "cassandra_reset_choice": cassandra_reset_choice,
            "cassandra_cluster_reset_check": cassandra_cluster_reset_check,
            "cassandra_node_reset_dirs": cassandra_node_reset_dirs,
            "cassandra_node_reset_real": cassandra_node_reset_real,
            "cassandra_node_reset_ring": cassandra_node_reset_ring,
        }
