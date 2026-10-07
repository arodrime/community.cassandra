from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# apply_config restarts a node whose systemd unit changed since Cassandra
# started (a new User=, Group=), as one whose config files changed; with no
# record of what it started with, a unit written since is only listed.

import base64
import json
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

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks", "config_pending.yml")
with open(TASKS) as f:
    NOTE = next(t for t in yaml.safe_load(f) if t["name"] == "Note whether a restart is pending")

UNIT = "/etc/systemd/system/cassandra.service"
YAML = "/etc/cassandra/cassandra.yaml"


def pending(snapshot, unit_sum="u1", unit_ctime=100, jvm_start=50):
    variables = {
        "cassandra_pending_files": {"files": [{"path": YAML, "checksum": "y1", "ctime": 10}]},
        "cassandra_pending_unit_file": {"stat": {"exists": True, "path": UNIT, "checksum": unit_sum, "ctime": unit_ctime}},
        "cassandra_pending_snapshot": ({"content": base64.b64encode(json.dumps(snapshot).encode()).decode()}
                                       if snapshot is not None else {}),
        "cassandra_jvm": {"pid": "123", "start": str(jvm_start)},
    }
    for name in ("_started", "_files", "_newer"):
        variables[name] = trust_as_template(NOTE["vars"][name])
    templar = Templar(loader=DataLoader(), variables=variables)
    return (templar.template(trust_as_template(NOTE["vars"]["_changed"])),
            templar.template(trust_as_template(NOTE["vars"]["_unknown"])))


@pytest.mark.parametrize("unit_sum, expected", [("u1", []), ("u2", ["cassandra.service"])])
def test_unit_recorded_at_start(unit_sum, expected):
    assert pending({YAML: "y1", UNIT: "u1"}, unit_sum) == (expected, [])


@pytest.mark.parametrize("unit_ctime, expected", [(40, []), (60, ["cassandra.service"])])
def test_record_without_the_unit_compares_with_the_jvm_start(unit_ctime, expected):
    # a node recorded before the unit was (or the unit written since that start): written after the JVM started,
    # it waits for a restart
    assert pending({YAML: "y1"}, unit_ctime=unit_ctime) == (expected, [])


@pytest.mark.parametrize("unit_ctime, expected", [(40, []), (60, ["cassandra.service"])])
def test_no_record_compares_with_the_jvm_start(unit_ctime, expected):
    # no record at all (e.g. imported): what Cassandra read then is unknown, so nothing is pending; a unit written
    # since its JVM started is only listed
    assert pending(None, unit_ctime=unit_ctime) == ([], expected)
