from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_operation cluster_clocks.yml, health_check's CLOCKS block: read-only (one read, nothing started or
# waited for), skipped by cassandra_clock_check, a node not read said so, a clock not in sync one finding of the
# report on every node.

import os

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def tasks():
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "cluster_clocks.yml"), encoding="utf-8") as f:
        block = yaml.safe_load(f)
    assert [t["name"] for t in block] == ["Read the clocks"]
    return dict((t["name"], t) for t in block[0]["block"]), block[0]


def render(template, **variables):
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:  # ansible-core 2.19+ renders trusted templates only
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_the_block_is_read_only_and_skipped_by_the_same_switch_as_the_start_check():
    by_name, block = tasks()
    assert block["when"] == "cassandra_clock_check | bool"
    read = by_name["Read and compare the clocks"]
    assert read["ansible.builtin.include_tasks"] == "clocks_read.yml"
    assert read["vars"] == {"_cassandra_clock_wait": 0}  # one read, no chronyd -Q (root, 30 s), nothing started
    assert by_name["Note Cassandra runs here"]["ansible.builtin.set_fact"] == {"_cassandra_clock_running": True}
    show = by_name["Show the clocks"]
    assert show["run_once"] is True and show["vars"]["cassandra_output"] is True
    assert show["ansible.builtin.debug"]["msg"] == "{{ _cassandra_clock_health.lines }}"


def test_the_nodes_not_read_are_named_with_their_message():
    by_name = tasks()[0]
    note = by_name["Note what the clocks say"]
    assert note["run_once"] is True
    hostvars = {"n1": {"_cassandra_clock_read_ok": True, "_cassandra_clock_last": {"stdout": "..."}},
                "n2": {"_cassandra_clock_read_ok": False, "_cassandra_clock_last": {"failed": True, "msg": "MODULE FAILURE\nSee stdout"}},
                "n3": {"_cassandra_clock_read_ok": False, "_cassandra_clock_last": {"unreachable": True, "msg": " ssh: no route "}},
                "n4": {}}  # the probe never ran there (gone from the play): not in the block
    unread = render(note["vars"]["_unread"], hostvars=hostvars, ansible_play_hosts=["n1", "n2", "n3"])
    assert unread == {"n2": "MODULE FAILURE See stdout", "n3": "ssh: no route"}


def test_a_clock_not_in_sync_is_one_finding_on_every_node():
    by_name = tasks()[0]
    count = by_name["Count a clock not in sync among the problems"]
    assert "run_once" not in count  # each node's findings are the report's
    assert count["when"] == "_cassandra_clock_health.finding is not none"
    finding = {"kind": "clocks", "text": "n2 OFF (CLOCKS above says why)", "command": "ansible-playbook community.cassandra.sync_clocks --check"}
    found = render(count["ansible.builtin.set_fact"]["cassandra_health_findings"],
                   cassandra_health_findings=[{"kind": "streams", "on": "n1", "text": "streams in progress on n1"}],
                   _cassandra_clock_health={"lines": [], "finding": finding})
    assert found == [{"kind": "streams", "on": "n1", "text": "streams in progress on n1"}, finding]
    when = "{{ %s }}" % count["when"]
    assert render(when, _cassandra_clock_health={"lines": [], "finding": None}) is False
    assert render(when, _cassandra_clock_health={"lines": [], "finding": finding}) is True


def test_health_check_reads_the_clocks_after_the_cluster_and_before_its_report():
    with open(os.path.join(TOP, "playbooks", "health_check.yml"), encoding="utf-8") as f:
        play = [p for p in yaml.safe_load(f) if p["name"] == "Cluster health"][0]
    names = [t["name"] for t in play["tasks"]]
    clocks = play["tasks"][names.index("Read the clocks")]
    assert clocks["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_operation",
                                                      "tasks_from": "cluster_clocks.yml"}
    assert names.index("Check the cluster from this node") < names.index("Read the clocks") < names.index("Report")
