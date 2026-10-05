from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# create_cluster refuses identity settings left to the role defaults, but a new
# 5.0 cluster may leave cassandra_storage_compatibility_mode out (NONE), and
# gets refused in CASSANDRA_4 or UPGRADING. The expressions are read from the
# playbook, rendered by Ansible.

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

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "create_cluster.yml"), encoding="utf-8") as f:
    PLAY = next(p for p in yaml.safe_load(f) if p.get("name") == "Refuse an identity left to the role defaults")
IDENTITY = next(t for t in PLAY["tasks"] if t.get("name") == "Check the identity settings come from the inventory")
MODE = next(t for t in PLAY["tasks"] if t.get("name") == "A new 5.0 cluster starts in storage compatibility mode NONE")

SET = {"cassandra_cluster_name": "c", "cassandra_version": "50x", "cassandra_num_tokens": 16,
       "cassandra_partitioner": "p", "cassandra_endpoint_snitch": "s", "cassandra_dc": "dc1", "cassandra_rack": "r1",
       "cassandra_allocate_tokens_for_local_replication_factor": 3}


def render(template, variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def variables(inventory, running, task=IDENTITY, others=None, play=None, group=None, data=()):
    """others: {host: running (None: no preflight fact)}; play: the hosts in the run; group: the group's hosts;
    data: the hosts holding a system keyspace"""
    hostvars = {"n1": dict(inventory, _cassandra_preflight_running=running, inventory_hostname="n1")}
    for h, r in (others or {}).items():
        hostvars[h] = dict(inventory, inventory_hostname=h, **({} if r is None else {"_cassandra_preflight_running": r}))
    for h, v in hostvars.items():
        v["cassandra_create_system"] = {"results": [{"stat": {"exists": False}}, {"stat": {"exists": h in data}}]}
    hosts = play or list(hostvars)
    v = dict(PLAY["vars"], **task.get("vars", {}))
    v.update(inventory, hostvars=hostvars, inventory_hostname="n1", ansible_play_hosts_all=hosts, ansible_play_hosts=hosts,
             groups={"cassandra": group or list(hostvars)})
    return v


def missing(inventory, running, **kw):
    return render("{{ _missing }}", variables(inventory, running, **kw))


def test_new_cluster_may_leave_the_mode_out():
    assert missing(SET, running=False) == []
    assert missing(SET, running=False, others={"n2": False, "n3": False}) == []


@pytest.mark.parametrize("kw", [
    {"others": {"n2": True}},  # another node runs
    {"others": {"n2": None}},  # a host the preflight did not reach
    {"others": {"n2": True}, "play": ["n1"]},  # --limit to the stopped ones
    {"others": {"n2": False}, "data": ["n2"]},  # a cluster stopped as a whole (e.g. upgraded, in CASSANDRA_4)
])
def test_partly_running_or_unknown_is_not_new(kw):
    assert missing(SET, running=False, **kw) == ["cassandra_storage_compatibility_mode"]


def test_running_cluster_needs_the_mode():
    assert missing(SET, running=True) == ["cassandra_storage_compatibility_mode"]
    assert missing(dict(SET, cassandra_storage_compatibility_mode="CASSANDRA_4"), running=True) == []


def test_other_identity_settings_still_required():
    inv = dict(SET)
    del inv["cassandra_endpoint_snitch"]
    assert missing(inv, running=False) == ["cassandra_endpoint_snitch"]


@pytest.mark.parametrize("mode, ok", [(None, True), ("NONE", True), ("none", False), ("CASSANDRA_4", False),
                                      ("UPGRADING", False)])
def test_new_cluster_mode(mode, ok):
    inv = dict(SET) if mode is None else dict(SET, cassandra_storage_compatibility_mode=mode)
    v = variables(inv, running=False, task=MODE)
    assert render("{{ %s }}" % " and ".join(MODE["when"]), v) is True
    assert render("{{ %s }}" % MODE["ansible.builtin.assert"]["that"], v) is ok


def test_running_cluster_mode_not_checked():
    v = variables(dict(SET, cassandra_storage_compatibility_mode="CASSANDRA_4"), running=True, task=MODE)
    assert render("{{ %s }}" % " and ".join(MODE["when"]), v) is False


NOTE = next(t for t in PLAY["tasks"] if t.get("name") == "Say a new 5.0 cluster starts in NONE")


def test_note_names_every_host_without_the_mode():
    v = variables(dict(SET, cassandra_storage_compatibility_mode="NONE"), running=False, task=NOTE, others={"n2": False})
    del v["hostvars"]["n2"]["cassandra_storage_compatibility_mode"]
    assert render("{{ %s }}" % " and ".join(NOTE["when"]), v) is True
    assert render("{{ _unset }}", v) == ["n2"]
