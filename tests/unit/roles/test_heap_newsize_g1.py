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
    # its preflight runs cassandra_config on the target's settings (the config preview): the warning above, once
    with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    tasks = [t for p in plays for t in p.get("tasks", [])]
    preview = [t for t in tasks if t.get("vars", {}).get("_cassandra_config_target_preview")]
    assert preview and preview[0]["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_config"
    assert "_phase == 'preflight'" in str(preview[0]["when"])
    assert not [t for t in tasks if "cassandra_heap_newsize" in str(t.get("when", ""))]  # not said twice
