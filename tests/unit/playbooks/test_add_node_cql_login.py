from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# add_node reads the keyspaces' replication over CQL: without it (no CQL access)
# the plan assumes the whole datacenter hands data over, but a CQL login refused
# (authentication on, a wrong or missing cassandra_cql_password) stops the run at
# once, naming the variables.

import os
import warnings

import pytest
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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "add_node.yml")


def rescue():
    with open(PLAYBOOK, encoding="utf-8") as f:
        play = next(p for p in yaml.safe_load(f) if p.get("name") == "Check the new nodes")
    block = next(t for t in play["tasks"] if t.get("name") == "Read the keyspaces' replication")
    return dict((t["name"], t) for t in block["rescue"])


@pytest.mark.parametrize("err, msg, stops", [
    ("Connection error: ('Unable to connect to any servers', {'10.0.0.1:9042': AuthenticationFailed('Failed to"
     " authenticate to 10.0.0.1:9042: Error from server: code=0100 [Bad credentials] message=\"Provided username ops"
     " and/or password are incorrect\"')})", "", True),
    ("", "Connection error: ('Unable to connect to any servers', {'10.0.0.1': AuthenticationFailed('Remote end"
     " requires authentication')})", True),
    ("Connection error: ('Unable to connect to any servers', {'10.0.0.1:9042': ConnectionRefusedError(111)})", "", False),
    ("", "", False)])
def test_a_cql_login_refused_stops_at_once(err, msg, stops):
    tasks = rescue()
    assert "cassandra_keyspaces" in tasks["Go on without it"]["ansible.builtin.set_fact"]
    stop = tasks["Stop on a CQL login refused"]
    assert stop["any_errors_fatal"] is True and stop["vars"]["cassandra_output"] is True
    assert "cassandra_cql_username" in stop["ansible.builtin.fail"]["msg"]
    assert "cassandra_cql_password" in stop["ansible.builtin.fail"]["msg"]
    variables = {"ansible_failed_result": {"err": err, "msg": msg}}
    variables["_err"] = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(stop["vars"]["_err"]))
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ %s }}" % stop["when"])) is stops
