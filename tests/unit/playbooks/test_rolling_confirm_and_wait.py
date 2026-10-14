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
TASKS = os.path.join(TOP, "roles", "cassandra_service", "tasks")


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
    for name in sorted(os.listdir(os.path.join(TOP, "playbooks"))):
        if name == "cleanup.yml":  # no node taken down (nodetool cleanup on running nodes): no question
            continue
        steps = [tasks_from for play in load("playbooks", name) for tasks_from, dummy in includes(play.get("tasks"))]
        if "progress.yml" in steps:
            assert "screen.yml" in steps[:steps.index("progress.yml")], name


SESSION = "this run is not inside tmux or screen"


def screen_text(playbook, check=False, confirm=True, resume=False, mode="node"):
    play = next(p for p in load("playbooks", playbook) if p["name"] == "Set up the progress file")
    t = next(t for dummy, t in includes(play["tasks"]) if t["name"] == "Show the plan and confirm the run")
    screen = load("roles", "cassandra_service", "tasks", "screen.yml")[0]
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
    assert "skipped (cassandra_rolling_resume)" in screen_text("rolling_restart.yml", resume=True)[0]
    rack, question = screen_text("rolling_restart.yml", mode="rack")
    assert "rack by rack" in rack and question == "Restart node1..node3, rack by rack?"
    # --check and cassandra_operation_confirm false: the screen, no question (said on the screen)
    assert "--check: nothing will be changed" in screen_text("rolling_restart.yml", check=True)[0]
    assert "cassandra_operation_confirm" in screen_text("rolling_restart.yml", confirm=False)[0]


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
    # the screens' first lines: a block of their own
    from ansible_collections.community.cassandra.plugins.callback.ops import _HEADED
    assert _HEADED.match("rolling_reboot on cluster 'Orders': reboot node1..node3, one host at a time")
    assert _HEADED.match("rolling_restart on cluster 'Orders': restart node1..node3, one node at a time")


HEALTH = load("roles", "cassandra_service", "tasks", "cluster_health.yml")
CHECK = next(t for t in HEALTH if t["name"] == "Check the cluster state")


def wait_on(**given):
    variables = dict({"ansible_check_mode": False, "cassandra_service_health_timeout": 600}, **given)
    variables.update(trust(CHECK["vars"]))
    return render("{{ _cassandra_health_wait_on | bool }}", variables)


def test_when_the_health_check_waits():
    assert wait_on() is True
    assert wait_on(ansible_check_mode=True) is False  # nothing was started
    assert wait_on(cassandra_service_health_resumed_down=["10.0.0.2"]) is False  # the node a resumed run left down
    assert wait_on(_cassandra_health_no_wait=True) is False  # the nodes down_ok lists will not come back
    assert wait_on(cassandra_service_health_report_only=True) is False  # health_check: a report
    assert wait_on(cassandra_service_health_timeout=5) is False


def test_each_read_is_one_read_the_wait_reads_again_with_a_progress_line():
    read = load("roles", "cassandra_service", "tasks", "cluster_health_read.yml")[0]["block"]
    polls = [t["community.cassandra.cassandra_status"]["poll"] for t in read if "community.cassandra.cassandra_status" in t]
    assert polls == [1, 1]  # the old poll of the module waited in silence for every down node
    names = [t["name"] for t in CHECK["block"]]
    assert names[0] == "Read the cluster state"
    loop = CHECK["block"][names.index("Wait for the nodes down to come back")]
    assert loop["ansible.builtin.include_tasks"] == "wait_loop.yml"
    assert loop["vars"]["_cassandra_wait_check"] == "cluster_health_wait.yml"
    step = [t["name"] for t in load("roles", "cassandra_service", "tasks", "cluster_health_wait.yml")]
    assert step == ["Say what is waited for", "Wait before the next read", "Read the cluster state again",
                    "Note whether it still waits"]
    # the stop says how long it waited
    stop = next(t for t in HEALTH if t["name"] == "Stop on an unhealthy cluster")
    assert "for them to come back (cassandra_service_health_timeout" in stop["ansible.builtin.fail"]["msg"]
