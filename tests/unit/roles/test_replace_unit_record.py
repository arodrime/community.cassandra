from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# (UX audit A5) replace_node takes the replace flag out of the unit with no restart: the running Cassandra read it
# at its first boot only, so the record of the config it started with gets the new unit and apply_config says no
# restart pending for it; only when the record has that JVM and the unit as it was (else kept: pending as before).

import hashlib
import json
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(ROOT, *path)) as f:
        return yaml.safe_load(f)


def sha1(text):
    return hashlib.sha1(text.encode()).hexdigest()


UNIT = "[Service]\nEnvironment=X=1\n"
FLAGGED = "[Service]\nEnvironment=X=1 -Dcassandra.replace_address_first_boot=10.0.0.5\n"


def run(tmp_path, record):
    """The task's script on a unit (without the flag now) and a record of the start with the flagged one."""
    task = load("roles", "cassandra_service", "tasks", "unit_no_restart.yml")[0]
    unit = tmp_path / "cassandra.service"
    unit.write_text(UNIT)
    path = tmp_path / "record.json"
    if record is not None:
        path.write_text(json.dumps(dict((str(unit) if k == "unit" else k, v) for k, v in record.items())))
    out = subprocess.run([sys.executable, "-c", task["vars"]["_record"], str(unit), str(path), sha1(FLAGGED)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return out, (json.loads(path.read_text()) if path.exists() else None), str(unit)


def test_the_unit_without_the_flag_is_recorded(tmp_path):
    out, record, unit = run(tmp_path, {"_jvm": {"pid": "42", "start": 1.0}, "unit": sha1(FLAGGED)})
    assert out == "recorded" and record[unit] == sha1(UNIT) and record["_jvm"]["pid"] == "42"


@pytest.mark.parametrize("record, why", [
    ({"_jvm": {"pid": "42", "start": 1.0}, "unit": sha1("another unit")}, "kept"),  # started with another unit
    ({"unit": sha1(FLAGGED)}, "kept"),                                            # no JVM in the record
    (None, "no record"),
])
def test_anything_else_is_left_as_it_is(tmp_path, record, why):
    out, after, unit = run(tmp_path, record)
    assert out == why
    assert after is None or after[unit] != sha1(UNIT)


def test_replace_node_records_it_after_the_rewrite():
    tasks = load("roles", "cassandra_operation", "tasks", "action_replace.yml")
    names = [t["name"] for t in tasks]
    at = names.index("Take the replace flag out of the unit (no restart)")
    assert names[at - 1] == "Read the unit the replacement started with"
    assert tasks[at - 1]["ansible.builtin.stat"]["checksum_algorithm"] == "sha1"
    after = tasks[at + 1]
    assert after["ansible.builtin.include_role"]["tasks_from"] == "unit_no_restart.yml"
    # the record's path: the service role's own
    assert "_cassandra_service_config_snapshot" in load("roles", "cassandra_service", "vars", "main.yml")
