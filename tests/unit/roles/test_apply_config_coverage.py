from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config ignores no setting: every variable of the roles (their defaults and argument specs) has its
# category in plugin_utils cassandra_apply_config_coverage (A live, B restart, C procedure, D refused, - not a node
# setting). A new variable without one fails here.

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_apply_config_coverage as coverage

ROLES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")
MAP = "plugins/plugin_utils/cassandra_apply_config_coverage.py"


def role_variables():
    """{variable: [roles]} of every role's defaults and argument spec (not the private _ ones)."""
    out = {}
    for role in sorted(os.listdir(ROLES_DIR)):
        names = set()
        for path, get in (("defaults/main.yml", lambda d: d),
                          ("meta/argument_specs.yml", lambda d: d["argument_specs"]["main"].get("options", {}))):
            full = os.path.join(ROLES_DIR, role, path)
            if os.path.isfile(full):
                with open(full, encoding="utf-8") as f:
                    names.update(get(yaml.safe_load(f) or {}) or {})
        for name in names:
            if not name.startswith("_"):
                out.setdefault(name, []).append(role)
    return out


VARIABLES = role_variables()


def test_every_variable_of_the_roles_has_its_category():
    missing = sorted("%s (%s)" % (name, ", ".join(roles)) for name, roles in VARIABLES.items()
                     if coverage.category(name) is None)
    assert missing == [], (
        "These variables have no category for apply_config: add each one to the row of its class in %s (COVERAGE),"
        " or a new row: A live, B restart, C a procedure of its own, D refused, - not a node setting:\n  %s"
        % (MAP, "\n  ".join(missing)))


def test_the_map_has_no_row_that_matches_nothing():
    # (a row for a variable gone, or a pattern mistyped: the map would say more than the roles have)
    names = list(VARIABLES)
    for patterns, cat, what in coverage.COVERAGE:
        assert cat in coverage.CATEGORIES and what
        for p in patterns:
            assert any(coverage._matches(p, n) for n in names), "%s matches no variable of the roles" % p


@pytest.mark.parametrize("name, expected", [
    ("cassandra_cluster_name", "D"), ("cassandra_num_tokens", "D"), ("cassandra_dc", "D"), ("cassandra_rack", "D"),
    ("cassandra_partitioner", "D"), ("cassandra_initial_token", "D"),
    ("cassandra_version", "C"), ("cassandra_node_state", "C"),
    ("cassandra_package_version", "B"), ("cassandra_java_version", "B"), ("cassandra_java_home", "B"),
    ("cassandra_service_limit_nofile", "B"), ("cassandra_service_environment", "B"), ("cassandra_heap_size", "B"),
    ("cassandra_concurrent_reads", "B"), ("cassandra_log_level", "B"),
    ("cassandra_install_jemalloc", "A"), ("cassandra_install_tools", "A"), ("cassandra_cqlsh_python", "A"),
    ("cassandra_firewall_enabled", "A"), ("open_ports", "A"), ("cassandra_repository_key_url", "A"),
    ("cassandra_linux_swap_off", "A"), ("cassandra_medusa_version", "A"), ("cassandra_config_mode", "A"),
    ("cassandra_rolling_resume", "-"), ("cassandra_jmx_password", "-"), ("cassandra_offline", "-"),
])
def test_the_categories_of_the_owner_s_classes(name, expected):
    assert name in VARIABLES
    assert coverage.category(name)[0] == expected


def test_a_name_no_row_has_is_told():
    assert coverage.category("cassandra_zorglub_new_setting") is None


def test_the_service_state_and_boot_are_run_options_said_so():
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_apply_config_coverage import category
    for name in ("cassandra_service_enabled", "cassandra_service_state"):
        kind, said = category(name)
        assert "start_rack, stop_rack change it" in said, name
