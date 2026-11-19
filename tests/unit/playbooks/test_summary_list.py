from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The summaries are lists (one line per node), not the text of a list: the
# template must be only the list, without the whitespace around the tags.

import os
import warnings

import yaml

from ansible.plugins.loader import init_plugin_loader
from ansible_collections.community.cassandra.tests.unit.helpers import render_as_given

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    return render_as_given(template, variables)


def op_results(**variables):
    """the role var _cassandra_op_results (lazy), _cassandra_op_limit with it"""
    defaults = load("roles", "cassandra_operation", "vars", "main.yml")
    variables["_cassandra_op_limit"] = render(defaults["_cassandra_op_limit"], **variables)
    variables["_cassandra_op_limited"] = render(defaults["_cassandra_op_limited"], **variables)
    return render(defaults["_cassandra_op_results"], **variables)


def test_operation_summary_is_a_recap():
    task = load("roles", "cassandra_operation", "tasks", "summary.yml")[0]
    hostvars = {"n1": {"cassandra_op_result": "add done in 5s"}, "n2": {}, "n3": {}}
    variables = dict(cassandra_service_summary_hosts=["n1", "n2", "n3"], hostvars=hostvars, groups={"prod": ["n1", "n2", "n3"]}, cassandra_hosts="prod",
                     ansible_play_hosts_all=["n1", "n2"], ansible_check_mode=False, cassandra_cluster_name="my_cluster",
                     _seconds=None, ansible_limit="n1,n2")
    defaults = load("roles", "cassandra_operation", "vars", "main.yml")  # the results list: a role var (lazy)
    variables["_cassandra_op_results"] = op_results(**variables)
    assert variables["_cassandra_op_results"] == [{"node": "n1", "result": "add done in 5s", "stage": ""},
                                                  {"node": "n2", "result": "not reached", "stage": ""},
                                                  {"node": "n3", "result": "not in this run (--limit)", "stage": ""}]
    # no --limit (replace_node, S16: its other nodes not in its play): never said left out by --limit; nor a node
    # the --limit has, out of the play
    for limit in ({}, {"ansible_limit": "n1,n3"}):
        unlimited = dict((k, v) for k, v in variables.items()
                         if k not in ("ansible_limit", "_cassandra_op_results", "_cassandra_op_limit", "_cassandra_op_limited"))
        unlimited.update(limit)
        results = op_results(**unlimited)
        assert results[2] == {"node": "n3", "result": "not in this run", "stage": ""}
        unlimited["_cassandra_op_results"] = results
        said = [" ".join(line.split()) for line in render(task["ansible.builtin.debug"]["msg"], **dict(
            unlimited, _cluster="my_cluster", _warnings=[]))]
        assert said[-1] == "n3 not touched (not in this run)" and "--limit" not in " ".join(said)
    variables["_cluster"] = render(task["vars"]["_cluster"], **variables)
    variables["_warnings"] = render(task["vars"]["_warnings"], **variables)
    assert variables["_warnings"] == []
    # a verdict, the nodes grouped (a list: a line each under any callback)
    lines = [" ".join(line.split()) for line in render(task["ansible.builtin.debug"]["msg"], **variables)]
    assert lines == ["DONE add_node my_cluster 1 added, 2 not touched", "n1 added in 5s", "n2 not touched (not reached)",
                     "n3 not touched (left out by --limit)"]
    # a package update that changed a limits file left to the node (cassandra_linux_manage: false): said last
    changed = dict(variables)
    changed["hostvars"] = dict(hostvars, n1=dict(hostvars["n1"], cassandra_package_limits_changed="/etc/x.conf"))
    changed["_warnings"] = render(task["vars"]["_warnings"], **changed)
    said = render(task["ansible.builtin.debug"]["msg"], **changed)
    assert said[:len(lines)] == render(task["ansible.builtin.debug"]["msg"], **variables)
    assert said[len(lines) + 1] == "WARNING  n1: the Cassandra package update replaced /etc/x.conf"
    variables.update(cassandra_service_summary_operation="remove_datacenter")
    assert render(task["ansible.builtin.debug"]["msg"], **variables)[0].startswith("DONE  remove_datacenter  my_cluster")
    variables.update(ansible_check_mode=True)
    assert render(task["ansible.builtin.debug"]["msg"], **variables)[0].startswith("CHECK  remove_datacenter  my_cluster")
    # the cluster as the run's first line names it (its inventory group, cluster_banner.yml)
    shown = dict(variables)
    shown["hostvars"] = dict(hostvars, localhost={"_cassandra_cluster_shown": "prod"})
    shown["_cluster"] = render(task["vars"]["_cluster"], **shown)
    assert render(task["ansible.builtin.debug"]["msg"], **shown)[0].startswith("CHECK  remove_datacenter  prod  ")


def test_health_check_report_inputs_are_a_list_and_a_dict():
    play = load("playbooks", "health_check.yml")
    task = next(t for p in play for t in p.get("tasks", []) if t.get("name") == "Report")
    found = [{"kind": "gossip", "on": "n1", "text": "gossip is not running on n1"}]
    hostvars = {"n1": {"cassandra_health_findings": found}, "n2": {}}
    checked = render(task["vars"]["_checked"], ansible_play_hosts=["n1", "n2"], hostvars=hostvars)
    assert checked == ["n1"]  # n2: its check did not run (not reached)
    assert render(task["vars"]["_findings"], _checked=checked, hostvars=hostvars) == {"n1": found}


def test_a_limit_from_a_file_is_read_for_the_recap(tmp_path, monkeypatch):
    # --limit @retry.txt (a retry file): the inventory_hostnames lookup matched nothing (a [WARNING] in the run),
    # so every node out of the play was said "left out by --limit", the ones the file lists too. Now the file's
    # hosts read as Ansible reads them (from the run's directory); a file not read: "not in this run"
    (tmp_path / "retry.txt").write_text("n1\n\nn2\n")
    monkeypatch.chdir(tmp_path)
    variables = dict(cassandra_service_summary_hosts=["n1", "n2", "n3", "n4"], hostvars={"n1": {}, "n2": {}, "n3": {}, "n4": {}},
                     groups={"prod": ["n1", "n2", "n3", "n4"]}, cassandra_hosts="prod", ansible_play_hosts_all=["n1"])
    assert [r["result"] for r in op_results(ansible_limit="@retry.txt", **variables)] == [
        "not reached", "not in this run", "not in this run (--limit)", "not in this run (--limit)"]
    assert [r["result"] for r in op_results(ansible_limit="n3,@retry.txt", **variables)][2:] == [
        "not in this run", "not in this run (--limit)"]
    assert [r["result"] for r in op_results(ansible_limit="@missing.txt", **variables)][1:] == ["not in this run"] * 3
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import cassandra_limit_patterns
    assert cassandra_limit_patterns("n3,@retry.txt") == "n3,n1,n2" and cassandra_limit_patterns("") == ""
    assert cassandra_limit_patterns("all:!n3") == "all,!n3"  # (the same patterns)
    # a file's line is one pattern for Ansible, never split: "n1:n2" matches no host (it took n1 and n2); an
    # IPv6 host name stays whole; the path as written (Ansible expands no ~)
    (tmp_path / "odd.txt").write_text("n1:n2\nn3,n4\n2001:db8::5\nn4\n")
    assert cassandra_limit_patterns("@odd.txt") == "2001:db8::5,n4"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cassandra_limit_patterns("@~/retry.txt") == ""
