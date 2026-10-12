from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster judges the cassandra.yaml keys by the release the node's next
# start runs: the package installed since Cassandra started (a restart
# pending), else the running one.

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
    VARS = find(yaml.safe_load(f), "_release")


@pytest.mark.parametrize("running, package, judged", [
    ("4.0.14", "4.0.15", "4.0.15"),          # upgraded, restart pending
    ("4.0.15", "4.0.15", "4.0.15"),
    ("4.1.5", "4.1.3-1", "4.1.5"),           # the package older: the running one
    ("4.1.5", "", "4.1.5"),                  # a tarball
    ("4.1.5", "5.0.2", "4.1.5"),             # another series: its config is not the one read
    ("5.0.9", "5.0.10~beta", "5.0.10"),
])
def test_release_judged(running, package, judged):
    variables = dict((k, trust_as_template(VARS[k]) if isinstance(VARS[k], str) else VARS[k])
                     for k in ("_package", "_release"))
    variables.update(import_cluster_version=running, import_cluster_package=package)
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ _release }}")) == judged
