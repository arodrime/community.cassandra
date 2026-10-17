from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_close_names

with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_service", "defaults", "main.yml"),
          encoding="utf-8") as _f:
    PLAYBOOK_VARIABLES = yaml.safe_load(_f)["_cassandra_playbook_variables"]

KNOWN = ["cassandra_hosts", "cassandra_target_nodes", "cassandra_target_dc", "cassandra_target_rack", "cassandra_status_raw"]


def test_misspelt_names_found():
    assert cassandra_close_names(["cassandra_host", "casandra_hosts", "cassandra_target_node", "cassandra_hots"], KNOWN) == {
        "cassandra_host": "cassandra_hosts", "casandra_hosts": "cassandra_hosts",
        "cassandra_target_node": "cassandra_target_nodes", "cassandra_hots": "cassandra_hosts"}


def test_known_and_other_settings_left_alone():
    # the playbooks' own names, and the usual settings of an inventory
    names = KNOWN + ["cassandra_dc", "cassandra_rack", "cassandra_seeds", "cassandra_version", "cassandra_cluster_name",
                     "cassandra_jmx_port", "cassandra_status_raw_output", ""]
    assert cassandra_close_names(names, KNOWN) == {}
    assert cassandra_close_names(None, KNOWN) == {}


def test_role_variables_are_not_typos():
    # close to cassandra_replace_address, but a cassandra_config variable
    assert cassandra_close_names(["cassandra_rpc_address"], ["cassandra_replace_address"]) == {}


def test_unknown_names_with_a_suggestion():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_names
    out = cassandra_unknown_names(["cassandra_config_usr", "cassandra_frobnicate_level", "cassandra_config_user",
                                   "cassandra_hosts", "cassandra_config_applied", "cassandra_config_owner"],
                                  ["cassandra_hosts"])
    # a role variable, a playbook one, a result of a role's task, a former name still read: known
    assert out == {"cassandra_config_usr": "cassandra_config_user", "cassandra_frobnicate_level": ""}


def test_former_names():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_renamed_names
    former = ["cassandra_new_nodes", "cassandra_leaving_nodes", "cassandra_reset_nodes", "cassandra_dead_node_address",
              "cassandra_upgrade_canary", "cassandra_status_from"]
    assert cassandra_renamed_names(former + ["cassandra_target_nodes", "cassandra_hosts", "cassandra_leaving_node"]) == dict(
        (name, "cassandra_target_nodes") for name in former)
    assert cassandra_renamed_names(None) == {}


def test_comments_are_not_variables():
    # hosts_check.yml shows the typo -e cassandra_host=... in a comment: still unknown
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_names
    assert cassandra_unknown_names(["cassandra_host"], KNOWN) == {"cassandra_host": "cassandra_hosts"}


@pytest.mark.parametrize("name, close", [
    ("import_cluster_keep_hand_edit", "import_cluster_keep_hand_edits"),  # a typo, silently ignored before
    ("cassandra_target_node", "cassandra_target_nodes"),
    ("cassandra_rolling_resum", "cassandra_rolling_resume"),
    ("casandra_hosts", "cassandra_hosts"),  # a typo in the prefix too
    ("help_topc", "help_topic"),
    ("cassandra_allow_unknown_variable", "cassandra_allow_unknown_variables"),
    ("CASSANDRA_HOSTS", "cassandra_hosts"),  # names are case-sensitive
    ("CASSANDRA_NEW_NODES", "cassandra_target_nodes"),  # a former name, in another case
    ("CASSANDRA_CLUSTER", "cassandra_hosts"),  # the environment variable given with -e
    ("Cassandra_target_nodes", "cassandra_target_nodes"),
    ("cassandra_frobnicate_level", ""),  # nothing close
    ("import_cluster_xyzzy_level", ""),
])
def test_unknown_extra_vars(name, close):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    assert cassandra_unknown_extra_vars([name], PLAYBOOK_VARIABLES) == {name: close}


def test_known_extra_vars_pass():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    names = PLAYBOOK_VARIABLES + [
        "import_cluster_keep_hand_edits", "import_cluster_force", "import_cluster_dir", "import_cluster_allow_unread",
        "import_cluster_set_ansible_host", "import_cluster_vault_password_file", "import_cluster_medusa_path",
        "help_topic", "help_write", "help_runbook_dir", "help_inventory",
        "cassandra_version", "cassandra_jmx_username", "cassandra_operation_confirm", "cassandra_cleanup_jobs",
        "cassandra_rack_force", "cassandra_token_auto", "cassandra_medusa_config_user"]
    assert cassandra_unknown_extra_vars(names, PLAYBOOK_VARIABLES) == {}


def test_other_prefixes_and_former_names_left_alone():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    # other owners' variables, Ansible's own, and the former names (refused with their name now, not as unknown)
    names = ["ansible_user", "my_cassandra_thing", "cassandrax", "helper_x", "import_clusters", "cassandra",
             "cassandra_new_nodes", "cassandra_status_from"]
    assert cassandra_unknown_extra_vars(names, PLAYBOOK_VARIABLES) == {}
    assert cassandra_unknown_extra_vars(None, PLAYBOOK_VARIABLES) == {}


def test_no_file_read_without_a_name_of_ours(monkeypatch):
    # the usual run: no name to look up, the roles' files are not read
    from ansible_collections.community.cassandra.plugins.filter import cassandra_names

    def read():
        raise AssertionError("read")
    monkeypatch.setattr(cassandra_names, "_role_variables", read)
    monkeypatch.setattr(cassandra_names, "_used_names", read)
    assert cassandra_names.cassandra_unknown_extra_vars(["cassandra_hosts", "ansible_user", "cassandra_new_nodes"],
                                                        PLAYBOOK_VARIABLES) == {}


def test_the_secrets_import_cluster_moved_are_known():
    # cassandra_secret_<name>: a secret import_cluster took out of a config file's line, which refers to it
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_names
    names = ["cassandra_secret_keystore_password", "cassandra_secret_javax_net_ssl_truststorepassword_2"]
    assert cassandra_unknown_names(names, []) == {}
    assert cassandra_close_names(names, ["cassandra_secrets"]) == {}
