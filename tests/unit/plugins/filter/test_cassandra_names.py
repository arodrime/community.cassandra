from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_close_names

with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_operation", "vars", "main.yml"),
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
    # a role variable, a playbook one, a result of a role's task: known; a former name: its name now
    assert out == {"cassandra_config_usr": "cassandra_config_user", "cassandra_frobnicate_level": "",
                   "cassandra_config_owner": "cassandra_config_user"}


def test_former_names():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_renamed_names
    former = ["cassandra_new_nodes", "cassandra_leaving_nodes", "cassandra_reset_nodes", "cassandra_dead_node_address",
              "cassandra_upgrade_canary", "cassandra_status_from"]
    assert cassandra_renamed_names(former + ["cassandra_target_nodes", "cassandra_hosts", "cassandra_leaving_node"]) == dict(
        (name, "cassandra_target_nodes") for name in former)
    assert cassandra_renamed_names(None) == {}


@pytest.mark.parametrize("former, now", [
    ("cassandra_repository_deb_url", "cassandra_install_url"), ("cassandra_repository_rpm_url", "cassandra_install_url"),
    ("cassandra_repository_username", "cassandra_install_username"),
    ("cassandra_repository_password", "cassandra_install_password"),
    ("cassandra_medusa_config_owner", "cassandra_medusa_config_user"),
    ("cassandra_medusa_key_file_owner", "cassandra_medusa_key_file_user"),
    ("cassandra_config_owner", "cassandra_config_user")])
def test_names_of_the_release_candidates_are_refused(former, now):
    """An inventory written for v2.1.0-rc3: each former name refused with the one to set, never silently ignored."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import (
        cassandra_renamed_names, cassandra_unknown_extra_vars, _role_variables)
    assert cassandra_renamed_names([former, now]) == {former: now}
    assert now in _role_variables() and former not in _role_variables()
    # (-e: refused as renamed, not also as unknown)
    assert cassandra_unknown_extra_vars([former], []) == {}


def test_comments_are_not_variables():
    # hosts_check.yml shows the typo -e cassandra_host=... in a comment: still unknown
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_names
    assert cassandra_unknown_names(["cassandra_host"], KNOWN) == {"cassandra_host": "cassandra_hosts"}


@pytest.mark.parametrize("name, close", [
    ("import_keep_hand_edit", "import_keep_hand_edits"),  # a typo, silently ignored before
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
    ("import_xyzzy_level", ""),
    ("import_forse", "import_force"),  # the import_ prefix is the collection's too
    ("status_resolve_ips", "status_resolve_ip"),  # status's option: a typo stops, never a lookup left on
    # words of the playbooks that are no variable: not known
    ("import_tasks", ""), ("import_playbook", ""), ("import_role", ""), ("import_distribution", ""),
    ("import_read_files", ""), ("import_given", ""),
])
def test_unknown_extra_vars(name, close):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    assert cassandra_unknown_extra_vars([name], PLAYBOOK_VARIABLES) == {name: close}


def test_known_extra_vars_pass():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    names = PLAYBOOK_VARIABLES + [
        "import_keep_hand_edits", "import_force", "import_dir", "import_allow_unread",
        "import_set_ansible_host", "import_vault_password_file", "import_medusa_path",
        "help_topic", "help_write", "help_runbook_dir", "help_inventory",
        "cassandra_version", "cassandra_jmx_username", "cassandra_operation_confirm", "cassandra_cleanup_jobs",
        "cassandra_rack_force", "cassandra_token_auto", "cassandra_medusa_config_user"]
    assert cassandra_unknown_extra_vars(names, PLAYBOOK_VARIABLES) == {}


def test_other_prefixes_and_former_names_left_alone():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    # other owners' variables, Ansible's own, and the former names (refused with their name now, not as unknown)
    names = ["ansible_user", "my_cassandra_thing", "cassandrax", "helper_x", "importer_x", "cassandra",
             "cassandra_new_nodes", "cassandra_status_from"]
    assert cassandra_unknown_extra_vars(names, PLAYBOOK_VARIABLES) == {}
    assert cassandra_unknown_extra_vars(None, PLAYBOOK_VARIABLES) == {}


def test_no_file_read_without_a_name_of_ours(monkeypatch):
    # the usual run: no name to look up, the roles' files are not read
    from ansible_collections.community.cassandra.plugins.filter import cassandra_names

    def read():
        raise AssertionError("read")
    monkeypatch.setattr(cassandra_names, "_role_variables", read)
    monkeypatch.setattr(cassandra_names, "used_names", read)
    assert cassandra_names.cassandra_unknown_extra_vars(["cassandra_hosts", "ansible_user", "cassandra_new_nodes"],
                                                        PLAYBOOK_VARIABLES) == {}


def test_the_secrets_import_cluster_moved_are_known():
    # cassandra_secret_<name>: a secret import_cluster took out of a config file's line, which refers to it
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_names
    names = ["cassandra_secret_keystore_password", "cassandra_secret_javax_net_ssl_truststorepassword_2"]
    assert cassandra_unknown_names(names, []) == {}
    assert cassandra_close_names(names, ["cassandra_secrets"]) == {}


def _names_read(top):
    """The cassandra_* names the .yml and .j2 files under top read outside comments, whatever their subdirectory."""
    from ansible_collections.community.cassandra.plugins.filter import cassandra_names
    names = set()
    for where, _dirs, files in os.walk(top):
        for name in files:
            if name.endswith((".yml", ".j2")):
                with open(os.path.join(where, name), encoding="utf-8") as f:
                    text = cassandra_names._COMMENT.sub("", f.read())
                names.update(n for n in cassandra_names._NAME.findall(text) if n.startswith("cassandra_"))
    return names


def test_every_name_the_playbooks_read_is_known():
    # a variable read only in a playbook's task file or template (playbooks/tasks, playbooks/templates) is no
    # unknown variable or typo, for the unknown-variable check nor for help (a filter they call added since rc1
    # is no variable: NOT_VARIABLES)
    from ansible_collections.community.cassandra.plugins.filter import cassandra_help
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import NOT_VARIABLES, cassandra_unknown_names
    read = _names_read(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "playbooks")) - NOT_VARIABLES
    assert cassandra_unknown_names(sorted(read), []) == {}
    assert read - cassandra_help._known_names() == set()


def test_every_name_rc1_knew_is_known():
    # a name 3.0.0-rc1 knew (a variable, or a filter's name its Jinja called) is not reported unknown now
    from ansible_collections.community.cassandra.plugins.filter import cassandra_help
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_names
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "known_names_rc1.txt"), encoding="utf-8") as f:
        rows = [line.rstrip("\n").split("\t") for line in f if not line.startswith("#")]
    assert len(rows) > 1000
    assert cassandra_unknown_names([name for name, where in rows if where != "help"], []) == {}
    assert sorted(set(name for name, where in rows if where != "names") - cassandra_help._known_names()) == []


def _rc1_names(where):
    """The names 3.0.0-rc1 knew (known_names_rc1.txt): where "names" (the unknown-variable check) or "help"."""
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "known_names_rc1.txt"), encoding="utf-8") as f:
        rows = [line.rstrip("\n").split("\t") for line in f if not line.startswith("#")]
    return set(name for name, tag in rows if tag in (where, "both"))


def _filter_names():
    """The name of every filter the collection registers."""
    import glob
    import importlib
    names = set()
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "plugins", "filter", "*.py")):
        module = os.path.basename(path)[:-3]
        if module != "__init__":
            plugin = importlib.import_module("ansible_collections.community.cassandra.plugins.filter." + module)
            names.update(plugin.FilterModule().filters())
    return names


HOW_TO_ADD = ("a new variable or fact: add its row to fixtures/known_names_rc1.txt; a word the playbooks write that is"
              " no variable (a new filter): add it to cassandra_names.NOT_VARIABLES (the fixture's header says how)")


def test_the_unknown_variable_check_knows_exactly_rc1s_names():
    # both ways: no name rc1 knew is unknown, no name rc1 reported unknown is let through
    from ansible_collections.community.cassandra.plugins.filter import cassandra_names
    known = cassandra_names._role_variables() | cassandra_names.used_names()
    assert sorted(name for name in known if cassandra_names._NAME.match(name)) == sorted(_rc1_names("names")), HOW_TO_ADD


def test_help_knows_exactly_rc1s_names():
    from ansible_collections.community.cassandra.plugins.filter import cassandra_help
    assert sorted(cassandra_help._known_names()) == sorted(_rc1_names("help")), HOW_TO_ADD


def test_each_check_reads_its_own_files(tmp_path, monkeypatch):
    # as rc1: the unknown-variable check reads the roles' files/ and only the .j2 templates, help's the other way
    from ansible_collections.community.cassandra.plugins.filter import cassandra_names
    for path, name in (("roles/r/files/f.yml", "cassandra_in_files"), ("roles/r/templates/t.sh", "cassandra_in_sh"),
                       ("roles/r/templates/sub/t.j2", "cassandra_in_j2"), ("roles/r/tasks/main.yml", "cassandra_in_tasks"),
                       ("playbooks/tasks/t.yml", "cassandra_in_playbook_tasks"),
                       ("playbooks/templates/t.sh.j2", "cassandra_in_playbook_templates")):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x: '{{ %s }}'  # cassandra_in_a_comment\n" % name)
    monkeypatch.setattr(cassandra_names, "_TOP", str(tmp_path))
    read = cassandra_names.used_names.__wrapped__
    both = {"cassandra_in_j2", "cassandra_in_tasks", "cassandra_in_playbook_tasks", "cassandra_in_playbook_templates"}
    assert read() - cassandra_names.RC1_NAMES_NOW_IN_PYTHON == both | {"cassandra_in_files"}
    help_only = {"cassandra_in_sh", "cassandra_in_a_comment"}
    assert read(for_help=True) - cassandra_names.RC1_NAMES_NOW_IN_PYTHON == both | help_only


def test_a_filter_added_since_rc1_is_no_variable():
    # rc1 took the name of each filter its playbooks called for a variable; a filter added since is not one
    from ansible_collections.community.cassandra.plugins.filter import cassandra_help
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    filters = _filter_names()
    assert len(filters) > 250
    added = sorted(filters - _rc1_names("names"))
    assert added and sorted(cassandra_unknown_extra_vars(added, [])) == added
    assert sorted(filters - _rc1_names("help")) == sorted(filters - cassandra_help._known_names())
    assert cassandra_unknown_extra_vars(sorted(filters & _rc1_names("names")), []) == {}


def test_a_filters_name_given_for_a_variable_is_refused_with_the_variable_close_to_it():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars
    assert cassandra_unknown_extra_vars(["cassandra_seed_hosts"], []) == {"cassandra_seed_hosts": "cassandra_seeds"}


def test_a_comment_pasted_into_a_key_names_the_variable_its_last_word_makes():
    """(2026-11-26) "cassandra_# Logging (logback.xml)log_console": a comment pasted into the key; the run left
    cassandra_log_console at its default. The warning names it, and says it keeps its default."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import (
        cassandra_unknown_names, cassandra_unknown_note)
    name = "cassandra_# Logging (logback.xml)log_console"
    assert cassandra_unknown_names([name, "cassandra_# a note)frobnicate"], []) == {
        name: "cassandra_log_console", "cassandra_# a note)frobnicate": ""}
    note = cassandra_unknown_note(name, "cassandra_log_console", "all 3 nodes").split("\n")
    assert note == ["WARNING  probably a typo in a variable name, not known to this collection:",
                    "         cassandra_# Logging (logback.xml)log_console",
                    "         set for: all 3 nodes",
                    "         no role or playbook reads it",
                    "         closest known variable: cassandra_log_console",
                    "         cassandra_log_console not set: its default applies meanwhile"]
    assert all(len(line) <= 79 for line in note)
    assert cassandra_unknown_note("cassandra_x", "", "n1").rsplit("\n", maxsplit=1)[-1] == "         no role or playbook reads it"
