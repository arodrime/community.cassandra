from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import jinja2
import pytest
import yaml

from ansible.errors import AnsibleFilterError
from ansible.plugins.filter.core import FilterModule as CoreFilters

from ansible_collections.community.cassandra.plugins.filter.cassandra_medusa import (
    cassandra_medusa_ini,
    cassandra_medusa_ini_changes,
)

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_medusa")

FACTS = {"os_family": "RedHat"}


def defaults(variables=None, facts=None):
    """The role's defaults, their expressions resolved, then the variables."""
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        ctx = yaml.safe_load(f)
    ctx.update(variables or {})
    ctx.update({"ansible_facts": facts or FACTS})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for dummy in range(3):  # defaults built from other defaults
        for key, value in list(ctx.items()):
            if isinstance(value, str) and "{{" in value and key not in (variables or {}):
                try:
                    ctx[key] = env.from_string(value).render(ctx)
                except jinja2.UndefinedError:
                    pass
    return ctx


def render(variables=None, facts=None):
    """medusa.ini as the role writes it with these variables."""
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    env.filters["bool"] = CoreFilters().filters()["bool"]  # as in Ansible
    with open(os.path.join(ROLE, "templates", "medusa.ini.j2")) as f:
        return env.from_string(f.read()).render(defaults(variables, facts))


def test_ini_parse():
    assert cassandra_medusa_ini("[storage]\nBucket_Name = b\n; c\n") == {"storage": {"bucket_name": "b"}}
    assert cassandra_medusa_ini("not an ini") == {}


def test_changes_ignore_comments_order_spacing_and_boolean_case():
    old = "[storage]\n; comment\nsecure=true\nbucket_name = b\n"
    new = "# other\n[storage]\nbucket_name=b\nsecure = True\n"
    assert cassandra_medusa_ini_changes(old, new) == []


def test_changes_masked():
    old = "[cassandra]\ncql_password = a\nsstableloader_kspw = x\n[storage]\nbucket_name = b\n"
    new = "[cassandra]\ncql_password = b\n[storage]\nbucket_name = c\nprefix = p\n"
    assert cassandra_medusa_ini_changes(old, new) == [
        {"item": "medusa.ini [cassandra] cql_password", "before": "****", "after": "****"},
        {"item": "medusa.ini [cassandra] sstableloader_kspw", "before": "****", "after": "absent"},
        {"item": "medusa.ini [storage] bucket_name", "before": "b", "after": "c"},
        {"item": "medusa.ini [storage] prefix", "before": "absent", "after": "p"},
    ]


def test_changes_new_file_and_unreadable_old():
    assert cassandra_medusa_ini_changes("", "[storage]\nbucket_name = b\n") == [
        {"item": "medusa.ini [storage] bucket_name", "before": "absent", "after": "b"}]
    assert cassandra_medusa_ini_changes("garbage", "[storage]\nbucket_name = b\n", "credentials") == [
        {"item": "credentials", "before": "not valid INI", "after": "rewritten"}]


def test_changes_invalid_new_hides_the_content():
    with pytest.raises(AnsibleFilterError) as err:
        cassandra_medusa_ini_changes("", "cql_password = hunter2")
    assert "hunter2" not in str(err.value)


def test_role_defaults_give_a_valid_medusa_ini():
    rendered = cassandra_medusa_ini(render({"cassandra_medusa_storage_provider": "local",
                                            "cassandra_medusa_base_path": "/backups"}))
    assert rendered == {
        "cassandra": {"config_file": "/etc/cassandra/conf/cassandra.yaml", "nodetool_port": "7199",
                      "resolve_ip_addresses": "True"},
        "storage": {"storage_provider": "local", "bucket_name": "cassandra_backups", "base_path": "/backups",
                    "max_backup_age": "0", "max_backup_count": "0", "transfer_max_bandwidth": "50MB/s",
                    "concurrent_transfers": "1", "multi_part_upload_threshold": "20MB",
                    "backup_grace_period_in_days": "10", "use_sudo_for_restore": "True"},
        "monitoring": {}, "ssh": {}, "checks": {}, "logging": {}, "grpc": {}, "kubernetes": {},
    }


def test_stack_variables_by_default():
    rendered = cassandra_medusa_ini(render({
        "cassandra_medusa_storage_provider": "s3", "cassandra_jmx_port": 7299,
        "cassandra_conf_dir": "/srv/cassandra/conf"}))["cassandra"]
    assert rendered == {"config_file": "/srv/cassandra/conf/cassandra.yaml", "nodetool_port": "7299",
                        "resolve_ip_addresses": "True"}


@pytest.mark.parametrize("variables, owner, group", [
    ({}, "cassandra", "cassandra"),
    ({"cassandra_user": "dbuser", "cassandra_group": "dbgroup"}, "dbuser", "dbgroup"),
])
def test_files_owned_by_the_cassandra_account(variables, owner, group):
    ctx = defaults(variables)
    assert (ctx["cassandra_medusa_config_owner"], ctx["cassandra_medusa_config_group"]) == (owner, group)
    assert (ctx["cassandra_medusa_key_file_owner"], ctx["cassandra_medusa_key_file_group"]) == (owner, group)
    assert ctx["cassandra_medusa_config_mode"] == ctx["cassandra_medusa_key_file_mode"] == "0600"


def test_credentials_masked():
    old = "[default]\naws_access_key_id = A\naws_secret_access_key = S\n"
    new = "[default]\naws_access_key_id = B\naws_secret_access_key = T\n"
    assert cassandra_medusa_ini_changes(old, new, "credentials") == [
        {"item": "credentials [default] aws_access_key_id", "before": "****", "after": "****"},
        {"item": "credentials [default] aws_secret_access_key", "before": "****", "after": "****"},
    ]


@pytest.mark.parametrize("value, written", [(False, "False"), ("false", "False"), ("no", "False"), (True, "True")])
def test_resolve_ip_addresses_as_medusa_compares_it(value, written):
    # Medusa skips the name resolution only for "False", written so
    rendered = cassandra_medusa_ini(render({"cassandra_medusa_storage_provider": "s3",
                                            "cassandra_medusa_resolve_ip_addresses": value}))
    assert rendered["cassandra"]["resolve_ip_addresses"] == written


@pytest.mark.parametrize("value", [True, "True", "true"])
def test_nodetool_ssl_in_lower_case(value):
    # Medusa compares it with "true"
    rendered = cassandra_medusa_ini(render({"cassandra_medusa_storage_provider": "s3",
                                            "cassandra_medusa_nodetool_ssl": value}))
    assert rendered["cassandra"]["nodetool_ssl"] == "true"


def test_changes_case_of_the_keys_medusa_compares_as_written():
    old = "[cassandra]\nresolve_ip_addresses = false\nnodetool_ssl = True\nuse_sudo = false\n"
    new = "[cassandra]\nresolve_ip_addresses = False\nnodetool_ssl = true\nuse_sudo = False\n"
    assert [c["item"] for c in cassandra_medusa_ini_changes(old, new)] == [
        "medusa.ini [cassandra] nodetool_ssl", "medusa.ini [cassandra] resolve_ip_addresses"]


def test_logins():
    rendered = cassandra_medusa_ini(render({
        "cassandra_medusa_storage_provider": "s3", "cassandra_medusa_cql_username": "u",
        "cassandra_medusa_cql_password": "p", "cassandra_medusa_nodetool_username": "j",
        "cassandra_medusa_nodetool_password": "jp"}))["cassandra"]
    assert (rendered["cql_username"], rendered["cql_password"]) == ("u", "p")
    assert (rendered["nodetool_username"], rendered["nodetool_password"]) == ("j", "jp")


def test_password_file_wins_over_password():
    rendered = cassandra_medusa_ini(render({
        "cassandra_medusa_storage_provider": "s3", "cassandra_medusa_nodetool_username": "j",
        "cassandra_medusa_nodetool_password": "jp",
        "cassandra_medusa_nodetool_password_file": "/etc/cassandra/jmxremote.password"}))["cassandra"]
    assert "nodetool_password" not in rendered
    assert rendered["nodetool_password_file_path"] == "/etc/cassandra/jmxremote.password"


def test_extra_settings_added_and_override():
    rendered = cassandra_medusa_ini(render({
        "cassandra_medusa_storage_provider": "s3",
        "cassandra_medusa_extra_settings": {"storage": {"bucket_name": "other", "kms_id": "k"},
                                            "ssh": {"username": "ops"}}}))
    assert rendered["storage"]["bucket_name"] == "other"
    assert rendered["storage"]["kms_id"] == "k"
    assert rendered["ssh"] == {"username": "ops"}
