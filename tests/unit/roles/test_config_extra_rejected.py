from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config refuses, before writing anything, a cassandra_extra_settings
# key the installed Cassandra rejects (it would not restart): the installed
# release's, else the series'.

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

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks", "main.yml")


def task(name):
    with open(TASKS) as f:
        todo = list(yaml.safe_load(f))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


TASK = task("Refuse extra settings Cassandra rejects")


def run(extra, installed, series="4.1"):
    """(the assert passes, its message), as the role renders them."""
    variables = {"inventory_hostname": "node1", "cassandra_extra_settings": extra,
                 "_cassandra_config_installed": installed, "_cassandra_config_series": series}
    variables.update((k, trust_as_template(v)) for k, v in TASK["vars"].items())
    templar = Templar(loader=DataLoader(), variables=variables)

    def render(text):
        return templar.template(trust_as_template(text))

    assert render("{{ %s }}" % TASK["when"]) is (len(extra) > 0)
    passed = render("{{ %s }}" % TASK["ansible.builtin.assert"]["that"])
    return passed, " ".join(render(TASK["ansible.builtin.assert"]["fail_msg"]).split())


def test_rejected_key_stops_the_role():
    passed, msg = run({"fake_option": 42, "auto_bootstrap": False}, "4.1.5")
    assert passed is False
    assert msg == ("node1: cassandra_extra_settings has fake_option, which Cassandra 4.1.5 rejects: it would not"
                   " restart. Remove it from the inventory; nothing is changed.")


@pytest.mark.parametrize("extra", [{"auto_bootstrap": False}, {"commitlog_total_space_in_mb": 8192}])
def test_accepted_keys_pass(extra):
    assert run(extra, "4.1.5")[0] is True


def test_installed_release_or_the_series():
    # added in 4.0.15
    assert run({"batchlog_endpoint_strategy": "dynamic_remote"}, "4.0.14", "4.0")[0] is False
    assert run({"batchlog_endpoint_strategy": "dynamic_remote"}, "4.0.15", "4.0")[0] is True
    # not installed (a tarball), or another series (the upgrade's preview): the series'
    assert run({"batchlog_endpoint_strategy": "dynamic_remote"}, "", "4.0")[0] is True
    passed, msg = run({"fake_option": 1, "other_fake": 2}, "4.1.5", "5.0")
    assert passed is False and "has fake_option, other_fake, which Cassandra 5.0 rejects" in msg and "Remove them" in msg
    # 4.1 names on 4.0
    assert run({"key_cache_size": "100MiB"}, "4.0.21", "4.0")[0] is False


def test_nothing_to_check():
    assert run({}, "4.1.5")[0] is True
