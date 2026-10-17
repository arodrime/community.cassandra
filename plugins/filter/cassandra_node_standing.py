# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Where a node stands, worked out in one place from what was read on it
(roles/cassandra_service/tasks/node_standing_read.yml, then
node_standing.yml): whether Cassandra runs
there, the ring the cluster's nodes see, what its Cassandra directories hold
(the data directories its config uses: the inventory's and the live
cassandra.yaml's), the cluster its live cassandra.yaml names and add_node's
mark of a bootstrap it started there.

cassandra_node_standing: where a node stands, its state (STATES), with its
    reasons. The operations decide from it: add_node and topology
    (cassandra_node_add_state, cassandra_add_node_reset_check), reset_node
    and replace_node (cassandra_node_reset_refusal), create_cluster and
    cassandra_config's question (its 'blank'), import_cluster (WORDS).
cassandra_node_data_dirs: the data directories the config of a node uses,
    and the usual ones it does not use (a system keyspace left there is not
    this node's).
cassandra_node_add_state: add_node's view of a state.
cassandra_add_node_reset_check: whether add_node empties a node first.
cassandra_node_reset_refusal: why reset_node may not empty a node.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import collections
import posixpath

import yaml

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_status import address_part
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_keyspace_names import is_system_keyspace

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
    ("member_busy", "in the ring, leaving or moving (UL, UM), or joining (UJ) without its host ID or system keyspace"
                    " read here"),
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


def status_address(address):
    """An address of nodetool status, without a :port (nodetool -pp: 10.0.0.1:7000, [::1]:7000)."""
    return address_part(str(address or ""))


def _loopback(address):
    return address.startswith("127.") or address in ("::1", "0:0:0:0:0:0:0:1", "localhost")


def status_nodes(status):
    return [n for dc in (status or {}).values() if isinstance(dc, dict) for n in dc.get("nodes") or []]


def _true(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def _user_keyspace(name):
    return not is_system_keyspace(name) and name != "lost+found"


def _names(names, most=5):
    names = sorted(names)
    return ", ".join(names[:most]) + (", ..." if len(names) > most else "")


def _strip(path):
    path = posixpath.normpath(str(path))
    return "/" + path.lstrip("/") if path.startswith("/") else path


def _seen(node):
    return "%s%s, host ID %s" % (node.get("status", "?"), node.get("state", "?"), node.get("host_id") or "?")


def _unread_ring(view):
    """Why the ring the cluster's nodes see is unknown, in short."""
    stopped = [h for h in view.get("silent") or [] if h not in view["tried"]]
    silent = [h for h in view.get("silent") or [] if h in view["tried"]]
    others = "; ".join(x for x in (("%s did not answer as up and normal" % ", ".join(silent)) if silent else "",
                                   ("Cassandra stopped on %s" % ", ".join(stopped)) if stopped else "") if x)
    lone = view.get("lone") or []
    if len(lone) > 1:
        return "%s answered, each alone in its ring: which one is the cluster can't be told%s" % (
            ", ".join(lone), ("; " + others) if others else "")
    if lone:
        return "only %s answered, alone in its ring; %s" % (lone[0], others)
    return "no other node of the cluster answered as up and normal (UN) (asked: %s%s)" % (
        ", ".join(view["tried"]) or "none", ("; Cassandra stopped on %s" % ", ".join(stopped)) if stopped else "")


def _no_ring(view):
    return "%s: whether it is in the ring can't be checked" % _unread_ring(view)


def _unknown_down(view):
    other, n = view["unknown_down"][0]
    return ("%s sees %s down (host ID %s), a node no host of the inventory has: maybe this node under an old address"
            % (other, status_address(n.get("address")), n.get("host_id") or "?"))


def _unknown_dirs(live):
    return "its directories are unknown: the live cassandra.yaml can not be %s" % ("read" if live == "unreadable" else "parsed")


def mine(addresses):
    """This node's addresses, as nodetool status shows them (no port, no loopback)."""
    return set(status_address(a) for a in addresses or [] if a and not _loopback(status_address(a)))


def _usable(answers):
    """The answers (ring_view's) -> [(host, nodes)] of the nodes that see themselves up and normal (UN): the
    rings that count."""
    usable = []
    for a in answers:
        nodes = status_nodes(a.get("status"))
        own_addr = set(status_address(x) for x in a.get("addresses") or [])
        if any(status_address(n.get("address")) in own_addr and n.get("status") == "U" and n.get("state") == "N" for n in nodes):
            usable.append((a.get("host"), nodes))
    return usable


def _placed(usable, me, own, replace, known, has_data):
    """The rings that count, this node's addresses and host IDs, replace_node's dead node, the group's addresses
    -> {entries, replaced, replaced_up, unknown_down}: where this node (or the node it replaces) shows in them,
    and the down nodes no host of the inventory has (when this node holds data)."""
    out = {"entries": [], "replaced": [], "replaced_up": [], "unknown_down": []}
    for other, nodes in usable:
        for n in nodes:
            address = status_address(n.get("address"))
            if address not in me and n.get("host_id") not in own:
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


def ring_view(answers, addresses, ids=(), replace_address="", known=None, has_data=False):
    """What the rings other nodes of the cluster see say of this node.
    answers: [{'host', 'addresses', 'status'}] (status: cassandra_status
    cluster_status, None when it did not answer); only the answer of a node
    that sees itself up and normal (UN) counts. Returns {'usable': [(host,
    nodes)], 'tried': [hosts], 'entries': [(host, node)]: this node, by one
    of its addresses or host IDs; 'replaced': [(host, node)]: replace_address
    down, the node being replaced; 'replaced_up'; 'unknown_down': [(host,
    node)]: a down node no host of the inventory has (known), when this node
    holds data (maybe this one under an old address)}."""
    answers = answers or []
    out = {"usable": _usable(answers), "tried": [str(a.get("host")) for a in answers if a.get("asked", True)],
           "entries": [], "replaced": [], "replaced_up": [], "unknown_down": []}
    # for a node holding data, a node alone in its ring (a host of the group not added yet, the package's own
    # instance) does not stand for the cluster while other nodes of the group did not answer (asked, or stopped:
    # asked False): their ring is unknown. A node without data needs no proof of that (nothing to lose): a one-node
    # cluster adding its second node, a stock instance on 127.0.0.1 elsewhere in the group (never usable), do not
    # stop it. Several nodes each alone (a one-node cluster and a stray instance at a real address): unknown for any.
    silent = [a for a in answers if not a.get("status")
              or (len(status_nodes(a.get("status"))) > 1 and a.get("host") not in [h for h, dummy in out["usable"]])]
    out["lone"], out["silent"] = [], [str(a.get("host")) for a in silent]
    alone = [h for h, nodes in out["usable"] if len(nodes) <= 1]
    if (_true(has_data) and silent) or (len(alone) > 1 and len(alone) == len(out["usable"])):
        # several nodes each alone in its ring (a one-node cluster and a stray instance): which is the cluster?
        out["lone"] = alone
        out["usable"] = [(h, nodes) for h, nodes in out["usable"] if len(nodes) > 1]
    out.update(_placed(out["usable"], mine(addresses), set(i for i in ids or [] if i), status_address(replace_address),
                       known, has_data))
    return out


def own_ids(addresses, host_id="", running=False, own=None):
    """Its own host IDs: nodetool info's, and its own ring's at its addresses."""
    me = mine(addresses)
    ids = set([host_id] if host_id else [])
    if _true(running) and own:
        ids |= set(n.get("host_id") for n in status_nodes(own) if status_address(n.get("address")) in me and n.get("host_id"))
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
            others = sorted(set(status_address(n.get("address")) for n in status_nodes(own)) - me)
            if len(status_nodes(own)) > 1 and others:
                problems.append("Cassandra runs on %s and its ring has other nodes (%s): it is a member of a cluster,"
                                " never reset a live member" % (name, ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")))
    if not view["usable"]:
        problems.append("%s: whether %s is in the ring can't be checked, nothing is reset" % (_unread_ring(view), name))
    for other, n in view["unknown_down"]:
        problems.append("%s sees %s down (host ID %s), a node no host of the inventory has: if it is %s"
                        " under an old address, its data would be lost. Remove it first (remove_dead_node),"
                        " or give the inventory host its address" % (other, status_address(n.get("address")), n.get("host_id") or "?", name))
    for other, n in view["replaced_up"]:
        problems.append("%s sees %s up (%s): only a dead node is replaced, never reset a live member"
                        % (other, status_address(n.get("address")), _seen(n)))
    for other, n in view["entries"]:
        problems.append("%s sees %s in its ring (%s): %s is a member of the cluster, never reset it."
                        " A node leaves with decommission_node (remove_dead_node when dead), then can be reset"
                        % (other, status_address(n.get("address")), _seen(n), name))
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
    blank.
    e.g. (["/data/cassandra"], a live cassandra.yaml with data_file_directories: [/data2],
    ["/var/lib/cassandra/data"]) -> {own: ["/data/cassandra", "/data2"], elsewhere: ["/var/lib/cassandra/data"],
    problem: ""}."""
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


# what cassandra_node_standing read on a node, normalised, for the helpers that decide its state
_Facts = collections.namedtuple("_Facts", [
    "name", "cluster_name", "running", "jvm", "own", "no_mbean", "my_id", "me", "known", "view", "ring_read",
    "ring_known", "live", "live_cluster", "has_data", "has_system", "unreadable", "holding", "users", "ours", "peers",
    "bootstrap_started", "foreign", "cluster_keyspaces"])


def _evidence(system, held, unreadable, live, running):
    """A node's system keyspaces, what its directories hold, what could not be read, its live cassandra.yaml's
    state, whether Cassandra runs -> what makes it not blank (cassandra_config's question, create_cluster's new
    cluster): [str]."""
    evidence = list(system)
    rest = [h for h in held if h not in system]
    if rest:
        evidence.append("%s in its Cassandra directories" % ", ".join(rest[:3]) + (", ..." if len(rest) > 3 else ""))
    evidence += unreadable
    if live in ("unreadable", "invalid") and not [u for u in unreadable if "cassandra.yaml" in u]:
        evidence.append("the live cassandra.yaml can not be %s" % ("read" if live == "unreadable" else "parsed"))
    if running:
        evidence.append("Cassandra is running")
    return evidence


def _entry(view, me, my_id):
    """ring_view's result, this node's addresses, its host ID -> its entry in the ring, (host, node) or None:
    at one of its addresses first (another host ID there is a conflict, wherever its own host ID shows)."""
    at_address = [e for e in view["entries"] + view["replaced_up"] if status_address(e[1].get("address")) in me]
    # its own entry first, before a down one (a dead node's old address); another host ID up there stays a conflict
    at_address.sort(key=lambda e: 0 if e[1].get("status") == "U" and e[1].get("host_id") != my_id
                    else 1 if my_id and e[1].get("host_id") == my_id else 2)
    return (at_address or view["entries"] or view["replaced_up"] or [None])[0]


def _in_ring_state(entry, f):
    """Its entry in the ring (host, node), the facts -> (state, reasons) of a node the ring has."""
    other, n = entry
    e_id = n.get("host_id") or ""
    in_ring = "%s%s" % (n.get("status", "?"), n.get("state", "?"))
    reasons = ["%s sees %s in its ring (%s)" % (other, status_address(n.get("address")), _seen(n))]
    if f.my_id and e_id and f.my_id != e_id:
        return "address_conflict", reasons + ["its own host ID is %s" % f.my_id]
    if in_ring == "UN" and f.my_id and (f.has_system or f.my_id == e_id):
        return "member", reasons
    if in_ring == "UJ" and f.has_system and (f.my_id or _true(f.no_mbean)):
        return "member_joining", reasons
    if n.get("status") == "D" and f.jvm:
        return "unknown", reasons + ["Cassandra runs here but the ring sees it down"]
    if in_ring == "DN" and not f.has_system:
        return "dead_address", reasons
    if n.get("status") == "D":
        return "member_down", reasons
    if in_ring == "UN":
        return "address_conflict", reasons + ["it does not answer (Cassandra stopped, or JMX unreachable)" if not f.my_id
                                              else "its host ID %s, no system keyspace in its data directories" % f.my_id]
    return "member_busy", reasons


def _running_state(f):
    """The facts of a node the ring does not have, Cassandra running -> (state, reasons): another cluster's, alone
    in its own ring, or unknown."""
    others = sorted(set(status_address(x.get("address")) for x in status_nodes(f.own)) - f.me)
    cluster = set(others) & set(status_address(k) for k in f.known or [])
    if f.live_cluster is not None and f.live_cluster not in (f.cluster_name, STOCK_CLUSTER):
        state, reason = "other_cluster", "its cassandra.yaml names cluster '%s'" % f.live_cluster
    elif not f.own and f.jvm:
        state, reason = "unknown", "Cassandra runs but nodetool status does not answer there"
    elif len(status_nodes(f.own)) > 1 and others and cluster:
        state = "unknown"
        reason = "its own ring lists nodes of this cluster (%s), whose rings do not list it" % _names(cluster)
    elif len(status_nodes(f.own)) > 1 and others:
        state, reason = "other_cluster", "Cassandra runs in another ring (%s)" % _names(others)
    elif not f.ring_read:
        state, reason = "unknown", "Cassandra is running (the ring was not read)"
    elif not f.ring_known:
        state, reason = "unknown", _no_ring(f.view)
    elif f.has_data and f.view["unknown_down"]:
        state, reason = "unknown", _unknown_down(f.view)
    else:
        state = "running_outside_ring"
        reason = "Cassandra runs alone in its own ring" if f.jvm else "its cassandra unit is active, no JVM yet"
    return state, [reason] + ([f.holding] if f.holding else [])


def _data_state(f):
    """The facts of a node holding data, Cassandra stopped, not in the ring as far as it was read -> (state,
    reasons): whose data it is (never joined, a former member, another cluster's, unidentified), or unknown."""
    reasons = [f.holding]
    if not f.ring_read:
        return "unknown", reasons + ["the ring was not read"]
    if not f.ring_known:
        return "unknown", reasons + [_no_ring(f.view)]
    if f.view["unknown_down"]:
        return "unknown", reasons + [_unknown_down(f.view)]
    if f.unreadable or f.live in ("unreadable", "invalid"):
        return "unknown", reasons + (f.unreadable or [_unknown_dirs(f.live)])
    live_cluster, users = f.live_cluster, f.users
    if live_cluster is None:
        return "unidentified_data", reasons + ["the cluster its data belongs to is unknown (no readable cassandra.yaml)"]
    if live_cluster not in (f.cluster_name, STOCK_CLUSTER):
        return "other_cluster", reasons + ["its cassandra.yaml names cluster '%s'" % live_cluster]
    if users and not f.ours:
        return "unidentified_data", reasons + ["user keyspaces (%s) under the cluster name '%s'%s" % (
            _names(users), live_cluster, ", the stock one" if live_cluster == STOCK_CLUSTER else "")]
    if users and f.cluster_keyspaces is None:
        return "unidentified_data", reasons + [
            "user keyspaces (%s), and this cluster's keyspaces could not be read" % _names(users)]
    if f.foreign:
        return "unidentified_data", reasons + ["keyspaces this cluster does not have (%s)" % _names(f.foreign)]
    if f.peers and not f.ours:
        return "unidentified_data", reasons + [
            "its system.peers lists other nodes: a member of another '%s' ring" % live_cluster]
    if users and f.bootstrap_started:
        return "failed_bootstrap", reasons + [
            "this cluster's keyspaces (%s), of a bootstrap add_node started there and that never ended" % _names(users)]
    if users:
        return "former_member", reasons + [
            "this cluster's keyspaces (%s), no bootstrap of add_node left unfinished there" % _names(users)]
    return "failed_bootstrap", reasons + ["no user keyspace, cluster '%s'" % live_cluster]


def _state(entry, f):
    """Its entry in the ring (or None), the facts -> (state, reasons)."""
    if entry:
        return _in_ring_state(entry, f)
    if f.running:
        return _running_state(f)
    if f.has_data:
        return _data_state(f)
    if f.unreadable or f.live in ("unreadable", "invalid"):
        return "unknown", list(f.unreadable or [_unknown_dirs(f.live)])
    return "blank", []


def cassandra_node_standing(node, cluster_name, title="", running=False, jvm=None, own=None, host_id="", no_mbean=False,
                            answers=None, addresses=None, known=None, replace_address="", live="absent",
                            live_cluster=None, held=None, system=None, elsewhere=None, unreadable=None, keyspaces=None,
                            peers=False, bootstrap_started=False, cluster_keyspaces=None):
    """The state of a node, from what was read there (node_standing_read.yml).
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

    Returns {'state', 'words', 'reasons': why, in the operator's words
    ('holding': the reason saying what it holds, among them); 'blank':
    nothing in its directories, Cassandra stopped, all of it read, whatever
    the ring says (the state 'blank' is that and not in the ring; a caller
    that counts a system keyspace left elsewhere, as cassandra_config outside
    an operation, looks at 'elsewhere' too), 'evidence': what makes it not
    blank; 'holds_data', 'has_system', 'running', 'ring': {'read', 'known',
    'in_ring' (UN, DN, ... or ''), 'entry', 'seen_by', 'from', 'tried',
    'problems': why the ring forbids a reset}, 'users': its user keyspaces,
    'ours': its data is of this very cluster, ...}.
    e.g. ("node4", "my_cluster", answers=[], addresses=["10.0.0.4"]) -> {state: "blank", words: "blank:
    nothing in its Cassandra directories, Cassandra stopped, not in the ring", reasons: [], blank: True, ...}; a node
    node1 sees UN under its own host ID -> {state: "member", reasons: ["node1 sees 10.0.0.4 in its ring (UN, host
    ID b4)"], ...}."""
    name = title or node
    running = _true(running)
    jvm = running if jvm is None else _true(jvm)
    live_cluster = live_cluster or None  # a none Ansible before 2.19 may give as ''
    held = list(held or [])
    unreadable = list(unreadable or [])
    system = list(system or [])
    has_data = len(held) > 0
    users = sorted(set(k for k in keyspaces or [] if _user_keyspace(k)))
    ids = own_ids(addresses, host_id, jvm, own)
    # its host ID: nodetool info's, else its own ring's at its addresses
    my_id = host_id or (sorted(ids)[0] if len(ids) == 1 else "")
    ring_read = answers is not None
    view = ring_view(answers or [], addresses, ids, replace_address, known, has_data)
    ring_known = bool(view["usable"])
    # a cluster that kept the stock name: a stock node is not known to be one of its own
    ours = live_cluster is not None and live_cluster == cluster_name and cluster_name != STOCK_CLUSTER
    me = mine(addresses)
    f = _Facts(name=name, cluster_name=cluster_name, running=running, jvm=jvm, own=own, no_mbean=no_mbean, my_id=my_id,
               me=me, known=known, view=view, ring_read=ring_read, ring_known=ring_known, live=live,
               live_cluster=live_cluster, has_data=has_data, has_system=len(system) > 0, unreadable=unreadable,
               holding="it holds data (%s)" % ", ".join((system or held)[:3]) if has_data else "", users=users,
               ours=ours, peers=_true(peers), bootstrap_started=_true(bootstrap_started),
               foreign=[k for k in users if cluster_keyspaces is not None and k not in cluster_keyspaces],
               cluster_keyspaces=cluster_keyspaces)
    # what makes it not blank (cassandra_config's question, create_cluster's new cluster)
    evidence = _evidence(system, held, unreadable, live, running)
    ring = {"read": ring_read, "known": ring_known, "in_ring": "", "entry": {}, "seen_by": "", "tried": view["tried"],
            # why it is unknown, when it is (the operator's words)
            "why": "" if ring_known else _unread_ring(view),
            # the first node of the cluster that answered UN, one not alone in its ring first: the ring add_node plans with
            "from": ([h for h, nodes in view["usable"] if len(nodes) > 1] + [h for h, dummy in view["usable"]] + [""])[0],
            "problems": ring_problems(view, addresses, name, replace_address, jvm, own) if ring_read else []}
    entry = _entry(view, me, my_id)
    if entry:
        ring.update({"in_ring": "%s%s" % (entry[1].get("status", "?"), entry[1].get("state", "?")), "entry": entry[1],
                     "seen_by": entry[0]})
    state, reasons = _state(entry, f)
    for other, n in view["replaced"]:
        reasons.append("%s sees %s down (%s): the node being replaced" % (other, status_address(n.get("address")), _seen(n)))
    return {"state": state, "words": WORDS[state], "reasons": reasons, "node": node, "name": name,
            "cluster_name": cluster_name, "blank": not evidence, "evidence": evidence, "holds_data": has_data,
            "has_system": f.has_system, "system": system, "held": held, "elsewhere": list(elsewhere or []),
            "unreadable": unreadable, "running": running, "ring": ring, "holding": f.holding, "users": users,
            "ours": ours, "live": live, "live_cluster": live_cluster, "peers": f.peers,
            "bootstrap_started": f.bootstrap_started, "host_id": my_id,
            "cluster_keyspaces_known": cluster_keyspaces is not None, "foreign": f.foreign}


def cassandra_node_add_state(state, choice="auto", address=""):
    """add_node's view of a node's state (cassandra_node_standing). Returns
    {'state', 'refusal'}: 'new' (not in the ring: added, emptied first when
    it holds data and may be: cassandra_add_node_reset_check), 'joining' (a
    bootstrap an earlier run started: waited for again), 'joined' (in the
    ring: not added again), or refused with the reason: 'orphan' (data,
    -e cassandra_add_node_reset=false), 'other_host', 'replace', 'down',
    'busy', 'unknown' (in the ring but running there, or holding nothing
    while something could not be read), 'other_cluster', 'running' (no data,
    Cassandra running there: never added, even without new_node_checks).
    A node holding data that is not in the ring is 'new': whether it is
    emptied first is cassandra_add_node_reset_check's. choice:
    cassandra_add_node_reset (cassandra_reset_choice); address: its address,
    for the replace_node command.
    e.g. a state "member" -> {state: "joined", refusal: ""}; "blank" -> {state: "new", refusal: ""}."""
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
            % (e_id, state.get("node"), address or status_address(entry.get("address"))))}
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
    why = "; ".join(r for r in state.get("reasons") or [] if r != state.get("holding"))
    if s == "unknown" and (entry or not state.get("holds_data")):
        return {"state": "unknown", "refusal": "where it stands can't be told: %s. Fix that, then run add_node again." % why}
    if s == "other_cluster" and not state.get("holds_data"):
        return {"state": "other_cluster", "refusal": (
            "a node of another cluster: %s. Take it out of cassandra_target_nodes." % why)}
    if s == "running_outside_ring" and not state.get("holds_data"):
        return {"state": "running", "refusal": (
            "Cassandra runs on it, outside the cluster's ring (%s): stop it (systemctl stop cassandra) if it holds"
            " nothing you need, then run add_node again." % why)}
    # not in the ring: blank, or emptied first when it may be (cassandra_add_node_reset_check)
    if state.get("holds_data") and choice == "false":
        return {"state": "orphan", "refusal": (
            "has data (%s) but is not in the ring: an interrupted bootstrap, or data of another cluster. It was not"
            " reset: -e cassandra_add_node_reset=false was given. Without it, add_node empties such a node first when"
            " it may (Cassandra down, in no ring, cluster this one or the stock '%s', no user keyspace but a"
            " failed bootstrap's of this cluster; what it deletes is on the screen), then adds it."
            % (", ".join((state.get("system") or state.get("held") or [])[:3]), STOCK_CLUSTER))}
    return {"state": "new", "refusal": ""}


def _reset_problems(state, explicit):
    """A node's state (holding data), whether the reset was asked (-e cassandra_add_node_reset=true) -> why add_node
    may not empty it: [str], in the order the refusal gives them."""
    live_cluster = state.get("live_cluster")
    cluster_name = state.get("cluster_name")
    users = state.get("users") or []
    ours = state.get("ours")
    problems = []
    if state.get("running"):
        problems.append("Cassandra runs on it: add_node resets only a node where Cassandra is down. Stop it"
                        " (systemctl stop cassandra) if it holds nothing you need, then run add_node again")
    problems.extend((state.get("ring") or {}).get("problems") or [])
    if live_cluster is None:
        problems.append("the cluster its data belongs to is unknown (no readable cassandra.yaml)")
    elif live_cluster not in (cluster_name, STOCK_CLUSTER):
        problems.append("its data is of cluster '%s', neither '%s' nor the package's stock '%s'"
                        % (live_cluster, cluster_name, STOCK_CLUSTER))
    elif state.get("peers") and not ours:
        problems.append("its system.peers lists other nodes: a member of another '%s' ring" % live_cluster)
    if users and live_cluster is not None:
        if not ours:
            problems.append("it holds user keyspaces (%s): only a failed bootstrap of this cluster may" % _names(users))
        elif not state.get("cluster_keyspaces_known"):
            problems.append("it holds user keyspaces (%s) and the cluster's keyspaces could not be read: whether they"
                            " are a failed bootstrap's can't be checked" % _names(users))
        elif state.get("foreign"):
            problems.append("it holds keyspaces this cluster does not have (%s)" % _names(state["foreign"]))
    if state.get("state") == "former_member" and not _true(explicit):
        problems.append("it holds this cluster's keyspaces (%s) and no bootstrap add_node left unfinished: a former"
                        " member of this cluster (removed with removenode?). The writes only it holds would be lost:"
                        " snapshot or copy its data directories first if in doubt, then -e cassandra_add_node_reset=true"
                        " resets it" % _names(users))
    return problems


def _ring_word(state, ring_probs):
    """A node's state, its ring's problems -> where the ring has it, in a few words ("not in any ring", ...)."""
    seen = [p for p in ring_probs if " sees " in p]
    return ("in the ring of this cluster" if seen else "ring unknown" if ring_probs
            else "in another ring" if state.get("peers") and not state.get("ours") else "not in any ring")


def _holds(state, size, ring_word, former, problems):
    """A node's state, the bytes in its directories, where the ring has it, whether a former member, why it may
    not be reset -> (what it holds, for the plan's line: "node4: has data (...)"; the same in short, for
    cassandra_add_node_refusal: "holds 12.0 GiB of cluster 'x' ..., not in the ring, stopped")."""
    live_cluster = state.get("live_cluster")
    users = state.get("users") or []
    running = state.get("running")
    held = ["%s" % cassandra_output.size(float(size or 0)), "cluster '%s'" % live_cluster if live_cluster is not None
            else "cluster unknown"]
    if users:
        held.append("user keyspaces %s" % _names(users)
                    + ((" (a former member of this cluster)" if former else " (a failed bootstrap of this cluster)")
                       if state.get("ours") and not problems else ""))
    held.extend([ring_word, "running" if running else "down"])
    holds = "%s: has data (%s)" % (state.get("name") or state.get("node"), ", ".join(held))
    short = "holds %s of %s%s, %s, %s" % (
        cassandra_output.size(float(size or 0)), ("cluster '%s'" % live_cluster) if live_cluster is not None
        else "an unknown cluster", (" (keyspaces %s)" % _names(users)) if users else "",
        {"not in any ring": "not in the ring"}.get(ring_word, ring_word), "running" if running else "stopped")
    return holds, short


def _reset_way(state, ring_probs):
    """A node's state (refused), its ring's problems -> the way out that works for the first reason it is refused
    for."""
    live_cluster = state.get("live_cluster")
    if [p for p in ring_probs if " sees " in p]:
        return "a member of this cluster: never reset it; take it out of cassandra_target_nodes"
    if ring_probs:
        return "its ring could not be checked: wait until the cluster's nodes answer"
    if live_cluster is not None and live_cluster not in (state.get("cluster_name"), STOCK_CLUSTER):
        return ("a node of another cluster: take it out of cassandra_target_nodes, or, if nothing it holds is needed,"
                " empty its directories by hand (reset_node refuses another cluster's node)")
    if state.get("running"):
        return "if it holds nothing you need, stop Cassandra on it (systemctl stop cassandra): add_node then resets it"
    if (state.get("users") and live_cluster is not None and state.get("ours")
            and not state.get("cluster_keyspaces_known")):
        return "let add_node read the cluster's keyspaces (CQL login: cassandra_cql_username, cassandra_cql_password)"
    return ("check what it holds; if nothing is needed, empty it (reset_node), else take it out of"
            " cassandra_target_nodes")


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
    first reason it is refused for.
    e.g. (a failed_bootstrap state of node4, size=12 GiB) -> {reset: True, problems: [], holds: "holds 12.0
    GiB of cluster 'my_cluster', not in the ring, stopped", line: "node4: has data (12.0 GiB, cluster 'my_cluster', not
    in any ring, down) \u2014 will be reset", ...}."""
    if not state.get("holds_data"):
        return {"reset": False, "problems": [], "line": "", "holds": "", "why": "", "former": False, "way": ""}
    ring_probs = list((state.get("ring") or {}).get("problems") or [])
    problems = _reset_problems(state, explicit)
    former = state.get("state") == "former_member"
    # what its state allows, and nothing said against it
    reset = (state.get("state") == "failed_bootstrap" or (former and _true(explicit))) and not problems
    if not reset and not problems:
        # refused by its state for a reason the lines above do not give (e.g. a directory that can't be read)
        problems.extend([r for r in state.get("reasons") or [] if r != state.get("holding")] or [state.get("words")])
    holds, short = _holds(state, size, _ring_word(state, ring_probs), former, problems)
    if not reset:
        only_former = former and len(problems) == 1
        return {"reset": False, "problems": problems, "holds": short, "former": only_former,
                "way": _reset_way(state, ring_probs),
                "why": ("a former member of this cluster: the writes only it holds would be lost if wiped" if only_former
                        else "; ".join(problems)),
                "line": "%s: not reset automatically, %s. Nothing was changed: check what it holds; if nothing is"
                        " needed, empty it (reset_node) or remove it from cassandra_target_nodes"
                        % (holds, "; ".join(problems))}
    given = (" (-e cassandra_add_node_reset=true: the writes only it holds are lost; snapshot or copy its data first"
             " if in doubt)") if former else ""
    return {"reset": True, "problems": [], "holds": short, "why": "", "former": False, "way": "",
            "line": "%s — will be reset%s" % (holds, given)}


def cassandra_node_reset_refusal(state, whole_cluster=False):
    """Why reset_node (or replace_node's reset) may not empty a node, from
    where it stands (cassandra_node_standing): [] when it may. It may when
    its state is RESETTABLE (blank, never joined, a former member, data not
    known to be this cluster's, Cassandra running alone outside the ring:
    stopped first) and neither its ring (a node of the cluster sees it, a
    down node no host has while it holds data, no node of the cluster
    answered UN, its own ring has other nodes or does not answer) nor its
    cassandra.yaml (another cluster's) says otherwise: the state is never the
    only guard. whole_cluster: create_cluster wipes every node, it checked
    the ring itself: the cassandra.yaml only.
    Input: cassandra_node_standing's result; output: [str]. e.g. a failed_bootstrap state -> []; a member running
    -> ["Cassandra runs on node4 and its ring has other nodes (10.0.0.1): ...", "node1 sees 10.0.0.4 in its ring (UN,
    host ID b4): node4 is a member of the cluster, never reset it. ..."]."""
    out = [] if whole_cluster else list((state.get("ring") or {}).get("problems") or [])
    live = state.get("live_cluster")
    if live is not None and live not in (state.get("cluster_name"), STOCK_CLUSTER):
        out.append("the live cassandra.yaml is for cluster '%s', not '%s' (nor the package's stock '%s'): a node of"
                   " another cluster?" % (live, state.get("cluster_name"), STOCK_CLUSTER))
    if whole_cluster or state.get("state") in RESETTABLE:
        return out
    if not out:
        # what could not be read (the reset's plan says it too, in the same words), or what it is
        out = [r for r in state.get("reasons") or [] if r != state.get("holding")]
    return out or ["%s: %s" % (state.get("name"), state.get("words"))]


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_add_node_reset_check": cassandra_add_node_reset_check,
            "cassandra_node_add_state": cassandra_node_add_state,
            "cassandra_node_data_dirs": cassandra_node_data_dirs,
            "cassandra_node_reset_refusal": cassandra_node_reset_refusal,
            "cassandra_node_standing": cassandra_node_standing,
        }
