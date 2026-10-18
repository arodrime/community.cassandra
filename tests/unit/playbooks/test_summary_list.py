from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The summaries are lists (one line per node), not the text of a list: the
# template must be only the list, without the whitespace around the tags.

import os
import warnings

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


def test_operation_summary_is_a_recap():
    task = load("roles", "cassandra_operation", "tasks", "summary.yml")[0]
    hostvars = {"n1": {"cassandra_op_result": "restart done in 5s"}, "n2": {}, "n3": {}}
    variables = dict(hostvars=hostvars, groups={"prod": ["n1", "n2", "n3"]}, cassandra_hosts="prod",
                     ansible_play_hosts_all=["n1", "n2"], ansible_check_mode=False, cassandra_cluster_name="my_cluster")
    variables["_results"] = render(task["vars"]["_results"], **variables)
    assert variables["_results"] == [{"node": "n1", "result": "restart done in 5s"}, {"node": "n2", "result": "not reached"},
                                     {"node": "n3", "result": "not in this run (--limit)"}]
    # a verdict, the nodes grouped (a list: a line each under any callback)
    lines = [" ".join(line.split()) for line in render(task["ansible.builtin.debug"]["msg"], **variables)]
    assert lines == ["DONE rolling_restart my_cluster 1 restarted, 2 not touched", "n1 restarted 5s", "n2 not touched (not reached)",
                     "n3 not touched (left out by --limit)"]
    variables.update(cassandra_service_node_action="reboot")
    assert render(task["ansible.builtin.debug"]["msg"], **variables)[0].startswith("DONE  rolling_reboot  my_cluster")
    variables.update(ansible_check_mode=True)
    assert render(task["ansible.builtin.debug"]["msg"], **variables)[0].startswith("CHECK  rolling_reboot  my_cluster")


def test_health_check_report_inputs_are_a_list_and_a_dict():
    play = load("playbooks", "health_check.yml")
    task = next(t for p in play for t in p.get("tasks", []) if t.get("name") == "Report")
    found = [{"kind": "gossip", "on": "n1", "text": "gossip is not running on n1"}]
    hostvars = {"n1": {"cassandra_health_findings": found}, "n2": {}}
    checked = render(task["vars"]["_checked"], ansible_play_hosts=["n1", "n2"], hostvars=hostvars)
    assert checked == ["n1"]  # n2: its check did not run (not reached)
    assert render(task["vars"]["_findings"], _checked=checked, hostvars=hostvars) == {"n1": found}
