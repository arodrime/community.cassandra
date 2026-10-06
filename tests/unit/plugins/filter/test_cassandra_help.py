from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The help playbook's text, from a model as the lookup cassandra_inventory
# returns it: a neutral inventory (one cluster, one datacenter, three racks,
# five nodes, three seeds, one node marked absent).

import copy
import os
import re

import pytest

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_help import (
    OPERATIONS, cassandra_help, cassandra_help_runbook)

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
    "sources": ["/work/inventories/orders/hosts.yml"], "vault_skipped": [], "auto": "orders", "options": ["-b"],
    "imported": True,
    "clusters": [{"name": "orders", "hosts": [
        node("node1", "192.0.2.11", "rack1"), node("node2", "192.0.2.12", "rack1"),
        node("node3", "192.0.2.13", "rack2"), node("node4", "192.0.2.14", "rack2", cassandra_node_state="absent"),
        node("node5", "192.0.2.15", "rack3")]}]}
PLAYBOOKS = [op["name"] for op in OPERATIONS]

GOLDEN = """\
Cassandra help for the inventory inventories/orders/hosts.yml (read from the inventory only: no node
  contacted)

Run the commands from the directory help was run from (the one with ansible.cfg, if any). Each
operation shows its plan first; --check runs it without changing anything. One operation in detail:
-e help_topic=<operation>. UPPERCASE words in a command: your own values.

1. The cluster as the inventory describes it
--------------------------------------------
Cluster 'Orders' (inventory group orders): 4 nodes, 1 more marked absent
  Cassandra: series 41x, package 4.1.10, installed from repository (role default)
  Java: 11, package
  Snitch: GossipingPropertyFileSnitch, num_tokens: 16, authenticator: PasswordAuthenticator
  Seeds: 192.0.2.11, 192.0.2.13, 192.0.2.15

  dc1: 5 nodes (1 marked absent), 3 racks
    rack1: node1 192.0.2.11 (seed), node2 192.0.2.12
    rack2: node3 192.0.2.13 (seed), node4 192.0.2.14 (absent)
    rack3: node5 192.0.2.15 (seed)

2. Operations
-------------

Read-only (change nothing):
  help - This overview, from the inventory alone (no node contacted).
    $ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.help
  status - The ring as nodetool status shows it from one node, per datacenter; a down node is shown,
    not an error.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.status
  health_check - Checks the cluster from every node (ring, gossip, native transport, streams,
    schema, ports); fails on a problem, so it can be scheduled.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.health_check
  preflight - Checks the nodes against the inventory before a change: settings that must match,
    racks for the token allocator, versions, seeds.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.preflight

Nodes:
  add_node - Adds new hosts to the running cluster, one at a time. Put them in their rack's group
    first, not in cassandra_seeds.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.add_node -e cassandra_new_nodes=NEW_NODE
  decommission_node - Removes nodes from the running cluster, one at a time, their data streamed to
    the others; refuses seeds.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.decommission_node -e cassandra_leaving_nodes=node4
  replace_node - Replaces a dead node by a blank host, which takes over its tokens and data. In the
    inventory, the new host in, the dead one out.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.replace_node -e cassandra_new_nodes=NEW_NODE -e cassandra_replace_address=DEAD_NODE_ADDRESS
  remove_dead_node - Last resort for a dead node that will not be replaced: removenode (or
    assassinate). Take it out of the inventory first.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.remove_dead_node -e cassandra_dead_node_address=DEAD_NODE_ADDRESS
  reset_node - Empties nodes that are not members of the ring (started once by mistake, a failed
    bootstrap) for a fresh start.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.reset_node -e cassandra_reset_nodes=NODE
  move_node - One token per node: moves nodes to new tokens, one at a time (by default the fewest
    moves that even out each datacenter). Not for this cluster (num_tokens 16).
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.move_node

Cluster:
  create_cluster - Builds the cluster from blank hosts: prepared in parallel, then started one at a
    time, seeds first. Starts nothing on a running cluster.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.create_cluster
  rolling_restart - Drains and restarts the nodes one at a time, the cluster checked before and
    after each one.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.rolling_restart
  rolling_reboot - Same as rolling_restart, rebooting the hosts (OS patching).
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.rolling_reboot
  stop_rack - Stops every node of one rack at once (maintenance), when the replication allows losing
    that rack.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.stop_rack -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3
  start_rack - Starts the nodes of a rack stop_rack stopped, then checks the whole cluster.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.start_rack -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3
  apply_config - Applies the inventory's config: shows every diff, asks once, then writes and
    restarts only the nodes that need it, one at a time.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.apply_config
  change_seeds - Applies a new cassandra_seeds list to every node, live (no restart).
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.change_seeds
  update_java - Moves the cluster to the Java in cassandra_java_version, one node at a time.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.update_java
  upgrade - Upgrades the cluster to the version in the inventory, one phase per run: preflight,
    prepare, canary, rolling, sstables, cleanup.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.upgrade -e cassandra_upgrade_phase=preflight
  cleanup - Runs nodetool cleanup (the data a node no longer owns, after nodes were added), the
    cluster checked before each batch.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.cleanup
  add_datacenter - Adds a datacenter: its nodes join without streaming, the keyspaces get replicas
    there, then each node rebuilds from another datacenter.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.add_datacenter -e cassandra_new_nodes=NEW_DC_GROUP -e cassandra_rebuild_source_dc=dc1 -e '{cassandra_datacenter_replication: {KEYSPACE: 3}}'
  remove_datacenter - Removes a datacenter: the keyspaces stop keeping replicas there, then its
    nodes leave one at a time. Move its clients first.
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.remove_datacenter -e cassandra_target_dc=DC_TO_REMOVE

Takeover:
  import_cluster - Reads the running cluster into an inventory, changing nothing on the nodes; a
    re-import into an inventory it wrote keeps the files it did not write.
    $ ansible-playbook -i 192.0.2.11, community.cassandra.import_cluster -e import_cluster_dir=inventories/orders -e import_cluster_force=true -e import_cluster_runbook=true

3. Advice
---------
- Marked cassandra_node_state: absent: node4. decommission_node removes them from the ring (--check
  first), then take them out of the inventory:
    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.decommission_node -e cassandra_leaving_nodes=node4
- dc1: racks of different sizes (1, 1, 2 nodes): the data is not shared evenly; add or remove nodes
  rack by rack."""


def model(**changes):
    out = copy.deepcopy(MODEL)
    for name, value in changes.items():
        if name == "hosts":
            out["clusters"][0]["hosts"] = value
        else:
            out[name] = value
    return out


def advice(text):
    return text.split("3. Advice\n---------\n", 1)[1]


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


def test_operation_variables_are_the_playbooks_own():
    # each -e variable named by a command or an option is in the playbook (or the roles it runs)
    for op in OPERATIONS:
        with open(os.path.join(TOP, "playbooks", op["name"] + ".yml"), encoding="utf-8") as f:
            text = f.read()
        for arg in op.get("args") or []:
            name = re.search(r"cassandra_\w+", arg).group(0)
            assert name in text, (op["name"], name)
        for name in op.get("options") or {}:
            assert name in text or name in ("cassandra_rolling_resume", "cassandra_reboot_timeout",
                                            "cassandra_cleanup_jobs"), (op["name"], name)


def test_markdown_has_the_same_commands():
    text = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD)
    runbook = cassandra_help(MODEL, PLAYBOOKS, cwd=CWD, markdown=True)
    commands = [line.split("$ ", 1)[1] for line in text.splitlines() if line.lstrip().startswith("$ ")]
    assert runbook.startswith("# RUNBOOK\n")
    assert runbook.endswith(".\n") and not runbook.endswith("\n\n")
    for command in commands:
        assert "\n" + command + "\n" in runbook or "\n  " + command + "\n" in runbook, command
    assert "## 3. Advice" in runbook


def test_topic():
    with open(os.path.join(TOP, "playbooks", "decommission_node.yml"), encoding="utf-8") as f:
        header = f.read()
    text = cassandra_help(MODEL, PLAYBOOKS, topic="decommission_node", header=header, cwd=CWD)
    lines = text.splitlines()
    assert lines[0].startswith("decommission_node (nodes): Removes nodes")
    assert ("    $ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.decommission_node"
            " -e cassandra_leaving_nodes=node4") in lines
    assert "What it does and checks (playbooks/decommission_node.yml):" in lines
    assert "  Removes nodes from a running cluster, one at a time: each one streams its" in lines
    assert "- name: Preflight" not in text  # the comment only
    assert "  cassandra_decommission_force  true goes on when a datacenter would keep fewer nodes than replicas" in lines
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
    assert ("- Authentication is on (PasswordAuthenticator) but cassandra_cql_username is not set:\n"
            "  decommission_node, remove_dead_node, rolling_restart (rack mode), stop_rack, add_datacenter,\n"
            "  remove_datacenter read the replication over CQL") in text


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
    assert "- dc1: no seed in rack2, rack3 (one seed per rack keeps one up when a rack is down)." in text
    assert "- dc1: more than one seed in rack1 (node1, node2): one per rack is enough." in text


def test_absent_seed():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[3]["vars"]["cassandra_seeds"] = hosts[3]["vars"]["cassandra_seeds"] + ["192.0.2.14"]
    text = advice(cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD))
    assert "- Seed and marked absent: node4. Take it out of cassandra_seeds (change_seeds) before it leaves." in text


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
    assert "1. The clusters as the inventory describes them" in text
    assert "community.cassandra.status -e cassandra_hosts=orders" in text
    assert "community.cassandra.status -e cassandra_hosts=billing" in text
    assert "- billing: Marked cassandra_node_state: absent: node4." in text


def test_vault_skipped():
    text = cassandra_help(model(vault_skipped=["/work/inventories/orders/group_vars/orders/secrets.yml"],
                                options=["-b", "--ask-vault-pass"]), PLAYBOOKS, cwd=CWD)
    assert "- Vault-encrypted files not read (help decrypts nothing): group_vars/orders/secrets.yml." in text
    assert "$ ansible-playbook -i inventories/orders/hosts.yml -b --ask-vault-pass community.cassandra.status" in text


def test_import_command_keeps_the_connection_and_jmx():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[0]["vars"].update(ansible_user="admin", cassandra_jmx_username="monitor",
                            cassandra_jmx_password_file="/etc/cassandra/jmxremote.password")
    text = cassandra_help(model(hosts=hosts, options=["-b", "--ask-vault-pass"]), PLAYBOOKS, cwd=CWD)
    assert ("$ ansible-playbook -i 192.0.2.11, -u admin community.cassandra.import_cluster"
            " -e import_cluster_dir=inventories/orders -e import_cluster_force=true -e import_cluster_runbook=true"
            " -e cassandra_jmx_username=monitor"
            " -e cassandra_jmx_password_file=/etc/cassandra/jmxremote.password") in text


def test_absent_nodes_with_topology():
    text = advice(cassandra_help(MODEL, PLAYBOOKS + ["topology"], cwd=CWD))
    assert "topology --check shows the plan" in text
    assert "$ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.topology --check" in text


def test_single_token_cluster():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    for host in hosts:
        host["vars"]["cassandra_num_tokens"] = "1"
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert "Not for this cluster" not in text
    assert "token allocator" not in advice(text)


def test_inventory_outside_the_current_dir():
    text = cassandra_help(MODEL, PLAYBOOKS, cwd="/elsewhere")
    assert "$ ansible-playbook -i /work/inventories/orders/hosts.yml -b community.cassandra.status" in text


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
    for changed in (model(imported=False), model(clusters=MODEL["clusters"] * 2, auto="")):
        text = cassandra_help(changed, PLAYBOOKS, cwd=CWD)
        assert "community.cassandra.import_cluster -e import_cluster_dir=NEW_DIR\n" in text
        assert "import_cluster_force" not in text


def test_import_command_placeholders_for_what_help_could_not_read():
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    hosts[0]["vars"].update(cassandra_jmx_username="(vaulted)", cassandra_jmx_password=True,
                            cassandra_listen_address="10.9.9.9", ansible_port="2222")
    hosts[0]["names"] += ["cassandra_jmx_username", "cassandra_jmx_password"]
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert ("$ ansible-playbook -i 192.0.2.11, -e ansible_port=2222 community.cassandra.import_cluster"
            " -e import_cluster_dir=inventories/orders -e import_cluster_force=true -e import_cluster_runbook=true"
            " -e cassandra_jmx_username=JMX_USER -e cassandra_jmx_password_file=JMX_PASSWORD_FILE") in text
    assert "(vaulted)" not in text.split("1. The cluster")[0]


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
    text = cassandra_help(model(options=["-b", "--vault-id", "prod@/work/vault_pass"]), PLAYBOOKS, cwd=CWD)
    assert "-b --vault-id prod@vault_pass community.cassandra.status" in text


def test_no_node_chosen_for_removal():
    # without a node marked absent, a placeholder: never a real node nobody chose
    hosts = copy.deepcopy(MODEL["clusters"][0]["hosts"])
    del hosts[3]["vars"]["cassandra_node_state"]
    text = cassandra_help(model(hosts=hosts), PLAYBOOKS, cwd=CWD)
    assert "community.cassandra.decommission_node -e cassandra_leaving_nodes=NODE\n" in text
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
