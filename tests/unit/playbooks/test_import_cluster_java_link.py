from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: a node whose /usr/bin/java is not the running Java keeps it
# (cassandra_java_set_default: false in its host_vars), and cassandra_install
# then leaves /usr/bin/java as it is, with a package Java or a Java home.

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.tests.unit.import_nodes import node

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(ROOT, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name, tasks=None):
    todo = list(tasks) if tasks is not None else [t for play in PLAYS for t in play.get("tasks", [])]
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


@pytest.mark.parametrize("system, running, expected", [
    ("/apps/jdk17/bin/java", "/apps/jdk17/bin/java", {}),
    # Java 17 tarball run through JAVA_HOME, /usr/bin/java the Java 11 package of other programs
    ("/usr/lib/jvm/java-11-openjdk-11.0.25/bin/java", "/srv/java/jdk-17.0.20+1-jre/bin/java",
     {"cassandra_java_set_default": False}),
    ("", "/srv/java/jdk-17/bin/java", {}),  # none, or leading nowhere: the roles make it (nodetool needs one)
])
def test_java_link(system, running, expected):
    hv = {"import_cluster_system_java": system, "import_cluster_java_bin": running}
    keep = node(hv)["keep"]
    assert dict((k, v) for k, v in keep.items() if k == "cassandra_java_set_default") == expected


def test_kept_per_node():
    # host_vars of that node only: a node added later gets the running Java as its system java
    base = {"dc": "dc1", "rack": "r1", "read": True, "vars": {"cassandra_cluster_name": "c"}, "hand_edits": [],
            "normalized": [], "notes": []}
    nodes = [dict(base, name="n1", address="10.0.0.1", keep={"cassandra_java_set_default": False}),
             dict(base, name="n2", address="10.0.0.2", keep={})]
    layout = cassandra_inventory_layout(nodes, "c")
    assert layout["host_vars"]["n1"] == {"cassandra_java_set_default": False}
    assert "cassandra_java_set_default" not in str(layout["group_vars"])
    assert "the system java (/usr/bin/java), which is not the running one (cassandra_java_set_default: false)" in layout["report"]


def test_unread_nodes_keep_it_too():
    hv = {"import_cluster_system_java": "", "import_cluster_java_bin": "/x/bin/java"}
    assert node(hv, read=False)["keep"]["cassandra_java_set_default"] is False


def test_role_leaves_the_system_java():
    with open(os.path.join(ROOT, "roles", "cassandra_install", "tasks", "java_tarball.yml")) as f:
        link = task("Make it the system java", yaml.safe_load(f))
    assert "cassandra_java_set_default | bool" in link["when"]
    with open(os.path.join(ROOT, "roles", "cassandra_install", "tasks", "java.yml")) as f:
        alt = task("Make the Java package's java the default java", yaml.safe_load(f))  # the block: its when
    assert "cassandra_java_set_default | bool" in alt["when"]
