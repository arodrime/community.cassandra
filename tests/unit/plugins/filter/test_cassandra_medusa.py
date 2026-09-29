from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import jinja2
import pytest
import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_medusa import (
    ROLE,
    cassandra_medusa_import,
    cassandra_medusa_ini,
    cassandra_medusa_ini_changes,
)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_inventory_files,
    cassandra_inventory_layout,
)

FACTS = {"os_family": "RedHat"}
FOUND = {"venv": "/opt/cassandra-medusa", "version": "0.30.1", "link_dir": "/usr/local/bin", "package": "",
         "bin": "/opt/cassandra-medusa/bin/medusa", "ini_owner": "cassandra", "ini_group": "cassandra",
         "ini_mode": "600"}

# medusa.ini as found on a node set up by hand (comments, its own order)
HAND = """; set up by hand
[cassandra]
config_file = /etc/cassandra/conf/cassandra.yaml
cql_username = medusa
cql_password = cql-secret
nodetool_username = cassops
nodetool_password_file_path = /etc/cassandra/jmxremote.password
resolve_ip_addresses = False

[storage]
storage_provider = s3_compatible
bucket_name = orders-backups
key_file = /etc/medusa/credentials
host = s3.example.com
port = 443
secure = True
prefix = orders
max_backup_count = 14
transfer_max_bandwidth = 200MB/s
concurrent_transfers = 4
multi_part_upload_threshold = 100MB
use_sudo_for_restore = True

[checks]
enable_md5_checks = true

[logging]
enabled = 1
file = /var/log/medusa/medusa.log
maxBytes = 20000000
"""
CREDENTIALS = "[default]\naws_access_key_id = AKIAEXAMPLE\naws_secret_access_key = s3-secret/example\n"


def render(variables=None, facts=None):
    """medusa.ini as the role writes it with these variables (the role's
    defaults, their expressions resolved, then the variables)."""
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        ctx = yaml.safe_load(f)
    ctx.update(variables or {})
    ctx.update({"ansible_facts": facts or FACTS})
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    for dummy in range(3):  # defaults built from other defaults
        for key, value in list(ctx.items()):
            if isinstance(value, str) and "{{" in value and key not in (variables or {}):
                try:
                    ctx[key] = env.from_string(value).render(ctx)
                except jinja2.UndefinedError:
                    pass
    with open(os.path.join(ROLE, "templates", "medusa.ini.j2")) as f:
        return env.from_string(f.read()).render(ctx)


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
                      "resolve_ip_addresses": "true"},
        "storage": {"storage_provider": "local", "bucket_name": "cassandra_backups", "base_path": "/backups",
                    "max_backup_age": "0", "max_backup_count": "0", "transfer_max_bandwidth": "50MB/s",
                    "concurrent_transfers": "1", "multi_part_upload_threshold": "20MB",
                    "backup_grace_period_in_days": "10", "use_sudo_for_restore": "true"},
        "monitoring": {}, "ssh": {}, "checks": {}, "logging": {}, "grpc": {}, "kubernetes": {},
    }


def test_collection_logins_by_default():
    rendered = cassandra_medusa_ini(render({
        "cassandra_medusa_storage_provider": "s3", "cassandra_cql_username": "u", "cassandra_cql_password": "p",
        "cassandra_jmx_username": "j", "cassandra_jmx_password": "jp", "cassandra_jmx_port": 7299,
        "cassandra_conf_dir": "/srv/cassandra/conf"}))["cassandra"]
    assert rendered == {"config_file": "/srv/cassandra/conf/cassandra.yaml", "cql_username": "u", "cql_password": "p",
                        "nodetool_username": "j", "nodetool_password": "jp", "nodetool_port": "7299",
                        "resolve_ip_addresses": "true"}


def test_password_file_wins_over_password():
    rendered = cassandra_medusa_ini(render({
        "cassandra_medusa_storage_provider": "s3", "cassandra_jmx_username": "j", "cassandra_jmx_password": "jp",
        "cassandra_jmx_password_file": "/etc/cassandra/jmxremote.password"}))["cassandra"]
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


def test_import_then_render_changes_nothing():
    """The import no-op rule: the role writes the same settings back."""
    imported = cassandra_medusa_import(HAND, CREDENTIALS, FOUND)
    assert cassandra_medusa_ini_changes(HAND, render(imported["vars"])) == []


def test_import_variables():
    imported = cassandra_medusa_import(HAND, CREDENTIALS, FOUND)
    assert imported["vars"] == {
        "cassandra_medusa_enabled": True,
        "cassandra_medusa_version": "0.30.1",
        "cassandra_medusa_cassandra_config_file": "/etc/cassandra/conf/cassandra.yaml",
        "cassandra_medusa_cql_username": "medusa",
        "cassandra_medusa_cql_password": "cql-secret",
        "cassandra_medusa_nodetool_username": "cassops",
        "cassandra_medusa_nodetool_password": "",
        "cassandra_medusa_nodetool_password_file": "/etc/cassandra/jmxremote.password",
        "cassandra_medusa_nodetool_port": "",
        "cassandra_medusa_resolve_ip_addresses": "False",
        "cassandra_medusa_storage_provider": "s3_compatible",
        "cassandra_medusa_bucket_name": "orders-backups",
        "cassandra_medusa_key_file": "/etc/medusa/credentials",
        "cassandra_medusa_s3_access_key_id": "AKIAEXAMPLE",
        "cassandra_medusa_s3_secret_access_key": "s3-secret/example",
        "cassandra_medusa_host": "s3.example.com",
        "cassandra_medusa_port": "443",
        "cassandra_medusa_secure": "True",
        "cassandra_medusa_prefix": "orders",
        "cassandra_medusa_fqdn": "",  # none: Medusa's own, not <hostname>.<cassandra_medusa_fqdn_domain>
        "cassandra_medusa_max_backup_age": "",
        "cassandra_medusa_max_backup_count": "14",
        "cassandra_medusa_transfer_max_bandwidth": "200MB/s",
        "cassandra_medusa_concurrent_transfers": "4",
        "cassandra_medusa_multi_part_upload_threshold": "100MB",
        "cassandra_medusa_backup_grace_period_in_days": "",
        "cassandra_medusa_logging_enabled": "1",
        "cassandra_medusa_logging_file": "/var/log/medusa/medusa.log",
        "cassandra_medusa_logging_max_bytes": "20000000",
        "cassandra_medusa_extra_settings": {"checks": {"enable_md5_checks": "true"}},
    }
    assert imported["notes"] == ["Medusa 0.30.1 in /opt/cassandra-medusa, medusa.ini imported (cassandra_medusa_enabled: true)"]


def test_import_secrets_go_to_secrets_yml():
    imported = cassandra_medusa_import(HAND, CREDENTIALS, FOUND)
    files = cassandra_inventory_files({"group_vars": {"c": imported["vars"]}, "host_vars": {}})
    secrets = yaml.safe_load([f for f in files if f["secret"]][0]["content"])
    public = yaml.safe_load([f for f in files if not f["secret"]][0]["content"])
    assert sorted(secrets) == ["cassandra_medusa_cql_password", "cassandra_medusa_s3_access_key_id",
                               "cassandra_medusa_s3_secret_access_key"]
    assert "cassandra_medusa_nodetool_password" in public  # "" is no secret


def test_import_venv_from_a_login_profile():
    """A virtualenv of its own, activated from a login profile, without a link:
    new nodes get the same path, plus the default link; the profile is reported."""
    found = dict(FOUND, venv="/srv/tools/cassandra-virtual", link_dir="", profile="/home/ops/.bash_profile",
                 bin="/srv/tools/cassandra-virtual/bin/medusa")
    imported = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, found)
    assert imported["vars"]["cassandra_medusa_venv"] == "/srv/tools/cassandra-virtual"
    assert "cassandra_medusa_link_dir" not in imported["vars"]
    assert imported["notes"][0] == ("Medusa 0.30.1 in /srv/tools/cassandra-virtual, medusa.ini imported"
                                    " (cassandra_medusa_enabled: true)")
    assert "no /usr/local/bin/medusa link" in imported["notes"][1]
    assert "by /home/ops/.bash_profile (login profile)" in imported["notes"][2]


def test_import_profile_d_of_the_role():
    out = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, dict(FOUND, profile_d="yes"))
    assert out["vars"]["cassandra_medusa_profile_d"] is True
    assert not [n for n in out["notes"] if "login profile" in n]


def test_import_link_elsewhere_kept():
    found = dict(FOUND, venv="/opt/medusa", link_dir="/usr/bin")
    out = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, found)["vars"]
    assert out["cassandra_medusa_venv"] == "/opt/medusa"
    assert out["cassandra_medusa_link_dir"] == "/usr/bin"


def test_import_outside_a_venv():
    """pip into a system Python: new nodes get it the same way (no virtualenv)."""
    found = dict(FOUND, venv="", link_dir="", bin="/usr/local/bin/medusa", python="/usr/bin/python3.11")
    imported = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, found)
    assert imported["vars"]["cassandra_medusa_enabled"] is True
    assert imported["vars"]["cassandra_medusa_version"] == "0.30.1"
    assert imported["vars"]["cassandra_medusa_venv"] == ""
    assert imported["vars"]["cassandra_medusa_python"] == "/usr/bin/python3.11"
    assert "cassandra_medusa_link_dir" not in imported["vars"]
    assert "not in a virtualenv (Python /usr/bin/python3.11)" in imported["notes"][1]


@pytest.mark.parametrize("python, imported", [
    ("/usr/bin/python3", "/usr/bin/python3"),  # maybe not the role's pick (outside the supported versions)
    ("python3.11", None),  # env shebang: depends on the PATH
    ("", None),
])
def test_import_outside_a_venv_python(python, imported):
    found = dict(FOUND, venv="", link_dir="", bin="/usr/local/bin/medusa", python=python)
    out = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, found)["vars"]
    assert out["cassandra_medusa_venv"] == ""
    assert out.get("cassandra_medusa_python") == imported


def test_import_untrusted_not_run():
    found = dict(FOUND, version="", untrusted="yes", bin="/home/ops/v/bin/medusa", venv="/home/ops/v")
    imported = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, found)
    assert "cassandra_medusa_enabled" not in imported["vars"]
    assert "owned by neither root nor cassandra" in imported["notes"][0]


def test_import_venv_python_not_imported():
    out = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, dict(FOUND, python="/usr/bin/python3.11"))
    assert "cassandra_medusa_python" not in out["vars"]


def test_import_package_install_not_enabled():
    found = dict(FOUND, package="cassandra-medusa")
    imported = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, found)
    assert "cassandra_medusa_enabled" not in imported["vars"]
    assert "package cassandra-medusa" in imported["notes"][0]


def test_import_no_install_found():
    imported = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, {})
    assert "cassandra_medusa_enabled" not in imported["vars"]
    assert "no Medusa install found" in imported["notes"][0]


@pytest.mark.parametrize("credentials", [
    None,
    "",
    "[default]\naws_access_key_id = A\naws_secret_access_key = S\nregion = eu\n",  # more than the keys
    "[default]\naws_access_key_id = A\naws_secret_access_key = S\n[other]\naws_access_key_id = B\n",
    '{"type": "service_account"}',  # a GCS key
])
def test_import_key_file_left_alone(credentials):
    imported = cassandra_medusa_import("[storage]\nstorage_provider = s3\nkey_file = /etc/medusa/k\n", credentials, FOUND)
    assert "cassandra_medusa_s3_access_key_id" not in imported["vars"]
    assert imported["vars"]["cassandra_medusa_key_file"] == "/etc/medusa/k"
    assert "kept as it is" in imported["notes"][-1]


def test_import_credentials_of_the_api_profile():
    ini = "[storage]\nstorage_provider = s3\nkey_file = /etc/medusa/k\napi_profile = backup\n"
    creds = "[backup]\naws_access_key_id = A\naws_secret_access_key = S\n"
    out = cassandra_medusa_import(ini, creds, FOUND)["vars"]
    assert out["cassandra_medusa_s3_access_key_id"] == "A"
    assert out["cassandra_medusa_api_profile"] == "backup"


def test_import_password_and_password_file_both_kept():
    ini = ("[cassandra]\nnodetool_password = pw\nnodetool_password_file_path = /etc/f\n"
           "[storage]\nstorage_provider = s3\n")
    imported = cassandra_medusa_import(ini, None, FOUND)
    assert imported["vars"]["cassandra_medusa_extra_settings"] == {"cassandra": {"nodetool_password": "pw"}}
    assert cassandra_medusa_ini_changes(ini, render(imported["vars"])) == []


def test_import_empty_value_kept():
    ini = "[storage]\nstorage_provider = s3\nprefix =\n"
    imported = cassandra_medusa_import(ini, None, FOUND)
    assert cassandra_medusa_ini_changes(ini, render(imported["vars"])) == []


def test_booleans_in_lower_case():
    """Medusa compares nodetool_ssl with "true": a YAML true must give that."""
    rendered = cassandra_medusa_ini(render({"cassandra_medusa_storage_provider": "s3",
                                            "cassandra_medusa_nodetool_ssl": True,
                                            "cassandra_medusa_extra_settings": {"grpc": {"enabled": False}}}))
    assert rendered["cassandra"]["nodetool_ssl"] == "true"
    assert rendered["grpc"]["enabled"] == "false"


def test_import_key_file_owner_and_mode_apart():
    """A key file stricter than medusa.ini keeps its own owner and mode."""
    found = dict(FOUND, ini_owner="root", ini_group="root", ini_mode="644",
                 key_owner="root", key_group="root", key_mode="0600")
    out = cassandra_medusa_import(HAND, CREDENTIALS, found)["vars"]
    assert (out["cassandra_medusa_config_owner"], out["cassandra_medusa_config_mode"]) == ("root", "0644")
    assert out["cassandra_medusa_key_file_mode"] == "0600"
    assert "cassandra_medusa_key_file_owner" not in out  # same as medusa.ini's


def test_import_key_file_same_as_config():
    found = dict(FOUND, key_owner="cassandra", key_group="cassandra", key_mode="0600")
    out = cassandra_medusa_import(HAND, CREDENTIALS, found)["vars"]
    assert not [k for k in out if k.startswith("cassandra_medusa_key_file_") or k.startswith("cassandra_medusa_config_")]


def test_import_owner_and_mode():
    found = dict(FOUND, ini_owner="root", ini_group="root", ini_mode="640")
    out = cassandra_medusa_import("[storage]\nstorage_provider = s3\n", None, found)["vars"]
    assert (out["cassandra_medusa_config_owner"], out["cassandra_medusa_config_group"],
            out["cassandra_medusa_config_mode"]) == ("root", "root", "0640")


def test_import_invalid_ini():
    imported = cassandra_medusa_import("cql_password = hunter2", None, FOUND)
    assert imported["vars"] == {}
    assert "not valid INI" in imported["notes"][0]
    assert "hunter2" not in imported["notes"][0]


def fqdn_nodes(*pairs):
    """Nodes that read a medusa.ini with fqdn = value, on a host of that short hostname."""
    ini = "[storage]\nstorage_provider = s3\n%s"
    return [{"name": "n%d" % i, "hostname": hostname, "dc": "dc1", "rack": "r1", "read": True,
             "hand_edits": [], "normalized": [], "notes": [],
             "vars": cassandra_medusa_import(ini % ("fqdn = %s\n" % fqdn if fqdn is not None else ""), None, FOUND)["vars"]}
            for i, (hostname, fqdn) in enumerate(pairs, 1)]


def fqdn_written(layout, node):
    """The fqdn the role writes on that node with the imported inventory."""
    variables = dict(layout["group_vars"]["c"])
    for group in ("c_dc1", "c_dc1_r1"):
        variables.update(layout["group_vars"].get(group, {}))
    variables.update(layout["host_vars"].get(node["name"], {}))
    return cassandra_medusa_ini(render(variables, dict(FACTS, hostname=node["hostname"])))["storage"].get("fqdn")


@pytest.mark.parametrize("pairs, domain", [
    # one rule gives every node's value: kept as the domain, no host_vars
    ([("node1", "node1.int.example"), ("node2", "node2.int.example"), ("node3", "node3.int.example")], "int.example"),
    # anything else: each node keeps its own value
    ([("node1", "node1.a.example"), ("node2", "node2.b.example")], None),
    ([("node1", "node1"), ("node2", "node2")], None),
    ([("node1", "node1.int.example"), ("node2", "other.int.example")], None),
    ([("node1", "node1.int.example"), ("node2", None)], None),  # no fqdn: Medusa's own
    ([("Node1", "node1.int.example"), ("node2", "node2.int.example")], None),  # not byte for byte
    ([("", "node1.int.example"), ("node2", "node2.int.example")], None),  # no hostname fact
    ([("node1", "node1."), ("node2", "node2.")], None),
])
def test_import_medusa_fqdn_exactly(pairs, domain):
    nodes = fqdn_nodes(*pairs)
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["group_vars"]["c"].get("cassandra_medusa_fqdn_domain") == domain
    if domain:
        assert all("cassandra_medusa_fqdn" not in v for v in layout["host_vars"].values())
        assert "<short hostname>.%s on every node" % domain in layout["report"]
    else:
        assert "no <short hostname>.<domain> rule" in layout["report"]
    # the round trip: every node gets its value back, byte for byte
    for node, (hostname, fqdn) in zip(nodes, pairs):
        assert fqdn_written(layout, node) == fqdn
    assert "fqdn" not in layout["differences"]  # the node's own name: no difference


def test_new_node_gets_the_domain_rule():
    layout = cassandra_inventory_layout(fqdn_nodes(("node1", "node1.int.example"), ("node2", "node2.int.example")), "c")
    assert fqdn_written(layout, {"name": "node9", "hostname": "node9"}) == "node9.int.example"


GUARD = next(t for t in yaml.safe_load(open(os.path.join(ROLE, "tasks", "main.yml")))
             if t.get("name") == "Keep this node's name in the backups")


@pytest.mark.parametrize("current, old, new, change, ok", [
    (None, "", "node1.x", False, True),  # no medusa.ini yet
    ("ini", "node1.x", "node1.x", False, True),
    ("ini", "", "", False, True),
    ("ini", "node1.x", "node1.y", False, False),
    ("ini", "", "node1.x", False, False),  # Medusa's own name, maybe another one
    ("ini", "node1.x", "", False, False),
    ("ini", "node1.x", "node1.y", True, True),
])
def test_fqdn_change_refused(current, old, new, change, ok):
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    variables = {"cassandra_medusa_ini_current": {"content": current} if current else {"failed": True},
                 "_old": old, "_new": new, "cassandra_medusa_fqdn_change": change}
    that = trust_as_template("{{ " + GUARD["ansible.builtin.assert"]["that"] + " }}")
    assert Templar(loader=DataLoader(), variables=variables).template(that) is ok
    # the fqdn of the current file, and the one the template writes
    assert "cassandra_medusa_ini" in GUARD["vars"]["_old"] and "medusa.ini.j2" in GUARD["vars"]["_new"]


def test_no_fqdn_anywhere_no_medusa_line():
    layout = cassandra_inventory_layout(fqdn_nodes(("node1", None), ("node2", None)), "c")
    assert "Medusa fqdn" not in layout["report"]
    assert layout["group_vars"]["c"]["cassandra_medusa_fqdn"] == ""
