from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The help playbook's text, from a model as the lookup cassandra_inventory
# returns it: a neutral inventory (one cluster, one datacenter, three racks,
# five nodes, three seeds, one node marked absent).

import copy
import glob
import os
import re

import pytest
import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_help import (
    OPERATIONS, cassandra_help, cassandra_help_runbook)
from ansible_collections.community.cassandra.plugins.module_utils.cassandra_output import FLOW, NBSP, laid_out

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
CWD = "/work"


def node(name, address, rack, **more):
    vars_ = {"ansible_host": address, "cassandra_rack": rack, "cassandra_dc": "dc1",
             "cassandra_cluster_name": "Orders", "cassandra_version": "41x", "cassandra_package_version": "4.1.10",
             "cassandra_java_version": "11", "cassandra_num_tokens": "16",
             "cassandra_endpoint_snitch": "GossipingPropertyFileSnitch",
             "cassandra_authenticator": "PasswordAuthenticator", "cassandra_cql_username": True,
             "cassandra_seeds": ["192.0.2.11", "192.0.2.13", "192.0.2.15"]}
    vars_.update(more)
    return {"name": name, "vars": vars_, "names": sorted(k for k in vars_ if k.startswith("cassandra_"))}


MODEL = {
    "sources": ["/work/inventories/orders/hosts.yml"], "vault_skipped": [], "auto": "orders", "options": [],
    "imported": [],
    "clusters": [{"name": "orders", "hosts": [
        node("node1", "192.0.2.11", "rack1"), node("node2", "192.0.2.12", "rack1"),
        node("node3", "192.0.2.13", "rack2"), node("node4", "192.0.2.14", "rack2", cassandra_node_state="absent"),
        node("node5", "192.0.2.15", "rack3")]}]}
PLAYBOOKS = [op["name"] for op in OPERATIONS]
# the cluster as import_cluster writes it: inventories/orders.yml
IMPORTED = {"sources": ["/work/inventories/orders.yml"], "imported": ["orders"]}

# $PLAY, $C. and $REP stand for the start of the commands, the collection and add_datacenter's replication
# (lines kept under 160)
GOLDEN = u"""\
Cassandra help for the inventory inventories/orders/hosts.yml (read from the inventory only: no node
  contacted)

Run the commands from the directory help was run from (the one with ansible.cfg, if any). Each
operation shows its plan first; --check runs it without changing anything. One operation in detail:
-e help_topic=<operation>. Placeholders such as NEW_NODE or NODE: your own values.

Options of every operation that changes something:
        -e cassandra_hosts=<group>            the cluster to run on (default: orders)
        -e cassandra_operation_confirm=false  skips the question (of the operations that ask one),
                                              for runs without a terminal (default: true)
        --check                               shows the plan, changing nothing

==== The cluster as the inventory describes it =====================================================

Cluster 'Orders' (inventory group orders): 4 nodes, 1 more marked absent
  Cassandra: series 41x, package 4.1.10, installed from repository (role default)
  Java: 11, package
  Snitch: GossipingPropertyFileSnitch, num_tokens: 16, authenticator: PasswordAuthenticator
  Seeds  dc1  node1 (rack1), node3 (rack2), node5 (rack3)  ok

  dc1: 5 nodes (1 marked absent), 3 racks
    rack1: node1 192.0.2.11 (seed), node2 192.0.2.12
    rack2: node3 192.0.2.13 (seed), node4 192.0.2.14 (absent)
    rack3: node5 192.0.2.15 (seed)

==== Read-only (change nothing) ====================================================================

  ● help               This overview, from the inventory alone (no node contacted).
      $ $PLAY $C.help
        -e help_topic=<operation>             one operation in detail: its options, an example, its
                                              documentation (default: the overview)
        -e help_write=true                    also writes RUNBOOK.md next to the inventory (changed
                                              only when its content changes; --check --diff shows
                                              the difference) (default: false)
        -e help_runbook_dir=<dir>             writes RUNBOOK.md in that existing dir instead
                                              (default: the inventory's dir)
        -e help_inventory=<path>              reads that inventory instead of the run's
                                              (default: the -i one)
      Example (one operation in detail):
      $ $PLAY $C.help -e help_topic=rolling_restart

  ● status             The ring as nodetool status shows it from one node, per datacenter; a down
                       node is shown, not an error.
      $ $PLAY $C.status
        -e cassandra_target_nodes=<nodes>     reads the ring from the first of them that answers
                                              (default: the first node that answers)
        -e cassandra_status_raw=true          also prints nodetool's own output (default: false)
      Example (from one node):
      $ $PLAY $C.status -e cassandra_target_nodes=node1

  ● health_check       Checks the cluster from every node (ring, gossip, native transport, streams,
                       schema, ports); fails on a problem, so it can be scheduled.
      $ $PLAY $C.health_check

  ● preflight          Checks the nodes against the inventory before a change: settings that must
                       match, racks for the token allocator, versions, seeds.
      $ $PLAY $C.preflight

==== Nodes =========================================================================================

  ● add_node           Adds new hosts to the running cluster: all prepared at once, then each
                       started and bootstrapped in turn. Put them in their rack's group first; one
                       in cassandra_seeds joins as a regular node, then becomes a seed.
      $ $PLAY $C.add_node -e cassandra_target_nodes=NEW_NODE
        -e cassandra_target_nodes=NEW_NODE    the hosts to add (comma-separated), already in the
                                              inventory (required)
        -e cassandra_add_node_reset=false     refuses a new node that has data instead of emptying
                                              it (only when down, in no ring, of this cluster or
                                              'Test Cluster', without user keyspaces but a failed
                                              bootstrap's); true also empties a former member of
                                              this cluster (the writes only it holds are lost)
                                              (default: auto)
        -e cassandra_add_node_cleanup=sequential
                                              runs the cleanup afterwards: sequential, rack, dc or
                                              all; none prints its command (default: none)
        -e cassandra_token_auto=bisect        one token per node: how the new nodes get their
                                              tokens, bisect, balanced or true (shows both, asks)
                                              (default: false)
        -e cassandra_clock_check=false        starts the nodes without checking their clocks first
                                              (kept in sync by other means) (default: true)
      Example (then the cleanup, one node at a time):
      $ $PLAY $C.add_node -e cassandra_target_nodes=NEW_NODE -e cassandra_add_node_cleanup=sequential

  ● topology           Makes the ring match the inventory: adds the hosts of the cluster's group not
                       in the ring, applies cassandra_seeds, removes the hosts marked
                       cassandra_node_state: absent; one node at a time, --check shows the plan and
                       its warnings.
      $ $PLAY $C.topology
        -e cassandra_add_node_reset=false     refuses a host to add that has data instead of
                                              emptying it; true also empties a former member of this
                                              cluster (default: auto)
        -e cassandra_token_auto=bisect        one token per node: bisect or balanced for the hosts
                                              to add (default: false)
        -e cassandra_decommission_force=true  goes on when a datacenter would keep fewer nodes than
                                              replicas (nodetool decommission --force)
                                              (default: false)
      Example (the plan only):
      $ $PLAY $C.topology --check

  ● decommission_node  Removes nodes from the running cluster, one at a time, their data streamed to
                       the others; a node no longer in cassandra_seeds is first dropped from the
                       other nodes' seed lists; refuses a datacenter left with nodes but no seed, or
                       fewer nodes than replicas.
      $ $PLAY $C.decommission_node -e cassandra_target_nodes=node4
        -e cassandra_target_nodes=<nodes>     the nodes to remove (comma-separated) (required)
        -e cassandra_decommission_force=true  goes on when a datacenter would keep fewer nodes than
                                              replicas (nodetool decommission --force)
                                              (default: false)
        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already
                                              done (default: false)
      Example (changing nothing):
      $ $PLAY $C.decommission_node -e cassandra_target_nodes=node4 --check

  ● replace_node       Replaces a dead node by a blank host, which takes over its tokens and data.
                       In the inventory, the new host in, the dead one out.
      $ $PLAY $C.replace_node -e cassandra_target_nodes=NEW_NODE -e cassandra_replace_address=DEAD_NODE_ADDRESS
        -e cassandra_target_nodes=NEW_NODE    the new host (required)
        -e cassandra_replace_address=DEAD_NODE_ADDRESS
                                              the address of the dead node (required)
        -e cassandra_replace_node_reset=true  first empties the new host (a node replacing itself)
                                              (default: false)
      Example (a node replacing itself):
      $ $PLAY $C.replace_node -e cassandra_target_nodes=NEW_NODE -e cassandra_replace_address=DEAD_NODE_ADDRESS -e cassandra_replace_node_reset=true

  ● remove_dead_node   Last resort for a dead node that will not be replaced: removenode (or
                       assassinate). Take it out of the inventory (or mark it absent) first.
      $ $PLAY $C.remove_dead_node -e cassandra_target_nodes=DEAD_NODE_ADDRESS
        -e cassandra_target_nodes=DEAD_NODE_ADDRESS
                                              the dead node: its address (or its host ID, or its
                                              inventory name when marked absent) (required)
        -e cassandra_dead_node_method=removenode_force
                                              removenode, removenode_force (finishes a stuck
                                              removenode) or assassinate (default: removenode)
        -e cassandra_dead_node_new_removal=true
                                              starts a removenode when one seems to be running
                                              (default: false)
      Example (changing nothing):
      $ $PLAY $C.remove_dead_node -e cassandra_target_nodes=DEAD_NODE_ADDRESS --check

  ● reset_node         Empties nodes that are not members of the ring (started once by mistake, a
                       failed bootstrap) for a fresh start.
      $ $PLAY $C.reset_node -e cassandra_target_nodes=NODE
        -e cassandra_target_nodes=NODE        the nodes to empty (comma-separated) (required)
      Example (changing nothing):
      $ $PLAY $C.reset_node -e cassandra_target_nodes=NODE --check

  ● move_node          One token per node: moves nodes to new tokens, one at a time (by default the
                       fewest moves that even out each datacenter). Not for this cluster (num_tokens
                       16).
      $ $PLAY $C.move_node
        -e '{"cassandra_move_tokens": {"NODE": "TOKEN"}}'
                                              only those nodes move, to those tokens (default: {})
        -e cassandra_move_cleanup=sequential  runs the cleanup afterwards: sequential, rack, dc or
                                              all; none prints its commands (default: none)
        -e cassandra_move_force_disk=true     goes on when a node would keep less free disk than
                                              cassandra_move_min_free_percent (20) (default: false)
      Example (the plan only):
      $ $PLAY $C.move_node --check

==== Cluster =======================================================================================

  ● create_cluster     Builds the cluster from blank hosts: prepared in parallel, then started one
                       at a time, seeds first. Starts nothing on a running cluster.
      $ $PLAY $C.create_cluster
        -e cassandra_accept_default_identity=true
                                              goes on with identity settings left to the defaults
                                              (default: false)
        -e cassandra_create_cluster_reset=true
                                              rebuilds a running cluster, ALL ITS DATA LOST
                                              (default: false)
        -e '{"cassandra_create_cluster_reset_confirm": "<cluster name>"}'
                                              the reset without a terminal (with
                                              cassandra_operation_confirm=false): the cluster's
                                              name, the answer to its question
        -e cassandra_clock_check=false        starts the nodes without checking their clocks first
                                              (kept in sync by other means) (default: true)
      Example (the plan only):
      $ $PLAY $C.create_cluster --check

  ● rolling_restart    Drains and restarts the nodes one at a time, the cluster checked before and
                       after each one.
      $ $PLAY $C.rolling_restart
        -e cassandra_rolling_mode=rack        the nodes of a rack together, rack by rack
                                              (default: node)
        -e cassandra_rack_force=true          rack mode: goes on although keyspaces would lose more
                                              than one replica (default: false)
        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already
                                              done (default: false)
      Example (a rack at a time):
      $ $PLAY $C.rolling_restart -e cassandra_rolling_mode=rack

  ● rolling_reboot     Same as rolling_restart, rebooting the hosts (OS patching).
      $ $PLAY $C.rolling_reboot
        -e cassandra_reboot_timeout=3600      seconds to wait for a host to come back
                                              (default: 1800)
        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already
                                              done (default: false)
      Example (an interrupted run resumed):
      $ $PLAY $C.rolling_reboot -e cassandra_rolling_resume=true

  ● stop_rack          Stops every node of one rack at once (maintenance), when the replication
                       allows losing that rack.
      $ $PLAY $C.stop_rack -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3
        -e cassandra_target_dc=<dc>           the rack's datacenter (required)
        -e cassandra_target_rack=<rack>       the rack to stop (required)
        -e cassandra_rack_force=true          goes on although keyspaces would lose more than one
                                              replica (default: false)
      Example (changing nothing):
      $ $PLAY $C.stop_rack -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3 --check

  ● start_rack         Starts the nodes of a rack stop_rack stopped, then checks the whole cluster.
      $ $PLAY $C.start_rack -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3
        -e cassandra_target_dc=<dc>           the rack's datacenter (required)
        -e cassandra_target_rack=<rack>       the rack to start (required)
        -e cassandra_accept_default_identity=true
                                              goes on when no node of another rack answers and the
                                              cluster name is the default 'Test Cluster'
                                              (default: false)
      Example (changing nothing):
      $ $PLAY $C.start_rack -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3 --check

  ● apply_config       Applies the inventory's config: shows the settings that differ (the
                       inventory's value, then each other value with its nodes), asks once, then
                       writes the nodes that need it, one at a time, restarting only those that need
                       it.
      $ $PLAY $C.apply_config
        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already
                                              done (default: false)
      Example (the settings that differ, changing nothing):
      $ $PLAY $C.apply_config --check

  ● change_seeds       Applies a new cassandra_seeds list to every node, live (no restart);
                       topology, add_node and decommission_node apply it too when nodes come and go.
      $ $PLAY $C.change_seeds
      Example (changing nothing):
      $ $PLAY $C.change_seeds --check

  ● update_java        Moves the cluster to the Java in cassandra_java_version, one node at a time.
      $ $PLAY $C.update_java
        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already
                                              done (default: false)
      Example (an interrupted run resumed):
      $ $PLAY $C.update_java -e cassandra_rolling_resume=true

  ● sync_clocks        Shows each node's clock as its time sync daemon reports it (synchronised,
                       source, offset) with a verdict, then brings time sync up where it is missing;
                       a running node whose clock needs a step gets a restart window, one node at a
                       time.
      $ $PLAY $C.sync_clocks
        -e cassandra_clock_max_offset_ms=50   a node is in sync within this many milliseconds of its
                                              source (offset plus the daemon's error bound)
                                              (default: 50)
        -e cassandra_clock_step_ms=400        a correction above it steps the clock: on a running
                                              node, in a restart window (default: 400)
        -e cassandra_clock_sync_wait=60       seconds to wait for a time sync to synchronise
                                              (default: 60)
        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already
                                              done (default: false)
      Example (the clocks, changing nothing):
      $ $PLAY $C.sync_clocks --check

  ● upgrade            Upgrades the cluster to the version in the inventory, one phase per run:
                       preflight, prepare, canary, rolling, sstables, cleanup.
      $ $PLAY $C.upgrade -e cassandra_upgrade_phase=preflight
        -e cassandra_upgrade_phase=preflight  preflight, prepare, canary, rolling, sstables or
                                              cleanup, in that order (required)
        -e cassandra_target_nodes=<node>      canary phase: the node it upgrades (default: the first
                                              non-seed by datacenter, rack and name (else the first
                                              node))
        -e cassandra_rolling_resume=true      sstables phase: resumes an interrupted run, skipping
                                              the nodes already done (default: false)
      Example (the next phase):
      $ $PLAY $C.upgrade -e cassandra_upgrade_phase=prepare

  ● cleanup            Runs nodetool cleanup (the data a node no longer owns, after nodes were
                       added), the cluster checked before each batch.
      $ $PLAY $C.cleanup
        -e cassandra_cleanup_mode=rack        sequential (one node at a time), rack, dc or all
                                              (every node at once) (default: sequential)
        -e cassandra_cleanup_jobs=4           compaction threads per node (0: all of them)
                                              (default: 2)
        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already
                                              done (default: false)
      Example (a rack at a time):
      $ $PLAY $C.cleanup -e cassandra_cleanup_mode=rack

  ● add_datacenter     Adds a datacenter: its nodes join without streaming, the keyspaces get
                       replicas there, then each node rebuilds from another datacenter.
      $ $PLAY $C.add_datacenter -e cassandra_target_nodes=NEW_DC_GROUP -e cassandra_rebuild_source_dc=dc1 $REP
        -e cassandra_target_nodes=NEW_DC_GROUP
                                              the new datacenter's nodes (a group or a
                                              comma-separated list) (required)
        -e cassandra_rebuild_source_dc=<dc>   the datacenter they stream from (required)
        $REP
                                              the replicas of each keyspace in the new datacenter
                                              (required)
      Example (changing nothing):
      $ $PLAY $C.add_datacenter -e cassandra_target_nodes=NEW_DC_GROUP -e cassandra_rebuild_source_dc=dc1 $REP --check

  ● remove_datacenter  Removes a datacenter: the keyspaces stop keeping replicas there, then its
                       nodes leave one at a time. Move its clients first.
      $ $PLAY $C.remove_datacenter -e cassandra_target_dc=DC_TO_REMOVE
        -e cassandra_target_dc=DC_TO_REMOVE   the datacenter to remove (required)
      Example (changing nothing):
      $ $PLAY $C.remove_datacenter -e cassandra_target_dc=DC_TO_REMOVE --check

==== Takeover ======================================================================================

  ● import_cluster     Reads a running cluster into an inventory, changing nothing on the nodes. A
                       new cluster: give one of its nodes (-i <node>,), the others are found in the
                       ring. A re-import reads the cluster from the inventory (its logins too),
                       keeps the files it did not write, --check --diff shows its changes first.
      $ ansible-playbook -i 192.0.2.11, $C.import_cluster -e import_cluster_dir=NEW_DIR
        -e import_cluster_dir=<dir>           the inventory dir, every cluster's
                                              (default: inventories)
        -e import_cluster_report_dir=<dir>    where to write report.txt
                                              (default: reports/<cluster group> next to
                                              import_cluster_dir)
        -e import_cluster_force=true          a re-import, over this cluster's files
                                              (default: false)
        -e import_cluster_adopt=true          takes over the files of an earlier import that do not
                                              say whose they are (each replaced one kept as
                                              <file>.<date>~) (default: false)
        -e import_cluster_allow_unread=true   accepts a ring node it could not read (else the import
                                              fails) (default: false)
        -e import_cluster_keep_hand_edits=true
                                              leaves the config files with hand edits as they are on
                                              their node (default: false)

==== Advice ========================================================================================

- Marked cassandra_node_state: absent: node4. topology --check shows the plan to remove them, then
  topology without --check does it:
    $ $PLAY $C.topology --check

- dc1: racks of different sizes (1, 1, 2 nodes): the data is not shared evenly; add or remove nodes
  rack by rack.""".replace("$PLAY", "ansible-playbook -i inventories/orders/hosts.yml").replace("$C.", "community.cassandra.").replace(
    "$REP", "-e '{cassandra_datacenter_replication: {KEYSPACE: 3}}'")


def model(**changes):
    out = copy.deepcopy(MODEL)
    for name, value in changes.items():
        if name == "hosts":
            out["clusters"][0]["hosts"] = value
        else:
            out[name] = value
    return out


def commands(text):
    """The command lines of the text."""
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith("$ ")) + "\n"


def advice(text):
    return re.split(r"\n==== Advice =+\n", text, 1)[1]


def test_golden():
    assert cassandra_help(MODEL, PLAYBOOKS, cwd=CWD) == GOLDEN


def test_every_playbook_is_listed_once():
    # the one list to keep up to date: a playbook added or removed fails here
    present = sorted(os.path.splitext(name)[0] for name in os.listdir(os.path.join(TOP, "playbooks"))
                     if name.endswith(".yml"))
    listed = [op["name"] for op in OPERATIONS]
    assert sorted(listed) == present
    assert len(set(listed)) == len(listed)


def test_every_operation_has_a_theme_and_a_summary():
    for op in OPERATIONS:
        assert op["theme"] in ("read-only", "nodes", "cluster", "takeover"), op["name"]
        assert op["summary"].endswith("."), op["name"]


def read(*patterns):
    text = ""
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(TOP, pattern))):
            with open(path, encoding="utf-8") as f:
                text += f.read() + "\n"
    return text


def playbook(name):
    """The playbook's text, with the role task files it includes (one level)."""
    text = read("playbooks/%s.yml" % name)
    files = set(re.findall(r"tasks_from: (\S+)", text))
    files.update("action_%s.yml" % a for a in re.findall(r"cassandra_service_node_action: (\w+)", text))  # node_operation
    return text + read(*["roles/*/tasks/%s" % f for f in sorted(files)])


def code(text):
    """The text without its comments."""
    return "\n".join(line.split(" #")[0] for line in text.splitlines() if not line.lstrip().startswith("#"))


def reads(name, variable):
    """Whether the playbook's code (or the role tasks it includes) reads the variable."""
    return re.search(r"\b%s\b" % variable, code(playbook(name))) is not None


def documented(name):
    """The variables the comment that starts the playbook gives with -e."""
    header = []
    with open(os.path.join(TOP, "playbooks", name + ".yml"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                header.append(line)
            elif line.strip() not in ("---", ""):
                break
    return set(re.findall(r"-e +'?\{?\"?(\w+)", "".join(header)))


def test_operation_variables_are_the_playbooks_own():
    # each variable an option or an example sets is read by the playbook or the role tasks it includes (its
    # code, not a comment): help lists no option that does not exist
    for op in OPERATIONS:
        args = [o[0] for o in op.get("options") or []] + list((op.get("example") or ("", []))[1])
        for arg in args:
            if arg != "--check":
                name = re.search(r"(?:cassandra|help|import_cluster)_\w+", arg).group(0)
                assert reads(op["name"], name), (op["name"], name)


def test_every_documented_option_is_in_help():
    # each -e option the comment starting the playbook gives, and its code reads, is listed with the operation
    # (the options of every operation once, at the top): help lacks none
    common = ("cassandra_hosts", "cassandra_operation_confirm")
    checked = 0
    for op in OPERATIONS:
        listed = set(re.search(r"\w+", o[0][3:]).group(0) for o in op.get("options") or [] if o[0].startswith("-e "))
        for name in sorted(documented(op["name"])):
            if name not in common and reads(op["name"], name):
                assert name in listed, (op["name"], name)
                checked += 1
    assert checked > 25


def test_option_defaults_are_the_code_s():
    # a default of one word is the playbook's own (name | default(value), or name: value in its vars), or
    # the role default
    defaults = {}
    for path in glob.glob(os.path.join(TOP, "roles", "*", "defaults", "main.yml")):
        with open(path, encoding="utf-8") as f:
            defaults.update(yaml.safe_load(f) or {})
    checked = 0
    for op in OPERATIONS:
        code = "\n".join(line for line in playbook(op["name"]).splitlines()
                         if not line.lstrip().startswith("#"))
        for arg, _text, default in op.get("options") or []:
            if default is None or " " in default:
                continue
            name = re.search(r"\w+", arg[3:]).group(0)
            # (default(''): a check whether it is given)
            found = set(re.findall(r"\b%s \| default\(['\"]?([^'\")]*)" % name, code)) - {""}
            found.update(re.findall(r"^\s+%s: (\S+)\s*(?:#.*)?$" % name, code, re.M))
            if name in defaults:
                found.add(str(defaults[name]).lower() if isinstance(defaults[name], bool) else str(defaults[name]))
            assert found == {default}, (op["name"], name, default, found)
            checked += 1
    assert checked > 30


def test_examples_set_their_operation_s_options():
    for op in OPERATIONS:
        names = [re.search(r"\w+", o[0][3:]).group(0) for o in op.get("options") or []]
        for arg in (op.get("example") or ("", []))[1]:
            assert arg == "--check" or re.search(r"\w+", arg[3:]).group(0) in names, (op["name"], arg)


def test_markdown_has_the_same_commands():
    text = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD)
    runbook = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD, markdown=True)
    commands = [line.split("$ ", 1)[1] for line in text.splitlines() if line.lstrip().startswith("$ ")]
    assert runbook.startswith("# RUNBOOK\n")
    assert runbook.endswith(".\n") and not runbook.endswith("\n\n")
    for command in commands:
        assert "\n" + command + "\n" in runbook or "\n  " + command + "\n" in runbook, command
    assert "## 3. Advice" in runbook


def test_markdown_operation_layout():
    # the command, the options one per line with their default, an example: in code blocks
    runbook = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD, markdown=True)
    play = "ansible-playbook -i inventories/orders/hosts.yml community.cassandra."
    section = runbook.split("**rolling_restart**", 1)[1].split("**rolling_reboot**", 1)[0]
    assert section == (
        " - Drains and restarts the nodes one at a time, the cluster checked before and after each one.\n\n"
        "```sh\n" + play + "rolling_restart\n```\n\n"
        "Options:\n\n"
        "- `-e cassandra_rolling_mode=rack`: the nodes of a rack together, rack by rack (default: node)\n"
        "- `-e cassandra_rack_force=true`: rack mode: goes on although keyspaces would lose more than one replica"
        " (default: false)\n"
        "- `-e cassandra_rolling_resume=true`: resumes an interrupted run, skipping the nodes already done"
        " (default: false)\n\n"
        "Example (a rack at a time):\n\n"
        "```sh\n" + play + "rolling_restart -e cassandra_rolling_mode=rack\n```\n\n")
    # the options of every operation once, at the top
    assert "- `-e cassandra_operation_confirm=false`: skips the question (of the operations that ask one)" in runbook.split("## 1.", 1)[0]


def test_every_operation_and_its_example_in_the_runbook():
    runbook = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD, markdown=True)
    sections = dict(re.findall(r"\n\*\*(\w+)\*\* - (.*?)(?=\n\*\*\w+\*\* - |\n## )", runbook, re.S))
    assert sorted(sections) == sorted(op["name"] for op in OPERATIONS)
    for op in OPERATIONS:
        example = "\nExample (%s):\n" % op["example"][0] if op.get("example") else "\nExample ("
        assert (example in sections[op["name"]]) == bool(op.get("example")), op["name"]


def test_lines_within_the_width():
    # every line of the text fits in 100 columns but the commands (copied whole) and the playbooks' comments;
    # no layout mark left in it
    texts = [cassandra_help(MODEL, PLAYBOOKS, cwd=CWD)]
    texts += [cassandra_help(MODEL, PLAYBOOKS, topic=op["name"], cwd=CWD) for op in OPERATIONS]
    for text in texts:
        assert FLOW not in text and NBSP not in text
        assert not [line for line in text.splitlines() if len(line) > 100 and not line.lstrip().startswith("$ ")]


def test_flow_is_the_same_text_laid_out_by_the_callback():
    # under the ops callback (flow), the lines are laid out where they are printed: at 100 columns, the same text
    for topic in [""] + [op["name"] for op in OPERATIONS]:
        header = ""
        if topic:
            with open(os.path.join(TOP, "playbooks", topic + ".yml"), encoding="utf-8") as f:
                header = f.read()
        plain = cassandra_help(MODEL, PLAYBOOKS, topic=topic, header=header, cwd=CWD)
        flowing = cassandra_help(MODEL, PLAYBOOKS, topic=topic, header=header, cwd=CWD, flow=True)
        assert FLOW in flowing
        assert "\n".join(out for line in flowing.split("\n") for out in laid_out(line, 100)[0]) == plain, topic


def test_operations_aligned_with_their_commands_options_and_example():
    # each operation: its name after a bullet, what it does in one column for all of them, its command, its options
    # one per line with their defaults, its example; a blank line between two operations
    text = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD)
    section = text.split("\n  \u25cf cleanup", 1)[1].split("\n\n", 1)[0]
    play = "ansible-playbook -i inventories/orders/hosts.yml community.cassandra.cleanup"
    assert section.splitlines() == [
        "            Runs nodetool cleanup (the data a node no longer owns, after nodes were",
        "                       added), the cluster checked before each batch.",
        "      $ " + play,
        "        -e cassandra_cleanup_mode=rack        sequential (one node at a time), rack, dc or all",
        "                                              (every node at once) (default: sequential)",
        "        -e cassandra_cleanup_jobs=4           compaction threads per node (0: all of them)",
        "                                              (default: 2)",
        "        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already",
        "                                              done (default: false)",
        "      Example (a rack at a time):",
        "      $ " + play + " -e cassandra_cleanup_mode=rack"]
    columns = set(line.index("\u25cf") for line in text.splitlines() if "\u25cf" in line)
    summaries = set(len(re.match(r"  \u25cf \w+ +", line).group(0)) for line in text.splitlines() if "\u25cf" in line)
    assert columns == {2} and summaries == {23}  # decommission_node, the longest name, and 2 spaces
    # the options' texts in one column for every operation (a longer option on a line of its own)
    options = set(len(m.group(0)) for m in re.finditer(r"(?m)^        (?:-e|--)\S*(?: \S+)?  +(?=\S)", text))
    assert options == {46}
    assert "default:\n" not in text and "<cluster\n" not in text  # never broken there


def test_no_trailing_space():
    # clean lines (the default callback strips trailing spaces under the yaml result format; RUNBOOK.md keeps them)
    texts = [cassandra_help(MODEL, PLAYBOOKS, cwd=CWD), cassandra_help(MODEL, PLAYBOOKS, cwd=CWD, markdown=True)]
    texts += [cassandra_help(MODEL, PLAYBOOKS, topic=op["name"], cwd=CWD) for op in OPERATIONS]
    for text in texts:
        assert not [line for line in text.splitlines() if line != line.rstrip()], text.splitlines()[0]


def test_example_replaces_the_needed_argument():
    text = cassandra_help(MODEL, PLAYBOOKS, topic="upgrade", cwd=CWD)
    example = text.split("Example (the next phase):\n", 1)[1].splitlines()[0]
    assert example == ("      $ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.upgrade"
                       " -e cassandra_upgrade_phase=prepare")


def test_long_option_on_its_own_line():
    lines = cassandra_help(MODEL, PLAYBOOKS, topic="remove_dead_node", cwd=CWD).splitlines()
    at = lines.index("        -e cassandra_dead_node_method=removenode_force")
    assert lines[at + 1] == " " * 46 + "removenode, removenode_force (finishes a stuck"


def test_common_options_only_for_the_operations_that_change_something():
    for op in OPERATIONS:
        text = cassandra_help(MODEL, PLAYBOOKS, topic=op["name"], cwd=CWD)
        assert ("-e cassandra_hosts=<group>" in text) == (op["theme"] in ("nodes", "cluster")), op["name"]
        # cassandra_operation_confirm: only for the operations that ask (their screen or question)
        asks = bool(re.search(r"tasks_from: (confirm|screen)\.yml|cassandra_operation_confirm", playbook(op["name"])))
        assert ("-e cassandra_operation_confirm=false" in text) == (asks and op["theme"] in ("nodes", "cluster")), \
            op["name"]
    text = cassandra_help(MODEL, PLAYBOOKS, topic="cleanup", cwd=CWD)
    assert re.search(r"\n        -e cassandra_hosts=<group> +the cluster to run on \(default: orders\)\n", text)
    # the commands give the group: when the inventory has several, or CASSANDRA_CLUSTER named another
    text = cassandra_help(model(auto=""), PLAYBOOKS, topic="cleanup", cwd=CWD)
    assert re.search(r"\n        -e cassandra_hosts=<group> +the cluster to run on \(default: the one in the\n +commands\)\n",
                     text)
    assert "community.cassandra.cleanup -e cassandra_hosts=orders" in text


def test_topic():
    with open(os.path.join(TOP, "playbooks", "decommission_node.yml"), encoding="utf-8") as f:
        header = f.read()
    text = cassandra_help(MODEL, PLAYBOOKS, topic="decommission_node", header=header, cwd=CWD)
    lines = text.splitlines()
    assert lines[:3] == ["==== Nodes " + "=" * 89, "",
                         "  \u25cf decommission_node  Removes nodes from the running cluster, one at a time, their data"
                         " streamed to"]
    assert ("      $ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.decommission_node"
            " -e cassandra_target_nodes=node4") in lines
    assert "  Removes nodes from a running cluster, one at a time: each one streams its" in lines
    assert "- name: Preflight" not in text  # the comment only
    # the command, the options one per line with their default, an example, in that order
    options = [i for i, line in enumerate(lines) if line.startswith("        -e ")][0]
    assert lines[options - 1].startswith("      $ ansible-playbook ")  # under its command
    assert lines[options:options + 12] == [
        "        -e cassandra_target_nodes=<nodes>     the nodes to remove (comma-separated) (required)",
        "        -e cassandra_decommission_force=true  goes on when a datacenter would keep fewer nodes than",
        "                                              replicas (nodetool decommission --force)",
        "                                              (default: false)",
        "        -e cassandra_rolling_resume=true      resumes an interrupted run, skipping the nodes already",
        "                                              done (default: false)",
        "        -e cassandra_hosts=<group>            the cluster to run on (default: orders)",
        "        -e cassandra_operation_confirm=false  skips the question (of the operations that ask one),",
        "                                              for runs without a terminal (default: true)",
        "        --check                               shows the plan, changing nothing",
        "      Example (changing nothing):",
        "      $ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.decommission_node"
        " -e cassandra_target_nodes=node4 --check"]
    assert lines.index("---- What it does and checks (playbooks/decommission_node.yml) " + "-" * 37) > options
    assert ("      $ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.decommission_node"
            " -e cassandra_target_nodes=node4 --check") in lines
    assert "cassandra_cql_username and cassandra_cql_password" in text


def test_topic_placeholders_named():
    text = cassandra_help(MODEL, PLAYBOOKS, topic="replace_node", cwd=CWD)
    assert "Replace DEAD_NODE_ADDRESS, NEW_NODE with your own values." in text


def test_unknown_topic():
    with pytest.raises(AnsibleFilterError, match="no operation decomission_node"):
        cassandra_help(MODEL, PLAYBOOKS, topic="decomission_node", cwd=CWD)


def test_no_cluster():
    with pytest.raises(AnsibleFilterError, match="no cluster group"):
        cassandra_help(model(clusters=[]), PLAYBOOKS, cwd=CWD)


def test_password_authenticator_without_cql_user():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        del host["vars"]["cassandra_cql_username"]
    text = advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD))
    assert ("- Authentication is on (PasswordAuthenticator) but cassandra_cql_username is not set: topology (to\n"
            "  remove nodes), decommission_node, remove_dead_node, rolling_restart (rack mode), stop_rack,\n"
            "  add_datacenter, remove_datacenter read the replication over CQL") in text


def test_close_variable_name():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[1]["names"].append("cassandra_heap_sise")
    text = advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD))
    assert "- cassandra_heap_sise is set (node2) but no role or playbook reads it: did you mean\n  cassandra_heap_size?" in text


def test_seeds_layout():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"]["cassandra_seeds"] = "192.0.2.11:7000,192.0.2.12:7000,192.0.2.99"
    text = advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD))
    assert "- Seeds that are no node of the inventory: 192.0.2.99." in text
    assert ("- dc1: 2 seeds on rack1, none on rack2, rack3: put them on different racks (the rule: 2 or 3 seeds\n"
            "  per datacenter, on different racks when there are several): set cassandra_seeds, then change_seeds\n"
            "  applies it live.") in text
    full = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert ("  WARNING  Seeds  dc1  node1 (rack1), node2 (rack1)  2 seeds on rack1, none on rack2, rack3: put\n"
            "    them on different racks\n") in full


def test_absent_seed():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[3]["vars"]["cassandra_seeds"] = hosts[3]["vars"]["cassandra_seeds"] + ["192.0.2.14"]
    text = advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD))
    assert "- Seed and marked absent: node4. Take it out of cassandra_seeds in the inventory: topology then\n" \
           "  drops it from the seed lists before it leaves." in text


def test_racks_against_the_allocator():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[4]["vars"]["cassandra_rack"] = "rack2"
    text = advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD))
    assert ("- dc1 has 2 racks with allocate_tokens_for_local_replication_factor 3: the token allocator needs one\n"
            "  rack or at least 3 (preflight refuses it).") in text
    for host in hosts:
        host["vars"]["cassandra_allocate_tokens_for_local_replication_factor"] = "2"
    assert "token allocator" not in advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD))


def test_mixed_versions():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[0]["vars"].update(cassandra_package_version="4.1.9", cassandra_java_version="17")
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert "package MIXED: 4.1.10 (node2, node3, node5); 4.1.9 (node1)" in text
    assert "  Java: MIXED: 11, package (node2, node3, node5); 17, package (node1)" in text
    assert "- Mixed package versions across the nodes: 4.1.10 (node2, node3, node5); 4.1.9 (node1)." in advice(text)
    assert "- Mixed Java across the nodes" in advice(text)


def test_role_defaults_shown():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        for key in ("cassandra_version", "cassandra_java_version", "cassandra_package_version"):
            del host["vars"][key]
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert "  Cassandra: series 50x (role default), package latest of the series, installed from" in text
    assert "  Java: 17 (role default), package" in text


def test_two_clusters_name_their_group():
    two = copy.deepcopy(MODEL)
    other = copy.deepcopy(two["clusters"][0])
    other["name"] = "billing"
    two["clusters"].append(other)
    two["auto"] = ""
    text = cassandra_help(two, PLAYBOOKS, cwd=CWD)
    assert "\n==== The clusters as the inventory describes them ====" in text
    assert "\n==== orders: Nodes ====" in text and "\n==== billing: Nodes ====" in text
    assert "community.cassandra.status -e cassandra_hosts=orders" in text
    assert "community.cassandra.status -e cassandra_hosts=billing" in text
    assert "- billing: Marked cassandra_node_state: absent: node4." in text


def test_vault_skipped():
    text = cassandra_help(model(vault_skipped=["/work/inventories/orders/group_vars/orders/secrets.yml"],
                                options=["--ask-vault-pass"]), PLAYBOOKS, cwd=CWD)
    assert "- Vault-encrypted files not read (help decrypts nothing): group_vars/orders/secrets.yml." in text
    assert "$ ansible-playbook -i inventories/orders/hosts.yml --ask-vault-pass community.cassandra.status" in text


def test_import_command_keeps_the_connection_and_jmx():
    # a cluster the import did not write here: a new import, read from a node (-i <address>,), which reads no
    # inventory: its connection and JMX login given on the command line
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[0]["vars"].update(ansible_user="admin", cassandra_jmx_username="monitor",
                            cassandra_jmx_password_file="/etc/cassandra/jmxremote.password")
    text = cassandra_help(model(hosts=hosts, options=["--ask-vault-pass"]), PLAYBOOKS, cwd=CWD)
    assert ("$ ansible-playbook -i 192.0.2.11, -u admin community.cassandra.import_cluster"
            " -e import_cluster_dir=NEW_DIR"
            " -e cassandra_jmx_username=monitor"
            " -e cassandra_jmx_password_file=/etc/cassandra/jmxremote.password") in text


def test_reimport_reads_the_inventory():
    # a cluster the import wrote: re-imported from the inventory, its connection variables and logins (vaulted
    # too) with it: no -i <node>, no JMX login on the command line, the run's options kept (the vault's)
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[0]["vars"].update(ansible_user="admin", cassandra_jmx_username="(vaulted)", cassandra_jmx_password=True)
    hosts[0]["names"] += ["cassandra_jmx_username", "cassandra_jmx_password"]
    text = cassandra_help(model(hosts=hosts, options=["--ask-vault-pass"], all_hosts=[h["name"] for h in hosts], **IMPORTED),
                          PLAYBOOKS, cwd=CWD)
    assert ("$ ansible-playbook -i inventories/orders.yml --ask-vault-pass community.cassandra.import_cluster"
            " -e import_cluster_force=true\n") in text
    assert "-i 192.0.2.11," not in text and "JMX_USER" not in text and "cassandra_jmx" not in commands(text)


def test_absent_nodes_with_topology():
    text = advice(cassandra_help(MODEL, PLAYBOOKS + ["topology"], cwd=CWD))
    assert "topology --check shows the plan" in text
    assert "$ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.topology --check" in text


def test_single_token_cluster():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"]["cassandra_num_tokens"] = "1"
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert "Not for this cluster" not in text
    assert "token allocator" not in advice(text)


def test_inventory_outside_the_current_dir():
    text = cassandra_help(MODEL, PLAYBOOKS, cwd="/elsewhere")
    assert "$ ansible-playbook -i /work/inventories/orders/hosts.yml community.cassandra.status" in text


def test_runbook_path(tmp_path):
    (tmp_path / "hosts.yml").write_text("")
    assert cassandra_help_runbook({"sources": [str(tmp_path / "hosts.yml")]}) == str(tmp_path / "RUNBOOK.md")
    assert cassandra_help_runbook({"sources": [str(tmp_path)]}) == str(tmp_path / "RUNBOOK.md")
    with pytest.raises(AnsibleFilterError, match="not a file or a dir"):
        cassandra_help_runbook({"sources": ["192.0.2.11,"]})


def test_password_authenticator_planning_operations():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        del host["vars"]["cassandra_cql_username"]
    text = " ".join(advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)).split())
    assert "without them, add_node and move_node plan as if every keyspace had replicas everywhere." in text


def test_reimport_only_into_an_inventory_the_import_wrote():
    for changed in (model(), model(imported=["billing"])):
        text = cassandra_help(changed, PLAYBOOKS, cwd=CWD)
        assert "community.cassandra.import_cluster -e import_cluster_dir=NEW_DIR\n" in text
        assert "import_cluster_force" not in commands(text)


def test_reimport_into_the_inventory_dir(tmp_path):
    """A cluster the import wrote (its <cluster>.yml): its re-import goes there again, import_cluster_dir left out
    for the default inventories, read from the inventory; the cluster named unless the import takes it alone (the
    inventory holds its hosts only, or CASSANDRA_CLUSTER names it)."""
    (tmp_path / "inventories").mkdir()
    sources = [str(tmp_path / "inventories")]
    billing = {"name": "billing", "hosts": [node("node9", "192.0.2.19", "rack1", cassandra_cluster_name="Billing")]}
    everyone = ["node%d" % i for i in range(1, 6)] + ["node9"]
    shared = model(sources=sources, imported=["orders"], clusters=MODEL["clusters"] + [billing], auto="",
                   all_hosts=everyone)
    text = cassandra_help(shared, PLAYBOOKS, cwd=str(tmp_path))
    assert ("$ ansible-playbook -i inventories community.cassandra.import_cluster -e cassandra_hosts=orders"
            " -e import_cluster_force=true\n") in text
    assert ("$ ansible-playbook -i 192.0.2.19, community.cassandra.import_cluster -e import_cluster_dir=NEW_DIR\n") in text
    # help -e cassandra_hosts=orders: that cluster only shown, the others still in the inventory: named
    text = cassandra_help(model(sources=sources, imported=["orders"], all_hosts=everyone), PLAYBOOKS, cwd=str(tmp_path))
    assert "community.cassandra.import_cluster -e cassandra_hosts=orders -e import_cluster_force=true\n" in text
    # CASSANDRA_CLUSTER names it: not on screen, in RUNBOOK.md
    named = model(sources=sources, imported=["orders"], all_hosts=everyone, auto="", env_cluster="orders")
    assert "$ ansible-playbook -i inventories community.cassandra.import_cluster -e import_cluster_force=true\n" in \
        cassandra_help(named, PLAYBOOKS, cwd=str(tmp_path))
    assert "community.cassandra.import_cluster -e cassandra_hosts=orders -e import_cluster_force=true" in \
        cassandra_help(named, PLAYBOOKS, cwd=str(tmp_path), markdown=True)
    alone = model(sources=sources, imported=["orders"], all_hosts=everyone[:5])
    text = cassandra_help(alone, PLAYBOOKS, cwd=str(tmp_path))
    assert "$ ansible-playbook -i inventories community.cassandra.status\n" in text  # alone there: not needed
    assert "$ ansible-playbook -i inventories community.cassandra.import_cluster -e import_cluster_force=true\n" in text
    assert "-e cassandra_hosts" not in commands(text)
    assert "-e cassandra_hosts" not in commands(cassandra_help(model(), PLAYBOOKS, cwd=CWD))
    text = cassandra_help(shared, PLAYBOOKS, cwd=str(tmp_path))  # with another cluster: needed
    assert "$ ansible-playbook -i inventories community.cassandra.status -e cassandra_hosts=orders\n" in text
    # another dir: named
    text = cassandra_help(alone, PLAYBOOKS, cwd="/elsewhere")
    assert ("community.cassandra.import_cluster -e import_cluster_dir=%s/inventories -e import_cluster_force=true"
            % tmp_path) in text


def test_import_command_placeholders_for_what_help_could_not_read():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[0]["vars"].update(cassandra_jmx_username="(vaulted)", cassandra_jmx_password=True,
                            cassandra_listen_address="10.9.9.9", ansible_port="2222")
    hosts[0]["names"] += ["cassandra_jmx_username", "cassandra_jmx_password"]
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert ("$ ansible-playbook -i 192.0.2.11, -e ansible_port=2222 community.cassandra.import_cluster"
            " -e import_cluster_dir=NEW_DIR"
            " -e cassandra_jmx_username=JMX_USER -e cassandra_jmx_password_file=JMX_PASSWORD_FILE") in text
    assert "(vaulted)" not in text.split("1. The cluster", maxsplit=1)[0]


def test_unresolved_values_named():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"]["cassandra_rack"] = "{{ vault_rack }}"
    text = " ".join(advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)).split())
    assert ("- cassandra_rack could not be read from the inventory alone (a vaulted value, a fact of the node, a"
            " lookup; every node): what is shown above for it, and the commands filled from it, may be wrong.") in text


def test_values_are_quoted_for_the_shell():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"]["cassandra_dc"] = "dc one"
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    # as JSON: Ansible's key=value parsing would cut 'dc one' at the space
    assert ("community.cassandra.stop_rack -e '{\"cassandra_target_dc\": \"dc one\"}' -e cassandra_target_rack=rack3"
            in text)


def test_runbook_says_where_to_run_from():
    runbook = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD, markdown=True)
    assert "Run the commands from `../..`, relative to this file (where help was run, with its ansible.cfg" in runbook
    assert "/work" not in runbook
    runbook = cassandra_help(MODEL, PLAYBOOKS, cwd="/work/inventories/orders", markdown=True)
    assert "Run the commands from this file's directory (where help was run, with its ansible.cfg" in runbook


def test_vault_id_path_relative():
    text = cassandra_help(model(options=["--vault-id", "prod@/work/vault_pass"]), PLAYBOOKS, cwd=CWD)
    assert "--vault-id prod@vault_pass community.cassandra.status" in text


def test_no_node_chosen_for_removal():
    # without a node marked absent, a placeholder: never a real node nobody chose
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    del hosts[3]["vars"]["cassandra_node_state"]
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert "community.cassandra.decommission_node -e cassandra_target_nodes=NODE\n" in text
    topic = cassandra_help(model(hosts=hosts), PLAYBOOKS, topic="decommission_node", cwd=CWD)
    assert "Replace NODE with your own value." in topic


def test_topic_names_the_placeholders_of_the_command():
    text = cassandra_help(model(imported=False), PLAYBOOKS, topic="import_cluster", cwd=CWD)
    assert "Replace NEW_DIR with your own value." in text


def test_seeds_from_a_template():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"]["cassandra_seeds"] = "{{ groups['orders'] | map('extract', hostvars, 'ansible_host') | join(',') }}"
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert "  Seeds: not readable from the inventory alone\n" in text
    assert "seed" not in advice(text).replace("cassandra_seeds could not be read", "")
    assert "(seed)" not in text


def test_real_uppercase_values_are_not_placeholders():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"].update(cassandra_dc="EU_WEST", cassandra_rack="RACK_A")
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, topic="stop_rack", cwd=CWD)
    assert "Replace" not in text


def test_help_command_without_the_vault_prompt_help_added():
    text = cassandra_help(model(options=["--ask-vault-pass"], vault_prompt_added=True), PLAYBOOKS, cwd=CWD)
    assert "$ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.help\n" in text
    # given by the user (a vaulted group_vars/all): help needs it too
    text = cassandra_help(model(options=["--ask-vault-pass"]), PLAYBOOKS, cwd=CWD)
    assert "$ ansible-playbook -i inventories/orders/hosts.yml --ask-vault-pass community.cassandra.help\n" in text


def test_help_command_without_the_sudo_password_prompt():
    # help becomes root nowhere: no -K for it; the operations keep it
    text = cassandra_help(model(options=["-K"]), PLAYBOOKS, cwd=CWD)
    assert "$ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.help\n" in text
    assert "$ ansible-playbook -i inventories/orders/hosts.yml -K community.cassandra.status\n" in text


@pytest.mark.parametrize("more, java", [
    ({}, "Java: 11, package"),
    ({"cassandra_java_tarballs": {"11": {"url": "https://mirror.example.com/jdk-11.tar.gz"}}}, "Java: 11, tarball"),
    ({"cassandra_java_tarballs": {17: {"url": "https://mirror.example.com/jdk-17.tar.gz"}}}, "Java: 11, package"),
    ({"cassandra_java_tarballs": {"11": {"url": "u"}}, "cassandra_java_tarball": ""}, "Java: 11, package"),
    ({"cassandra_java_tarballs": {"11": {"url": "u"}}, "cassandra_install_java": False}, "Java: 11, set up by other means"),
    ({"cassandra_java_tarball": "https://mirror.example.com/jdk.tar.gz"}, "Java: 11, tarball"),
    ({"cassandra_java_home": "/opt/jdk-11"}, "Java: 11, at /opt/jdk-11"),
])
def test_java_origin(more, java):
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"].update(more)
    assert "\n  %s\n" % java in cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)


def test_commands_without_needless_options(tmp_path):
    """No -i when the inventory is the configuration's (ansible.cfg), no -e cassandra_hosts when the playbooks
    take the cluster without it: the inventory's only one, or on screen the one CASSANDRA_CLUSTER names (not in
    RUNBOOK.md, read in other shells)."""
    (tmp_path / "inventories").mkdir()
    sources = [str(tmp_path / "inventories")]
    configured = model(sources=sources, default_sources=[str(tmp_path / "inventories") + "/"])
    text = cassandra_help(configured, PLAYBOOKS, cwd=str(tmp_path))
    assert "$ ansible-playbook community.cassandra.status\n" in text and " -i inventories" not in text
    # the import of a new cluster still needs a node (-i <node>,)
    assert "$ ansible-playbook -i 192.0.2.11, community.cassandra.import_cluster" in text
    topic = cassandra_help(configured, PLAYBOOKS, topic="cleanup", cwd=str(tmp_path))
    assert "$ ansible-playbook community.cassandra.cleanup" in topic and "-i inventories" not in topic
    # another inventory given with -i: kept
    other = model(sources=sources, default_sources=["/etc/ansible/hosts"])
    assert "$ ansible-playbook -i inventories community.cassandra.status\n" in cassandra_help(
        other, PLAYBOOKS, cwd=str(tmp_path))
    # two clusters, CASSANDRA_CLUSTER names one: help shows that one, its commands on screen without the group
    billing = {"name": "billing", "hosts": [node("node9", "192.0.2.19", "rack1", cassandra_cluster_name="Billing")]}
    named = model(sources=sources, default_sources=sources, auto="", env_cluster="billing", clusters=[billing])
    text = cassandra_help(named, PLAYBOOKS, cwd=str(tmp_path))
    assert "$ ansible-playbook community.cassandra.status\n" in text and "-e cassandra_hosts" not in commands(text)
    runbook = cassandra_help(named, PLAYBOOKS, markdown=True, cwd=str(tmp_path))
    assert "ansible-playbook -i inventories community.cassandra.status -e cassandra_hosts=billing" in runbook
    # RUNBOOK.md: no -i for ansible.cfg's inventory only (not ANSIBLE_INVENTORY's), and an imported cluster named
    runbook = cassandra_help(dict(named, default_sources_origin="cfg"), PLAYBOOKS, markdown=True, cwd=str(tmp_path))
    assert "ansible-playbook community.cassandra.status -e cassandra_hosts=billing" in runbook
    alone = dict(configured, default_sources_origin="cfg", imported=["orders"])
    assert "$ ansible-playbook community.cassandra.status\n" in cassandra_help(alone, PLAYBOOKS, cwd=str(tmp_path))
    assert "ansible-playbook community.cassandra.status -e cassandra_hosts=orders" in cassandra_help(
        alone, PLAYBOOKS, markdown=True, cwd=str(tmp_path))
