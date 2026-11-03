from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The titles, questions, recaps and stop blocks name the cluster as the run's
# first line does: its inventory group (cluster_banner.yml keeps it in
# hostvars['localhost']._cassandra_cluster_shown), not cassandra_cluster_name.

import glob
import os
import re
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
    init_plugin_loader()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
FILES = sorted(glob.glob(os.path.join(TOP, "playbooks", "*.yml"))
               + glob.glob(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "*.yml")))
NAMES = ("_cluster", "_cluster_name", "_plan_cluster")  # the variables the display tasks name the cluster with

# each display task that names the cluster (file, variable): the ones the group must reach
SITES = [
    ("add_node.yml", "_cluster"), ("add_node.yml", "_cluster"), ("add_node.yml", "_cluster"), ("add_node.yml", "_cluster"),
    ("apply_config.yml", "_cluster"), ("apply_config.yml", "_cluster"),
    ("cleanup_batch.yml", "_cluster"), ("clocks_check.yml", "_cluster"),
    ("decommission_node.yml", "_cluster_name"), ("decommission_node.yml", "_cluster_name"),
    ("node_operation.yml", "_cluster"), ("reset_node.yml", "_cluster"), ("restart_batch.yml", "_cluster"),
    ("rolling_reboot.yml", "_cluster"), ("rolling_restart.yml", "_cluster"), ("summary.yml", "_cluster"),
    ("topology.yml", "_plan_cluster"), ("update_java.yml", "_cluster"), ("upgrade.yml", "_cluster"),
]

# cassandra_cluster_name said or used as itself, on purpose: the checks of the name, the data loss texts and the
# typed confirmation (the name Cassandra knows), the variables a run requires or records
ALLOWED = {
    "cluster_name.yml": None,  # the whole file: the check of the name Cassandra reports
    "reset_node.yml": ("_given in", "| length > 0", "cassandra_create_cluster_reset_confirm", "DATA LOSS",
                       "user_input"),
    "create_cluster.yml": ("- cassandra_cluster_name", "ALL THE DATA of cluster"),
    "preflight.yml": ("- cassandra_cluster_name",),
    "node_facts.yml": ("cassandra_cluster_name: ",),
    "node_standing.yml": ("cassandra_node_standing(",),
    "import_cluster.yml": ("selectattr('cassandra_cluster_name'", "vars.cassandra_cluster_name"),
}


def vars_of(node):
    """Every vars mapping of a loaded YAML file (plays, tasks, blocks, nested)."""
    if isinstance(node, dict):
        if isinstance(node.get("vars"), dict):
            yield node["vars"]
        for value in node.values():
            yield from vars_of(value)
    elif isinstance(node, list):
        for item in node:
            yield from vars_of(item)


def sites():
    found = []
    for path in FILES:
        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        for variables in vars_of(loaded):
            for name in NAMES:
                if "_cassandra_cluster_shown" in str(variables.get(name, "")):
                    found.append((os.path.basename(path), name, variables[name]))
    return found


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_every_display_site_names_the_group_of_the_first_line():
    found = sites()
    assert sorted((f, n) for f, n, dummy in found) == sorted(SITES)
    preflight = {"cassandra_cluster_name": "Real Name"}
    known = dict(cassandra_cluster_name="Real Name", _cassandra_preflight=preflight, _p0=preflight, _p=preflight,
                 ansible_play_hosts_all=["n1"])
    for where, name, template in found:
        shown = render(template, hostvars={"localhost": {"_cassandra_cluster_shown": "orders_eu"},
                                           "n1": {"_cassandra_preflight": preflight}}, **known)
        assert shown == "orders_eu", (where, name, shown)
        # no first line (a task run on its own): the cluster's name, as before
        alone = render(template, hostvars={"n1": {"_cassandra_preflight": preflight}}, **known)
        assert alone == "Real Name", (where, name, alone)


def test_the_cluster_arguments_of_the_display_filters_are_those_variables():
    for path in FILES:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for argument in re.findall(r"(?<![\w.])cluster=([^,)\s]+)", text):
            assert argument in ("_cluster", "_tp_cluster", "lookup('community.cassandra.cassandra_hosts'"), (path, argument)


def test_no_display_task_says_cassandra_cluster_name_itself():
    for path in FILES:
        allowed = ALLOWED.get(os.path.basename(path), ())
        if allowed is None:
            continue
        with open(path, encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                if ("cassandra_cluster_name" not in line or line.strip().startswith("#")
                        or "_cassandra_cluster_shown" in line or any(a in line for a in allowed)):
                    continue
                raise AssertionError("%s:%d says cassandra_cluster_name: %s" % (path, number, line.strip()))
