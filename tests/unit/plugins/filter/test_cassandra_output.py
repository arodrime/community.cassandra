from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The cassandra_progress_line filter: the other ends of the streams summed per node, in columns at the
# terminal's width, after a blank line; the cassandra_normal_wait_line filter: the wait for a node to be NORMAL.

from ansible_collections.community.cassandra.plugins.filter import cassandra_output as module

GIB = 1024.0 ** 3


def _state(count):
    streams = {}
    for i in range(1, count + 1):
        streams["s%d" % i] = {"other": "10.0.0.%d" % i, "way": "to", "done": 2 * GIB, "total": 4 * GIB,
                              "moved": True, "since": 0}
    return {"streams": streams, "bytes_done": 2 * GIB * count, "bytes_total": 4 * GIB * count, "now": 100,
            "start": 0}


def test_the_peers_in_columns_at_the_terminals_width(monkeypatch):
    names = dict(("10.0.0.%d" % i, "node%d" % i) for i in range(1, 7))

    class Display(object):
        columns = 200
    monkeypatch.setattr(module, "Display", Display)
    wide = module.cassandra_progress_line(_state(6), 1, 2, "node9", "decommission", "LEAVING", names)
    Display.columns = 60
    narrow = module.cassandra_progress_line(_state(6), 1, 2, "node9", "decommission", "LEAVING", names)
    assert wide[0].startswith("[1/2] node9  decommission  LEAVING  [#####-----]  50%  12.0/24.0 GiB  ") and wide[1] == ""
    assert wide[2:] == ["      to node1  2.0/4.0 GiB  50%   to node2  2.0/4.0 GiB  50%   to node3  2.0/4.0 GiB  50%"
                        "   to node4  2.0/4.0 GiB  50%",
                        "      to node5  2.0/4.0 GiB  50%   to node6  2.0/4.0 GiB  50%"]
    assert len(narrow) == 8 and all(len(line) <= 60 for line in narrow[2:])  # one a line


def test_a_peer_quiet_for_a_minute_with_data_left_stalled(monkeypatch):
    class Display(object):
        columns = 60
    monkeypatch.setattr(module, "Display", Display)
    state = _state(2)
    state["streams"]["s2"].update(moved=False, since=30)
    lines = module.cassandra_progress_line(state, 1, 2, "node9", "decommission", "LEAVING")
    assert lines[1:] == ["", "      to 10.0.0.1  2.0/4.0 GiB  50%", "      to 10.0.0.2  2.0/4.0 GiB  50% STALLED"]
    state["streams"]["s2"]["since"] = 50  # 50 s: not yet
    assert "STALLED" not in "".join(module.cassandra_progress_line(state, 1, 2, "node9", "decommission")[1:])
    # the node's line STALLED (the state's warn reached): the peer in cause only, the other summed up
    Display.columns = 100
    state["streams"]["s2"]["since"] = 0
    state.update(idle=100, idle_checks=3, limit=1800, warn=100, rate=0)
    lines = module.cassandra_progress_line(state, 1, 2, "node9", "decommission", "LEAVING", status="going")
    assert lines == ["[1/2] node9  decommission  LEAVING  STALLED 1m/30m  50%  4.0/8.0 GiB  1m", "",
                     "      to 10.0.0.2  2.0/4.0 GiB  50%  nothing for 1m    (10.0.0.1 50%)"]


def test_the_done_line_of_a_stream():
    state = dict(_state(2), bytes_done=4 * GIB, bytes_total=4 * GIB, now=600, start=0)
    assert module.cassandra_progress_line(state, 1, 2, "node9", "bootstrap", "NORMAL", status="done") == \
        ["[1/2] node9  bootstrapped  NORMAL  4.0 GiB in 10m00s  avg 6.8 MiB/s"]


def test_the_normal_wait_line():
    assert module.cassandra_normal_wait_line("JOINING", waited=70, timeout=600, node="node2", operation="restart") == \
        "node2  restart  waiting  UJ, waiting for UN  1m10s/10m"
    assert module.cassandra_normal_wait_line("STARTING", waited=5, timeout=600, index=2, steps=5, node="node2",
                                             operation="restart") == \
        "[2/5] node2  restart  waiting  mode STARTING, waiting for NORMAL  5s/10m"
    assert module.cassandra_normal_wait_line("", waited=10, timeout=600, node="node2", operation="start") == \
        "node2  start  waiting  no JMX answer yet, waiting for NORMAL  10s/10m"
    # the step over: its frozen line, the step's whole time when given
    assert module.cassandra_normal_wait_line("NORMAL", done=True, waited=80, node="node2", operation="restart") == \
        "node2  restarted  NORMAL after 1m20s"
    assert module.cassandra_normal_wait_line("NORMAL", done=True, waited=30, index=1, steps=5, node="node1",
                                             operation="restart", elapsed=110) == \
        "[1/5] node1  restarted  NORMAL after 30s  1m50s"
    assert module.cassandra_normal_wait_line("NORMAL", done=True, waited=30, node="node1", operation="write_config") == \
        "node1  config written  NORMAL after 30s"
