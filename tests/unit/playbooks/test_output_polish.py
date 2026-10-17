from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The operator output of the node operations: the service report left to the
# recap, the end of reset_node, the stop on a setting that differs, the import's
# --diff, rendered from the playbooks themselves.

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
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def tasks(tree):
    for t in tree or []:
        yield t
        for key in ("block", "rescue", "always", "tasks"):
            yield from tasks(t.get(key))


@pytest.mark.parametrize("action", ["add", "apply_config", "decommission", "reboot", "replace", "restart", "upgrade"])
def test_the_service_report_is_left_to_the_recap(action):
    starts = [t for t in tasks(load("roles", "cassandra_operation", "tasks", "action_%s.yml" % action))
              if (t.get("ansible.builtin.include_role") or {}).get("tasks_from") == "main.yml"]
    assert starts, action
    for t in starts:
        quiet = t["vars"]["_cassandra_change_report_quiet"]
        assert render(quiet, ansible_verbosity=0) is True and render(quiet, ansible_verbosity=1) is False


def test_the_service_report_says_would_under_check():
    report = next(t for t in load("roles", "cassandra_service", "tasks", "main.yml") if t.get("name") == "Report")
    v = dict(report["vars"])

    def items(check, state, enabled, before):
        variables = dict(v, ansible_check_mode=check, cassandra_service_state=state, cassandra_service_enabled=enabled,
                         _cassandra_service_before=before, cassandra_service_unit={"changed": True})
        variables["_want_active"] = render(v["_want_active"], **variables)
        variables["_would"] = render(v["_would"], **variables)
        return [(i["item"], i["after"]) for i in render(v["cassandra_change_report_items"], **variables)]

    before = {"unit": "/run/systemd/generator.late/cassandra.service", "active": "inactive", "enabled": "generated"}
    assert items(True, "started", True, before) == [("/etc/systemd/system/cassandra.service", "would be a native unit"),
                                                    ("cassandra", "would be active"), ("cassandra at boot", "would be enabled")]
    assert items(False, "started", True, before) == [("/etc/systemd/system/cassandra.service", "native unit"),
                                                     ("cassandra", "active"), ("cassandra at boot", "enabled")]
    assert items(True, "restarted", True, dict(before, active="active", enabled="enabled"))[1:] == [("cassandra", "would restart")]


def test_reset_node_ends_with_a_verdict_and_the_add_node_command():
    play = load("playbooks", "reset_node.yml")[1]
    t = play["tasks"][-1]
    assert t["vars"]["cassandra_output"] is True and t["run_once"] is True
    hostvars = {"node4": {"_cassandra_node_reset_plan": {"delete": ["/d/a", "/d/b"], "stop": True, "disable": True}},
                "node5": {"_cassandra_node_reset_plan": {"delete": [], "stop": False, "disable": False}}}
    variables = dict(hostvars=hostvars, ansible_play_hosts_all=["node4", "node5"], cassandra_cluster_name="my_cluster",
                     ansible_inventory_sources=[], cassandra_hosts="my_cluster", ansible_check_mode=False)
    variables["_nodes"] = render(t["vars"]["_nodes"], **variables)
    lines = render(t["ansible.builtin.debug"]["msg"], **variables)
    assert lines[0] == "DONE  reset_node  my_cluster  node4 emptied (2 entries deleted), Cassandra stopped and disabled on node4"
    assert lines[1] == "  node5  nothing to do (empty, Cassandra stopped and disabled)"
    assert lines[-3:] == ["TO DO", "  1. add it back to the cluster:",
                          "     ansible-playbook community.cassandra.add_node -e cassandra_hosts=my_cluster"
                          " -e cassandra_target_nodes=node4"]
    variables["ansible_check_mode"] = True
    assert render(t["ansible.builtin.debug"]["msg"], **variables)[0].startswith("CHECK  reset_node  my_cluster  would empty node4")


def test_a_setting_that_differs_stops_with_a_sentence():
    play = load("playbooks", "preflight.yml")
    t = next(t for p in play for t in tasks(p.get("tasks")) if t.get("name") == "Settings that must match on every node")
    assert t["vars"]["cassandra_output"] is True  # printed as is by the ops callback, not as a result dict
    hostvars = {"node1": {"_cassandra_preflight": {"_cassandra_service_seeds": ["10.0.0.1"]}},
                "node2": {"_cassandra_preflight": {"_cassandra_service_seeds": ["10.0.0.2"]}}}
    msg = render(t["ansible.builtin.assert"]["fail_msg"].replace("lookup('community.cassandra.cassandra_hosts')", "'prod'"),
                 hostvars=hostvars, ansible_play_hosts=["node1", "node2"], item="_cassandra_service_seeds")
    assert msg.startswith("The seed list (cassandra_seeds) is not the same on every node of prod (node1: 10.0.0.1; node2: 10.0.0.2)")
    assert "_cassandra_service_seeds" not in msg and "{" not in msg


def test_the_import_shows_no_whole_new_file_under_diff():
    play = load("playbooks", "import_cluster.yml")
    found = {t["name"]: t for p in play for t in tasks(p.get("tasks")) if "name" in t}
    hosts, files = found["Write the hosts file"], found["Write group_vars and host_vars"]
    existing = {"hosts.yml": "0644", "group_vars/prod/main.yml": "0644"}
    for on in (True, False):  # without --diff: none (the keyword sets the diff mode, it does not only narrow it)
        v = dict(import_cluster_existing_modes=existing, ansible_diff_mode=on)
        assert render(hosts["diff"], _hosts_file="hosts.yml", **v) is on
        assert render(hosts["diff"], _hosts_file="prod.yml", **v) is False
        assert render(files["diff"], item={"path": "group_vars/prod/main.yml"}, **v) is on
        assert render(files["diff"], item={"path": "host_vars/node1.yml"}, **v) is False


def test_add_node_cleanup_recap_is_the_cleanup_s():
    """A cleanup that fails after the add's own DONE recap: a recap named cleanup, of the cleanup's nodes only
    (not a second add_node one, with the nodes added in it)."""
    with open(os.path.join(TOP, "playbooks", "add_node.yml"), encoding="utf-8") as f:
        play = next(p for p in yaml.safe_load(f) if p.get("name") == "Clean up the nodes that handed data over")
    assert play["vars"]["cassandra_service_summary_operation"] == "cleanup"
    hosts = play["vars"]["cassandra_service_summary_hosts"]
    assert hosts.startswith("{{ ansible_play_hosts_all | select('in', _plan.cleanup")  # a node failed earlier too
