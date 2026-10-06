from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Owner, group and mode of the files cassandra_config writes: what the role
# sets, what import_cluster reads back, what its self-check compares.

import os

import pytest
import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import (
    DEFAULTS as IMPORT_DEFAULTS,
    ROLE,
    cassandra_file_permissions,
    cassandra_permissions_import,
    mode_text,
    cassandra_permission_changes,
    permission_differences,
)

CONFIG_50 = ["cassandra.yaml", "cassandra-env.sh", "jvm-server.options", "jvm11-server.options", "jvm17-server.options",
             "cassandra-rackdc.properties", "logback.xml"]
DEFAULTS = {"owner": "root", "group": "cassandra", "mode": "0640", "public_mode": "0644", "files": {},
            "user": "cassandra", "user_group": "cassandra"}


def st(owner, group, mode, uid=1000, gid=1000):
    return {"exists": True, "pw_name": owner, "gr_name": group, "mode": mode, "uid": uid, "gid": gid}


def node(owner, group, mode, **per_file):
    """The config files of a 5.0 node, all owner:group mode but the ones given."""
    out = dict((name, st(owner, group, mode)) for name in CONFIG_50)
    out.update(per_file)
    return out


@pytest.mark.parametrize("given, text", [("0640", "0640"), ("640", "0640"), (0o644, "0644"), ("00600", "0600"),
                                         ("u=rw,g=r", "u=rw,g=r")])
def test_mode_text(given, text):
    assert mode_text(given) == text


def test_role_defaults_per_file():
    # passwords may be there (keystores, the JVM options' extra options)
    for name in ("cassandra.yaml", "jvm-server.options", "jvm8-server.options", "jvm11-server.options",
                 "jvm17-server.options"):
        assert cassandra_file_permissions(name, DEFAULTS) == {"owner": "root", "group": "cassandra", "mode": "0640"}
    for name in ("cassandra-env.sh", "cassandra-rackdc.properties", "logback.xml"):
        # no secret there: nodetool, run by any user, reads the JMX port in cassandra-env.sh
        assert cassandra_file_permissions(name, DEFAULTS) == {"owner": "root", "group": "cassandra", "mode": "0644"}
    for name in ("jmxremote.password", "jmxremote.access"):
        assert cassandra_file_permissions(name, dict(DEFAULTS, user="dbsvc", user_group="dbgrp")) == {
            "owner": "dbsvc", "group": "dbgrp", "mode": "0400"}


def test_per_file_settings_win():
    settings = dict(DEFAULTS, files={"logback.xml": {"mode": "0600"}, "jmxremote.password": {"mode": 0o600}})
    assert cassandra_file_permissions("logback.xml", settings) == {"owner": "root", "group": "cassandra", "mode": "0600"}
    assert cassandra_file_permissions("jmxremote.password", settings)["mode"] == "0600"
    with pytest.raises(AnsibleFilterError, match="must map to owner, group and/or mode"):
        cassandra_file_permissions("logback.xml", dict(DEFAULTS, files={"logback.xml": {"mod": "0600"}}))


@pytest.mark.parametrize("files", [{"logbak.xml": {"mode": "0600"}}, {"cassandra.yml": {}}, ["logback.xml"]])
def test_per_file_settings_of_another_file_refused(files):
    with pytest.raises(AnsibleFilterError, match="cassandra_config_file_permissions"):
        cassandra_file_permissions("cassandra.yaml", dict(DEFAULTS, files=files))


@pytest.mark.parametrize("mode", ["u=rw,g=r", "rw-r-----", "99"])
def test_mode_not_octal_refused(mode):
    # stat gives octal: anything else would never compare equal
    with pytest.raises(AnsibleFilterError, match="give it in octal"):
        cassandra_file_permissions("cassandra.yaml", dict(DEFAULTS, mode=mode))


def test_defaults_are_the_roles():
    """The import writes a variable when the node differs from these: they must be the role's."""
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        role = yaml.safe_load(f)
    assert (role["cassandra_user"], role["cassandra_group"], role["cassandra_config_user"], role["cassandra_config_group"],
            role["cassandra_config_mode"], role["cassandra_config_public_mode"]) == (
        IMPORT_DEFAULTS["user"], IMPORT_DEFAULTS["group"], IMPORT_DEFAULTS["owner"], "{{ cassandra_group }}",
        IMPORT_DEFAULTS["mode"], IMPORT_DEFAULTS["public_mode"])


def test_jmx_password_file_kept_readable():
    """A node whose JVM does not read it (local JMX, JAAS) may have it readable: kept as it is."""
    files = node("root", "cassandra", "0640", **{"cassandra-env.sh": st("root", "cassandra", "0644"),
                                                 "cassandra-rackdc.properties": st("root", "cassandra", "0644"),
                                                 "logback.xml": st("root", "cassandra", "0644"),
                                                 "jmxremote.password": st("cassandra", "cassandra", "0644"),
                                                 "jmxremote.access": st("cassandra", "cassandra", "0400")})
    out = cassandra_permissions_import(files, {"user": "cassandra", "group": "cassandra"}, "50x")
    assert out["vars"] == {"cassandra_config_file_permissions": {"jmxremote.password": {"mode": "0644"}}}
    settings = dict(DEFAULTS, files=out["vars"]["cassandra_config_file_permissions"])
    assert cassandra_file_permissions("jmxremote.password", settings)["mode"] == "0644"


def test_a_stat_not_read_is_a_difference():
    out = cassandra_permissions_import({"cassandra.yaml": {}, "logback.xml": {"exists": False}},
                                       {"user": "cassandra", "group": "cassandra"}, "50x")
    assert out["files"] == {"cassandra.yaml": None}
    assert permission_differences({"cassandra.yaml": cassandra_file_permissions("cassandra.yaml", DEFAULTS),
                                   "logback.xml": cassandra_file_permissions("logback.xml", DEFAULTS)}, out["files"]) == [
        "cassandra.yaml: owner, group and mode not read on the node, the roles may change them"]


def test_service_account_group_files_0640():
    """Files owned by the service account and its own group, all 0640."""
    out = cassandra_permissions_import(node("cassandra", "dbgrp", "0640"), {"user": "cassandra", "group": "dbgrp"}, "50x")
    assert out["vars"] == {"cassandra_group": "dbgrp", "cassandra_config_user": "cassandra",
                           "cassandra_config_public_mode": "0640"}
    assert not any(n.startswith("The account") for n in out["notes"])
    # the config group is cassandra_group's default: not written
    assert "cassandra_config_group" not in out["vars"]
    assert any("Cassandra runs as cassandra:dbgrp" in n for n in out["notes"])
    assert any(n.startswith("cassandra-env.sh is not readable by other users") for n in out["notes"])
    # read back, the role gives every file what it has
    settings = dict(DEFAULTS, owner="cassandra", group="dbgrp", public_mode="0640", user_group="dbgrp")
    wanted = dict((n, cassandra_file_permissions(n, settings)) for n in CONFIG_50)
    assert permission_differences(wanted, out["files"]) == []


def test_role_defaults_write_nothing():
    public = ("cassandra-env.sh", "cassandra-rackdc.properties", "logback.xml")
    files = node("root", "cassandra", "0640", **dict((n, st("root", "cassandra", "0644")) for n in public))
    out = cassandra_permissions_import(files, {"user": "cassandra", "group": "cassandra"}, "50x")
    assert out["vars"] == {}
    assert out["notes"] == []


def test_old_default_root_cassandra_0640():
    """root:cassandra 0640 everywhere (the role's former default): kept, public mode 0640."""
    out = cassandra_permissions_import(node("root", "cassandra", "0640"), {"user": "cassandra", "group": "cassandra"}, "41x")
    assert out["vars"] == {"cassandra_config_public_mode": "0640"}


def test_mixed_modes_kept_per_file():
    files = node("root", "dbgrp", "0644", **{"cassandra.yaml": st("root", "dbgrp", "0600"),
                                             "jvm-server.options": st("root", "dbgrp", "0600"),
                                             "jvm11-server.options": st("root", "dbgrp", "0600"),
                                             "logback.xml": st("dbsvc", "dbgrp", "0664"),
                                             "jvm17-server.options": st("root", "root", "0644")})
    out = cassandra_permissions_import(files, {"user": "dbsvc", "group": "dbgrp"}, "50x")
    assert out["vars"] == {"cassandra_user": "dbsvc", "cassandra_group": "dbgrp", "cassandra_config_mode": "0600",
                           "cassandra_config_file_permissions": {"logback.xml": {"owner": "dbsvc", "mode": "0664"},
                                                                 "jvm17-server.options": {"group": "root", "mode": "0644"}}}
    note = [n for n in out["notes"] if n.startswith("Files whose owner, group or mode differ")]
    assert note and "logback.xml dbsvc:dbgrp 0664" in note[0] and "jvm17-server.options root:root 0644" in note[0]
    # the role, with these variables, gives each file what it has
    v = out["vars"]
    settings = {"owner": "root", "group": "dbgrp", "mode": v["cassandra_config_mode"], "public_mode": "0644",
                "files": v["cassandra_config_file_permissions"], "user": "dbsvc", "user_group": "dbgrp"}
    wanted = dict((n, cassandra_file_permissions(n, settings)) for n in CONFIG_50)
    assert permission_differences(wanted, out["files"]) == []


def test_jmx_files_compared_with_the_account():
    files = node("root", "cassandra", "0640", **{"cassandra-env.sh": st("root", "cassandra", "0644"),
                                                 "cassandra-rackdc.properties": st("root", "cassandra", "0644"),
                                                 "logback.xml": st("root", "cassandra", "0644"),
                                                 "jmxremote.password": st("dbsvc", "dbgrp", "0600"),
                                                 "jmxremote.access": st("dbsvc", "dbgrp", "0400")})
    out = cassandra_permissions_import(files, {"user": "dbsvc", "group": "dbgrp"}, "50x")
    assert out["vars"] == {"cassandra_user": "dbsvc", "cassandra_group": "dbgrp",
                           # the config group stays cassandra, not the new cassandra_group
                           "cassandra_config_group": "cassandra",
                           "cassandra_config_file_permissions": {"jmxremote.password": {"mode": "0600"}}}
    # no JMX users imported: the role does not write their files, nothing kept for them
    out = cassandra_permissions_import(files, {"user": "dbsvc", "group": "dbgrp"}, "50x", jmx=False)
    assert "cassandra_config_file_permissions" not in out["vars"]
    assert "jmxremote.password" not in out["files"]


def test_files_missing_and_other_series_left_out():
    files = {"cassandra.yaml": st("root", "cassandra", "0640"), "jvm8-server.options": st("root", "root", "0600"),
             "logback.xml": {"exists": False}}
    out = cassandra_permissions_import(files, {"user": "cassandra", "group": "cassandra"}, "50x")
    assert out["vars"] == {}
    assert set(out["files"]) == {"cassandra.yaml", "jvm8-server.options"}


def test_account_not_read_is_said():
    out = cassandra_permissions_import({}, {"user": "", "group": ""}, "50x")
    assert out["vars"] == {}
    assert out["notes"] == ["The account Cassandra runs as could not be read: cassandra:cassandra assumed"]


def test_directories_not_owned_by_the_account():
    # the mode a package or Cassandra gave them (0755) is no matter
    dirs = {"/var/lib/cassandra/data": st("dbsvc", "dbgrp", "0755"), "/var/log/cassandra": st("root", "root", "0755"),
            "/var/lib/cassandra/hints": {"exists": False}, "/srv/commitlog": {}}
    out = cassandra_permissions_import({}, {"user": "dbsvc", "group": "dbgrp"}, "50x", dirs)
    note = [n for n in out["notes"] if n.startswith("Directories")]
    assert note == ["Directories not owned by dbsvc:dbgrp, the account Cassandra runs as (cassandra_config leaves them"
                    " as they are; it creates the missing ones dbsvc:dbgrp 0750): /var/log/cassandra root:root 0755"]


def test_numeric_owner_without_a_name():
    found = {"cassandra.yaml": {"owner": "1234", "group": "1234", "mode": "0640", "uid": 1234, "gid": 1234}}
    assert permission_differences({"cassandra.yaml": {"owner": "1234", "group": "1234", "mode": "0640"}}, found) == []
    assert permission_differences({"cassandra.yaml": {"owner": "root", "group": "1234", "mode": "0640"}}, found) == [
        "cassandra.yaml: owner:group mode: node has 1234:1234 0640, import would write root:1234 0640"]


def test_unsupported_series():
    with pytest.raises(AnsibleFilterError):
        cassandra_permissions_import({}, {}, "30x")


def test_changes_for_the_report():
    results = [{"cassandra_config_file": "cassandra.yaml", "stat": dict(st("root", "cassandra", "0640"), path="/c/cassandra.yaml")},
               {"cassandra_config_file": "cassandra-env.sh", "stat": dict(st("root", "cassandra", "0640"), path="/c/cassandra-env.sh")},
               {"cassandra_config_file": "logback.xml", "stat": {"exists": False}},
               # a numeric owner for the same account: no change
               {"cassandra_config_file": "jmxremote.access", "stat": dict(st("cassandra", "cassandra", "0400", 990, 990),
                                                                          path="/etc/cassandra/jmxremote.access")}]
    settings = dict(DEFAULTS, user="990")
    assert cassandra_permission_changes(results, settings) == [
        {"item": "/c/cassandra-env.sh (owner:group mode)", "before": "root:cassandra 0640", "after": "root:cassandra 0644"}]
