from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# 5.0 with G1 and a cassandra_heap_newsize (one of a 4.x on CMS, or found by import_cluster): written as
# HEAP_NEWSIZE, which Cassandra ignores with G1; apply_config/site.yml and the upgrade say so (a warning, not a
# refusal).

import os

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _task(path, name):
    with open(os.path.join(TOP, path), encoding="utf-8") as f:
        docs = yaml.safe_load(f)
    tasks = [t for p in docs for t in p.get("tasks", [])] if any("tasks" in d for d in docs) else docs
    return next(t for t in tasks if t.get("name") == name)


def _when(task, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(templar.template(trust_as_template("{{ %s }}" % w)) for w in task["when"])


def test_the_config_role_warns():
    warn = _task("roles/cassandra_config/tasks/checks.yml", "Warn about a young generation size G1 ignores")
    assert warn["vars"]["cassandra_output"] is True and "ansible.builtin.debug" in warn
    assert _when(warn, cassandra_version="50x", cassandra_heap_newsize="128M", _cassandra_config_gcs=["G1", "G1"]) is True
    assert _when(warn, cassandra_version="50x", cassandra_heap_newsize="", _cassandra_config_gcs=["G1", "G1"]) is False
    assert _when(warn, cassandra_version="50x", cassandra_heap_newsize="1G", _cassandra_config_gcs=["CMS", "CMS"]) is False
    assert _when(warn, cassandra_version="41x", cassandra_heap_newsize="1G", _cassandra_config_gcs=["G1", "G1"]) is False


def test_the_upgrade_preflight_lists_it():
    warn = _task("playbooks/upgrade.yml", "Settings the target series ignores with G1")
    assert "ansible.builtin.debug" in warn  # said, the upgrade goes on
    base = dict(_phase="preflight", cassandra_version="50x", cassandra_heap_newsize="800M", cassandra_jvm_gc="G1")
    assert _when(warn, **base) is True
    assert _when(warn, **dict(base, cassandra_jvm_gc="CMS")) is False
    assert _when(warn, **dict(base, cassandra_heap_newsize="")) is False
    assert _when(warn, **dict(base, _phase="cleanup")) is False
