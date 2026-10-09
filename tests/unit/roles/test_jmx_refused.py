from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A JMX login refused during a wait (stream_check.yml, cleanup_check.yml):
# the wait stops at that check with a message naming the node and the
# variables, rather than after cassandra_stream_stall_time.

import os

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks")


def load(name):
    with open(os.path.join(TASKS, name)) as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_each_check_reads_the_refusals_and_stops_on_them():
    block = load("stream_check.yml")[0]["block"]
    where = next(t for t in block if t["name"] == "Where it stands")
    facts = where["ansible.builtin.set_fact"]
    assert "refused=_refused" in facts["_cassandra_stream_now"] and facts["_cassandra_stream_refused"] == "{{ _refused }}"
    refused = where["vars"]["_refused"]
    for read in ("cassandra_stream_read.results", "cassandra_stream_read_peers.results", "cassandra_stream_seen.results"):
        assert read in refused
    # every variable the facts read is there (the real wait, tests/unit/roles/test_wait_leave.py, runs it)
    assert set(where["vars"]) == {"_refused", "_own_login", "_seen"}
    # the joining node's own refusals: only the ones for good (cassandra_jmx_refused's joining)
    assert "joining=inventory_hostname if _cassandra_stream_join" in refused
    # the other side's: only when none of them answered (one node with another login does not stop a join)
    assert "if cassandra_stream_read_peers.results | default([]) | selectattr('sessions', 'defined') | list | length == 0" \
        in refused
    cleanup = load("cleanup_check.yml")[0]["block"]
    where = next(t for t in cleanup if t["name"] == "Where it stands")
    assert "else 'jmx_refused' if _refused" in where["ansible.builtin.set_fact"]["_cassandra_cleanup_now"]
    assert "cassandra_cleanup_read.results | community.cassandra.cassandra_jmx_refused(" in where["vars"]["_refused"]
    assert "answered=_cassandra_stream_state.answered_hosts | default([]), hosts=_own_login" in where["vars"]["_refused"]


def test_the_stop_names_the_node_and_the_login_variables():
    stop = next(t for t in load("stream_wait.yml") if t["name"] == "Stop when it failed or stopped moving")
    msg = render(stop["ansible.builtin.fail"]["msg"], inventory_hostname="node5", _cassandra_stream_status="jmx_refused",
                 _cassandra_stream_what="bootstrap",
                 _cassandra_stream_refused=[{"host": "node5", "error": "SecurityException: 'Authentication failed! Invalid"
                                                                       " username or password'."}])
    assert " ".join(msg.split()) == (
        "node5, bootstrap: JMX login refused on node5: check cassandra_jmx_username/password (or"
        " cassandra_jmx_password_file) for it (nodetool: SecurityException: 'Authentication failed! Invalid username"
        " or password'.). Nothing was stopped: the bootstrap goes on by itself; once fixed, run the playbook again to"
        " follow it.")
    stop = next(t for t in load("cleanup_wait.yml") if t["name"].startswith("Stop when a cleanup"))
    assert "jmx_refused" in stop["when"]


INTEGRATED = "nodetool: Failed to connect to '127.0.0.1:7199' - SecurityException: 'Authentication error'."


def refusals(own, peers, join=True, answered=(), hostvars=None):
    block = load("stream_check.yml")[0]["block"]
    where = next(t for t in block if t["name"] == "Where it stands")["vars"]
    variables = dict(inventory_hostname="node5", _cassandra_stream_join=join, _cassandra_stream_from=["node5"],
                     _cassandra_stream_peers=["node1", "node2"], cassandra_jmx_username="cassandra",
                     cassandra_jmx_password="secret", cassandra_jmx_password_file="",
                     hostvars=hostvars or {"node1": {}, "node2": {}, "node5": {}},
                     _cassandra_stream_state={"answered_hosts": list(answered)},
                     cassandra_stream_read={"results": own}, cassandra_stream_read_peers={"results": peers},
                     cassandra_stream_seen={"results": []})
    variables["_own_login"] = render(where["_own_login"], **variables)
    return render(where["_refused"], **variables)


def test_the_other_side_counts_only_when_none_of_it_answered():
    own = [{"item": "node5", "msg": "netstats command failed", "stderr": INTEGRATED}]
    refused = [{"item": "node1", "msg": "netstats command failed", "stderr": INTEGRATED},
               {"item": "node2", "msg": "netstats command failed", "stderr": INTEGRATED}]
    # a joining node with Cassandra's own authenticator: not its own refusal; all the others refusing: stop
    assert [r["host"] for r in refusals(own, refused)] == ["node1", "node2"]
    # one of them answered: going on with it
    assert refusals(own, refused[:1] + [{"item": "node2", "sessions": [], "mode": "NORMAL"}]) == []
    # not a join (a decommission): the node's own refusal stops it
    assert [r["host"] for r in refusals(own, [], join=False)] == ["node5"]
    # but not once it answered in this wait (a passing refusal), nor for a node with its own login in host_vars
    assert refusals(own, [], join=False, answered=["node5"]) == []
    assert [r["host"] for r in refusals(own, refused, hostvars={"node1": {"cassandra_jmx_password": "other"},
                                                                "node2": {}, "node5": {}})] == ["node2"]
