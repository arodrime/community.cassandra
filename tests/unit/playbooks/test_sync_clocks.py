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


def test_a_node_unreachable_or_not_read_stops_the_run_before_the_screen():
    read = play("Read the clocks")
    assert read["any_errors_fatal"] is True  # unreachable: jvm_started.yml fails there
    names = [t["name"] for t in read["tasks"]]
    refuse = read["tasks"][names.index("Refuse to go on when a clock could not be read")]
    assert names.index("Refuse to go on when a clock could not be read") < names.index("Show the clocks and confirm the run")
    assert refuse["run_once"] is True and refuse["ansible.builtin.assert"]["that"] == "_unread | length == 0"
    assert "selectattr('_cassandra_clock_action', 'equalto', 'unread')" in refuse["vars"]["_unread"]
    # the result words of the live play, as the recap knows them (test_cassandra_clocks)
    live = " ".join(str(play("Set the clocks live")["tasks"]).split())
    assert "'clock_' ~ (_cassandra_clock_did | join('_') or 'slew')" in live
