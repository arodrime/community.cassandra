from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config creates the missing Cassandra directories, but never on a
# mount point of /etc/fstab that is not mounted (the root filesystem).

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

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config", "tasks", "main.yml")


def task(name):
    with open(TASKS, encoding="utf-8") as f:
        todo = list(yaml.safe_load(f))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def on_unmounted(missing, unmounted):
    variables = {
        "cassandra_config_dirs": {"results": [{"item": d, "stat": {"exists": False}} for d in missing]
                                  + [{"item": "/var/lib/cassandra/hints", "stat": {"exists": True}}]},
        "cassandra_config_unmounted": {"stdout_lines": unmounted},
    }
    template = task("Refuse to create them on a disk that is not mounted")["vars"]["_on_unmounted"]
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


@pytest.mark.parametrize("missing, unmounted, refused", [
    (["/data/cassandra/data"], ["/data"], ["/data/cassandra/data (/data)"]),
    (["/data"], ["/data"], ["/data (/data)"]),
    (["/data/cassandra/data"], ["/data2", "/dat"], []),  # not a prefix of a path component
    (["/data/cassandra/data"], [], []),
])
def test_refused_on_unmounted_disk(missing, unmounted, refused):
    assert on_unmounted(missing, unmounted) == refused


def test_seed_reload_logs_in_to_jmx():
    # remote JMX with authentication: nodetool reloadseeds needs the login too
    reload = task("Reload the seed list on the running node")["community.cassandra.cassandra_reload"]
    assert {"username", "password_file", "password"} <= set(reload)
