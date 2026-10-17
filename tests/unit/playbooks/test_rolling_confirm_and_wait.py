from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# rolling_restart and rolling_reboot ask once, like the other node operations (screen.yml, before the progress
# file); the cluster health check waits for a node down with a progress line at each read, not in silence.

import os
import warnings

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_health import (
    cassandra_health_findings, cassandra_health_wait_line, cassandra_health_waiting)

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the collection's filters, under plain pytest too

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def render(template, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def includes(tasks):
    for t in tasks or []:
        for key in ("ansible.builtin.include_role", "ansible.builtin.import_role"):
            if key in t:
                yield t[key].get("tasks_from"), t
        for key in ("block", "rescue", "always"):
            yield from includes(t.get(key))


def test_every_rolling_operation_asks_before_its_progress_file():
    # the progress file set up after the question: a run stopped there leaves none, a resume takes the latest
    for name in sorted(n for n in os.listdir(os.path.join(TOP, "playbooks")) if n.endswith(".yml")):
        if name == "cleanup.yml":  # no node taken down (nodetool cleanup on running nodes): no question
            continue
        steps = [tasks_from for play in load("playbooks", name) for tasks_from, dummy in includes(play.get("tasks"))]
        if "progress.yml" in steps:
            assert "screen.yml" in steps[:steps.index("progress.yml")], name


SESSION = "this run is not inside tmux or screen"


def screen_text(playbook, check=False, confirm=True, resume=False, mode="node"):
    play = next(p for p in load("playbooks", playbook) if p["name"] == "Set up the progress file")
    t = next(t for dummy, t in includes(play["tasks"]) if t["name"] == "Show the plan and confirm the run")
    screen = load("roles", "cassandra_operation", "tasks", "screen.yml")[0]
    hosts = ["node1", "node2", "node3"]
    variables = {"ansible_check_mode": check, "cassandra_operation_confirm": confirm, "_cassandra_session_warning": SESSION,
                 "ansible_play_hosts": hosts, "cassandra_rolling_resume": resume, "cassandra_rolling_mode": mode,
                 "_cassandra_preflight": {"cassandra_cluster_name": "Orders"}}
    variables.update(trust(t["vars"]))
    variables.update(trust(screen["vars"]))
    return render("{{ _cassandra_screen_text }}", variables), render(t["vars"]["cassandra_screen_question"], variables)


def test_rolling_restart_screen_and_question():
    text, question = screen_text("rolling_restart.yml")
    assert text.startswith("rolling_restart on cluster 'Orders': restart node1..node3, one node at a time\n\n"
                           "Drains and restarts each node in turn, the cluster checked healthy before and after each;")
    assert "WARNING - session: " + SESSION in text
    assert question == "Restart node1..node3, one node at a time?"
    assert "skipped (cassandra_rolling_resume)" in " ".join(screen_text("rolling_restart.yml", resume=True)[0].split())
    rack, question = screen_text("rolling_restart.yml", mode="rack")
    assert "rack by rack" in rack and question == "Restart node1..node3, rack by rack?"
    assert "Refused before any restart if a rack down would take down more than one replica" in " ".join(rack.split())
    assert "skipped (cassandra_rolling_resume)" in " ".join(screen_text("rolling_restart.yml", resume=True, mode="rack")[0].split())
    # --check and cassandra_operation_confirm false: the screen, no question (said on the screen)
    assert "--check: nothing will be changed" in screen_text("rolling_restart.yml", check=True)[0]
    said = "cassandra_operation_confirm is false: no question, the run goes on."
    assert said in screen_text("rolling_restart.yml", confirm=False)[0] and said not in text


def test_the_screens_say_it_with_single_spaces():
    for playbook, modes in (("rolling_restart.yml", ("node", "rack")), ("rolling_reboot.yml", ("node",))):
        for mode in modes:
            for resume in (False, True):
                intro = screen_text(playbook, resume=resume, mode=mode)[0].split("\n\n")[1]
                assert "  " not in intro and intro == intro.strip(), (playbook, mode, resume, intro)


def test_rolling_reboot_screen_and_question():
    text, question = screen_text("rolling_reboot.yml")
    assert text.startswith("rolling_reboot on cluster 'Orders': reboot node1..node3, one host at a time")
    assert question == "Reboot node1..node3, one host at a time?"


def node(address, status="U", state="N"):
    return {"address": address, "status": status, "state": state, "rack": "r1"}


def view(host, *nodes):
    return {"from": host, "result": {"cluster_status": {"dc1": {"nodes": list(nodes)}}}}


def test_the_wait_is_for_nodes_down_and_ports_only():
    ring = [node("10.0.0.1"), node("10.0.0.2"), node("10.0.0.4", "D")]
    ports = {"results": [{"failed": True, "item": {"name": "CQL", "port": 9042, "host": "10.0.0.2"}}]}
    found = cassandra_health_findings([view("node2", *ring), view("node1", *ring)], 3, "node2", ports=ports,
                                      schema={"failed": True, "msg": "2 versions"})
    waiting = cassandra_health_waiting(found)
    # node4 once (seen from both), the port; not the schema disagreement
    assert [f["kind"] for f in waiting] == ["state", "port"]
    assert cassandra_health_waiting(cassandra_health_findings([view("node2", node("10.0.0.1"))], 3, "node2")) == []


REFUSED_JMX = ("nodetool gets no answer from JMX on 10.0.0.2:7199 (Connection refused): Cassandra is not running there, "
               "or its JMX listens on another port (cassandra_jmx_port). Check: systemctl status cassandra, and its system.log")
TIMED_OUT = "nodetool gets no answer from JMX on 10.0.0.2:7199 (no answer in time): a firewall, or JMX listening on another address"
NAMES = {"10.0.0.1": "node1", "10.0.0.2": "node2", "10.0.0.4": "node4"}


def test_the_jmx_failures_waited_for_are_the_ones_nodetool_says():
    # the module's own words (version_error), not a copy: a cause renamed or reordered there would end the wait
    from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import version_error
    seen_down = view("node1", node("10.0.0.1"), node("10.0.0.2", "D"))
    for why, waited in (("ConnectException: 'Connection refused (Connection refused)'", True),
                        ("SocketTimeoutException: 'connect timed out'", True),
                        ("NoRouteToHostException: 'No route to host'", False),
                        ("UnknownHostException: 'node2'", False)):
        msg = version_error("", "nodetool: Failed to connect to '10.0.0.2:7199' - %s." % why)
        waiting = cassandra_health_waiting(cassandra_health_findings([failed_on("node2", msg), seen_down], 2, "node2"), NAMES)
        assert ("nodetool" in [f["kind"] for f in waiting]) is waited, msg


def failed_on(host, msg, stderr=""):
    return {"from": host, "result": {"msg": msg, "stderr": stderr}}


def test_a_nodetool_failure_is_waited_for_only_while_another_node_sees_that_node_down():
    # node2 just restarted: JMX not up yet (refused) or too slow (timed out), node1 sees it down: waited for, said why
    for error, cause in ((REFUSED_JMX, "Connection refused"), (TIMED_OUT, "no answer in time")):
        found = cassandra_health_findings([failed_on("node2", error), view("node1", node("10.0.0.1"), node("10.0.0.2", "D"))],
                                          2, "node2")
        waiting = cassandra_health_waiting(found, NAMES)
        assert [f["kind"] for f in waiting] == ["nodetool", "state"]
        assert cassandra_health_wait_line(waiting, NAMES, waited=10, timeout=600, node="node2", operation="restart") == (
            "node2 restart  waiting for node2 (10.0.0.2) DN to come back; nodetool status on node2 to answer (JMX: %s)"
            "  10s/10m" % cause)
        # node1 sees node2 up (a wrong JMX port for node2): never goes away, no wait
        up = cassandra_health_findings([failed_on("node2", error), view("node1", node("10.0.0.1"), node("10.0.0.2"))], 2, "node2")
        assert cassandra_health_waiting(up, NAMES) == []
        # another node down (node4): the wait is for node4, not for node2's nodetool
        other = cassandra_health_findings([failed_on("node2", error), view("node1", node("10.0.0.2"), node("10.0.0.4", "D"))],
                                          2, "node2")
        assert [f["kind"] for f in cassandra_health_waiting(other, NAMES)] == ["state"]
        # node2 seen joining or leaving (not waited for): its nodetool is not either
        for state in ("J", "L", "M"):
            busy = cassandra_health_findings([failed_on("node2", error), view("node1", node("10.0.0.1"), node("10.0.0.2", "U", state))],
                                             2, "node2")
            assert cassandra_health_waiting(busy, NAMES) == [], state
        # no view of node2 at all (the other read failed too): no wait
        assert cassandra_health_waiting(cassandra_health_findings([failed_on("node2", error), failed_on("node1", error)], 2,
                                                                  "node2"), NAMES) == []
    # the errors that do not go away waiting, node2 seen down or not: a login refused, nodetool missing or failing,
    # a JMX it can't speak to; nodetool status failing after its version answered
    seen_down = view("node1", node("10.0.0.1"), node("10.0.0.2", "D"))
    for msg, stderr in (("JMX login refused (Authentication failed! Invalid username or password): check the JMX user", ""),
                        ("nodetool version failed on 10.0.0.2:7199: is Cassandra installed and running? (no error printed)", ""),
                        ("[Errno 2] No such file or directory: b'nodetool'", ""),
                        ("nodetool can't reach JMX on 10.0.0.2:7199 (non-JRMP server at remote endpoint): is Cassandra running", ""),
                        ("nodetool gets no answer from JMX on 10.0.0.2:7199 (no route to host): the host is down", ""),
                        ("nodetool error: error: Connection refused", ""),
                        ("nodetool error: " + REFUSED_JMX, "")):
        waiting = cassandra_health_waiting(cassandra_health_findings([failed_on("node2", msg, stderr), seen_down], 2, "node2"),
                                           NAMES)
        assert [f["kind"] for f in waiting] == ["state"], msg
    # by its inventory name only when the names say it: an address the names do not have is not node2
    assert [f["kind"] for f in cassandra_health_waiting(
        cassandra_health_findings([failed_on("node2", REFUSED_JMX), seen_down], 2, "node2"))] == ["state"]


def test_the_progress_line():
    ring = [node("10.0.0.1"), node("10.0.0.4", "D"), node("10.0.0.5", "D")]
    waiting = cassandra_health_waiting(cassandra_health_findings([view("node2", *ring)], 3, "node2"))
    names = {"10.0.0.4": "node4"}
    assert cassandra_health_wait_line(waiting, names, waited=90, timeout=600, index=2, steps=5, node="node2",
                                      operation="restart") == (
        "[2/5] node2 restart  waiting for node4 (10.0.0.4) DN, 10.0.0.5 DN to come back  1m30s/10m")
    ports = [{"kind": "port", "name": "CQL", "port": 9042, "on": "node2", "host": "10.0.0.2"}]
    assert cassandra_health_wait_line(ports, waited=0, timeout=600, node="node2", operation="cluster check") == (
        "node2 cluster check  waiting for the CQL port 9042 on node2 (10.0.0.2) to answer  0s/10m")
    assert cassandra_health_wait_line([], done=True, waited=100, index=2, steps=5, node="node2", operation="restart") == (
        "[2/5] node2 restart  all back after 1m40s")


def test_the_progress_lines_are_coloured_as_progress():
    from ansible_collections.community.cassandra.plugins.callback.ops import PROGRESS_COLOR, colour
    assert colour("[2/5] node2 restart  waiting for node4 (10.0.0.4) DN to come back  1m30s/10m") == PROGRESS_COLOR
    assert colour("node2 cluster check  waiting for 10.0.0.5 DN to come back  10s/10m") == PROGRESS_COLOR
    assert colour("node2 cluster check  all back after 40s") == PROGRESS_COLOR
    assert colour("[2/5] node2 restart  waiting for node2 (10.0.0.2) DN to come back; nodetool status on node2 to answer"
                  " (JMX: Connection refused)  10s/10m") == PROGRESS_COLOR
    # the screens' first lines: a block of their own
    from ansible_collections.community.cassandra.plugins.callback.ops import _HEADED
    assert _HEADED.match("rolling_reboot on cluster 'Orders': reboot node1..node3, one host at a time")
    assert _HEADED.match("rolling_restart on cluster 'Orders': restart node1..node3, one node at a time")


HEALTH = load("roles", "cassandra_operation", "tasks", "cluster_health.yml")
CHECK = next(t for t in HEALTH if t["name"] == "Check the cluster state")


def wait_on(**given):
    variables = dict({"ansible_check_mode": False, "cassandra_service_health_timeout": 600}, **given)
    variables.update(trust(CHECK["vars"]))
    return render("{{ _cassandra_health_wait_on | bool }}", variables)


def test_when_the_health_check_waits():
    assert wait_on() in (True, "True")
    assert wait_on(ansible_check_mode=True) in (False, "False")  # nothing was started
    assert wait_on(cassandra_service_health_resumed_down=["10.0.0.2"]) in (False, "False")  # the node a resumed run left down
    assert wait_on(_cassandra_health_no_wait=True) in (False, "False")  # the nodes down_ok lists will not come back
    # a report waits too (remove_dead_node after removenode force: gossip may lag), health_check reads once (5s)
    assert wait_on(cassandra_service_health_report_only=True) in (True, "True")
    assert wait_on(cassandra_service_health_timeout=5) in (False, "False")
    report = next(t for play in load("playbooks", "health_check.yml") for dummy, t in includes(play.get("tasks"))
                  if t["name"] == "Check the cluster from this node")
    assert wait_on(**report["vars"]) in (False, "False")
    assert report["vars"]["_cassandra_health_no_wait"] is True  # not only its 5s timeout


def test_each_read_is_one_read_the_wait_reads_again_with_a_progress_line():
    read = load("roles", "cassandra_operation", "tasks", "cluster_health_read.yml")[0]["block"]
    polls = [t["community.cassandra.cassandra_status"]["poll"] for t in read if "community.cassandra.cassandra_status" in t]
    assert polls == [1, 1]  # the old poll of the module waited in silence for every down node
    names = [t["name"] for t in CHECK["block"]]
    assert names[0] == "Read the cluster state"
    loop = CHECK["block"][names.index("Wait for the nodes down to come back")]
    assert loop["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_service", "tasks_from": "wait_loop.yml"}
    assert loop["vars"]["_cassandra_wait_check"] == "{{ role_path }}/../cassandra_operation/tasks/cluster_health_wait.yml"
    step = [t["name"] for t in load("roles", "cassandra_operation", "tasks", "cluster_health_wait.yml")]
    assert step == ["Say what is waited for", "Wait before the next read", "Read the cluster state again",
                    "Note whether it still waits"]
    # the stop says how long it waited
    stop = next(t for t in HEALTH if t["name"] == "Stop on an unhealthy cluster")
    assert "for them to come back (cassandra_service_health_timeout" in stop["ansible.builtin.fail"]["msg"]


def check_task(name):
    return next(t for t in CHECK["block"] if t["name"] == name)


DOWN = [{"kind": "state", "address": "10.0.0.4", "state": "DN", "rack": "r1", "seen_from": "node1", "text": "x"}]


def test_it_waits_only_when_allowed_and_while_something_is_down():
    going = check_task("Note what is waited for")["ansible.builtin.set_fact"]["_cassandra_health_going"]
    base = dict(trust(CHECK["vars"]), ansible_check_mode=False, cassandra_service_health_timeout=600, inventory_hostname="node2",
                ansible_play_hosts=["node2"], hostvars={"node2": {"cassandra_health_findings": DOWN}})
    assert render(going, base) in (True, "True")
    assert render(going, dict(base, ansible_check_mode=True)) in (False, "False")  # not allowed: no wait, even with a node down
    assert render(going, dict(base, hostvars={"node2": {"cassandra_health_findings": []}})) in (False, "False")
    # a batch: the findings of its nodes, not those of a node an interrupted run did (skipped)
    batch = dict(base, cassandra_service_health_batch=True, ansible_play_hosts=["node1", "node2"],
                 hostvars={"node1": {"cassandra_health_findings": []}, "node2": {"cassandra_health_findings": DOWN}})
    assert render(going, dict(batch, inventory_hostname="node1")) in (True, "True")
    assert render(going, dict(batch, inventory_hostname="node1", cassandra_progress_done=["node2"])) in (False, "False")
    # a nodetool failed on node2 (JMX refused): waited for while node1 sees node2 (by its ring address) down
    nodetool = [{"kind": "nodetool", "on": "node2", "error": REFUSED_JMX, "text": "x"}]
    seen = {"kind": "state", "address": "192.168.1.2", "state": "DN", "rack": "r1", "seen_from": "node1", "text": "y"}
    findings = {"node2": {"cassandra_health_findings": nodetool,  # (node2 seen by no one)
                          "_cassandra_preflight": {"ring_address": "192.168.1.2"}}}
    assert render(going, dict(base, hostvars=findings)) in (False, "False")
    findings["node2"]["cassandra_health_findings"] = nodetool + [seen]
    waiting = render("{{ _cassandra_health_waiting }}", dict(base, hostvars=findings))
    assert [f["kind"] for f in waiting] == ["nodetool", "state"]
    # the wait and its lines only when it waits
    for name in ("Start the wait", "Wait for the nodes down to come back"):
        assert check_task(name)["when"] == "_cassandra_health_going | bool"


def test_each_check_goes_on_while_something_is_down_and_time_is_left():
    tasks = load("roles", "cassandra_operation", "tasks", "cluster_health_wait.yml")
    going = tasks[-1]["ansible.builtin.set_fact"]["_cassandra_health_going"]
    import time
    now = int(time.time())
    base = dict(trust(CHECK["vars"]), ansible_check_mode=False, cassandra_service_health_timeout=600, inventory_hostname="node2",
                ansible_play_hosts=["node2"], hostvars={"node2": {"cassandra_health_findings": DOWN}})
    assert render(going, dict(base, _cassandra_health_wait_start=now - 30)) in (True, "True")
    assert render(going, dict(base, _cassandra_health_wait_start=now - 601)) in (False, "False")  # the timeout: the stop follows
    assert render(going, dict(base, _cassandra_health_wait_start=now - 30,
                              hostvars={"node2": {"cassandra_health_findings": []}})) in (False, "False")  # back
    # the wait between two reads: 10s at first, then 30s, never past the timeout
    wait = tasks[1]
    timeout = wait["ansible.builtin.wait_for"]["timeout"]

    def pause(waited, limit=600):
        variables = dict(cassandra_stream_early_check_interval=10, cassandra_stream_check_interval=30,
                         cassandra_stream_early_time=300, cassandra_service_health_timeout=limit,
                         _cassandra_health_wait_start=int(time.time()) - waited)  # (now: a slow run takes seconds)
        variables.update(trust(wait["vars"]))
        return int(render(timeout, variables))
    assert pause(20) == 10 and pause(400) == 30 and pause(595) in (4, 5, 6) and pause(700) == 1


def test_the_ports_are_read_for_5s_when_the_loop_waits():
    read = load("roles", "cassandra_operation", "tasks", "cluster_health_read.yml")[0]["block"]
    ports = next(t for t in read if "ansible.builtin.wait_for" in t)["ansible.builtin.wait_for"]["timeout"]

    def timeout(wait_on, batch=False, check=False, host="node2"):
        return int(render(ports, {"_cassandra_health_wait_on": wait_on, "_batch": batch, "ansible_check_mode": check,
                                  "cassandra_service_health_timeout": 600, "inventory_hostname": host,
                                  "_cassandra_health_hosts": ["node2", "node3"] if batch else ["node2"]}))
    assert timeout(True) == 5 and timeout(False, check=True) == 5
    # a node of a batch waited for too (its ports in the batch's wait, with its progress line)
    assert timeout(True, batch=True) == 5
    # no loop to wait for them (a node of a batch an interrupted run did, a resumed node, a node down for good): as
    # before, the full timeout
    assert timeout(False) == 600 and timeout(True, batch=True, host="node1") == 600


def test_this_nodes_services_just_restarted_are_waited_for():
    found = [{"kind": "gossip", "on": "node2", "text": "x"}, {"kind": "cql", "on": "node2", "text": "y"},
             {"kind": "schema", "msg": "2 versions", "text": "z"}]
    waiting = cassandra_health_waiting(found)
    assert [f["kind"] for f in waiting] == ["gossip", "cql"]
    assert cassandra_health_wait_line(waiting, waited=5, timeout=600, node="node2", operation="restart") == (
        "node2 restart  waiting for gossip on node2 to run; the native transport (CQL) on node2 to run  5s/10m")
