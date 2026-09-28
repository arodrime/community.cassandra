from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the heap set in the systemd unit (Environment=), which
# cassandra_service's own unit replaces. The expressions are read from the
# playbook and rendered by Ansible.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

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
UNIT = find(PLAYS, "import_cluster_unit")["import_cluster_unit"]
UNIT_HEAP = find(PLAYS, "_unit_heap")["_unit_heap"]


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


@pytest.mark.parametrize("environment, heap, newsize", [
    ("CASSANDRA_LOG_DIR=/data/log MAX_HEAP_SIZE=512M HEAP_NEWSIZE=128M", "512M", "128M"),
    ("MAX_HEAP_SIZE=8G", "8G", ""),
    ("JVM_EXTRA_OPTS=-Dx=1 LOCAL_JMX=yes", "", ""),
    ("", "", ""),
    ("MY_MAX_HEAP_SIZE=1G", "", ""),  # another variable
])
def test_unit_heap_read(environment, heap, newsize):
    kv = {"unit_Environment": environment}
    assert render(UNIT["heap"], _kv=kv) == heap
    assert render(UNIT["newsize"], _kv=kv) == newsize


def unit_heap(series, cms, heap="512M", newsize="128M", config=None, read=True):
    hv = {
        "import_cluster_series": series,
        "import_cluster_unit": {"heap": heap, "newsize": newsize, "cms": cms},
        "import_cluster_config": {"vars": config or {}},
    }
    return render(UNIT_HEAP, _hv=hv, _read=read)


def test_cms_read():
    assert render(UNIT["cms"], _kv={"cms": "1"}) is True
    assert render(UNIT["cms"], _kv={"cms": "0"}) is False


def test_4x_pair_imported():
    assert unit_heap("41x", cms=True) == {"cassandra_heap_size": "512M", "cassandra_heap_newsize": "128M"}


def test_50x_g1_heap_only():
    # the 5.0 config has no HEAP_NEWSIZE, G1 needs none
    assert unit_heap("50x", cms=False) == {"cassandra_heap_size": "512M"}


def test_50x_cms_not_imported():
    # MAX_HEAP_SIZE alone under CMS: cassandra-env.sh exits, the node would not start
    assert unit_heap("50x", cms=True) == {}


def test_env_sh_heap_wins():
    assert unit_heap("41x", cms=False, config={"cassandra_heap_size": "4G", "cassandra_heap_newsize": "1G"}) == {}


def test_config_not_read():
    # e.g. an unsupported version: nothing imported, nothing reported as imported
    assert unit_heap("41x", cms=False, read=False) == {}
