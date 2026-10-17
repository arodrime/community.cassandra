# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The text of the help playbook, from the inventory alone.

OPERATIONS: every playbook of the collection, by theme, with what it does,
    its options and their defaults (the ones its command needs filled from
    the inventory) and an example. The one list to keep up to date: a unit
    test fails when a playbook in playbooks/ is missing from it, or listed
    but absent, or when a default is not the playbook's or the role's.
cassandra_help: model (lookup community.cassandra.cassandra_inventory) ->
    the clusters as the inventory describes them, every operation with its
    command for this inventory, its options and an example, and advice; with
    topic, the detail of one operation; markdown true: the same as RUNBOOK.md;
    flow true: its lines for the ops callback, which lays them out at the
    terminal's width and colours them (plugin_utils cassandra_output.flow).
cassandra_help_runbook: model -> where RUNBOOK.md goes (the inventory's dir, or runbook_dir).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import difflib
import glob
import os
import re
import shlex

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_java import cassandra_java_major
from ansible_collections.community.cassandra.plugins.filter.cassandra_screen import WIDTH
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import (
    BULLET, NBSP, command as _full_command, command_flow, extra_var as _e, flow as _flow, is_command, laid_out,
    path_from as _path, seed_layout)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_roles import role_file

_TOP = os.path.join(os.path.dirname(__file__), "..", "..")

THEMES = (("read-only", "Read-only (change nothing)"), ("nodes", "Nodes"), ("cluster", "Cluster"),
          ("takeover", "Takeover"))

# options: (argument, what it does, default), each default checked against the playbooks and roles by a unit
# test; default None: the command needs it, <dc>, <rack>, <nodes> or <node> filled from the inventory
# (PLACEHOLDERS: a value only you know). example: (what it shows, the arguments it adds, in place of the
# needed ones of the same name). cql: reads the replication with cassandra_cql_username/_password when
# authentication is on ("plan": plans without it, as if every keyspace had replicas everywhere);
# single_token: only for num_tokens 1. confirm False: asks no question (no cassandra_operation_confirm).
RESUME = ("-e cassandra_rolling_resume=true", "resumes an interrupted run", "false")
CHECK = ("changing nothing", ["--check"])
# the words of a cleanup's batches (cassandra_cleanup_mode)
BATCHES = "sequential/rack/dc/all"
OPERATIONS = [
    {"name": "help", "theme": "read-only", "guide": "Help and runbook",
     "summary": "This overview, from the inventory alone (no node contacted).",
     "options": [("-e help_topic=<operation>", "one operation in detail", "the overview"),
                 ("-e help_write=true", "also writes RUNBOOK.md", "false"),
                 ("-e help_runbook_dir=<dir>", "where RUNBOOK.md goes", "the inventory's dir"),
                 ("-e help_inventory=<path>", "reads that inventory instead", "the -i one")],
     "example": ("one operation in detail", ["-e help_topic=rolling_restart"])},
    {"name": "status", "theme": "read-only", "guide": "Operation playbooks",
     "summary": "The ring as nodetool status shows it from one node, per datacenter; a down node is shown, not an"
                " error.",
     "options": [("-e cassandra_target_nodes=<nodes>", "nodes to read from", "the first that answers"),
                 ("-e cassandra_status_raw=true", "also prints nodetool's own output", "false")],
     "example": ("from one node", ["-e cassandra_target_nodes=<node>"])},
    {"name": "health_check", "theme": "read-only", "guide": "Operation playbooks",
     "summary": "Checks the cluster from every node (ring, gossip, native transport, streams, schema, ports); fails"
                " on a problem, so it can be scheduled."},
    {"name": "preflight", "theme": "read-only", "guide": "Operation playbooks",
     "summary": "Checks the nodes against the inventory before a change: settings that must match, racks for the"
                " token allocator, versions, seeds."},
    {"name": "add_node", "theme": "nodes", "cql": "plan", "guide": "Adding a node",
     "summary": "Adds new hosts to the running cluster: all prepared at once, then each started and bootstrapped in"
                " turn. Put them in their rack's group first; one in cassandra_seeds joins as a regular node, then"
                " becomes a seed.",
     "options": [("-e cassandra_target_nodes=NEW_NODE", "the hosts to add (in the inventory)", None),
                 ("-e cassandra_add_node_reset=false", "false: refuses any node with data", "auto"),
                 ("-e cassandra_add_node_cleanup=sequential", "then cleanup: " + BATCHES, "none"),
                 ("-e cassandra_token_auto=bisect", "num_tokens 1: bisect/balanced/true", "false"),
                 ("-e cassandra_clock_check=false", "starts without checking the clocks", "true")],
     "example": ("then the cleanup, one node at a time", ["-e cassandra_add_node_cleanup=sequential"])},
    {"name": "topology", "theme": "nodes", "cql": True, "cql_when": "to remove nodes",
     "guide": "The inventory as the desired state",
     "summary": "Makes the ring match the inventory: adds the hosts of the cluster's group not in the ring, applies"
                " cassandra_seeds, removes the hosts marked cassandra_node_state: absent; one node at a time,"
                " --check shows the plan and its warnings.",
     "options": [("-e cassandra_add_node_reset=false", "false: refuses any host with data", "auto"),
                 ("-e cassandra_token_auto=bisect", "num_tokens 1: bisect/balanced", "false"),
                 ("-e cassandra_decommission_force=true", "allows fewer nodes than replicas", "false")],
     "example": ("the plan only", ["--check"])},
    {"name": "decommission_node", "theme": "nodes", "cql": True, "guide": "Removing a node",
     "summary": "Removes nodes from the running cluster, one at a time, their data streamed to the others;"
                " a node no longer in cassandra_seeds is first dropped from the other nodes' seed lists; refuses a"
                " datacenter left with nodes but no seed, or fewer nodes than replicas.",
     "options": [("-e cassandra_target_nodes=<nodes>", "the nodes to remove", None),
                 ("-e cassandra_decommission_force=true", "allows fewer nodes than replicas", "false"),
                 RESUME],
     "example": CHECK},
    {"name": "replace_node", "theme": "nodes", "guide": "Replacing a dead node",
     "summary": "Replaces a dead node by a blank host, which takes over its tokens and data. In the inventory, the"
                " new host put in first, the dead one taken out.",
     "options": [("-e cassandra_target_nodes=NEW_NODE", "the new host", None),
                 ("-e cassandra_replace_address=DEAD_NODE_ADDRESS", "the address of the dead node", None),
                 ("-e cassandra_replace_node_reset=true", "first empties the new host", "false")],
     "example": ("a node replacing itself", ["-e cassandra_replace_node_reset=true"])},
    {"name": "remove_dead_node", "theme": "nodes", "cql": True, "guide": "Removing a dead node",
     "summary": "Last resort for a dead node that will not be replaced: removenode (or assassinate). Take it out of"
                " the inventory (or mark it absent) first.",
     "options": [("-e cassandra_target_nodes=DEAD_NODE_ADDRESS", "the dead node: address, host ID or name", None),
                 ("-e cassandra_dead_node_method=removenode_force", "removenode_force/assassinate", "removenode"),
                 ("-e cassandra_dead_node_new_removal=true", "restarts a removal stuck elsewhere", "false"),
                 ("-e cassandra_decommission_force=true", "allows fewer nodes than replicas", "false")],
     "example": CHECK},
    {"name": "reset_node", "theme": "nodes", "guide": "Resetting a node",
     "summary": "Empties nodes that are not members of the ring (started once by mistake, a failed bootstrap) for a"
                " fresh start.",
     "options": [("-e cassandra_target_nodes=NODE", "the nodes to empty", None)],
     "example": CHECK},
    {"name": "move_node", "theme": "nodes", "single_token": True, "cql": "plan", "guide": "One token per node",
     "summary": "One token per node: moves nodes to new tokens, one at a time (by default the fewest moves that even"
                " out each datacenter).",
     "options": [("-e '{\"cassandra_move_tokens\": {\"NODE\": \"TOKEN\"}}'", "only those nodes move, to those tokens",
                  "{}"),
                 ("-e cassandra_move_cleanup=sequential", "then cleanup: " + BATCHES, "none"),
                 ("-e cassandra_move_force_disk=true", "goes on with little free disk left", "false")],
     "example": ("the plan only", ["--check"])},
    {"name": "create_cluster", "theme": "cluster", "guide": "Creating a cluster",
     "summary": "Builds the cluster from blank hosts: prepared in parallel, then started one at a time, seeds first."
                " Starts nothing on a running cluster.",
     "options": [("-e cassandra_accept_default_identity=true", "accepts default identity settings", "false"),
                 ("-e cassandra_create_cluster_reset=true", "wipes a running cluster: DATA LOST", "false"),
                 ("-e '{\"cassandra_create_cluster_reset_confirm\": \"<cluster name>\"}'",
                  "the reset without a terminal: the cluster's name", ""),
                 ("-e cassandra_clock_check=false", "starts without checking the clocks", "true")],
     "example": ("the plan only", ["--check"])},
    {"name": "rolling_restart", "theme": "cluster", "cql": True, "cql_when": "rack mode", "guide": "Restarting",
     "summary": "Drains and restarts the nodes one at a time, the cluster checked before and after each one.",
     "options": [("-e cassandra_rolling_mode=rack", "the nodes of a rack together", "node"),
                 ("-e cassandra_rack_force=true", "rack mode: allows 2+ replicas down", "false"),
                 RESUME],
     "example": ("a rack at a time", ["-e cassandra_rolling_mode=rack"])},
    {"name": "rolling_reboot", "theme": "cluster", "guide": "Restarting",
     "summary": "Same as rolling_restart, rebooting the hosts (OS patching).",
     "options": [("-e cassandra_reboot_timeout=3600", "seconds for a host to come back", "1800"), RESUME],
     "example": ("an interrupted run resumed", [RESUME[0]])},
    {"name": "stop_rack", "theme": "cluster", "cql": True, "guide": "Rack maintenance",
     "summary": "Stops every node of one rack at once (maintenance), when the replication allows losing that rack.",
     "options": [("-e cassandra_target_dc=<dc>", "the rack's datacenter", None),
                 ("-e cassandra_target_rack=<rack>", "the rack to stop", None),
                 ("-e cassandra_rack_force=true", "allows 2+ replicas down", "false")],
     "example": CHECK},
    {"name": "start_rack", "theme": "cluster", "confirm": False, "guide": "Rack maintenance",
     "summary": "Starts the nodes of a rack stop_rack stopped, then checks the whole cluster.",
     "options": [("-e cassandra_target_dc=<dc>", "the rack's datacenter", None),
                 ("-e cassandra_target_rack=<rack>", "the rack to start", None),
                 ("-e cassandra_accept_default_identity=true", "accepts the name 'Test Cluster'", "false")],
     "example": CHECK},
    {"name": "apply_config", "theme": "cluster", "guide": "Changing the configuration",
     "summary": "Applies the inventory's config: shows the settings that differ (the inventory's value, then each"
                " other value with its nodes), asks once, then writes the nodes that need it, one at a time,"
                " restarting only those that need it.",
     "options": [RESUME],
     "example": ("the settings that differ, changing nothing", ["--check"])},
    {"name": "change_seeds", "theme": "cluster", "guide": "Changing the seeds",
     "summary": "Applies a new cassandra_seeds list to every node, live (no restart); topology, add_node and"
                " decommission_node apply it too when nodes come and go.",
     "example": CHECK},
    {"name": "update_java", "theme": "cluster", "guide": "Restarting",
     "summary": "Moves the cluster to the Java in cassandra_java_version, one node at a time.",
     "options": [RESUME],
     "example": ("an interrupted run resumed", [RESUME[0]])},
    {"name": "sync_clocks", "theme": "cluster", "guide": "Clocks",
     "summary": "Shows each node's clock as its time sync daemon reports it (synchronised, source, offset) with a"
                " verdict, then brings time sync up where it is missing; a running node whose clock needs a step"
                " gets a restart window, one node at a time.",
     "options": [("-e cassandra_clock_max_offset_ms=50", "in sync within this many ms", "50"),
                 ("-e cassandra_clock_step_ms=400", "steps the clock above this many ms", "400"),
                 ("-e cassandra_clock_sync_timeout=180", "seconds to wait for a sync", "180")],
     "example": ("the clocks, changing nothing", ["--check"])},
    {"name": "upgrade", "theme": "cluster", "guide": "Upgrading",
     "summary": "Upgrades the cluster to the version in the inventory, one phase per run: preflight, prepare,"
                " canary, rolling, sstables, cleanup.",
     "options": [("-e cassandra_upgrade_phase=preflight", "the phase to run, in the order above", None),
                 ("-e cassandra_target_nodes=<node>", "canary: the node to upgrade", "a non-seed"),
                 ("-e cassandra_rolling_resume=true", "resumes the sstables phase", "false")],
     "example": ("the next phase", ["-e cassandra_upgrade_phase=prepare"])},
    {"name": "cleanup", "theme": "cluster", "confirm": False, "guide": "Adding a node",
     "summary": "Runs nodetool cleanup (the data a node no longer owns, after nodes were added), the cluster checked"
                " before each batch.",
     "options": [("-e cassandra_cleanup_mode=rack", BATCHES, "sequential"),
                 ("-e cassandra_cleanup_jobs=4", "cleanup jobs per node, 0: all", "2"),
                 RESUME],
     "example": ("a rack at a time", ["-e cassandra_cleanup_mode=rack"])},
    {"name": "add_datacenter", "theme": "cluster", "cql": True, "guide": "Datacenters",
     "summary": "Adds a datacenter: its nodes join without streaming, the keyspaces get replicas there, then each"
                " node rebuilds from another datacenter.",
     "options": [("-e cassandra_target_nodes=NEW_DC_GROUP", "the new datacenter's nodes", None),
                 ("-e cassandra_rebuild_source_dc=<dc>", "the datacenter they stream from", None),
                 ("-e '{cassandra_datacenter_replication: {KEYSPACE: 3}}'",
                  "each keyspace's replicas there", None)],
     "example": CHECK},
    {"name": "remove_datacenter", "theme": "cluster", "cql": True, "guide": "Datacenters",
     "summary": "Removes a datacenter: the keyspaces stop keeping replicas there, then its nodes leave one at a"
                " time. Move its clients first.",
     "options": [("-e cassandra_target_dc=DC_TO_REMOVE", "the datacenter to remove", None)],
     "example": CHECK},
    {"name": "import_cluster", "theme": "takeover", "guide": "Taking over an existing cluster",
     "summary": "Reads a running cluster into an inventory, changing nothing on the nodes. A new cluster: give"
                " one of its nodes (-i <node>,), the others are found in the ring. A re-import reads the cluster from"
                " the inventory (its logins too), keeps the files it did not write, --check --diff shows its changes"
                " first.",
     "options": [("-e import_cluster_dir=<dir>", "the inventory dir", "inventories"),
                 ("-e import_cluster_report_dir=<dir>", "report.txt dir", "next to the inventory dir"),
                 ("-e import_cluster_force=true", "re-import over this cluster's files", "false"),
                 ("-e import_cluster_adopt=true", "adopts an earlier import's files", "false"),
                 ("-e import_cluster_allow_unread=true", "accepts unread ring nodes", "false"),
                 ("-e import_cluster_keep_hand_edits=true", "keeps hand-edited files (=<names>)", "false"),
                 ("-e import_cluster_keep_unread_jvm_options=true", "imports unread jvm<N> options",
                  "false")]},
]
# an option's text and its default on one line from column 46 at 98 columns (a terminal of 100)
OPTION_ROOM = 52
GUIDE = "docs/docsite/rst/guide_roles.rst"

BY_NAME = dict((op["name"], op) for op in OPERATIONS)
# shown in section 1 or used in the commands: said when their template could not be resolved
UNRESOLVED_CHECKED = ("cassandra_cluster_name", "cassandra_version", "cassandra_package_version",
                      "cassandra_install_method", "cassandra_java_version", "cassandra_dc", "cassandra_rack",
                      "cassandra_seeds", "cassandra_num_tokens", "cassandra_authenticator", "cassandra_endpoint_snitch",
                      "cassandra_allocate_tokens_for_local_replication_factor", "cassandra_node_state")
# the values only the operator knows, written in the commands as is
PLACEHOLDERS = ("NEW_NODE", "DEAD_NODE_ADDRESS", "NODE", "NEW_DC_GROUP", "KEYSPACE", "DC_TO_REMOVE", "NEW_DIR",
                "JMX_USER", "JMX_PASSWORD_FILE", "CLUSTER_GROUP", "DC", "RACK")
_PLACEHOLDER = re.compile(r"(?<![\w-])(%s)(?![\w-])" % "|".join(PLACEHOLDERS))


def _role_defaults():
    out = {}
    for role in ("cassandra_config", "cassandra_install"):
        out.update(role_file(role) or {})
    return out


def _known_names():
    """Every cassandra_* name the collection reads (roles, playbooks)."""
    names = set()
    for pattern in ("roles/*/defaults/*.yml", "roles/*/meta/*.yml", "roles/*/tasks/*.yml", "roles/*/templates/**",
                    "roles/*/vars/*.yml", "roles/*/handlers/*.yml", "playbooks/*.yml"):
        for path in glob.glob(os.path.join(_TOP, pattern), recursive=True):
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    names.update(re.findall(r"\bcassandra_\w+", f.read()))
    return names


def _seeds(value):
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    out = []
    for item in items:
        item = str(item).strip()
        if item.startswith("[") and "]" in item:  # [ipv6]:port
            item = item[1:item.index("]")]
        elif item.count(":") == 1:
            item = item.split(":")[0]
        if item:
            out.append(item)
    return out


def _resolved(value):
    return value not in (None, "", "(vaulted)") and "{{" not in str(value) and "{%" not in str(value)


class _Cluster(object):
    """One cluster of the model, with what the text needs."""

    def __init__(self, cluster, defaults):
        self.name = cluster["name"]
        self.hosts = cluster["hosts"]
        self.defaults = defaults
        for host in self.hosts:
            v = host["vars"]
            host["dc"] = str(v["cassandra_dc"]) if _resolved(v.get("cassandra_dc")) else str(defaults["cassandra_dc"])
            host["rack"] = (str(v["cassandra_rack"]) if _resolved(v.get("cassandra_rack"))
                            else str(defaults["cassandra_rack"]))
            host["address"] = next((str(v[k]) for k in ("cassandra_listen_address", "ansible_host")
                                    if _resolved(v.get(k)) and v[k] != "localhost"), host["name"])
            host["absent"] = str(v.get("cassandra_node_state") or "").lower() == "absent"
        self.dcs = {}
        for host in self.hosts:
            self.dcs.setdefault(host["dc"], {}).setdefault(host["rack"], []).append(host)
        self.seeds = []
        # a template help can't resolve (e.g. from hostvars): no seed advice then
        self.seeds_unread = any("cassandra_seeds" in h["vars"] and not _resolved(h["vars"]["cassandra_seeds"])
                                for h in self.hosts)
        for host in self.hosts:
            if not _resolved(host["vars"].get("cassandra_seeds")):
                continue
            for seed in _seeds(host["vars"].get("cassandra_seeds")):
                if seed not in self.seeds:
                    self.seeds.append(seed)
        for host in self.hosts:
            host["seed"] = bool({host["name"], host["address"], str(host["vars"].get("ansible_host") or "")}
                                & set(self.seeds))
        self.present = [h for h in self.hosts if not h["absent"]]

    def seed_layout(self):
        """The seed rule (preflight's): the nodes not marked absent."""
        return seed_layout([dict((k, h[k]) for k in ("name", "address", "dc", "rack", "seed")) for h in self.present])

    def values(self, key, default=None):
        """{value: [hosts]} over the nodes that stay; the role default when not set."""
        out = {}
        for host in self.present or self.hosts:
            value = host["vars"].get(key)
            if not _resolved(value) and value != "(vaulted)":
                value = default
            out.setdefault(value, []).append(host["name"])
        return out

    def show(self, key, default=None):
        found = self.values(key, default)
        unset = [h["name"] for h in self.present or self.hosts
                 if not _resolved(h["vars"].get(key)) and h["vars"].get(key) != "(vaulted)"]
        if len(found) == 1:
            value = list(found)[0]
            if value is None:
                return "not set"
            return "%s%s" % (value, " (role default)" if unset else "")
        return "MIXED: " + "; ".join("%s (%s)" % (v if v is not None else "not set", ", ".join(h))
                                     for v, h in sorted(found.items(), key=lambda x: str(x[0])))

    def java(self):
        series = self.values("cassandra_version", self.defaults["cassandra_version"])
        versions = self.defaults.get("cassandra_java_versions") or {}
        out = {}
        for host in self.present or self.hosts:
            v = host["vars"]
            if _resolved(v.get("cassandra_java_version")):
                java = str(v["cassandra_java_version"])
            else:
                s = next(k for k, hs in series.items() if host["name"] in hs)
                java = "%s (role default)" % versions.get(str(s), "17")
            how = self._java_how(v, java.split()[0])
            out.setdefault("%s, %s" % (java, how), []).append(host["name"])
        if len(out) == 1:
            return list(out)[0]
        return "MIXED: " + "; ".join("%s (%s)" % (k, ", ".join(h)) for k, h in sorted(out.items()))

    @staticmethod
    def _java_how(v, java):
        """Where the Java comes from, as cassandra_install decides it."""
        install = str(v.get("cassandra_install_java", True)).lower() not in ("false", "no", "off", "0")
        tarballs = v.get("cassandra_java_tarballs")
        if "cassandra_java_tarball" in v:
            tarball = _resolved(v["cassandra_java_tarball"])
        else:  # the cassandra_java_tarballs entry of this Java (a mirror file shared by every cluster)
            tarball = install and isinstance(tarballs, dict) and any(
                cassandra_java_major(k) == cassandra_java_major(java) for k in tarballs)
        if tarball:
            return "tarball"
        if _resolved(v.get("cassandra_java_home")):
            return "at %s" % v["cassandra_java_home"]
        return "package" if install else "set up by other means"

    def num_tokens(self):
        found = self.values("cassandra_num_tokens", self.defaults["cassandra_num_tokens"])
        try:
            return int(list(found)[0]) if len(found) == 1 else None
        except (TypeError, ValueError):
            return None

    def rf(self):
        """allocate_tokens_for_local_replication_factor, when known."""
        found = self.values("cassandra_allocate_tokens_for_local_replication_factor")
        tokens = self.num_tokens()
        if list(found) == [None]:
            return 3 if tokens and tokens > 1 else None
        try:
            return int(list(found)[0]) if len(found) == 1 else None
        except (TypeError, ValueError):
            return None


def _inventory_dir(model):
    sources = model.get("sources") or []
    first = sources[0] if sources else "."
    return first if os.path.isdir(first) else os.path.dirname(first) or "."


def _options(model, cwd):
    """The run's options, the paths given made relative to the current dir as -i is."""
    out = list(model.get("options") or [])
    for i, option in enumerate(out[:-1]):
        if option in ("--vault-password-file", "--private-key"):
            out[i + 1] = _path(out[i + 1], cwd)
        elif option == "--vault-id" and "@" in out[i + 1] and out[i + 1].split("@", 1)[1] != "prompt":
            label, path = out[i + 1].split("@", 1)
            out[i + 1] = "%s@%s" % (label, _path(path, cwd))
        elif option == "--vault-id" and "@" not in out[i + 1]:
            out[i + 1] = _path(out[i + 1], cwd)
    return out


def _same_sources(a, b):
    def norm(sources):
        return sorted(os.path.realpath(os.path.expanduser(str(s))) for s in sources or [] if s)
    return norm(a) == norm(b)


def _command(op, model, cluster, cwd, extra=(), markdown=False):
    """The command of op for this cluster, its placeholders filled; extra: arguments added (an example); no -i
    when the inventory is the configuration's (ansible.cfg), no -e cassandra_hosts when the playbooks take
    this cluster without it."""
    if op["name"] == "import_cluster" and cluster.name in (model.get("imported") or []):
        # a re-import, into this inventory's dir again: read from the inventory itself (its connection variables
        # and logins, vaulted ones too), the cluster named unless the import takes it alone: on screen, the
        # one CASSANDRA_CLUSTER names, or the inventory holds its hosts only (without -e it reads every host)
        where = _path(_inventory_dir(model), cwd)
        target = ([] if where == "inventories" else [_e("import_cluster_dir", where)]) + ["-e import_cluster_force=true"]
        sources = model.get("sources") or []
        if model.get("default_sources") and _same_sources(sources, model["default_sources"]) and (
                not markdown or model.get("default_sources_origin") == "cfg"):
            sources = []
        alone = "all_hosts" in model and set(model["all_hosts"]) <= set(h["name"] for h in cluster.hosts)
        named = markdown or not (alone or model.get("env_cluster") == cluster.name)
        return _full_command(op["name"], inventory=sources, options=[shlex.quote(o) for o in _options(model, cwd)],
                             cwd=cwd, extra=target + list(extra), hosts=cluster.name if named else None)
    if op["name"] == "import_cluster":
        # -u, --private-key, -K only: the import becomes root itself, and reads its own vault password file
        options, user = [shlex.quote(o) for o in _options(model, cwd)], []
        for i, option in enumerate(options):
            if option in ("-u", "--private-key"):
                user += options[i:i + 2]
            elif option == "-K":
                user.append(option)
        if not cluster.hosts:  # no cluster named: one of its nodes, a dir of its own
            return " ".join(["ansible-playbook", "-i NODE,"] + user
                            + ["community.cassandra.import_cluster", "-e import_cluster_dir=NEW_DIR"])
        # the inventory's connection settings and JMX login: -i ADDRESS, reads none of them
        # (ansible_ssh_common_args, a bastion: from ansible.cfg only)
        host = (cluster.present or cluster.hosts)[0]
        v = host["vars"]
        if "-u" not in user and _resolved(v.get("ansible_user")):
            user += ["-u", shlex.quote(str(v["ansible_user"]))]
        if "--private-key" not in user and _resolved(v.get("ansible_ssh_private_key_file")):
            user += ["--private-key", shlex.quote(str(v["ansible_ssh_private_key_file"]))]
        if _resolved(v.get("ansible_port")):
            user += [_e("ansible_port", v["ansible_port"])]
        jmx = []
        for key, placeholder in (("cassandra_jmx_username", "JMX_USER"),
                                 ("cassandra_jmx_password_file", "JMX_PASSWORD_FILE")):
            if _resolved(v.get(key)):
                jmx.append(_e(key, v[key]))
            elif key in host["names"] or (key.endswith("_file") and "cassandra_jmx_password" in host["names"]):
                # vaulted or unread; a password in clear on the command line: its file on the nodes instead
                jmx.append(_e(key, placeholder))
        address = next((str(v[k]) for k in ("ansible_host", "cassandra_listen_address")
                        if _resolved(v.get(k)) and v[k] != "localhost"), host["name"])
        # a new cluster (not written by the import here): a dir of its own
        target = ["-e import_cluster_dir=NEW_DIR"]
        parts = (["ansible-playbook", "-i %s" % shlex.quote(address + ",")] + user
                 + ["community.cassandra.import_cluster"] + target + jmx)
        return " ".join(p for p in parts if p)
    first_dc = sorted(cluster.dcs)[0]
    absent = [h["name"] for h in cluster.hosts if h["absent"]]
    # a real node only when the inventory marks it for removal (or to read from): never one nobody chose
    fill = {"dc": first_dc, "rack": sorted(cluster.dcs[first_dc])[-1],
            "nodes": ",".join(absent) if absent else "NODE",
            "node": (cluster.present or cluster.hosts)[0]["name"] if cluster.hosts else "NODE"}
    # help needs no sudo password, nor the vault prompt help added for the operations
    drop = ("-K", "--ask-vault-pass") if model.get("vault_prompt_added") else ("-K",)
    options = [shlex.quote(o) for o in _options(model, cwd) if op["name"] != "help" or o not in drop]
    sources = model.get("sources") or []
    # (RUNBOOK.md is read in other shells: there, only ansible.cfg's inventory goes without saying)
    if model.get("default_sources") and _same_sources(sources, model["default_sources"]) and (
            not markdown or model.get("default_sources_origin") == "cfg"):
        sources = []
    args = []
    replaced = [_name(arg) for arg in extra]
    for arg in [o[0] for o in op.get("options") or [] if o[2] is None and _name(o[0]) not in replaced] + list(extra):
        filled = re.match(r"^-e (\w+)=<(\w+)>$", arg)
        args.append(_e(filled.group(1), fill[filled.group(2)]) if filled and filled.group(2) in fill else arg)
    return _full_command(op["name"], inventory=sources, options=options, cwd=cwd, extra=args,
                         hosts=cluster.name if _hosts_given(model, cluster, markdown) else None)


def _hosts_given(model, cluster, markdown=False):
    """Whether the commands give -e cassandra_hosts: when the playbooks would not take this cluster without
    it: not the inventory's only cluster, nor (on screen) the one CASSANDRA_CLUSTER names. RUNBOOK.md, kept and
    read later, names a cluster the import wrote always (its dir is shared: another cluster may come)."""
    if markdown:
        return model.get("auto") != cluster.name or cluster.name in (model.get("imported") or [])
    return model.get("auto") != cluster.name and model.get("env_cluster") != cluster.name


def _name(arg):
    """The variable an argument sets (-e name=value, -e '{name: ...}'), or the argument."""
    return re.search(r"\w+", arg[3:]).group(0) if arg.startswith("-e ") else arg


def _advice(model, cluster, playbooks, cwd, known, markdown=False):
    out = []
    absent = [h for h in cluster.hosts if h["absent"]]
    if absent:
        names = ", ".join(h["name"] for h in absent)
        if "topology" in playbooks:  # the playbook of the desired state, when the collection has it
            out.append(("Marked cassandra_node_state: absent: %s. topology --check shows the plan to remove"
                        " them, then topology without --check does it:" % names,
                        _command(BY_NAME["topology"], model, cluster, cwd, ["--check"], markdown)))
        else:
            out.append(("Marked cassandra_node_state: absent: %s. decommission_node removes them from the ring"
                        " (--check first), then take them out of the inventory:" % names,
                        _command(BY_NAME["decommission_node"], model, cluster, cwd, markdown=markdown)))
        seeds = [h["name"] for h in absent if h["seed"]]
        if seeds:
            out.append("Seed and marked absent: %s. Take it out of cassandra_seeds in the inventory: topology"
                       " then drops it from the seed lists before it leaves." % ", ".join(seeds))

    auth = cluster.values("cassandra_authenticator", cluster.defaults["cassandra_authenticator"])
    if any("PasswordAuthenticator" in str(a) for a in auth) and not any(
            h["vars"].get("cassandra_cql_username") for h in cluster.hosts):
        needs = [op["name"] + (" (%s)" % op["cql_when"] if op.get("cql_when") else "")
                 for op in OPERATIONS if op.get("cql") is True and op["name"] in playbooks]
        plans = [op["name"] for op in OPERATIONS if op.get("cql") == "plan" and op["name"] in playbooks]
        out.append("Authentication is on (PasswordAuthenticator) but cassandra_cql_username is not set%s:"
                   " %s read the replication over CQL and need cassandra_cql_username and cassandra_cql_password"
                   " (in a vaulted file of the cluster's group_vars)%s."
                   % (" in the files read (a vaulted one may set it)" if model.get("vault_skipped") else "",
                      ", ".join(needs),
                      "; without them, %s plan as if every keyspace had replicas everywhere" % " and ".join(plans)
                      if plans else ""))

    unresolved = {}
    for host in cluster.hosts:
        for key in UNRESOLVED_CHECKED:
            if key in host["vars"] and not _resolved(host["vars"][key]):
                unresolved.setdefault(key, []).append(host["name"])
    for key, hosts in sorted(unresolved.items()):
        out.append("%s could not be read from the inventory alone (a vaulted value, a fact of the node, a lookup;"
                   " %s): what is shown above for it, and the commands filled from it, may be wrong."
                   % (key, ", ".join(hosts) if len(hosts) < len(cluster.hosts) else "every node"))

    names = set()
    for host in cluster.hosts:
        names.update(host["names"])
    for name in sorted(names - known):
        match = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.85)
        if match:
            users = [h["name"] for h in cluster.hosts if name in h["names"]]
            out.append("%s is set (%s) but no role or playbook reads it: did you mean %s?"
                       % (name, ", ".join(users) if len(users) < len(cluster.hosts) else "every node", match[0]))

    inventory_names = set()
    for host in cluster.hosts:
        inventory_names.update([host["name"], host["address"], str(host["vars"].get("ansible_host") or "")])
    if cluster.seeds_unread:
        pass
    elif not cluster.seeds:
        out.append("No cassandra_seeds in the inventory: set it in the cluster's group_vars, 2 or 3 nodes per"
                   " datacenter, on different racks when there are several.")
    strangers = [] if cluster.seeds_unread else [s for s in cluster.seeds if s not in inventory_names]
    if strangers:
        out.append("Seeds that are no node of the inventory: %s." % ", ".join(strangers))
    if cluster.seeds and not cluster.seeds_unread:
        layout = cluster.seed_layout()
        for problem in layout["problems"]:
            out.append("%s (the rule: 2 or 3 seeds per datacenter, on different racks when there are several): set"
                       " cassandra_seeds, then change_seeds applies it live." % problem)
        for note in layout["notes"]:
            out.append("%s (more seeds, more gossip; no gain)." % note)

    rf = cluster.rf()
    for dc in sorted(cluster.dcs):
        count = len([r for r, hosts in cluster.dcs[dc].items() if any(not h["absent"] for h in hosts)])
        if rf and 1 < count < rf:
            out.append("%s has %d racks with allocate_tokens_for_local_replication_factor %d: the token allocator"
                       " needs one rack or at least %d (preflight refuses it)." % (dc, count, rf, rf))
        sizes = sorted(len([h for h in hosts if not h["absent"]]) for hosts in cluster.dcs[dc].values())
        if len(sizes) > 1 and sizes[0] != sizes[-1]:
            out.append("%s: racks of different sizes (%s nodes): the data is not shared evenly; add or remove"
                       " nodes rack by rack." % (dc, ", ".join(str(s) for s in sizes)))

    for key, label in (("cassandra_version", "series"), ("cassandra_package_version", "package versions")):
        found = dict((v, h) for v, h in cluster.values(key).items() if v is not None)
        if len(found) > 1:
            out.append("Mixed %s across the nodes: %s. An upgrade in progress? Finish it (upgrade), or make the"
                       " inventory agree." % (label, "; ".join("%s (%s)" % (v, ", ".join(h))
                                                               for v, h in sorted(found.items()))))
    if cluster.java().startswith("MIXED"):
        out.append("Mixed Java across the nodes: update_java moves them all to cassandra_java_version.")
    return out


class _AnyCluster(object):
    """The cluster of the commands when none is named on an inventory of several (CLUSTER_GROUP, DC, RACK):
    nothing known of its nodes."""

    name = "CLUSTER_GROUP"
    hosts = present = ()
    dcs = {"DC": {"RACK": []}}

    def num_tokens(self):
        return 1  # (single_token operations: not said "not for this cluster")


def _clusters(model):
    if "unnamed" in model:
        return [_AnyCluster()]
    defaults = _role_defaults()
    return [_Cluster(dict(c, hosts=[dict(h) for h in c.get("hosts") or []]), defaults)
            for c in model.get("clusters") or []]


def _unnamed_lines(model):
    """Section 1 when no cluster is named on an inventory of several: why, the clusters found, how to name one."""
    lines = _paragraph("%s The operations below are for any cluster (CLUSTER_GROUP: its group), without what the"
                       " inventory says of one (its nodes, datacenters, advice)." % model.get("unnamed", ""), "", "  ")
    lines += [""] + _paragraph("Clusters found: %s" % (", ".join(model.get("found") or []) or "none"), "", "  ")
    if model.get("others"):
        lines += _paragraph("Other groups: %s" % ", ".join(model["others"]), "", "  ")
    lines += [""] + _paragraph("The help of one cluster: -e cassandra_hosts=CLUSTER_GROUP, or"
                               " CASSANDRA_CLUSTER=<group or cluster name> in your shell.", "", "  ")
    return lines


_KEY = 36  # the widest option on the line of its text (from column indent + _KEY + 2): a longer one has its own


def _paragraph(text, indent="", hang=None):
    """A paragraph, its next lines indented by hang (a string like indent; indent and 2 more by default), laid out by
    _lines."""
    return [_flow(len(indent) + 2 if hang is None else len(hang), indent + str(text).strip())]


def _pair(indent, key, width, text):
    """A key (an operation, an option) then its text from the column indent + width + 2, laid out by _lines."""
    return _flow(len(indent) + width + 2, text, indent + key)


def _rule(title, heavy=True):
    """A rule naming a section (the ops callback draws it as wide as the terminal)."""
    line = ("==== " if heavy else "---- ") + title + " "
    return ["", line + ("=" if heavy else "-") * max(4, WIDTH - len(line)), ""]


def _lines(lines, flow=False, cut=True):
    """The text: its lines laid out at WIDTH (cut false: the commands whole, on one line each), or as they are for
    the ops callback (flow), which lays them out at the terminal's width; one blank line at most between two lines,
    none first or last."""
    out = []
    for line in lines:
        width = WIDTH if cut or not is_command(line) else 10 ** 6
        for item in [line] if flow else laid_out(line, width)[0]:
            if item.strip():
                out.append(item)
            elif out and out[-1]:
                out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _cluster_lines(cluster):
    lines = []
    present = cluster.present or cluster.hosts
    lines += _paragraph("Cluster '%s' (inventory group %s): %d node%s%s"
                        % (cluster.show("cassandra_cluster_name", cluster.defaults["cassandra_cluster_name"]),
                           cluster.name, len(present), "s" if len(present) != 1 else "",
                           ", %d more marked absent" % (len(cluster.hosts) - len(present))
                           if len(present) < len(cluster.hosts) else ""), "", "  ")
    package = cluster.show("cassandra_package_version")
    lines += _paragraph("Cassandra: series %s, package %s, installed from %s"
                        % (cluster.show("cassandra_version", cluster.defaults["cassandra_version"]),
                           "latest of the series" if package == "not set" else package,
                           cluster.show("cassandra_install_method", cluster.defaults["cassandra_install_method"])),
                        "  ", "    ")
    lines += _paragraph("Java: %s" % cluster.java(), "  ", "    ")
    lines += _paragraph("Snitch: %s, num_tokens: %s, authenticator: %s"
                        % (cluster.show("cassandra_endpoint_snitch", cluster.defaults["cassandra_endpoint_snitch"]),
                           cluster.show("cassandra_num_tokens", cluster.defaults["cassandra_num_tokens"]),
                           cluster.show("cassandra_authenticator", cluster.defaults["cassandra_authenticator"])),
                        "  ", "    ")
    if cluster.seeds and not cluster.seeds_unread:  # the seed rule of preflight, one line per datacenter
        for line in cluster.seed_layout()["lines"]:
            lines += _paragraph(line, "  ", "    ")
    else:
        lines += _paragraph("Seeds: %s" % ("not readable from the inventory alone"
                                           if cluster.seeds_unread and not cluster.seeds
                                           else ", ".join(cluster.seeds) or "none"), "  ", "    ")
    for dc in sorted(cluster.dcs):
        racks = cluster.dcs[dc]
        count = sum(len(h) for h in racks.values())
        lines.append("")
        absent = sum(1 for hosts in racks.values() for h in hosts if h["absent"])
        lines.append("  %s: %d node%s%s, %d rack%s" % (dc, count, "s" if count != 1 else "",
                                                       " (%d marked absent)" % absent if absent else "",
                                                       len(racks), "s" if len(racks) != 1 else ""))
        for rack in sorted(racks):
            nodes = []
            for host in racks[rack]:
                marks = [m for m, on in (("seed", host["seed"]), ("absent", host["absent"])) if on]
                text = host["name"] if host["address"] == host["name"] else "%s %s" % (host["name"], host["address"])
                nodes.append(text + (" (%s)" % ", ".join(marks) if marks else ""))
            lines += _paragraph("%s: %s" % (rack, ", ".join(nodes)), "    ", "      ")
    return lines


def _header(model, cwd):
    sources = ", ".join(_path(s, cwd) for s in model.get("sources") or [])
    return "Cassandra help for the inventory %s (read from the inventory only: no node contacted)" % sources


def cassandra_help(model, playbooks=None, topic="", header="", markdown=False, cwd=None, runbook_dir="", flow=False,
                   cut=True):
    """model: lookup community.cassandra.cassandra_inventory; playbooks: the
    names of the playbooks there are; topic: one operation in detail, with
    header the comment that starts its playbook; markdown: RUNBOOK.md, in
    runbook_dir (default the inventory's dir); flow: the lines for the ops
    callback (laid out at the terminal's width there), else laid out at WIDTH;
    cut false: each command whole on one line (a list of lines to copy from)."""
    model = model or {}
    cwd = os.getcwd() if cwd is None else cwd
    playbooks = list(playbooks or [op["name"] for op in OPERATIONS])
    clusters = _clusters(model)
    if not clusters:
        raise AnsibleFilterError("no cluster group in the inventory %s" % ", ".join(model.get("sources") or []))
    if topic:
        return _topic(topic, header, model, clusters, cwd, flow, cut)

    sections = []  # (title, items): the cluster lines, (kind, title or operation), (advice, its command or "")
    unnamed = "unnamed" in model
    lines = _unnamed_lines(model) if unnamed else []
    for cluster in [] if unnamed else clusters:
        if lines:
            lines.append("")
        lines += _cluster_lines(cluster)
    several = len(clusters) > 1
    sections.append(("1. The clusters of the inventory" if unnamed else
                     "1. The cluster%s as the inventory describes %s" % (("s", "them") if several else ("", "it")), lines))

    ops = []
    for cluster in clusters:
        if len(clusters) > 1:
            ops.append(("cluster", cluster.name))
        for theme, title in THEMES:
            ops.append(("theme", title))
            ops += [("op", _op(op, model, cluster, cwd, markdown)) for op in OPERATIONS
                    if op["theme"] == theme and op["name"] in playbooks]
    sections.append(("2. Operations", ops))

    known = _known_names()
    advice = []
    if model.get("vault_skipped"):
        advice.append(("Vault-encrypted files not read (help decrypts nothing): %s. The values they set are not"
                       " shown above; the operations read them: vault_password_file in ansible.cfg, or the vault"
                       " option of the commands above."
                       % ", ".join(_path(p, _inventory_dir(model)) for p in model["vault_skipped"]), ""))
    for cluster in [] if unnamed else clusters:
        for item in _advice(model, cluster, playbooks, cwd, known, markdown):
            prefix = "%s: " % cluster.name if len(clusters) > 1 else ""
            advice.append((prefix + item[0], item[1]) if isinstance(item, tuple) else (prefix + item, ""))
    sections.append(("3. Advice", advice or [("Name the cluster for the advice of its inventory." if unnamed
                                              else "Nothing to point out.", "")]))

    if markdown:
        return _markdown(_header(model, cwd), sections, model, cwd, runbook_dir or _inventory_dir(model))
    return _text(_header(model, cwd), sections, _common(model, clusters), flow, cut)


_INTRO = ("Run the commands from the directory help was run from (the one with ansible.cfg, if any). Each"
          " operation shows its plan first; --check runs it without changing anything. One operation in detail:"
          " -e help_topic=<operation>. Placeholders such as NEW_NODE or NODE: your own values.")


def _op(op, model, cluster, cwd, markdown=False):
    """What the text and RUNBOOK.md show of one operation, for this cluster."""
    summary = op["summary"]
    if op.get("single_token") and cluster.num_tokens() != 1:
        summary += " Not for this cluster (num_tokens %s)." % (cluster.num_tokens() or "mixed")
    label, extra = op.get("example") or ("", [])
    return {"name": op["name"], "summary": summary, "command": _command(op, model, cluster, cwd, markdown=markdown),
            "options": op.get("options") or [],
            "example": (label, _command(op, model, cluster, cwd, extra, markdown)) if extra else None}


def _common(model, clusters, op=None, markdown=False):
    """The options of every operation that changes something (of op: without the question it does not ask)."""
    given = any(_hosts_given(model, cluster, markdown) for cluster in clusters)
    return [o for o in [("-e cassandra_hosts=<group>", "the cluster to run on",
                         "as in the commands" if given else model["auto"]),
                        ("-e cassandra_operation_confirm=false", "asks nothing (no terminal)", "true"),
                        ("--check", "shows the plan, changing nothing", "")]
            if op is None or op.get("confirm", True) or "confirm" not in o[0]]


def _option_text(text, default, glue=" "):
    return text + (" (required)" if default is None else " (default:%s%s)" % (glue, default) if default else "")


def _option_lines(options, indent):
    """One line per option: its argument, then what it does and its default, the texts of every operation in one
    column."""
    # "(default:" and its value's first word, a <placeholder>, on one line
    return [_pair(indent, arg, _KEY, re.sub(r"<[^>]*>", lambda m: m.group(0).replace(" ", NBSP),
                                            _option_text(text, default, NBSP)))
            for arg, text, default in options]


def _op_lines(op, width, commands, examples):
    """An operation: its name and what it does, its commands, its options, its example."""
    out = [_pair("  ", BULLET + " " + op["name"], len(BULLET) + 1 + width, op["summary"])] + commands
    out += _option_lines(op["options"], "        ")
    if examples:
        out += _paragraph("Example (%s):" % op["example"][0], "      ", "        ") + examples
    return out


def _text(header, sections, common, flow=False, cut=True):
    lines = _paragraph(header, "", "  ") + [""] + _paragraph(_INTRO, "", "") + [""]
    lines += _paragraph("Options of every operation that changes something:", "", "  ") + _option_lines(common, "        ")
    for title, items in sections:
        if title.startswith("1."):
            lines += _rule(title.split(" ", 1)[1]) + items
        elif title.startswith("3."):
            lines += _rule(title.split(" ", 1)[1])
            for text, command in items:
                lines += [""] + _paragraph(text, "- ", "  ") + ([command_flow("    ", command)] if command else [])
        else:
            width = max([len(item["name"]) for kind, item in items if kind == "op"] or [0])
            cluster = ""
            for kind, item in items:
                if kind == "cluster":  # several clusters: their names in the rules
                    cluster = item + ": "
                elif kind == "theme":
                    lines += _rule(cluster + item)
                else:
                    examples = [command_flow("      ", item["example"][1])] if item["example"] else []
                    lines += [""] + _op_lines(item, width, [command_flow("      ", item["command"])], examples)
    return _lines(lines, flow, cut)


def _markdown_options(options):
    return ["- `%s`: %s" % (arg, _option_text(text, default)) for arg, text, default in options] + [""]


def _markdown(header, sections, model, cwd, runbook_dir):
    where = os.path.relpath(cwd, runbook_dir)
    out = ["# RUNBOOK", "",
           "Written by `community.cassandra.help -e help_write=true` from the inventory alone (%s): run it again"
           " after changing the inventory." % ", ".join(_path(src, cwd) for src in model.get("sources") or []), "",
           _INTRO.replace("the directory help was run from (the one with ansible.cfg, if any)",
                          "this file's directory (where help was run, with its ansible.cfg if any)" if where == "."
                          else "`%s`, relative to this file (where help was run, with its ansible.cfg if any)"
                          % where), "",
           "Options of every operation that changes something:", ""]
    out += _markdown_options(_common(model, _clusters(model), markdown=True))
    for title, items in sections:
        out += ["## %s" % title, ""]
        if title.startswith("1."):
            out += ["```text"] + _lines(items).split("\n") + ["```", ""]
            continue
        for item in items:
            if title.startswith("3."):
                out += ["- %s" % item[0], ""] + (["  ```sh", "  " + item[1], "  ```", ""] if item[1] else [])
            elif item[0] == "op":
                op = item[1]
                out += ["**%s** - %s" % (op["name"], op["summary"]), "", "```sh", op["command"], "```", ""]
                out += (["Options:", ""] + _markdown_options(op["options"])) if op["options"] else []
                if op["example"]:
                    out += ["Example (%s):" % op["example"][0], "", "```sh", op["example"][1], "```", ""]
            elif item[0] == "cluster":
                out += ["### Cluster %s" % item[1], ""]
            else:
                out += ["%s %s" % ("####" if len(model.get("clusters") or []) > 1 else "###", item[1]), ""]
    return "\n".join(out).rstrip("\n") + "\n"


def _topic(topic, header, model, clusters, cwd, flow=False, cut=True):
    if topic not in BY_NAME:
        raise AnsibleFilterError("help_topic: no operation %s (operations: %s)"
                                 % (topic, ", ".join(op["name"] for op in OPERATIONS)))
    op = BY_NAME[topic]
    cmds, examples, raw = [], [], []
    for cluster in clusters:
        item = _op(op, model, cluster, cwd)
        raw += [item["command"]] + ([item["example"][1]] if item["example"] else [])
        named = _paragraph("Cluster %s:" % cluster.name, "      ") if len(clusters) > 1 else []
        cmds += named + [command_flow("      ", item["command"])]
        if op.get("single_token") and cluster.num_tokens() != 1:
            cmds.append("      (not for this cluster: num_tokens %s)" % (cluster.num_tokens() or "mixed"))
        if item["example"]:
            examples += named + [command_flow("      ", item["example"][1])]
    options = list(op.get("options") or [])
    options += _common(model, clusters, op) if op["theme"] in ("nodes", "cluster") else []
    lines = _rule(dict(THEMES)[op["theme"]]) + _op_lines(dict(op, options=options), len(op["name"]), cmds, examples)
    # (the -i NODE, of a new cluster's import: a placeholder too)
    shown = " ".join(c.split("community.cassandra.", 1)[-1] + (" NODE" if "-i NODE," in c else "") for c in raw)
    placeholders = sorted(set(_PLACEHOLDER.findall(shown)))
    if placeholders:
        plural = "s" if len(placeholders) > 1 else ""
        lines += [""] + _paragraph("Replace %s with your own value%s." % (", ".join(placeholders), plural), "      ", "      ")
    if op.get("cql"):
        lines += [""] + _paragraph("With authentication on: cassandra_cql_username and cassandra_cql_password (in a"
                                   " vaulted file of the cluster's group_vars).", "      ", "      ")
    if op.get("guide"):
        lines += [""] + _paragraph('More: "%s" in the collection\'s %s.' % (op["guide"], GUIDE), "      ", "        ")
    paragraphs = header_paragraphs(header)
    if paragraphs:
        lines += _rule("What it does (playbooks/%s.yml)" % op["name"], heavy=False)
        for text in paragraphs:
            # an option and its value on one line
            lines += _paragraph(re.sub(r"(?<!\S)(-e|--check) (?=\S)", "\\1" + NBSP, text), "  ", "  ") + [""]
    return _lines(lines, flow, cut)


def header_paragraphs(header):
    """The paragraphs of the comment that starts a playbook, each on one line: what it does, then the ones after
    its command (shown filled in above) but where its options are (shown above too)."""
    paragraphs, current = [], []
    for line in str(header or "").splitlines():
        if line.strip() in ("---", "") and not paragraphs and not current:
            continue
        if not line.startswith("#"):
            break
        text = line[1:]
        if not text.strip():
            paragraphs, current = paragraphs + ([" ".join(current)] if current else []), []
        elif not text.startswith("   "):  # a command, indented: not shown
            current.append(text.strip())
    paragraphs += [" ".join(current)] if current else []
    return [p for p in paragraphs if not p.startswith("Options and defaults:")]


def cassandra_help_runbook(model, runbook_dir=""):
    first = ((model or {}).get("sources") or [""])[0]
    if not runbook_dir and not os.path.exists(first):
        raise AnsibleFilterError("help_write: the inventory %s is not a file or a dir to write RUNBOOK.md next to"
                                 % (first or "(none)"))
    return os.path.join(runbook_dir or _inventory_dir(model), "RUNBOOK.md")


class FilterModule(object):
    def filters(self):
        return {"cassandra_help": cassandra_help, "cassandra_help_runbook": cassandra_help_runbook}
