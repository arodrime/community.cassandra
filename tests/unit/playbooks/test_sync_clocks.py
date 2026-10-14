from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# sync_clocks' plays: what ties a window an interrupted run left open to the next run (preflight lets that seed be
# down, its window first, only it tolerated down), and the reads that stop the run when a node is not read.

import os

import yaml

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def plays():
    with open(os.path.join(TOP, "playbooks", "sync_clocks.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def play(name):
    return [p for p in plays() if p.get("name") == name][0]


def test_preflight_lets_a_member_left_down_through():
    assert play("Preflight")["vars"] == {"_cassandra_preflight_members_down_ok": True}
    with open(os.path.join(TOP, "playbooks", "preflight.yml"), encoding="utf-8") as f:
        text = f.read()
    assert "or (_cassandra_preflight_members_down_ok | default(false) | bool and _addr in _all)" in text


def test_a_window_left_open_comes_first_and_alone_may_be_down():
    window = play("Set the clocks in a restart window, one node at a time")
    hosts = " ".join(window["hosts"].split())
    first, rest = hosts.split(" + ", 1)
    assert "groups['cassandra_window_left_True']" in first
    assert "reject('in', groups['cassandra_window_left_True']" in rest
    assert window["serial"] == 1 and window["any_errors_fatal"] is True
    assert window["vars"]["cassandra_rolling_resume"] == (
        "{{ inventory_hostname in groups['cassandra_window_left_True'] | default([]) }}")
    assert "run sync_clocks again" in window["vars"]["cassandra_service_resume_hint"]
    progress = play("Set up the progress file")
    assert progress["vars"]["_cassandra_progress_resume"] is False  # a fresh file: the clocks read again say the rest


def test_a_node_not_read_stops_the_run():
    assert play("Read the clocks")["any_errors_fatal"] is True
