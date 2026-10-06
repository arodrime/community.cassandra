from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The lookup cassandra_inventory: the inventory read as the operations read
# it, nothing decrypted, no node contacted.

import os

import pytest

from ansible.errors import AnsibleError
from ansible.parsing.vault import VaultLib, VaultSecret

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import GENERATED
from ansible_collections.community.cassandra.plugins.lookup.cassandra_inventory import read

HOSTS = """\
all:
  children:
    orders:
      children:
        orders_dc1:
          hosts:
            node1: {ansible_host: 192.0.2.11}
            node2: {ansible_host: 192.0.2.12}
"""


def vaulted(text):
    return VaultLib([("default", VaultSecret(b"pw"))]).encrypt(text).decode()


def inventory(tmp_path, hosts=HOSTS, **group_vars):
    inv = tmp_path / "inv"
    (inv / "group_vars").mkdir(parents=True)
    (inv / "hosts.yml").write_text(hosts)
    for name, text in group_vars.items():
        (inv / "group_vars" / (name + ".yml")).write_text(text)
    return str(inv / "hosts.yml")


@pytest.fixture(autouse=True)
def no_become_env(monkeypatch):
    monkeypatch.delenv("ANSIBLE_BECOME", raising=False)


def host(model, name):
    return next(h for c in model["clusters"] for h in c["hosts"] if h["name"] == name)


def test_reads_the_cluster(tmp_path):
    model = read([inventory(tmp_path, orders="cassandra_dc: dc1\ncassandra_cluster_name: Orders\n")])
    assert model["auto"] == "orders"
    assert [c["name"] for c in model["clusters"]] == ["orders"]
    assert host(model, "node1")["vars"] == {"ansible_host": "192.0.2.11", "cassandra_dc": "dc1",
                                            "cassandra_cluster_name": "Orders"}
    assert model["options"] == ["-b"]
    assert model["vault_skipped"] == [] and model["imported"] is False


def test_vaulted_file_skipped_not_decrypted(tmp_path):
    source = inventory(tmp_path, orders="cassandra_dc: dc1\n")
    secrets = tmp_path / "inv" / "group_vars" / "orders_dc1.yml"
    secrets.write_text(vaulted("cassandra_cql_username: admin\n"))
    model = read([source])
    assert model["vault_skipped"] == [str(secrets)]
    assert "cassandra_cql_username" not in host(model, "node1")["names"]
    assert model["options"] == ["-b", "--ask-vault-pass"]


def test_inline_vault_not_decrypted(tmp_path):
    value = "\n".join("  " + line for line in vaulted("admin").splitlines())
    source = inventory(tmp_path, orders="cassandra_cql_username: !vault |\n%s\ncassandra_dc: !vault |\n%s\n"
                       % (value, value))
    vars_ = host(read([source]), "node1")["vars"]
    assert vars_["cassandra_cql_username"] is True
    assert vars_["cassandra_dc"] == "(vaulted)"


def test_template_resolved_or_kept_raw(tmp_path):
    source = inventory(tmp_path, orders='cassandra_listen_address: "{{ ansible_host }}"\n'
                                        'cassandra_rack: "{{ vault_rack }}"\n')
    vars_ = host(read([source]), "node2")["vars"]
    assert vars_["cassandra_listen_address"] == "192.0.2.12"
    assert vars_["cassandra_rack"] == "{{ vault_rack }}"


def test_secrets_not_returned(tmp_path):
    source = inventory(tmp_path, orders="cassandra_cql_password: s3cr3t\ncassandra_jmx_password: s3cr3t\n")
    model = read([source])
    assert "s3cr3t" not in repr(model)
    assert host(model, "node1")["vars"] == {"ansible_host": "192.0.2.11", "cassandra_jmx_password": True}


def test_yaml_error_not_hidden(tmp_path):
    source = inventory(tmp_path, orders="cassandra_dc: [dc1\n")
    with pytest.raises(AnsibleError):
        read([source])


def test_inventory_become_drops_b(tmp_path):
    assert read([inventory(tmp_path, orders="ansible_become: yes\n")])["options"] == []


def test_given_group_missing(tmp_path):
    with pytest.raises(AnsibleError, match="group 'prod' not found"):
        read([inventory(tmp_path)], given="prod")


def test_several_clusters(tmp_path):
    hosts = HOSTS + "    billing:\n      hosts:\n        node9: {ansible_host: 192.0.2.19}\n"
    model = read([inventory(tmp_path, hosts=hosts)])
    assert model["auto"] == ""
    assert [c["name"] for c in model["clusters"]] == ["billing", "orders"]


def test_current_dir_group_vars_not_read(tmp_path, monkeypatch):
    # the operations read group_vars next to the inventory and the playbooks, not the current dir's
    source = inventory(tmp_path)
    (tmp_path / "work" / "group_vars").mkdir(parents=True)
    (tmp_path / "work" / "group_vars" / "all.yml").write_text("cassandra_cluster_name: FROM_CWD\n")
    monkeypatch.chdir(str(tmp_path / "work"))
    model = read([source], basedir=str(tmp_path / "playbooks"))
    assert "cassandra_cluster_name" not in host(model, "node1")["vars"]


def test_imported(tmp_path):
    source = inventory(tmp_path, hosts=GENERATED + "\n" + HOSTS)
    assert read([source])["imported"] is True
    assert read([os.path.dirname(source)])["imported"] is True


def test_value_from_an_inline_vault_masked(tmp_path):
    value = "\n".join("  " + line for line in vaulted("TOPSECRET").splitlines())
    source = inventory(tmp_path, orders="the_secret: !vault |\n%s\ncassandra_cluster_name: \"{{ the_secret }}\"\n"
                       "cassandra_dc: \"x-{{ the_secret }}\"\n" % value)
    model = read([source])
    vars_ = host(model, "node1")["vars"]
    assert vars_["cassandra_cluster_name"] == "(vaulted)" and vars_["cassandra_dc"] == "(vaulted)"
    assert model["options"] == ["-b", "--ask-vault-pass"]  # the operations decrypt it


def test_lookup_not_run(tmp_path):
    (tmp_path / "marker").write_text("TOPSECRET")
    source = inventory(tmp_path, orders="ansible_user: \"{{ lookup('ansible.builtin.file', '%s') }}\"\n"
                       % (tmp_path / "marker"))
    assert host(read([source]), "node1")["vars"]["ansible_user"].startswith("{{ lookup(")


def test_lookup_through_another_variable_not_run(tmp_path):
    marker = tmp_path / "ran"
    source = inventory(tmp_path, orders=(
        "indirect: \"{{ lookup('ansible.builtin.pipe', 'touch %s; echo INDIRECT') }}\"\n"
        "cassandra_endpoint_snitch: \"{{ indirect }}\"\n"
        "cassandra_dc: \"{{ (lookup)('ansible.builtin.pipe', 'touch %s; echo DIRECT') }}\"\n") % (marker, marker))
    vars_ = host(read([source]), "node1")["vars"]
    assert not marker.exists()
    assert vars_["cassandra_endpoint_snitch"].startswith("{{") and vars_["cassandra_dc"].startswith("{{")


def test_transformed_vault_value_masked(tmp_path):
    value = "\n".join("  " + line for line in vaulted("TOPSECRET").splitlines())
    source = inventory(tmp_path, orders="the_secret: !vault |\n%s\ncassandra_cluster_name: \"{{ the_secret | b64encode }}\"\n"
                       "cassandra_dc: \"{{ the_secret[1:] }}\"\n" % value)
    vars_ = host(read([source]), "node1")["vars"]
    assert vars_["cassandra_cluster_name"] == "(vaulted)" and vars_["cassandra_dc"] == "(vaulted)"


def test_empty_group(tmp_path):
    hosts = HOSTS + "    empty:\n      hosts: {}\n"
    with pytest.raises(AnsibleError, match="group 'empty' has no host"):
        read([inventory(tmp_path, hosts=hosts)], given="empty")
