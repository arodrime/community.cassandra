from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The cassandra_progress_line filter: the other ends of the streams summed per node, in columns at the
# terminal's width.

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
    assert wide[1:] == ["      to node1  2.0/4.0 GiB  50%   to node2  2.0/4.0 GiB  50%   to node3  2.0/4.0 GiB  50%"
                        "   to node4  2.0/4.0 GiB  50%",
                        "      to node5  2.0/4.0 GiB  50%   to node6  2.0/4.0 GiB  50%"]
    assert len(narrow) == 7 and all(len(line) <= 60 for line in narrow[1:])  # one a line


def test_a_peer_quiet_for_a_minute_with_data_left_stalled(monkeypatch):
    class Display(object):
        columns = 60
    monkeypatch.setattr(module, "Display", Display)
    state = _state(2)
    state["streams"]["s2"].update(moved=False, since=30)
    lines = module.cassandra_progress_line(state, 1, 2, "node9", "decommission", "LEAVING")
    assert lines[1:] == ["      to 10.0.0.1  2.0/4.0 GiB  50%", "      to 10.0.0.2  2.0/4.0 GiB  50% STALLED"]
    state["streams"]["s2"]["since"] = 50  # 50 s: not yet
    assert "STALLED" not in "".join(module.cassandra_progress_line(state, 1, 2, "node9", "decommission")[1:])
