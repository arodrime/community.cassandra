from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Owner, group and mode of the files cassandra_config writes, and of
# Cassandra's directories: what the role sets, and the changes it reports.

import os

import pytest
import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import (
    DEFAULTS as FILTER_DEFAULTS, cassandra_dir_permission_changes, cassandra_file_permissions, cassandra_permission_changes,
    mode_text)

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config")

CONFIG_50 = ["cassandra.yaml", "cassandra-env.sh", "jvm-server.options", "jvm11-server.options", "jvm17-server.options",
             "cassandra-rackdc.properties", "logback.xml"]
DEFAULTS = {"owner": "root", "group": "cassandra", "mode": "0640", "public_mode": "0644", "files": {},
            "user": "cassandra", "user_group": "cassandra"}


def st(owner, group, mode, uid=1000, gid=1000):
    return {"exists": True, "pw_name": owner, "gr_name": group, "mode": mode, "uid": uid, "gid": gid}


@pytest.mark.parametrize("given, text", [("0640", "0640"), ("640", "0640"), (0o644, "0644"), ("00600", "0600"),
                                         ("u=rw,g=r", "u=rw,g=r")])
def test_mode_text(given, text):
    assert mode_text(given) == text


def test_role_defaults_per_file():
    # passwords may be there (keystores, the JVM options' extra options, cassandra-env.sh's extra lines)
    for name in ("cassandra.yaml", "jvm-server.options", "jvm8-server.options", "jvm11-server.options",
                 "jvm17-server.options", "cassandra-env.sh"):
        assert cassandra_file_permissions(name, DEFAULTS) == {"owner": "root", "group": "cassandra", "mode": "0640"}
    for name in ("cassandra-rackdc.properties", "logback.xml"):
        # no secret there
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
    """The filter's defaults must be the role's."""
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        role = yaml.safe_load(f)
    assert (role["cassandra_user"], role["cassandra_group"], role["cassandra_config_user"], role["cassandra_config_group"],
            role["cassandra_config_mode"], role["cassandra_config_public_mode"]) == (
        FILTER_DEFAULTS["user"], FILTER_DEFAULTS["group"], FILTER_DEFAULTS["owner"], "{{ cassandra_group }}",
        FILTER_DEFAULTS["mode"], FILTER_DEFAULTS["public_mode"])


def test_changes_for_the_report():
    results = [{"cassandra_config_file": "cassandra.yaml", "stat": dict(st("root", "cassandra", "0640"), path="/c/cassandra.yaml")},
               # written readable by all before it held secrets: 0640 now
               {"cassandra_config_file": "cassandra-env.sh", "stat": dict(st("root", "cassandra", "0644"), path="/c/cassandra-env.sh")},
               {"cassandra_config_file": "logback.xml", "stat": {"exists": False}},
               # a numeric owner for the same account: no change
               {"cassandra_config_file": "jmxremote.access", "stat": dict(st("cassandra", "cassandra", "0400", 990, 990),
                                                                          path="/etc/cassandra/jmxremote.access")}]
    settings = dict(DEFAULTS, user="990")
    assert cassandra_permission_changes(results, settings) == [
        {"item": "/c/cassandra-env.sh (owner:group mode)", "before": "root:cassandra 0644", "after": "root:cassandra 0640"}]


def dir_results(*dirs):
    return [{"item": [kind, path], "stat": dict(stat, isdir=True) if stat.get("exists") else stat} for kind, path, stat in dirs]


@pytest.mark.parametrize("owner, group, mode, after", [
    # the account cannot write or search it: given to it, u+rwx, the other bits kept
    ("root", "cassandra", "0750", "cassandra:cassandra 0750"),
    ("root", "root", "0755", "cassandra:cassandra 0755"),
    ("cassandra", "cassandra", "0000", "cassandra:cassandra 0700"),
    ("cassandra", "cassandra", "0555", "cassandra:cassandra 0755"),
    ("dbsvc", "dbgrp", "0770", "cassandra:cassandra 0770"),
    # it can: left as it is (a package's 0755, a group's 0770, a 0700: no change after an import)
    ("cassandra", "cassandra", "0755", None),
    ("cassandra", "cassandra", "0700", None),
    ("root", "cassandra", "0770", None),
    ("root", "root", "0777", None),
])
def test_dir_permission_changes(owner, group, mode, after):
    out = cassandra_dir_permission_changes(dir_results(("data dir", "/srv/data", st(owner, group, mode))), "cassandra", "cassandra")
    if after is None:
        assert out == []
    else:
        assert out == [{"item": "data dir /srv/data (owner:group mode)", "path": "/srv/data", "dir": "data dir",
                        "before": "%s:%s %s" % (owner, group, mode), "after": after, "owner": "cassandra",
                        "group": "cassandra", "mode": after.split()[1]}]


def test_dir_permission_changes_skip_missing_files_and_root():
    results = dir_results(("hints dir", "/srv/hints", {"exists": False}), ("commitlog dir", "/srv/cl", {}))
    results.append({"item": ["data dir", "/srv/file"], "stat": dict(st("root", "root", "0600"), isdir=False)})
    assert cassandra_dir_permission_changes(results, "cassandra", "cassandra") == []
    # Cassandra run as root reads and writes everywhere
    assert cassandra_dir_permission_changes(dir_results(("data dir", "/d", st("x", "x", "0000"))), "root", "root") == []
    # a numeric owner matches its number
    assert cassandra_dir_permission_changes(
        dir_results(("data dir", "/d", {"exists": True, "uid": 1234, "gid": 1234, "mode": "0750"})), "1234", "1234") == []


@pytest.mark.parametrize("path", ["/", "/var/lib", "/var/lib/", "/srv", "/opt", "/home"])
def test_dir_permission_changes_never_a_system_dir(path):
    # a mistyped cassandra_commitlog_dir: /var/lib is never given to Cassandra
    assert cassandra_dir_permission_changes(dir_results(("commitlog dir", path, st("root", "root", "0755"))), "cassandra", "cassandra") == []
    assert cassandra_dir_permission_changes(dir_results(("commitlog dir", "/srv/cl", st("root", "root", "0755"))), "cassandra", "cassandra")
