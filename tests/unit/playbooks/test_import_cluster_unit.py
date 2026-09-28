from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: what the node's systemd unit sets (Environment=) is kept in
# cassandra_service's unit, and the JMX login goes to the inventory.

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

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name):
    todo = [t for play in PLAYS for t in play.get("tasks", [])]
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", [])
    raise KeyError(name)


MATCH = task("Match the ring with the hosts")["vars"]


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def unit_env(env, unit_heap, log_dir="/var/log/cassandra", read=True):
    hv = {"import_cluster_unit": {"env": env}, "import_cluster_log_dir": log_dir}
    return render(MATCH["_unit_env"], _hv=hv, _unit_heap=unit_heap, _read=read)


def test_unit_environment_kept():
    env = {"LOCAL_JMX": "no", "MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "2G", "CASSANDRA_LOG_DIR": "/data/log"}
    # heap imported as variables, the log dir as cassandra_log_dir: the rest stays in the unit
    assert unit_env(env, {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "2G"}, "/data/log") == {"LOCAL_JMX": "no"}


def test_heap_not_imported_stays_in_the_unit():
    # e.g. 5.0 with CMS: MAX_HEAP_SIZE and HEAP_NEWSIZE only work as a pair, kept as they are
    env = {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "2G"}
    assert unit_env(env, {}) == env


def test_unit_environment_of_an_unread_node():
    assert unit_env({"LOCAL_JMX": "no"}, {}, read=False) == {}


WRITE = next(play for play in PLAYS if play["name"] == "Write the inventory")["vars"]


@pytest.mark.parametrize("given_vars, jmx", [
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/jmx.pw"},
     {"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/jmx.pw"}),
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password": "p", "cassandra_jmx_password_file": ""},
     {"cassandra_jmx_username": "ops", "cassandra_jmx_password": "p"}),
    ({}, {}),
])
def test_jmx_login_from_the_given_node(given_vars, jmx):
    # -e or the given node's own inventory: read on it, not on localhost
    hostvars = {"node1": given_vars}
    assert render(WRITE["_jmx"], _given=["node1"], hostvars=hostvars) == jmx
