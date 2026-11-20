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


def test_not_reached_says_only_what_was_observed():
    # Ansible did not reach the host: never "restart the server" nor "replace the node" (the server
    # and Cassandra may well run); the command that shows all of the error, the option to go on
    from ansible_collections.community.cassandra.plugins.filter.cassandra_output import cassandra_not_reached
    lines = cassandra_not_reached(["h-n3"], inventory=["/inv/hosts.yml"], default_inventory="/etc/ansible/hosts",
                                  cwd="/inv")
    # a refusal's block: 79 columns, an option kept with its value
    assert lines == ["REFUSED  h-n3 not reached by Ansible  nothing was changed",
                     "  Ansible's error: the UNREACHABLE line above",
                     "  all of the error:     ansible -i hosts.yml h-n3 -m ping -vvv",
                     "  to go on without it:  -e cassandra_allow_down_nodes=true",
                     "                        (the cluster health checks then need",
                     "                          -e cassandra_service_health_force=true)"]
    two = cassandra_not_reached(["n3", "n4"], inventory=["/etc/ansible/hosts"], default_inventory="/etc/ansible/hosts")
    assert two[:3] == ["REFUSED  2 nodes not reached by Ansible  nothing was changed", "  n3", "  n4"]
    assert "  all of the error:       ansible n3,n4 -m ping -vvv" in two
    text = " ".join(lines + two).lower()
    assert "restart" not in text and "replace" not in text and "server" not in text


def test_preflight_stops_on_a_node_not_reached_with_that_text():
    import os
    import yaml
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    with open(os.path.join(root, "playbooks", "preflight.yml")) as f:
        tasks = [t for play in yaml.safe_load(f) for t in play.get("tasks", [])]
    answers = next(t for t in tasks if t["name"] == "Every node answers")["ansible.builtin.assert"]["fail_msg"]
    assert "cassandra_not_reached(" in answers and "Restart" not in answers and "replace" not in answers


def test_preflight_mixed_versions_as_rows_with_exact_ways_out():
    # the versions as rows (no "5.0.9: [..]" inline), each way out a command or an exact option
    import os
    import yaml
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    try:
        from ansible.template import trust_as_template
    except ImportError:
        def trust_as_template(template):
            return template
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    with open(os.path.join(root, "playbooks", "preflight.yml")) as f:
        tasks = [t for play in yaml.safe_load(f) for t in play.get("tasks", []) for t in [t] + t.get("block", [])]
    task = next(t for t in tasks if t["name"] == "One Cassandra version in the cluster")
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in task["vars"].items()
                     if k not in ("_section", "_versions"))
    variables["_versions"] = ["5.0.9: [10.0.0.1, 10.0.0.2]", "4.1.10: [10.0.0.3]"]
    text = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(task["ansible.builtin.assert"]["fail_msg"]))
    assert text.split("\n")[:4] == [
        "REFUSED  the cluster runs 2 Cassandra versions (an upgrade is not finished)  nothing was changed",
        "  Version  Nodes", "  5.0.9    10.0.0.1, 10.0.0.2", "  4.1.10   10.0.0.3"]
    assert "    finish the upgrade:  ansible-playbook community.cassandra.upgrade" in text
    assert "    at your own risk:    -e cassandra_allow_mixed_versions=true" in text
