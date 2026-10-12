from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_linux's disk tuning on a running node: the tuned profile switch
# (it applies the profiles again, live) waits for a run with Cassandra stopped,
# like lvchange; and the directories it tunes are the ones cassandra_config
# writes (data_file_directories, by default [data_dir]).

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

DISKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_linux", "tasks", "disks.yml")
DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_linux", "defaults", "main.yml")


def tasks():
    with open(DISKS) as f:
        out = []
        todo = list(yaml.safe_load(f))
        while todo:
            t = todo.pop(0)
            out.append(t)
            todo[0:0] = t.get("block", [])
        return out


def task(name):
    return next(t for t in tasks() if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_switch_off_by_default_under_a_running_cassandra():
    with open(DEFAULTS) as f:
        assert yaml.safe_load(f)["cassandra_linux_tuned_switch_running"] is False
    switch = task("Add it to the profiles tuned runs")
    assert "not _cassandra_disk_tuned_deferred | bool" in switch["when"]
    deferred = task("Pick the tuned profile keeping the Cassandra devices out")["ansible.builtin.set_fact"][
        "_cassandra_disk_tuned_deferred"]
    for running, asked, expected in ((True, False, True), (False, False, False), (True, True, False)):
        assert render(deferred, _cassandra_data_running=running, cassandra_linux_tuned_switch_running=asked) == expected


@pytest.mark.parametrize("lvchange_running, tuned_running, probed", [
    (False, False, True), (True, False, True), (False, True, True), (True, True, False)])
def test_running_cassandra_looked_for_whenever_a_gate_needs_it(lvchange_running, tuned_running, probed):
    when = task("Look for a running Cassandra")["when"]
    variables = dict(cassandra_data_disk_tuning=True, cassandra_data_lvchange_running=lvchange_running,
                     cassandra_linux_tuned_switch_running=tuned_running)
    assert all(render("{{ %s }}" % w, **variables) for w in when) == probed


@pytest.mark.parametrize("asked, running, now", [(False, "running", False), (False, "", True), (True, "running", True)])
def test_lvchange_waits_for_a_stopped_cassandra_unless_asked(asked, running, now):
    facts = task("Decide whether lvchange runs now (not under a running Cassandra)")["ansible.builtin.set_fact"]
    variables = dict(_cassandra_data_lvm_here=True, cassandra_data_lvchange_running=asked,
                     cassandra_data_running={"stdout": running})
    assert render(facts["_cassandra_data_lvchange_now"], **variables) is now
    assert render(facts["_cassandra_data_running"], **variables) is (running == "running")


def test_deferred_switch_said_with_its_command():
    say = task("Say how the data disks are tuned")
    variables = dict(_cassandra_disk_tuned={"name": "cassandra-disks", "active": ["virtual-guest"],
                                            "profiles": ["virtual-guest", "cassandra-disks"]},
                     _cassandra_disk_tuned_deferred=True, cassandra_data_tuned_file={"changed": True},
                     cassandra_data_tuned_switch={"skipped": True})
    assert render(say["vars"]["_tuned_deferred"], **variables) is True
    assert render(say["vars"]["_tuned_failed"], **variables) is False
    assert "_tuned_deferred | bool" in say["when"][1]
    msg = " ".join(say["ansible.builtin.debug"]["msg"].split())
    assert "by hand:' ~ ' tuned-adm profile ' ~ _cassandra_disk_tuned.profiles | join(' ')" in msg
    assert "cassandra_linux_tuned_switch_running: true" in msg


def test_switch_timed_out_is_a_failure():
    say = task("Say how the data disks are tuned")
    variables = dict(_cassandra_disk_tuned={"name": "cassandra-disks"},
                     cassandra_data_tuned_switch={"failed": True, "msg": "timed out"})
    assert render(say["vars"]["_tuned_failed"], **variables) is True


@pytest.mark.parametrize("variables, dirs", [
    ({"cassandra_data_dir": "/data/cassandra"}, ["/data/cassandra"]),
    # data_file_directories set: data_dir is not a data directory (cassandra_config's default only)
    ({"cassandra_data_file_directories": ["/d1/data", "/d2/data"], "cassandra_data_dir": "/var/lib/cassandra/data",
      "cassandra_commitlog_dir": "/c/commitlog"}, ["/d1/data", "/d2/data", "/c/commitlog"]),
    ({"cassandra_data_file_directories": "/d1/data", "cassandra_data_dir": "/var/lib/cassandra/data"}, ["/d1/data"]),
    ({}, []),
])
def test_tuned_directories_are_the_data_directories(variables, dirs):
    find = task("Find the filesystem of each Cassandra directory")
    assert render(find["vars"]["_tuned"], **variables) == dirs


def test_probe_skipped_is_not_running():
    # both opt-ins: no probe, its result skipped (no stdout)
    facts = task("Decide whether lvchange runs now (not under a running Cassandra)")["ansible.builtin.set_fact"]
    variables = dict(_cassandra_data_lvm_here=True, cassandra_data_lvchange_running=True,
                     cassandra_data_running={"skipped": True, "changed": False})
    assert render(facts["_cassandra_data_running"], **variables) is False
    assert render(facts["_cassandra_data_lvchange_now"], **variables) is True


@pytest.mark.parametrize("deferred, check_mode, switch, listed", [
    (True, False, {"skipped": True}, False),
    (False, False, {"failed": True, "msg": "timed out"}, False),
    (False, False, {"rc": 0, "changed": True}, True),
    (False, True, {"skipped": True}, True),
])
def test_profile_change_listed_only_when_done(deferred, check_mode, switch, listed):
    items = task("Compare the disk tuning")["ansible.builtin.set_fact"]["_cassandra_disk_items"]
    out = render(items, _cassandra_disk_changes_seen=[], _cassandra_disk_lvm_failed=[],
                 _cassandra_disk_tuned={"name": "cassandra-disks", "active": ["virtual-guest"],
                                        "profiles": ["virtual-guest", "cassandra-disks"]},
                 _cassandra_disk_tuned_deferred=deferred, ansible_check_mode=check_mode,
                 cassandra_data_tuned_switch=switch, cassandra_data_disk_udev={"changed": False})
    assert ("tuned profiles" in [i["item"] for i in out]) is listed
