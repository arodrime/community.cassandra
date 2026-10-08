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

from ansible.parsing.vault import VaultLib, VaultSecret

COLLECTIONS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".."))

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
               ANSIBLE_STDOUT_CALLBACK=kwargs.get("callback", "ansible.builtin.default"),
               ANSIBLE_CALLBACK_RESULT_FORMAT=kwargs.get("result_format", "json"))
    # (ansible-test --color sets ANSIBLE_FORCE_COLOR, which wins over ANSIBLE_NOCOLOR)
    for name in ("ANSIBLE_VAULT_PASSWORD_FILE", "ANSIBLE_BECOME", "ANSIBLE_CONFIG", "ANSIBLE_INVENTORY", "ANSIBLE_FORCE_COLOR",
                 "CASSANDRA_CLUSTER"):
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
    # the json result format: first, how to get plain text
    assert ('    "msg": [\n        "Plain text, without the quotes: callback_result_format = yaml in ansible.cfg'
            ' ([defaults])",\n        "",\n        "Cassandra help for the inventory') in out
    assert "rack2: node3 192.0.2.13 (seed), node4 192.0.2.14 (absent)" in out
    assert ("$ ansible-playbook -i inventories/orders/hosts.yml --ask-vault-pass"
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
    assert "cassandra_rack could not be read from the inventory alone" in out
    assert "--vault-password-file vault_pass community.cassandra.status" in out  # no --ask-vault-pass added
    assert "--ask-vault-pass" not in out


def test_no_secret_from_inline_vault_extra_vars_or_lookups(tmp_path):
    # with the vault password: an inline !vault a shown setting templates, a vaulted -e @file, a lookup
    inv = inventory(tmp_path)
    secret = "\n".join("  " + line for line in VaultLib([("default", VaultSecret(b"pw"))]).encrypt(SECRET)
                       .decode().splitlines())
    (inv / "group_vars" / "orders" / "main.yml").write_text(
        CLUSTER_VARS.replace("cassandra_cluster_name: Orders\n", "") + "the_secret: !vault |\n%s\n" % secret
        + 'cassandra_cluster_name: "{{ the_secret }}"\n'
        + "ansible_user: \"{{ lookup('ansible.builtin.file', '%s') }}\"\n" % (tmp_path / "secret_file"))
    (tmp_path / "secret_file").write_text(SECRET)
    (tmp_path / "extra.yml").write_text(
        VaultLib([("default", VaultSecret(b"pw"))]).encrypt("cassandra_dc: %s\n" % SECRET).decode())
    (tmp_path / "vault_pass").write_text("pw\n")
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "--vault-password-file", "vault_pass",
                  "-e", "@extra.yml", "-e", "help_write=true")
    assert rc == 0, out
    runbook = (inv / "RUNBOOK.md").read_text()
    assert SECRET not in out and SECRET not in runbook
    assert "Cluster '(vaulted)' (inventory group orders)" in runbook


def test_inline_vault_without_password_asks_for_it(tmp_path):
    inv = inventory(tmp_path)
    secret = "\n".join("  " + line for line in VaultLib([("default", VaultSecret(b"pw"))]).encrypt(SECRET)
                       .decode().splitlines())
    (inv / "group_vars" / "orders" / "secrets.yml").write_text("cassandra_cql_password: !vault |\n%s\n" % secret)
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml")
    assert rc == 0, out
    assert "--ask-vault-pass community.cassandra.status" in out


def test_help_topic_under_the_yaml_result_format(tmp_path):
    # one block, not a list of lines the yaml dumper would wrap (and quote)
    inventory(tmp_path)
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "-e", "help_topic=stop_rack", result_format="yaml")
    assert "    msg: |-\n        stop_rack (cluster): Stops every node" in out
    assert rc == 0, out
    assert ("$ ansible-playbook -i inventories/orders/hosts.yml community.cassandra.stop_rack"
            " -e cassandra_target_dc=dc1 -e cassandra_target_rack=rack3") in out
    assert "What it does and checks (playbooks/stop_rack.yml):" in out
    assert "  Stops every node of one rack at once (maintenance of the rack's hosts,\n" in out
    # plain text: the options one per line, unquoted
    assert ("\n        Options:\n            -e cassandra_target_dc=<dc>           the rack's datacenter (required)\n"
            in out)
    assert "Plain text, without the quotes" not in out
    assert SECRET not in out


def test_help_under_another_callback(tmp_path):
    # a list of lines, without the advice for the default callback
    inventory(tmp_path)
    rc, out = run(tmp_path, "-i", "inventories/orders/hosts.yml", "-e", "help_topic=stop_rack",
                  callback="ansible.builtin.minimal")
    assert rc == 0, out
    assert '"stop_rack (cluster): Stops every node of one rack at once (maintenance), when the replication' in out
    assert "Plain text, without the quotes" not in out


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
    assert ("ansible-playbook -i inventories/orders/hosts.yml community.cassandra.decommission_node"
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


def test_help_inventory(tmp_path):
    # help_inventory: read instead of the run's own inventory (here the nodes given, -i node1,)
    inv = inventory(tmp_path)
    rc, out = run(tmp_path, "-i", "192.0.2.11,", "-e", "help_inventory=inventories/orders/hosts.yml",
                  "-e", "help_write=true")
    assert rc == 0, out
    assert "Cluster 'Orders'" in out
    assert "-i inventories/orders/hosts.yml community.cassandra.status" in (inv / "RUNBOOK.md").read_text()


def test_runbook_of_an_imported_cluster(tmp_path):
    # an inventory dir of imported clusters: help for one cluster, RUNBOOK.md next to its import report
    inv = inventory(tmp_path)
    shared = tmp_path / "inventories"
    (inv / "hosts.yml").rename(shared / "orders.yml")
    (inv / "group_vars").rename(shared / "group_vars")
    inv.rmdir()
    (shared / "billing.yml").write_text("all:\n  children:\n    billing:\n      hosts:\n        node9: {ansible_host: 192.0.2.19}\n")
    (tmp_path / "reports" / "orders").mkdir(parents=True)
    rc, out = run(tmp_path, "-i", "192.0.2.11,", "-e", "help_inventory=inventories", "-e", "cassandra_hosts=orders",
                  "-e", "help_runbook_dir=reports/orders", "-e", "help_write=true")
    assert rc == 0, out
    assert not (shared / "RUNBOOK.md").exists()
    runbook = (tmp_path / "reports" / "orders" / "RUNBOOK.md").read_text()
    assert "`../..`, relative to this file" in runbook
    assert "-i inventories community.cassandra.status -e cassandra_hosts=orders" in runbook
    assert "billing" not in runbook
