from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The help playbook, run by ansible-playbook on a neutral inventory whose
# nodes can't be reached (192.0.2.0/24, TEST-NET-1): it reads the inventory
# only, skips a vaulted file without the vault password, and writes
# RUNBOOK.md next to the inventory, idempotent, the difference shown under
# --check --diff.

import os
import re
import subprocess
import sys

import yaml

from ansible.parsing.vault import VaultLib, VaultSecret

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))
TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

HOSTS = """\
all:
  children:
    orders:
      children:
        orders_dc1:
          children:
            orders_dc1_rack1:
              hosts:
                node1: {ansible_host: 192.0.2.11}
                node2: {ansible_host: 192.0.2.12}
            orders_dc1_rack2:
              hosts:
                node3: {ansible_host: 192.0.2.13}
                node4: {ansible_host: 192.0.2.14, cassandra_node_state: absent}
            orders_dc1_rack3:
              hosts:
                node5: {ansible_host: 192.0.2.15}
"""
CLUSTER_VARS = """\
cassandra_cluster_name: Orders
cassandra_version: 41x
cassandra_package_version: 4.1.10
cassandra_java_version: "11"
cassandra_endpoint_snitch: GossipingPropertyFileSnitch
cassandra_authenticator: PasswordAuthenticator
cassandra_seeds: [192.0.2.11, 192.0.2.13, 192.0.2.15]
cassandra_listen_address: "{{ ansible_host }}"
cassandra_dc: dc1
"""
SECRET = "s3cr3t-value"


def inventory(tmp_path, vault_password=None):
    inv = tmp_path / "inventories" / "orders"
    (inv / "group_vars" / "orders").mkdir(parents=True)
    (inv / "hosts.yml").write_text(HOSTS)
    (inv / "group_vars" / "orders" / "main.yml").write_text(CLUSTER_VARS)
    for rack in (1, 2, 3):
        (inv / "group_vars" / ("orders_dc1_rack%d.yml" % rack)).write_text("cassandra_rack: rack%d\n" % rack)
    secrets = "cassandra_cql_username: admin\ncassandra_cql_password: %s\n" % SECRET
    if vault_password:
        secrets = VaultLib([("default", VaultSecret(vault_password.encode()))]).encrypt(secrets).decode()
    (inv / "group_vars" / "orders" / "secrets.yml").write_text(secrets)
    return inv


def run(tmp_path, *extra, **kwargs):
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=COLLECTIONS, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0", ANSIBLE_TIMEOUT="3",
               ANSIBLE_STDOUT_CALLBACK="ansible.builtin.default",
               ANSIBLE_CALLBACK_RESULT_FORMAT=kwargs.get("result_format", "json"))
    for name in ("ANSIBLE_VAULT_PASSWORD_FILE", "ANSIBLE_BECOME", "ANSIBLE_CONFIG", "ANSIBLE_INVENTORY"):
        env.pop(name, None)
    argv = [sys.executable, "-c", "from ansible.cli.playbook import main; main()",
            "community.cassandra.help"] + list(extra)
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=300, check=False)
    return result.returncode, result.stdout.decode(errors="replace")


def recap(output):
    return dict(re.findall(r"^(\S+)\s+: ok=\d+\s+changed=(\d+)", output, re.M))


def test_help_reads_the_inventory_only(tmp_path):
    inv = inventory(tmp_path, vault_password="pw")
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml")
    assert rc == 0, out
    assert list(recap(out)) == ["localhost"], out  # no node in the run
    assert '"Cluster \'Orders\' (inventory group orders): 4 nodes, 1 more marked absent",' in out  # a list of lines
    assert "rack2: node3 192.0.2.13 (seed), node4 192.0.2.14 (absent)" in out
    assert ("$ ansible-playbook -i inventories/orders/hosts.yml -b --ask-vault-pass"
            " community.cassandra.decommission_node -e cassandra_leaving_nodes=node4") in out
    assert "Vault-encrypted files not read (help decrypts nothing): group_vars/orders/secrets.yml." in out
    # the CQL user is in the file not read
    assert "cassandra_cql_username is not set in the files\",\n        \"  read (a vaulted one may set it)" in out
    assert SECRET not in out
    assert not (inv / "RUNBOOK.md").exists()


def test_help_decrypts_nothing_even_with_the_vault_password(tmp_path):
    # a shown setting from a vaulted variable stays unread: no secret in the output nor in RUNBOOK.md
    inv = inventory(tmp_path, vault_password="pw")
    (inv / "group_vars" / "orders_dc1_rack3.yml").write_text("cassandra_rack: \"{{ vault_rack }}\"\n")
    secrets = (inv / "group_vars" / "orders" / "secrets.yml")
    clear = "vault_rack: %s\ncassandra_cql_password: %s\n" % (SECRET, SECRET)
    secrets.write_text(VaultLib([("default", VaultSecret(b"pw"))]).encrypt(clear).decode())
    (tmp_path / "vault_pass").write_text("pw\n")
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "--vault-password-file", "vault_pass",
                  "-e", "help_write=true")
    assert rc == 0, out
    runbook = (inv / "RUNBOOK.md").read_text()
    assert SECRET not in out and SECRET not in runbook
    assert "Vault-encrypted files not read (help decrypts nothing)" in out
    assert "cassandra_rack could not be read without the vault" in out
    assert "-b --vault-password-file vault_pass community.cassandra.status" in out  # no --ask-vault-pass added
    assert "--ask-vault-pass" not in out


def test_help_topic_under_the_yaml_result_format(tmp_path):
    # one block, not a list of lines the yaml dumper would wrap (and quote)
    inventory(tmp_path)
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "-e", "help_topic=stop_rack", result_format="yaml")
    assert "    msg: |-\n        stop_rack (cluster): Stops every node" in out
    assert rc == 0, out
    assert ("$ ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.stop_rack"
            " -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3") in out
    assert "What it does and checks (playbooks/stop_rack.yml):" in out
    assert "  Stops every node of one rack at once (maintenance of the rack's hosts,\n" in out
    assert SECRET not in out


def test_help_unknown_topic(tmp_path):
    inventory(tmp_path)
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "-e", "help_topic=stop_racks")
    assert rc != 0
    assert "help_topic: no operation stop_racks (operations: help, status," in out


def test_help_write_is_idempotent(tmp_path):
    inv = inventory(tmp_path)
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "-e", "help_write=true")
    assert rc == 0, out
    assert recap(out) == {"localhost": "1"}, out
    runbook = (inv / "RUNBOOK.md").read_text()
    assert runbook.startswith("# RUNBOOK\n")
    assert ("ansible-playbook -i inventories/orders/hosts.yml -b community.cassandra.decommission_node"
            " -e cassandra_leaving_nodes=node4\n") in runbook
    assert SECRET not in runbook

    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "-e", "help_write=true")
    assert rc == 0, out
    assert recap(out) == {"localhost": "0"}, out

    # a change in the inventory: --check --diff shows it, writes nothing
    (inv / "group_vars" / "orders_dc1_rack3.yml").write_text("cassandra_rack: rack9\n")
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "-e", "help_write=true", "--check", "--diff")
    assert rc == 0, out
    assert recap(out) == {"localhost": "1"}, out
    assert "-    rack3: node5 192.0.2.15 (seed)" in out
    assert "+    rack9: node5 192.0.2.15 (seed)" in out
    assert (inv / "RUNBOOK.md").read_text() == runbook


def test_help_inventory_as_import_cluster_runs_it(tmp_path):
    # import_cluster_runbook: the run's own inventory is the nodes given (-i node1,), help reads the one written
    inv = inventory(tmp_path)
    rc, out = run(tmp_path, "-i", "192.0.2.11,", "-e", "help_inventory=inventories/orders/hosts.yml",
                  "-e", "help_write=true", "-e", "help_show=false")
    assert rc == 0, out
    assert "Cluster 'Orders'" not in out  # RUNBOOK.md only
    assert "-i inventories/orders/hosts.yml -b community.cassandra.status" in (inv / "RUNBOOK.md").read_text()


def test_import_cluster_writes_the_runbook_on_request():
    # _dir: the import's localhost fact, still there in the help play (test_help_inventory_as_import_cluster_runs_it
    # runs that play the way the import does)
    with open(os.path.join(TOP, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
        last = yaml.safe_load(f)[-1]
    assert last["ansible.builtin.import_playbook"] == "help.yml"
    assert last["when"] == "import_cluster_runbook | default(false) | bool and not ansible_check_mode"
    assert last["vars"] == {"help_inventory": "{{ _dir }}/hosts.yml", "help_write": True, "help_show": False}
