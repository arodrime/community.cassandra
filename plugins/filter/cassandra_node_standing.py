# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Where a node stands, worked out in one place from what was read on it
once (roles/cassandra_service/tasks/standing.yml): whether Cassandra runs
there, the ring the cluster's nodes see, what its Cassandra directories hold
(the data directories its config uses: the inventory's and the live
cassandra.yaml's), the cluster its live cassandra.yaml names and add_node's
mark of a bootstrap it started there.

cassandra_node_standing: where a node stands, its state (STATES), with its reasons. The
    operations decide from it: add_node and topology (cassandra_node_add_state,
    cassandra_add_node_reset_check), reset_node and replace_node
    (cassandra_node_reset_refusal), create_cluster and cassandra_config's
    question (its 'blank'), import_cluster (cassandra_node_standing_words).
cassandra_node_data_dirs: the data directories the config of a node uses,
    and the usual ones it does not use (a system keyspace left there is not
    this node's).
cassandra_node_add_state: add_node's view of a state.
cassandra_add_node_reset_check: whether add_node empties a node first.
cassandra_node_reset_refusal: why reset_node may not empty a node.
cassandra_node_standing_words: the operator's words for a state.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import posixpath

import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output

# The states, each with the one phrase the operator reads for it
# (docs/docsite/rst/guide_output.rst lists them)
STATES = (
    ("blank", "blank: nothing in its Cassandra directories, Cassandra stopped, not in the ring"),
    ("failed_bootstrap", "never joined: data of this cluster or of the package's stock config, no user keyspace but"
                         " those of a bootstrap add_node started there; not in the ring, Cassandra stopped"),
    ("former_member", "former member: this cluster's keyspaces on disk, not in the ring, Cassandra stopped"),
    ("unidentified_data", "data not known to be this cluster's (no readable cassandra.yaml, user keyspaces or other"
                          " nodes met under the stock cluster name, keyspaces this cluster does not have or could"
                          " not be read); not in the ring, Cassandra stopped"),
    ("other_cluster", "another cluster's node: its cassandra.yaml names another cluster, or Cassandra runs there in"
                      " another ring"),
    ("running_outside_ring", "Cassandra running alone, outside the cluster's ring"),
    ("member", "in the ring, up (UN), under its own host ID"),
    ("member_joining", "joining: UJ in the ring under its own host ID, a bootstrap running"),
    ("member_down", "in the ring, down (DN): Cassandra stopped there"),
    ("member_busy", "in the ring, leaving or moving (UL, UM), or joining without its system keyspace here"),
    ("dead_address", "a dead node (DN) of the ring has its address, and it holds no system keyspace"),
    ("address_conflict", "the ring has another node at its address (another host ID, or this one does not answer)"),
    ("unknown", "can't be told: something needed could not be read or checked"),
)
WORDS = dict(STATES)
# what reset_node (and replace_node's reset) may empty
RESETTABLE = ("blank", "failed_bootstrap", "former_member", "unidentified_data", "running_outside_ring")

# the cluster name of the package's own cassandra.yaml (and Cassandra's default)
STOCK_CLUSTER = "Test Cluster"
# cassandra.yaml keys of a node's data directories (its system keyspace is in one of them)
DATA_KEYS = ("data_file_directories", "local_system_data_file_directory")
STORAGE_DIR = "/var/lib/cassandra"


def _addr(address):
    """An address of nodetool status, without a :port (nodetool -pp)."""
    a = str(address or "")
    return a.rsplit(":", 1)[0] if a.count(":") == 1 else a


def _loopback(address):
    return address.startswith("127.") or address in ("::1", "0:0:0:0:0:0:0:1", "localhost")


def _nodes(status):
    return [n for dc in (status or {}).values() if isinstance(dc, dict) for n in dc.get("nodes") or []]


def _true(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def _user_keyspace(name):
    return not str(name).startswith("system") and name != "lost+found"


def _names(names, most=5):
    names = sorted(names)
    return ", ".join(names[:most]) + (", ..." if len(names) > most else "")


def _strip(path):
    path = posixpath.normpath(str(path))
    return "/" + path.lstrip("/") if path.startswith("/") else path


def _seen(node):
    return "%s%s, host ID %s" % (node.get("status", "?"), node.get("state", "?"), node.get("host_id") or "?")


def mine(addresses):
    """This node's addresses, as nodetool status shows them (no port, no loopback)."""
    return set(_addr(a) for a in addresses or [] if a and not _loopback(_addr(a)))


def ring_view(answers, addresses, own_ids=(), replace_address="", known=None, has_data=False):
    """What the rings other nodes of the cluster see say of this node.
    answers: [{'host', 'addresses', 'status'}] (status: cassandra_status
    cluster_status, None when it did not answer); only the answer of a node
    that sees itself up and normal (UN) counts. Returns {'usable': [(host,
    nodes)], 'tried': [hosts], 'entries': [(host, node)]: this node, by one
    of its addresses or host IDs; 'replaced': [(host, node)]: replace_address
    down, the node being replaced; 'replaced_up'; 'unknown_down': [(host,
    node)]: a down node no host of the inventory has (known), when this node
    holds data (maybe this one under an old address)}."""
    me = mine(addresses)
    own_ids = set(i for i in own_ids or [] if i)
    replace = _addr(replace_address)
    out = {"usable": [], "tried": [str(a.get("host")) for a in answers or []], "entries": [], "replaced": [],
           "replaced_up": [], "unknown_down": []}
    for a in answers or []:
        nodes = _nodes(a.get("status"))
        own_addr = set(_addr(x) for x in a.get("addresses") or [])
        if any(_addr(n.get("address")) in own_addr and n.get("status") == "U" and n.get("state") == "N" for n in nodes):
            out["usable"].append((a.get("host"), nodes))
    for other, nodes in out["usable"]:
        for n in nodes:
            address = _addr(n.get("address"))
            if address not in me and n.get("host_id") not in own_ids:
                if (_true(has_data) and n.get("status") == "D" and known is not None and address not in known
                        and address != replace):
                    out["unknown_down"].append((other, n))
                continue
            if replace and address == replace and n.get("status") == "D":
                out["replaced"].append((other, n))
            elif replace and address == replace:
                out["replaced_up"].append((other, n))
            else:
                out["entries"].append((other, n))
    return out


def own_ids(addresses, host_id="", running=False, own=None):
    """Its own host IDs: nodetool info's, and its own ring's at its addresses."""
    me = mine(addresses)
    ids = set([host_id] if host_id else [])
    if _true(running) and own:
        ids |= set(n.get("host_id") for n in _nodes(own) if _addr(n.get("address")) in me and n.get("host_id"))
    return ids


def ring_problems(view, addresses, name, replace_address="", running=False, own=None):
    """Why the ring forbids emptying the node, as reset_node says it: it runs
    and its own ring does not answer or has other nodes, no node of the
    cluster answered UN, a node of the cluster sees it (or a down node no
    host has, or the dead node to replace up)."""
    me = mine(addresses)
    problems = []
    if _true(running):
        if not own:
            problems.append("Cassandra runs on %s but nodetool status does not answer there: whether it is a member of a"
                            " cluster is unknown. Check it, stop it (systemctl stop cassandra), then run again" % name)
        else:
            # alone in its ring (e.g. the package's stock config on a loopback address): not a member
            others = sorted(set(_addr(n.get("address")) for n in _nodes(own)) - me)
            if len(_nodes(own)) > 1 and others:
                problems.append("Cassandra runs on %s and its ring has other nodes (%s): it is a member of a cluster,"
                                " never reset a live member" % (name, ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")))
    if not view["usable"]:
        problems.append("no other node of the cluster answered as up and normal (UN) (asked: %s): whether %s is in the"
                        " ring can't be checked, nothing is reset" % (", ".join(view["tried"]) or "none", name))
    for other, n in view["unknown_down"]:
        problems.append("%s sees %s down (host ID %s), a node no host of the inventory has: if it is %s"
                        " under an old address, its data would be lost. Remove it first (remove_dead_node),"
                        " or give the inventory host its address" % (other, _addr(n.get("address")), n.get("host_id") or "?", name))
    for other, n in view["replaced_up"]:
        problems.append("%s sees %s up (%s): only a dead node is replaced, never reset a live member"
                        % (other, _addr(n.get("address")), _seen(n)))
    for other, n in view["entries"]:
        problems.append("%s sees %s in its ring (%s): %s is a member of the cluster, never reset it."
                        " A node leaves with decommission_node (remove_dead_node when dead), then can be reset"
                        % (other, _addr(n.get("address")), _seen(n), name))
    return sorted(set(problems), key=problems.index)


def _live_data_dirs(text):
    """The data directories a live cassandra.yaml gives (Cassandra's default
    for a key it leaves out), or a problem."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return None, "the live cassandra.yaml is not valid YAML (%s)" % str(e).split("\n", 1)[0]
    if not isinstance(data, dict):
        return None, "the live cassandra.yaml is not a mapping"
    out = []
    for key in DATA_KEYS:
        value = data.get(key)
        if value is None or value == []:
            if key == "data_file_directories":
                out.append(posixpath.join(STORAGE_DIR, "data"))  # Cassandra's own default (the package's)
            continue
        for v in value if isinstance(value, list) else [value]:
            out.append(v if isinstance(v, str) else str(v))
    return out, ""


def cassandra_node_data_dirs(inventory, live_yaml=None, usual=None):
    """inventory: the data directories of the inventory
    (cassandra_data_file_directories, local_system_data_file_directory);
    live_yaml: the text of the live cassandra.yaml (None or '': no such
    file); usual: other places a system keyspace is often left (the
    package's /var/lib/cassandra/data, a tarball's data/data).
    Returns {'own': the data directories the config uses (the inventory's,
    the live file's), 'elsewhere': the usual ones it does not use,
    'problem': why the live file can't be parsed ('' when it can)}. A reset
    empties the 'own' ones (and the other directories): a node emptied is
    blank."""
    own, problem = [], ""
    for d in [inventory] if isinstance(inventory, str) else inventory or []:
        if d and str(d).strip():
            own.append(_strip(d))
    if live_yaml:  # None or '' (Ansible before 2.19 may turn a none into ''): no live file
        live, problem = _live_data_dirs(live_yaml)
        own.extend(_strip(d) for d in live or [] if d and str(d).strip())
    own = sorted(set(own), key=own.index)
    elsewhere = [d for d in (_strip(u) for u in usual or [] if u and str(u).strip()) if d not in own]
    return {"own": own, "elsewhere": sorted(set(elsewhere), key=elsewhere.index), "problem": problem}


def cassandra_node_standing(node, cluster_name, title="", running=False, jvm=None, own=None, host_id="", no_mbean=False,
                         answers=None, addresses=None, known=None, replace_address="", live="absent",
                         live_cluster=None, held=None, system=None, elsewhere=None, unreadable=None, keyspaces=None,
                         peers=False, bootstrap_started=False, cluster_keyspaces=None):
    """The state of a node, from what was read there (standing.yml).
    node: its inventory name; cluster_name: this cluster's; title: how lines
    name it ("node7 (dc1/rack_b)").
    running: a Cassandra JVM runs there, or its unit is active; jvm: a
    Cassandra JVM runs there (None: as running); own: then the ring it sees
    (cassandra_status cluster_status; None: it did not answer, or no JVM);
    host_id: its host ID (nodetool info; '' unknown); no_mbean: nodetool
    can't answer while it bootstraps (5.0.0 to 5.0.4, CASSANDRA-19902).
    answers: the rings other nodes of the cluster see (ring_view; None: the
    ring was not read, e.g. cassandra_config's, create_cluster's);
    addresses: this node's; known: the addresses of the cluster group's
    hosts; replace_address: replace_node's dead node (down: the node being
    replaced, not this one).
    live: its live cassandra.yaml, 'absent', 'read', 'unreadable' or
    'invalid'; live_cluster: the cluster name it gives (None: no file, or
    unreadable).
    held: what its Cassandra directories hold (paths, lost+found left out);
    system: the system keyspaces in the data directories its config uses;
    elsewhere: system keyspaces in a usual place its config does not use
    (left as they are, not its own); unreadable: what could not be read
    there (without root?); keyspaces: the keyspace directories in its data
    directories; peers: its system.peers tables hold data (it met other
    nodes); bootstrap_started: add_node's mark of a bootstrap it started
    there and that never ended (recent); cluster_keyspaces: this cluster's
    keyspaces (None: unknown).

    Returns {'state', 'words', 'reasons': why, in the operator's words;
    'blank': nothing in its directories, Cassandra stopped, all of it read
    (the ring aside), 'evidence': what makes it not blank; 'holds_data',
    'has_system', 'running', 'ring': {'read', 'known', 'in_ring' (UN, DN,
    ... or ''), 'entry', 'seen_by', 'problems': reset_node's}, 'users': its
    user keyspaces, 'ours': its data is of this very cluster, ...}."""
    name = title or node
    running = _true(running)
    jvm = running if jvm is None else _true(jvm)
    live_cluster = live_cluster or None  # a none Ansible before 2.19 may give as ''
    held = list(held or [])
    unreadable = list(unreadable or [])
    system = list(system or [])
    has_data = len(held) > 0
    has_system = len(system) > 0
    users = sorted(set(k for k in keyspaces or [] if _user_keyspace(k)))
    ids = own_ids(addresses, host_id, jvm, own)
    ring_read = answers is not None
    view = ring_view(answers or [], addresses, ids, replace_address, known, has_data)
    ring_known = bool(view["usable"])
    # a cluster that kept the stock name: a stock node is not known to be one of its own
    ours = live_cluster is not None and live_cluster == cluster_name and cluster_name != STOCK_CLUSTER
    peers = _true(peers)
    bootstrap_started = _true(bootstrap_started)
    foreign = [k for k in users if cluster_keyspaces is not None and k not in cluster_keyspaces]

    # what makes it not blank (cassandra_config's question, create_cluster's new cluster)
    evidence = list(system)
    rest = [h for h in held if h not in system]
    if rest:
        evidence.append("%s in its Cassandra directories" % ", ".join(rest[:3]) + (", ..." if len(rest) > 3 else ""))
    evidence += unreadable
    if live in ("unreadable", "invalid") and not [u for u in unreadable if "cassandra.yaml" in u]:
        evidence.append("the live cassandra.yaml can not be %s" % ("read" if live == "unreadable" else "parsed"))
    if running:
        evidence.append("Cassandra is running")
    holding = "it holds data (%s)" % ", ".join((system or held)[:3]) if has_data else ""

    reasons = []
    ring = {"read": ring_read, "known": ring_known, "in_ring": "", "entry": {}, "seen_by": "", "tried": view["tried"],
            # the first node of the cluster that answered UN: the ring add_node plans with
            "from": view["usable"][0][0] if ring_known else "",
            "problems": ring_problems(view, addresses, name, replace_address, jvm, own) if ring_read else []}
    entry = (view["entries"] or view["replaced_up"] or [None])[0]
    if entry:
        other, n = entry
        e_id = n.get("host_id") or ""
        in_ring = "%s%s" % (n.get("status", "?"), n.get("state", "?"))
        ring.update({"in_ring": in_ring, "entry": n, "seen_by": other})
        reasons.append("%s sees %s in its ring (%s)" % (other, _addr(n.get("address")), _seen(n)))
        if host_id and e_id and host_id != e_id:
            state = "address_conflict"
            reasons.append("its own host ID is %s" % host_id)
        elif in_ring == "UN" and host_id and (has_system or host_id == e_id):
            state = "member"
        elif in_ring == "UJ" and has_system and (host_id or _true(no_mbean)):
            state = "member_joining"
        elif in_ring == "DN" and not has_system:
            state = "dead_address"
        elif n.get("status") == "D":
            state = "member_down"
        elif in_ring == "UN":
            state = "address_conflict"
            reasons.append("it does not answer (Cassandra stopped, or JMX unreachable)")
        else:
            state = "member_busy"
    elif running:
        others = sorted(set(_addr(x.get("address")) for x in _nodes(own)) - mine(addresses))
        cluster = set(others) & set(_addr(k) for k in known or [])
        if live_cluster is not None and live_cluster not in (cluster_name, STOCK_CLUSTER):
            state = "other_cluster"
            reasons.append("its cassandra.yaml names cluster '%s'" % live_cluster)
        elif not own and jvm:
            state = "unknown"
            reasons.append("Cassandra runs but nodetool status does not answer there")
        elif len(_nodes(own)) > 1 and others and cluster:
            state = "unknown"
            reasons.append("its own ring lists nodes of this cluster (%s), whose rings do not list it" % _names(cluster))
        elif len(_nodes(own)) > 1 and others:
            state = "other_cluster"
            reasons.append("Cassandra runs in another ring (%s)" % _names(others))
        elif not ring_read:
            state = "unknown"
            reasons.append("Cassandra is running (the ring was not read)")
        elif not ring_known:
            state = "unknown"
            reasons.append("no other node of the cluster answered UN (asked: %s): whether it is in the ring can't be"
                           " checked" % (", ".join(view["tried"]) or "none"))
        else:
            state = "running_outside_ring"
            reasons.append("Cassandra runs alone in its own ring" if jvm else "its cassandra unit is active, no JVM yet")
        if holding:
            reasons.append(holding)
    elif not has_data:
        if unreadable or live in ("unreadable", "invalid"):
            state = "unknown"
            reasons.extend(unreadable or ["its directories are unknown: the live cassandra.yaml can not be %s"
                                          % ("read" if live == "unreadable" else "parsed")])
        else:
            state = "blank"
    else:
        # holds data, Cassandra stopped, not in the ring as far as it was read
        reasons.append(holding)
        if not ring_read:
            state = "unknown"
            reasons.append("the ring was not read")
        elif not ring_known:
            state = "unknown"
            reasons.append("no other node of the cluster answered UN (asked: %s): whether it is in the ring can't be"
                           " checked" % (", ".join(view["tried"]) or "none"))
        elif view["unknown_down"]:
            other, n = view["unknown_down"][0]
            state = "unknown"
            reasons.append("%s sees %s down (host ID %s), a node no host of the inventory has: maybe this node under an"
                           " old address" % (other, _addr(n.get("address")), n.get("host_id") or "?"))
        elif unreadable or live in ("unreadable", "invalid"):
            state = "unknown"
            reasons.extend(unreadable or ["its directories are unknown: the live cassandra.yaml can not be %s"
                                          % ("read" if live == "unreadable" else "parsed")])
        elif live_cluster is None:
            state = "unidentified_data"
            reasons.append("the cluster its data belongs to is unknown (no readable cassandra.yaml)")
        elif live_cluster not in (cluster_name, STOCK_CLUSTER):
            state = "other_cluster"
            reasons.append("its cassandra.yaml names cluster '%s'" % live_cluster)
        elif users and not ours:
            state = "unidentified_data"
            reasons.append("user keyspaces (%s) under the cluster name '%s'%s" % (
                _names(users), live_cluster, ", the stock one" if live_cluster == STOCK_CLUSTER else ""))
        elif users and cluster_keyspaces is None:
            state = "unidentified_data"
            reasons.append("user keyspaces (%s), and this cluster's keyspaces could not be read" % _names(users))
        elif foreign:
            state = "unidentified_data"
            reasons.append("keyspaces this cluster does not have (%s)" % _names(foreign))
        elif peers and not ours:
            state = "unidentified_data"
            reasons.append("its system.peers lists other nodes: a member of another '%s' ring" % live_cluster)
        elif users and bootstrap_started:
            state = "failed_bootstrap"
            reasons.append("this cluster's keyspaces (%s), of a bootstrap add_node started there and that never ended"
                           % _names(users))
        elif users:
            state = "former_member"
            reasons.append("this cluster's keyspaces (%s), no bootstrap of add_node left unfinished there" % _names(users))
        else:
            state = "failed_bootstrap"
            reasons.append("no user keyspace, cluster '%s'" % live_cluster)
    for other, n in view["replaced"]:
        reasons.append("%s sees %s down (%s): the node being replaced" % (other, _addr(n.get("address")), _seen(n)))
    return {"state": state, "words": WORDS[state], "reasons": reasons, "node": node, "name": name,
            "cluster_name": cluster_name, "blank": not evidence, "evidence": evidence, "holds_data": has_data,
            "has_system": has_system, "system": system, "held": held, "elsewhere": list(elsewhere or []),
            "unreadable": unreadable, "running": running, "ring": ring, "view": view, "users": users, "ours": ours,
            "live": live, "live_cluster": live_cluster, "peers": peers, "bootstrap_started": bootstrap_started,
            "host_id": host_id, "cluster_keyspaces_known": cluster_keyspaces is not None, "foreign": foreign}


def cassandra_node_add_state(state, choice="auto", address=""):
    """add_node's view of a node's state (cassandra_node_standing). Returns
    {'state', 'refusal'}: 'new' (not in the ring: added, emptied first when
    it holds data and may be: cassandra_add_node_reset_check), 'joining' (a
    bootstrap an earlier run started: waited for again), 'joined' (in the
    ring: not added again), or refused with the reason: 'orphan' (data,
    -e cassandra_add_node_reset=false), 'other_host', 'replace', 'down',
    'busy'. choice: cassandra_add_node_reset (cassandra_reset_choice);
    address: its address, for the replace_node command."""
    s = state.get("state")
    ring = state.get("ring") or {}
    entry = ring.get("entry") or {}
    in_ring = ring.get("in_ring") or ""
    my_id = state.get("host_id") or ""
    e_id = entry.get("host_id") or "?"
    if s == "member":
        return {"state": "joined", "refusal": ""}
    if s == "member_joining":
        return {"state": "joining", "refusal": ""}
    if s == "address_conflict":
        return {"state": "other_host", "refusal": (
            "the ring has %s host ID %s at this address, but this node %s: the address is used by another node"
            " (IP reused, or a replace in progress). Find which machine owns %s before going on."
            % (in_ring, e_id, ("has host ID " + my_id) if my_id else "does not answer (not running, or JMX unreachable)",
               e_id))}
    if s == "dead_address":
        return {"state": "replace", "refusal": (
            "a dead node with this address is in the ring (DN, host ID %s). Adding this node would be a replace, not"
            " an add: run replace_node (-e cassandra_target_nodes=%s -e cassandra_replace_address=%s)."
            % (e_id, state.get("node"), address or _addr(entry.get("address"))))}
    if s == "member_down" and entry.get("state") == "J":
        return {"state": "down", "refusal": (
            "down in the middle of its bootstrap (%s, host ID %s): see why in its system.log. Once fixed, start over:"
            " stop Cassandra, wait until it is gone from nodetool status (seen from another node), then run add_node"
            " again (it empties the node first: cassandra_add_node_reset)." % (in_ring, e_id))}
    if s == "member_down":
        return {"state": "down", "refusal": (
            "already in the ring (host ID %s) but down: start it (systemctl start cassandra) and check its logs;"
            " add_node is not needed." % e_id)}
    if s == "member_busy":
        return {"state": "busy", "refusal": (
            "%s in the ring (J joining, L leaving, M moving): wait until it is UN, then run add_node again." % in_ring)}
    # not in the ring: blank, or emptied first when it may be (cassandra_add_node_reset_check)
    if state.get("holds_data") and choice == "false":
        return {"state": "orphan", "refusal": (
            "has data (%s) but is not in the ring: an interrupted bootstrap, or data of another cluster. It was not"
            " reset: -e cassandra_add_node_reset=false was given. Without it, add_node empties such a node first when"
            " it may (Cassandra down, in no ring, cluster this one or the stock 'Test Cluster', no user keyspace but a"
            " failed bootstrap's of this cluster; what it deletes is on the screen), then adds it."
            % ", ".join((state.get("system") or state.get("held") or [])[:3]))}
    return {"state": "new", "refusal": ""}


def cassandra_add_node_reset_check(state, size=0, explicit=False):
    """add_node's reset of a new node holding data (cassandra_add_node_reset,
    on by default), from its state (cassandra_node_standing): done only for a
    node that never joined (failed_bootstrap), or a former member of this
    cluster when explicit (-e cassandra_add_node_reset=true: the writes only
    it holds are lost). size: bytes in its directories.
    Returns {'reset': bool, 'problems': [...], 'line': str, 'holds': str,
    'why': str, 'former': bool, 'way': str}: without data, nothing to reset
    (no line); else the line for the plan ("... - will be reset") or the
    refusal, which says what the node holds. holds and why: the refusal in
    short for cassandra_add_node_refusal ("holds 12.0 GiB of cluster 'x'
    (keyspaces shop), not in the ring, stopped", why it is refused); former:
    refused only as a former member (reset_node or -e
    cassandra_add_node_reset=true empties it); way: what to do about the
    first reason it is refused for."""
    title = state.get("name") or state.get("node")
    if not state.get("holds_data"):
        return {"reset": False, "problems": [], "line": "", "holds": "", "why": "", "former": False, "way": ""}
    s = state.get("state")
    running = state.get("running")
    ring = state.get("ring") or {}
    ring_probs = list(ring.get("problems") or [])
    live_cluster = state.get("live_cluster")
    cluster_name = state.get("cluster_name")
    users = state.get("users") or []
    ours = state.get("ours")
    peers = state.get("peers")
    problems = []
    if running:
        problems.append("Cassandra runs on it: add_node resets only a node where Cassandra is down. Stop it"
                        " (systemctl stop cassandra) if it holds nothing you need, then run add_node again")
    problems.extend(ring_probs)
    if live_cluster is None:
        problems.append("the cluster its data belongs to is unknown (no readable cassandra.yaml)")
    elif live_cluster not in (cluster_name, STOCK_CLUSTER):
        problems.append("its data is of cluster '%s', neither '%s' nor the package's stock '%s'"
                        % (live_cluster, cluster_name, STOCK_CLUSTER))
    elif peers and not ours:
        problems.append("its system.peers lists other nodes: a member of another '%s' ring" % live_cluster)
    if users and live_cluster is not None:
        if not ours:
            problems.append("it holds user keyspaces (%s): only a failed bootstrap of this cluster may" % _names(users))
        elif not state.get("cluster_keyspaces_known"):
            problems.append("it holds user keyspaces (%s) and the cluster's keyspaces could not be read: whether they"
                            " are a failed bootstrap's can't be checked" % _names(users))
        elif state.get("foreign"):
            problems.append("it holds keyspaces this cluster does not have (%s)" % _names(state["foreign"]))
    former = s == "former_member"
    if former and not _true(explicit):
        problems.append("it holds this cluster's keyspaces (%s) and no bootstrap add_node left unfinished: a former"
                        " member of this cluster (removed with removenode?). The writes only it holds would be lost:"
                        " snapshot or copy its data directories first if in doubt, then -e cassandra_add_node_reset=true"
                        " resets it" % _names(users))
    reset = s == "failed_bootstrap" or (former and _true(explicit))
    if not reset and not problems:
        # refused by its state for a reason the lines above do not give (e.g. a directory that can't be read)
        problems.extend(state.get("reasons")[1:] or [state.get("words")])
    seen = [p for p in ring_probs if " sees " in p]
    ring_word = ("in the ring of this cluster" if seen else "ring unknown" if ring_probs
                 else "in another ring" if peers and not ours else "not in any ring")
    held = ["%s" % cassandra_output.size(float(size or 0)), "cluster '%s'" % live_cluster if live_cluster is not None
            else "cluster unknown"]
    if users:
        held.append("user keyspaces %s" % _names(users)
                    + ((" (a former member of this cluster)" if former else " (a failed bootstrap of this cluster)")
                       if ours and not problems else ""))
    held.extend([ring_word, "running" if running else "down"])
    holds = "%s: has data (%s)" % (title, ", ".join(held))
    short = "holds %s of %s%s, %s, %s" % (
        cassandra_output.size(float(size or 0)), ("cluster '%s'" % live_cluster) if live_cluster is not None
        else "an unknown cluster", (" (keyspaces %s)" % _names(users)) if users else "",
        {"not in any ring": "not in the ring"}.get(ring_word, ring_word), "running" if running else "stopped")
    if not reset:
        only_former = former and len(problems) == 1
        # the way out that works for the first reason, what it is refused for
        if seen:
            way = "a member of this cluster: never reset it; take it out of cassandra_target_nodes"
        elif ring_probs:
            way = "its ring could not be checked: wait until the cluster's nodes answer"
        elif live_cluster is not None and live_cluster not in (cluster_name, STOCK_CLUSTER):
            way = ("a node of another cluster: take it out of cassandra_target_nodes, or, if nothing it holds is needed,"
                   " empty its directories by hand (reset_node refuses another cluster's node)")
        elif running:
            way = "if it holds nothing you need, stop Cassandra on it (systemctl stop cassandra): add_node then resets it"
        elif users and live_cluster is not None and ours and not state.get("cluster_keyspaces_known"):
            way = "let add_node read the cluster's keyspaces (CQL login: cassandra_cql_username, cassandra_cql_password)"
        else:
            way = ("check what it holds; if nothing is needed, empty it (reset_node), else take it out of"
                   " cassandra_target_nodes")
        return {"reset": False, "problems": problems, "holds": short, "former": only_former, "way": way,
                "why": ("a former member of this cluster: the writes only it holds would be lost if wiped" if only_former
                        else "; ".join(problems)),
                "line": "%s: not reset automatically, %s. Nothing was changed: check what it holds; if nothing is"
                        " needed, empty it (reset_node) or remove it from cassandra_target_nodes"
                        % (holds, "; ".join(problems))}
    given = (" (-e cassandra_add_node_reset=true: the writes only it holds are lost; snapshot or copy its data first"
             " if in doubt)") if former else ""
    return {"reset": True, "problems": [], "holds": short, "why": "", "former": False, "way": "",
            "line": "%s — will be reset%s" % (holds, given)}


def cassandra_node_reset_refusal(state):
    """Why reset_node (or replace_node's reset) may not empty a node, from
    where it stands (cassandra_node_standing): [] when it may (RESETTABLE:
    blank, never joined, a former member, data not known to be this
    cluster's, Cassandra running alone outside the ring: stopped first).
    Never a node the cluster's ring has (a member, even down; replace_node:
    but the dead node it replaces), one running in another ring or whose
    nodetool does not answer, another cluster's (its cassandra.yaml), one
    whose ring could not be checked, nor one whose directories could not all
    be read."""
    s = state.get("state")
    if s in RESETTABLE:
        return []
    out = list((state.get("ring") or {}).get("problems") or [])
    live = state.get("live_cluster")
    if live is not None and live not in (state.get("cluster_name"), STOCK_CLUSTER):
        out.append("the live cassandra.yaml is for cluster '%s', not '%s' (nor the package's stock 'Test Cluster'): a"
                   " node of another cluster?" % (live, state.get("cluster_name")))
    if not out:
        # what could not be read (the reset's plan says it too, in the same words), or what it is
        out = [r for r in state.get("reasons") or [] if not r.startswith("it holds data (")]
    return out or ["%s: %s" % (state.get("name"), state.get("words"))]


def cassandra_node_standing_words(state):
    """The operator's words for a state (STATES)."""
    if state not in WORDS:
        raise AnsibleFilterError("unknown node state %r: one of %s" % (state, ", ".join(WORDS)))
    return WORDS[state]


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_add_node_reset_check": cassandra_add_node_reset_check,
            "cassandra_node_add_state": cassandra_node_add_state,
            "cassandra_node_data_dirs": cassandra_node_data_dirs,
            "cassandra_node_reset_refusal": cassandra_node_reset_refusal,
            "cassandra_node_standing": cassandra_node_standing,
            "cassandra_node_standing_words": cassandra_node_standing_words,
        }
