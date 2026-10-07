from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster_keep_hand_edits: the config files with hand edits no variable
# covers are left as they are on their node (cassandra_config_keep_files), not
# written nor compared by cassandra_config and the self-check.

import os
import re

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
    cassandra_config_import, cassandra_inventory_files, cassandra_inventory_layout)
from ansible_collections.community.cassandra.plugins.filter.cassandra_import_check import cassandra_import_self_check

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
STOCK = os.path.join(ROOT, "roles", "cassandra_config", "molecule", "default", "files", "stock-5.0.9")
FACTS = {"os_family": "Debian", "default_ipv4": {"address": "10.0.0.1"}, "hostname": "n1"}

with open(os.path.join(ROOT, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def match():
    todo = [t for p in PLAYS for t in p.get("tasks", [])]
    while todo:
        t = todo.pop(0)
        if t.get("name") == "Match the ring with the hosts":
            return t["vars"]
        todo += t.get("block", [])


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


HAND = ["logback.xml, line 12:", "  - <x/>", "  + <y/>", "cassandra-env.sh: JMX_PORT", "jmxremote.password: its users NOT imported"]


def test_files_with_hand_edits_kept_on_request():
    hand_kept = match()["_hand_kept"]
    hv = {"import_cluster_config": {"hand_edits": HAND}}
    assert render(hand_kept, _hv=hv, _read=True) == {}  # by default: the self-check fails on them
    assert render(hand_kept, _hv=hv, _read=True, import_cluster_keep_hand_edits=True) == {
        "cassandra_config_keep_files": ["logback.xml", "cassandra-env.sh"]}


def stock():
    files = {}
    for name in os.listdir(STOCK):
        with open(os.path.join(STOCK, name), encoding="utf-8") as f:
            files[name.replace(".stock", "")] = f.read()
    files["cassandra-env.sh"] = files["cassandra-env.sh"].replace('CASSANDRA_LOG_DIR="$CASSANDRA_HOME/logs"',
                                                                  "CASSANDRA_LOG_DIR=/var/log/cassandra")
    return files


def role_permissions(files):
    # what the roles give them: root:cassandra, cassandra.yaml and the JVM options 0640, the others 0644
    return dict((name, {"owner": "root", "group": "cassandra", "uid": 0, "gid": 990,
                        "mode": "0640" if name == "cassandra.yaml" or name.endswith(".options") else "0644"})
                for name in files)


def check(files, keep, permissions=None):
    imported = cassandra_config_import(files, "50x", FACTS, "", "/var/lib/cassandra")
    node = {"name": "n1", "address": "10.0.0.1", "hostname": "n1", "dc": "dc1", "rack": "r1", "read": True,
            "vars": dict(imported["vars"], cassandra_version="50x"), "hand_edits": imported["hand_edits"],
            "normalized": imported["normalized"], "comments": imported["comments"], "notes": [],
            "keep": dict({"cassandra_service_unit_manage": False}, **keep)}
    layout = cassandra_inventory_layout([node], "c")
    out = cassandra_import_self_check(cassandra_inventory_files(layout), layout["hosts"], "n1", FACTS, files, {},
                                      "/var/lib/cassandra", "17", "",
                                      role_permissions(files) if permissions is None else permissions)
    return out, layout


def test_report_names_the_edits_left_and_the_others():
    base = {"name": "n1", "address": "10.0.0.1", "dc": "dc1", "rack": "r1", "read": True, "vars": {"cassandra_cluster_name": "c"},
            "normalized": [], "notes": [], "hand_edits": HAND, "keep": {"cassandra_config_keep_files": ["logback.xml"]}}
    report = cassandra_inventory_layout([base], "c")["report"].split("\n")
    kept = report.index("  HAND EDITS no variable covers, in files LEFT AS THEY ARE on this node"
                        " (cassandra_config_keep_files; nodes added later get the role's):")
    other = report.index("  HAND EDITS no variable covers (cassandra_config would revert them):")
    assert report[kept + 1:kept + 4] == ["    logback.xml, line 12:", "      - <x/>", "      + <y/>"]
    assert report[other + 1:other + 3] == ["    cassandra-env.sh: JMX_PORT", "    jmxremote.password: ****"]  # (masked)


def test_self_check_leaves_out_the_kept_files():
    files = stock()
    files["logback.xml"] = re.sub(r"<root level=\"INFO\">", '<logger name="com.example" level="WARN"/>\n  <root level="INFO">',
                                  files["logback.xml"], count=1)
    out, layout = check(files, {})
    assert [d for d in out["differences"] if d.startswith("logback.xml")]
    out, layout = check(files, {"cassandra_config_keep_files": ["logback.xml"]})
    assert out["differences"] == []
    assert "in files LEFT AS THEY ARE on this node" in layout["report"]


def test_role_leaves_them():
    with open(os.path.join(ROOT, "roles", "cassandra_config", "tasks", "main.yml")) as f:
        main = yaml.safe_load(f)
    block = next(t for t in main if t.get("name") == "Configure")["block"]
    note = next(t for t in block if t.get("name") == "List the files to change and whether to ask first")
    results = [{"item": "logback.xml", "stat": {"exists": True}}, {"item": "cassandra-env.sh", "stat": {"exists": False}}]
    for initialized, kept in [(True, ["logback.xml"]),  # a node without the file gets the role's
                              (False, [])]:  # a new node (e.g. rebuilt under that name): its files are the package's
        variables = {"cassandra_config_compared": {"results": []}, "cassandra_conf_dir": "/c", "cassandra_config_tmp": {"path": "/t"},
                     "cassandra_config_keep_stat": {"results": results},
                     "_cassandra_config_initialized": initialized}
        variables["_keep"] = trust_as_template(note["vars"]["_keep"])
        assert render(note["vars"]["_kept"], **variables) == kept


def test_self_check_compares_the_owner_and_mode_of_the_kept_files():
    # cassandra_config keeps their content, not their owner, group and mode: those are compared
    files = stock()
    files["logback.xml"] = re.sub(r"<root level=\"INFO\">", '<logger name="com.example" level="WARN"/>\n  <root level="INFO">',
                                  files["logback.xml"], count=1)
    permissions = role_permissions(files)
    permissions["logback.xml"] = dict(permissions["logback.xml"], mode="0600")
    out, layout = check(files, {"cassandra_config_keep_files": ["logback.xml"]}, permissions)
    assert out["differences"] == ["logback.xml: owner:group mode: node has root:cassandra 0600,"
                                  " import would write root:cassandra 0644"]
