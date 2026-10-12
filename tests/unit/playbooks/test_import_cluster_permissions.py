from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the owner, group and mode of the config files, the JMX
# users' files and the directories, read by one command, each by its name.

import json
import os
import warnings

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)

READ = next(p for p in PLAYS if p["name"] == "Read every node")
STAT = next(t for t in READ["tasks"] if t["name"] == "Read the owner and mode of the config files and directories")
TURN = next(t for t in READ["tasks"] if t["name"] == "Turn them into variables")
FILES = READ["vars"]["_files"]


def render(template, variables):
    variables = dict(variables)
    for name, value in list(variables.items()):
        if isinstance(value, str):
            variables[name] = trust_as_template(value)
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_one_stat_per_file_then_the_dirs():
    cfg = {"cassandra_data_file_directories": ["/d1", "/d2"], "cassandra_hints_dir": "/h"}
    variables = dict(STAT["vars"], _files=FILES, import_cluster_conf_dir="/etc/cassandra",
                     import_cluster_config={"vars": cfg}, import_cluster_log_dir="")
    paths = render(STAT["vars"]["_paths"], variables)
    assert paths[:len(FILES)] == ["/etc/cassandra/" + f for f in FILES]
    assert paths[len(FILES):] == ["/etc/cassandra/jmxremote.password", "/etc/cassandra/jmxremote.access",
                                  "/d1", "/d2", "/var/lib/cassandra/commitlog", "/h", "/var/lib/cassandra/saved_caches",
                                  "/var/log/cassandra"]
    # followed (the owner and mode of what a link leads to), each by its path
    assert "[['perms', 'stat', _paths | zip(_paths) | list]]" in STAT["ansible.builtin.command"]["stdin"]


def test_stats_matched_to_their_file_and_dir():
    variables = dict(STAT["vars"], _files=FILES, import_cluster_conf_dir="/etc/cassandra",
                     import_cluster_config={"vars": {}}, import_cluster_log_dir="/logs")
    paths = render(STAT["vars"]["_paths"], variables)
    results = [{"item": p, "stat": {"exists": True, "path": p}} for p in paths]
    results[1] = {"item": paths[1], "failed": True, "msg": "Permission denied"}  # no stat: an empty one
    read = {"rc": 0, "stdout": json.dumps({"perms": results})}
    variables = dict(TURN["vars"], _files=FILES, import_cluster_perm_read=read)
    files = render(TURN["vars"]["_file_stats"], variables)
    dirs = render(TURN["vars"]["_dir_stats"], variables)
    assert files["cassandra.yaml"]["path"] == "/etc/cassandra/cassandra.yaml"
    assert files[FILES[1]] == {}
    assert files["jmxremote.access"]["path"] == "/etc/cassandra/jmxremote.access"
    assert set(dirs) == {"/var/lib/cassandra/data", "/var/lib/cassandra/commitlog", "/var/lib/cassandra/hints",
                         "/var/lib/cassandra/saved_caches", "/logs"}
    assert all(dirs[p]["path"] == p for p in dirs)


def test_jmx_files_at_their_own_paths():
    # the files the JVM reads (cassandra_jmx_files): read, and their owner and mode, at their paths
    jmx = {"password": "/etc/cassandra/conf/jmx/my_jmx.password", "access": "/etc/cassandra/conf/jmx/my_jmx.access"}
    variables = dict(STAT["vars"], _files=FILES, import_cluster_conf_dir="/etc/cassandra",
                     import_cluster_config={"vars": {}}, import_cluster_log_dir="", import_cluster_jmx_files=jmx)
    paths = render(STAT["vars"]["_paths"], variables)
    assert paths[len(FILES):len(FILES) + 2] == [jmx["password"], jmx["access"]]
    read = next(t for t in READ["tasks"] if t["name"] == "Read the JMX users, the repository files' mode and their signing keys")
    assert [[i, render(p, dict(import_cluster_jmx_files=jmx))] for i, p in read["vars"]["_jmx"]] == [
        ["jmxremote.password", jmx["password"]], ["jmxremote.access", jmx["access"]]]
    assert [[i, render(p, dict(import_cluster_jmx_files={"password": ""}))] for i, p in read["vars"]["_jmx"]] == [
        ["jmxremote.password", "/etc/cassandra/jmxremote.password"], ["jmxremote.access", "/etc/cassandra/jmxremote.access"]]
    # found before they are read; the JVM's arguments read without showing them
    names = [t["name"] for t in READ["tasks"]]
    first = "Read the config files, when they were written, the JVM's arguments, the repository files and the unit"
    assert names.index(first) < names.index("Find the JMX password and access files the JVM reads") \
        < names.index(read["name"])
    assert next(t for t in READ["tasks"] if t["name"] == first)["no_log"] is True
    assert read["no_log"] is True
