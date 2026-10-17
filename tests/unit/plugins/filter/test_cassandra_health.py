from __future__ import (absolute_import, division, print_function)
__metaclass__ = type


from ansible_collections.community.cassandra.plugins.filter.cassandra_health import (
    cassandra_health_findings, cassandra_health_problems, cassandra_health_report)
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import PARAGRAPH as P

UP = {"is_up": True}
IDLE = {"mode": "NORMAL", "streaming": False}
AGREED = {"failed": False}


def node(address, status="U", state="N", rack="r1"):
    return {"address": address, "status": status, "state": state, "rack": rack}


def view(host, *nodes):
    return {"from": host, "result": {"cluster_status": {"dc1": {"nodes": list(nodes)}}}}


def problems(views, expected=3, **kwargs):
    checks = dict(gossip=UP, binary=UP, netstats=IDLE, schema=AGREED)
    checks.update(kwargs)
    return cassandra_health_problems(views, expected, "n1", **checks)


def test_healthy():
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3")]
    assert problems([view("n1", *ring), view("n2", *ring)]) == []


def test_down_and_joining_nodes_named_with_the_view():
    out = problems([view("n1", node("10.0.0.1"), node("10.0.0.2", status="D"), node("10.0.0.3", state="J"))])
    assert "10.0.0.2 (r1) is DN, seen from n1" in out
    assert "10.0.0.3 (r1) is UJ, seen from n1" in out


def test_a_node_of_the_inventory_named_by_its_host_and_address():
    # the operator does not map the address by hand (the ring knows addresses only)
    ring = [node("10.0.0.1"), node("10.0.0.5", status="D"), node("10.0.0.9", status="D")]
    names = {"10.0.0.1": "n1", "10.0.0.5": "n4", "n4": "n4"}
    out = problems([view("n2", *ring)], names=names)
    assert out == ["n4 (10.0.0.5, r1) is DN, seen from n2", "10.0.0.9 (r1) is DN, seen from n2"]
    # the playbooks' "a node is down" hint still finds it
    assert all(" is DN," in line for line in out)
    # IPv6: nodetool prints it uncompressed, the inventory compressed
    v6 = problems([view("n2", node("2001:db8:0:0:0:0:0:5", status="D"))], expected=1, names={"2001:db8::5": "n4"})
    assert v6 == ["n4 (2001:db8:0:0:0:0:0:5, r1) is DN, seen from n2"]
    # two spellings of one address: the first given wins, as cassandra_host_addresses orders them
    v6 = problems([view("n2", node("2001:db8:0:0:0:0:0:5", status="D"))], expected=1,
                  names={"2001:db8:0:0:0:0:0:5": "n4", "2001:db8::5": "n9"})
    assert v6 == ["n4 (2001:db8:0:0:0:0:0:5, r1) is DN, seen from n2"]
    findings = cassandra_health_findings([view("n2", *ring)], 3, "n1", names=names)
    assert [f["address"] for f in findings if f["kind"] == "state"] == ["10.0.0.5", "10.0.0.9"]


def test_the_health_checks_pass_the_inventory_names():
    import os
    import yaml
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_operation", "tasks",
                        "cluster_health_read.yml")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    tasks = [t for b in yaml.safe_load(text) for t in [b] + b.get("block", [])]
    listed = next(t for t in tasks if t.get("name") == "List the problems")
    findings = " ".join(listed["ansible.builtin.set_fact"]["cassandra_health_findings"].split())
    assert ("names=_cassandra_health_read_names if _cassandra_health_read_names is defined"
            " else query('community.cassandra.cassandra_nodes') | community.cassandra.cassandra_host_addresses(hostvars)") \
        in findings
    # the wait's reads: the names read once before it
    with open(os.path.join(os.path.dirname(path), "cluster_health_wait.yml"), encoding="utf-8") as f:
        again = next(t for t in yaml.safe_load(f) if t.get("name") == "Read the cluster state again")
    assert again["vars"]["_cassandra_health_read_names"] == "{{ _cassandra_health_names }}"


def test_node_missing_from_the_ring():
    out = problems([view("n2", node("10.0.0.1"), node("10.0.0.2"))])
    assert len(out) == 1
    assert out[0].startswith("the ring has 2 nodes, the inventory 3 (seen from n2)")


def test_view_that_failed():
    out = problems([{"from": "n2", "result": {"msg": "nodetool error: connection refused"}}], expected=1)
    assert out == ["nodetool status failed on n2: nodetool error: connection refused"]


def test_view_that_failed_shows_nodetool_stderr():
    result = {"msg": "Unable to determine Cassandra version: ", "stderr": "nodetool: Failed to connect to '127.0.0.1:7199'\n"}
    out = problems([{"from": "n2", "result": result}], expected=1)
    assert out == ["nodetool status failed on n2: Unable to determine Cassandra version:  (nodetool: Failed to connect to '127.0.0.1:7199')"]


def test_nodetool_error_during_poll():
    # cassandra_status sets cluster_status to None when nodetool itself fails
    out = problems([{"from": "n2", "result": {"cluster_status": None, "msg": "nodetool error: boom"}}], expected=1)
    assert out == ["nodetool status failed on n2: nodetool error: boom"]


def test_ports_not_answering():
    ports = {"results": [
        {"item": {"name": "storage", "host": "10.0.0.1", "port": 7000}, "failed": False},
        {"item": {"name": "CQL", "host": "10.0.0.1", "port": 9042}, "failed": True},
    ]}
    out = problems([view("n1", node("10.0.0.1"))], expected=1, ports=ports)
    assert out == ["CQL port 9042 is not answering on n1 (10.0.0.1)"]


def test_node_level_checks():
    ring = [node("10.0.0.1")]
    out = problems([view("n1", *ring)], expected=1,
                   gossip={"is_up": False}, binary={}, schema={"failed": True, "msg": "2 schema versions"},
                   netstats={"mode": "NORMAL", "streaming": True})
    assert out == [
        "gossip is not running on n1",
        "the native transport (CQL) is not running on n1",
        "streams in progress on n1 (nodetool netstats)",
        "schema disagreement: 2 schema versions",
    ]


def test_netstats_failure_is_not_reported_as_streams():
    out = problems([view("n1", node("10.0.0.1"))], expected=1,
                   netstats={"failed": True, "msg": "netstats command failed", "stderr": "connection refused\n"})
    assert out == ["nodetool netstats failed on n1: netstats command failed (connection refused)"]


def test_expected_down_node_is_not_a_problem():
    ring = [node("10.0.0.1"), node("10.0.0.2", status="D")]
    assert problems([view("n1", *ring)], expected=2, down_ok=["10.0.0.2"]) == []
    assert problems([view("n1", *ring)], expected=2, down_ok=["10.0.0.9"]) == ["10.0.0.2 (r1) is DN, seen from n1"]


def test_expected_joining_node_is_not_a_problem():
    ring = [node("10.0.0.1"), node("10.0.0.2", state="J")]
    assert problems([view("n1", *ring)], expected=2, joining_ok=["10.0.0.2"]) == []
    assert problems([view("n1", *ring)], expected=2, joining_ok=["10.0.0.9"]) == ["10.0.0.2 (r1) is UJ, seen from n1"]
    # only joining: the same node down or leaving is still a problem
    for status, state in (("D", "J"), ("U", "L"), ("D", "N")):
        down = [node("10.0.0.1"), node("10.0.0.2", status=status, state=state)]
        assert problems([view("n1", *down)], expected=2, joining_ok=["10.0.0.2"]) == [
            "10.0.0.2 (r1) is %s%s, seen from n1" % (status, state)]


def test_leaving_node_expected_by_the_caller():
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3", state="L")]
    assert problems([view("n2", *ring)], leaving_ok=["10.0.0.3"]) == []
    # only UL: down while leaving is still a problem
    ring[2] = node("10.0.0.3", status="D", state="L")
    assert problems([view("n2", *ring)], leaving_ok=["10.0.0.3"]) == ["10.0.0.3 (r1) is DL, seen from n2"]
    # another node leaving is not
    ring[2] = node("10.0.0.3", state="L")
    assert problems([view("n2", *ring)], leaving_ok=["10.0.0.2"]) == ["10.0.0.3 (r1) is UL, seen from n2"]


def ring_result(*nodes):
    return {"cluster_status": {"dc1": {"nodes": list(nodes)}}}


FULL = ring_result(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3", state="L"))
GONE = ring_result(node("10.0.0.1"), node("10.0.0.2"))


REMOVING = "RemovalStatus: Removing token (-42). Waiting for replication confirmation from [/10.0.0.2]."
IDLE_REMOVAL = "RemovalStatus: No token removals in process."


# nodetool ring (5.0.7): the rows of a node start with its address and end with one of its tokens
RING_TOKENS = """
Datacenter: dc1
==========
Address   Rack  Status State   Load       Owns    Token
                                                  9000
10.0.0.4  r1    Down   Leaving 1.02 MiB   ?       -42
10.0.0.1  r1    Up     Normal  1.01 MiB   ?       -7
10.0.0.40 r1    Up     Normal  1.01 MiB   ?       5
10.0.0.4  r1    Down   Leaving 1.02 MiB   ?       9000

  Warning: "nodetool ring" is used to output all the tokens of a node.
"""
REMOVING_OTHER = "RemovalStatus: Removing token (5). Waiting for replication confirmation from [/10.0.0.2]."


def state(state, on="", reason=""):
    return {"state": state, "on": on, "reason": reason}


# nodetool gossipinfo on a node that does not coordinate the removal (5.0.7): it shows the node DN in its status
GOSSIP_REMOVING = """/10.0.0.1
  generation:1790684482
  STATUS_WITH_PORT:76:NORMAL,-1112067028005469869
/10.0.0.4
  generation:1790685004
  heartbeat:2147483647
  HOST_ID:3:93a85637-b80c-4d14-9d9e-f041a120f498
  STATUS_WITH_PORT:588:removing,93a85637-b80c-4d14-9d9e-f041a120f498
  INTERNAL_ADDRESS_AND_PORT:9:10.0.0.4:7000
  REMOVAL_COORDINATOR:590:REMOVER,04d1969e-5b13-433d-a198-cf9e5288a55c
/10.0.0.40
  STATUS:12:NORMAL,42
"""


def force_ring(*nodes):
    return {"cluster_status": {"dc1": {"nodes": list(nodes)}}}


FORCE_DN = force_ring(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D"))
FORCE_DL = force_ring(node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D", state="L"))


def findings(host, views, expected=3, **kwargs):
    checks = dict(gossip=UP, binary=UP, netstats=IDLE, schema=AGREED)
    checks.update(kwargs)
    return cassandra_health_findings(views, expected, host, **checks)


NAMES = {"10.0.0.1": "n1", "10.0.0.2": "n2", "10.0.0.3": "n3"}


def test_findings_carry_the_sentences():
    ring = [node("10.0.0.1"), node("10.0.0.2", status="D")]
    found = findings("n1", [view("n1", *ring)])
    assert [f["text"] for f in found] == problems([view("n1", *ring)])
    assert found[0] == {"kind": "state", "address": "10.0.0.2", "rack": "r1", "state": "DN", "seen_from": "n1",
                        "text": "10.0.0.2 (r1) is DN, seen from n1"}
    assert found[1]["kind"] == "count" and found[1]["ring"] == 2


def test_report_names_ipv6_nodes_as_the_inventory_writes_them():
    ring = [node("2001:db8:0:0:0:0:0:1"), node("2001:db8:0:0:0:0:0:4", status="D")]
    per_host = {"n1": findings("n1", [view("n1", *ring)], expected=2)}
    lines = cassandra_health_report(per_host, "c", ["n1", "n4"], unreachable=["n4"],
                                    names={"2001:db8::1": "n1", "2001:db8::4": "n4", "n1": "n1", "n4": "n4"})["lines"]
    assert lines[1] == "  ring:  n4 2001:db8:0:0:0:0:0:4 (r1) DN   seen from n1"
    assert lines[-1].startswith("  1. bring n4 back (not reached over SSH, down in the ring)")


def test_report_healthy():
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.3")]
    report = cassandra_health_report(dict((h, findings(h, [view(h, *ring)])) for h in ("n1", "n2", "n3")),
                                     "my_cluster", ["n1", "n2", "n3"], names=NAMES, members=3)
    assert report == {"healthy": True, "problems": 0,
                      "lines": ["HEALTHY  my_cluster  3 nodes  3 UN, schema agreed, no streams, ports open"]}


def test_report_each_problem_once_with_who_saw_it():
    ring = [node("10.0.0.1"), node("10.0.0.2", status="D", rack="r2"), node("10.0.0.3"), node("10.0.0.7"),
            node("10.0.0.8")]
    per_host = {}
    for host, address in (("n1", "10.0.0.1"), ("n3", "10.0.0.3")):
        cql = {"results": [{"item": {"name": "CQL", "host": address, "port": 9042}, "failed": True}]}
        per_host[host] = findings(host, [view(host, *ring), view("n1" if host != "n1" else "n3", *ring)],
                                  ports=cql, netstats={"mode": "NORMAL", "streaming": host == "n3"})
    report = cassandra_health_report(per_host, "my_cluster", ["n1", "n2", "n3"], unreachable=["n2"],
                                     absent=["n7", "n8"], names=NAMES,
                                     topology_command="ansible-playbook community.cassandra.topology")
    assert report["healthy"] is False and report["problems"] == 5
    assert report["lines"] == [
        "NOT HEALTHY  my_cluster  2 nodes checked, 5 problems",
        "  ring:     n2 10.0.0.2 (r2) DN   seen from n1, n3",
        "  ring:     5 members, inventory 3   seen from n1, n3",
        "  ports:    CQL 9042 not answering on n1, n3",
        "  streams:  in progress on n3",
        "  ssh:      not reached by Ansible: n2",
        "  not counted, marked cassandra_node_state: absent: n7, n8",
        "TO DO",  # n2 down and not reached: one item
        "  1. bring n2 back (not reached over SSH, down in the ring): its server first, then Cassandra, then run this"
        " again; a node that can't be recovered: replace_node",
        P, "  2. remove n7, n8 from the ring:",
        "     ansible-playbook community.cassandra.topology",
        P, "  3. wait for the streams to end (nodetool netstats), then run this again",
    ]


def test_report_an_unreachable_host_known_by_no_address_taken_for_the_one_node_down():
    # no ansible_host, no facts (Ansible reached it by name): its ring entry is the one no host has
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", status="D")]
    names = {"10.0.0.1": "n1", "10.0.0.2": "n2", "n1": "n1", "n2": "n2", "n3": "n3"}
    per_host = dict((h, findings(h, [view(h, *ring)])) for h in ("n1", "n2"))
    status = view("n1", *ring)["result"]["cluster_status"]
    lines = cassandra_health_report(per_host, "c", ["n1", "n2", "n3"], unreachable=["n3"], names=names, ring=status)["lines"]
    assert lines[1:3] == ["  ring:  n3? 10.0.0.4 (r1) DN   seen from n1, n2", "  ssh:   not reached by Ansible: n3"]
    assert lines[3:] == ["TO DO", "  1. bring n3? back (not reached over SSH, down in the ring): its server first, then"
                                  " Cassandra, then run this again; a node that can't be recovered: replace_node"]
    # two such hosts, a host known by an address, or no ring given: no guess
    names4 = dict(names, n4="n4")
    lines = cassandra_health_report(per_host, "c", ["n1", "n2", "n3", "n4"], unreachable=["n3", "n4"], names=names4,
                                    ring=status)["lines"]
    assert "  ring:  10.0.0.4 (r1) DN   seen from n1, n2" in lines
    lines = cassandra_health_report(per_host, "c", ["n1", "n2", "n3"], unreachable=["n3"], names=names)["lines"]
    assert "  ring:  10.0.0.4 (r1) DN   seen from n1, n2" in lines
    # n3 UN in the ring (its entry no host has either) and an old dead node: two candidates, no guess
    ring5 = ring + [node("10.0.0.3")]
    per_host5 = dict((h, findings(h, [view(h, *ring5)], expected=4)) for h in ("n1", "n2"))
    lines = cassandra_health_report(per_host5, "c", ["n1", "n2", "n3"], unreachable=["n3"], names=names,
                                    ring=view("n1", *ring5)["result"]["cluster_status"])["lines"]
    assert "  ring:  10.0.0.4 (r1) DN   seen from n1, n2" in lines
    lines = cassandra_health_report(per_host, "c", ["n1", "n2", "n3"], unreachable=["n3"],
                                    names=dict(names, **{"10.0.0.3": "n3"}), ring=status)["lines"]
    assert "  ring:  10.0.0.4 (r1) DN   seen from n1, n2" in lines
    assert lines[-3:] == ["TO DO", "  1. start Cassandra on 10.0.0.4 (its server first if it is down); a node that can't"
                                   " be recovered: replace_node", "  2. reach n3 over SSH, then run this again"]


def test_report_node_level_failures():
    per_host = {"n1": findings("n1", [{"from": "n1", "result": {"msg": "nodetool error: boom"}}], expected=1,
                               gossip={"is_up": False}, binary={}, schema={"failed": True, "msg": "2 schema versions"},
                               netstats={"failed": True, "msg": "refused"})}
    lines = cassandra_health_report(per_host, "c", ["n1"])["lines"]
    assert lines == ["NOT HEALTHY  c  1 node checked, 5 problems",
                     "  nodetool:  status failed on n1: nodetool error: boom",
                     "  gossip:    not running on n1",
                     "  CQL:       native transport not running on n1",
                     "  netstats:  failed on n1: refused",
                     "  schema:    disagreement: 2 schema versions"]


def test_report_same_error_on_several_nodes_once():
    down = {"failed": True, "msg": "Connection refused"}
    per_host = dict((h, findings(h, [{"from": h, "result": {"msg": "Connection refused"}}], expected=1, netstats=down))
                    for h in ("n1", "n2", "n3"))
    assert cassandra_health_report(per_host, "c", ["n1", "n2", "n3"])["lines"] == [
        "NOT HEALTHY  c  3 nodes checked, 2 problems",
        "  nodetool:  status failed on n1..n3: Connection refused",
        "  netstats:  failed on n1..n3: Connection refused"]


def test_report_error_that_is_not_text():
    per_host = {"n1": [{"kind": "netstats", "on": "n1", "error": None, "text": "x"}]}
    assert cassandra_health_report(per_host, "c", ["n1"])["lines"][1] == "  netstats:  failed on n1: None"


def test_the_jmx_message_is_not_followed_by_nodetool_line():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_health import _error
    msg = ("nodetool gets no answer from JMX on 127.0.0.1:7199 (Connection refused): Cassandra is not running there, or"
           " its JMX listens on another port (cassandra_jmx_port). Check: systemctl status cassandra, and its system.log")
    err = "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'.\n"
    assert _error({"failed": True, "msg": msg, "stderr": err}) == msg
