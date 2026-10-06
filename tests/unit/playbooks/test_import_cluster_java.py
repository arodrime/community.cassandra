from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the Java settings written for a node, so that site.yml stays
# a no-op after the import: a Java package is kept off a shared offer of
# tarballs, and a Java its series does not support (5.0 starts on 21) gets
# cassandra_java_allow_unsupported.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template


load_collection_filters()

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")


def find(node, key):
    """The first mapping in node that has key."""
    if isinstance(node, dict):
        if key in node:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = find(item, key)
            if found is not None:
                return found
    return None


with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
JAVA = find(PLAYS, "_java_package")["_java"]
JAVA_DEFAULT = find(PLAYS, "_java_default")["_java_default"]


def java_vars(series, java, home=""):
    hv = {"import_cluster_config": {}, "import_cluster_series": series, "import_cluster_java": java,
          "import_cluster_java_home": home, "import_cluster_java_package": ""}
    variables = {"_hv": hv, "_read": trust_as_template("{{ _hv.import_cluster_config is defined }}"),
                 "_java_default": JAVA_DEFAULT, "_java_package": "java-%s-openjdk-headless" % java}
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(JAVA))


@pytest.mark.parametrize("series, java", [("50x", "17"), ("50x", "11"), ("41x", "8"), ("40x", "11")])
def test_supported_java_not_flagged(series, java):
    assert "cassandra_java_allow_unsupported" not in java_vars(series, java)


def test_java_package_kept_off_the_tarball_offer():
    assert java_vars("50x", "17") == {"cassandra_java_tarballs": {}}
    assert java_vars("50x", "11") == {"cassandra_java_version": "11", "cassandra_java_tarballs": {}}


def test_unsupported_running_java_kept():
    assert java_vars("50x", "21", home="/opt/jdk-21") == {
        "cassandra_java_version": "21", "cassandra_java_home": "/opt/jdk-21", "cassandra_java_allow_unsupported": True}
