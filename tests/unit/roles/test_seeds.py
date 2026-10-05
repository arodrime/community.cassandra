from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Seeds: cassandra_service refuses a new node listed as a seed in a running
# cluster, cassandra_config reloads the seed list of a running node live.

import os
import subprocess
import sys

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")


def load(role, name):
    with open(os.path.join(ROLES, role, "tasks", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def find(tasks, name):
    todo = list(tasks)
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", [])
    raise KeyError(name)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def task_vars(t, **variables):
    """The variables of a task, its own vars rendered in order on top."""
    for name, value in t.get("vars", {}).items():
        variables[name] = render(value, **variables) if isinstance(value, str) and "{{" in value else value
    return variables


def holds(t, **variables):
    when = t.get("when", [])
    return all(render("{{ %s }}" % c, **variables) for c in (when if isinstance(when, list) else [when]))


# ---- cassandra_service: a new seed in a running cluster ----

SERVICE_MAIN = load("cassandra_service", "main.yml")
NEW_SEED = load("cassandra_service", "new_seed.yml")
NEW = find(NEW_SEED, "Tell whether this node is new")
FIND = find(NEW_SEED, "Find this node in the seed list")
IS_SEED = find(NEW_SEED, "Tell whether this node is a seed")
PROBE = find(NEW_SEED, "Look for other seeds already up")
REFUSE = find(NEW_SEED, "Refuse to join a running cluster as a seed")

NODE = dict(inventory_hostname="node3", ansible_host="10.100.100.3", cassandra_listen_address="10.100.100.3",
            cassandra_extra_settings={},
            ansible_facts={"fqdn": "node3.example.com", "hostname": "node3",
                           "all_ipv4_addresses": ["10.100.100.3", "172.17.0.3"], "all_ipv6_addresses": ["fe80::3"]})
NAMES = ["node3", "10.100.100.3", "node3.example.com", "172.17.0.3", "fe80::3"]


def new_facts(active="inactive", main_pid=0, system=False, **node):
    v = dict(NODE, **node)
    v.update(cassandra_service_before={"status": {"ActiveState": active, "MainPID": str(main_pid)}},
             cassandra_service_system_dirs={"results": [{"item": "/var/lib/cassandra/data", "stat": {"exists": system}},
                                                        {"item": "/sys", "failed": False, "msg": "Not a directory"}]})
    v = task_vars(NEW, **v)
    return {k: render(val, **v) for k, val in NEW["ansible.builtin.set_fact"].items()}


@pytest.mark.parametrize("active, main_pid, system, node, new", [
    ("inactive", 0, False, {}, True),
    ("inactive", 0, True, {}, False),  # started before: a system keyspace
    ("active", 4321, False, {}, False),  # running already (data dirs elsewhere): nothing to start
    # the init script's unit left active with no JVM (e.g. it died before creating its data)
    ("active", 0, False, {}, True),
    ("failed", 0, False, {}, True),
    # cassandra_config found it initialized (e.g. a path in its live cassandra.yaml)
    ("inactive", 0, False, {"_cassandra_config_initialized": True}, False),
    ("inactive", 0, False, {"_cassandra_config_initialized": False}, True),
])
def test_new_node(active, main_pid, system, node, new):
    facts = new_facts(active, main_pid, system, **node)
    assert facts["_cassandra_service_new_node"] is new
    assert facts["_cassandra_service_check_seed"] is new


def test_new_in_this_run_stays_new():
    # started by an earlier play of the run: still one this run created, but not checked again
    facts = new_facts(system=True, _cassandra_service_new_node=True)
    assert facts["_cassandra_service_new_node"] is True
    assert facts["_cassandra_service_check_seed"] is False


@pytest.mark.parametrize("seeds, hosts", [
    ("127.0.0.1:7000,Node1:7000,[::1]", ["node1"]),
    (["10.100.100.1", "localhost"], ["10.100.100.1"]),
])
def test_seed_hosts_shared_with_the_other_hosts(seeds, hosts):
    assert new_facts(cassandra_seeds=seeds)["_cassandra_service_seed_hosts"] == hosts


@pytest.mark.parametrize("node, checked", [
    ({}, True),
    ({"cassandra_extra_settings": {"auto_bootstrap": False}}, False),  # does not bootstrap either way (a new datacenter)
    ({"cassandra_extra_settings": {"auto_bootstrap": True}}, True),
    ({"cassandra_seed_provider_class_name": "com.example.CloudSeedProvider"}, False),  # cassandra_seeds is not its list
    ({"cassandra_seed_provider_class_name": "org.apache.cassandra.locator.SimpleSeedProvider"}, True),
])
def test_checked_only_where_it_means_something(node, checked):
    assert new_facts(**node)["_cassandra_service_check_seed"] is checked


def test_names_shared_with_the_other_hosts():
    # what the other hosts of the run compare their seeds with: no loopback
    facts = new_facts(cassandra_cluster_name="Prod")
    assert facts["_cassandra_service_names"] == NAMES
    assert facts["_cassandra_service_cluster"] == "Prod"


def test_system_dirs_looked_at():
    t = find(NEW_SEED, "Look for the system keyspace of a node that already started")
    items = render(t["loop"], cassandra_data_file_directories=["/d1/", "/d2"],
                   cassandra_extra_settings={"local_system_data_file_directory": "/sys"})
    assert items == ["/d1", "/d2", "/sys"]
    # the service role alone: the package's data dir
    assert render(t["loop"]) == ["/var/lib/cassandra/data"]
    assert t["ansible.builtin.stat"]["follow"] is True


def getent(*addresses):
    return {"rc": 0, "stdout": "\n".join("%-15s STREAM x" % a for a in addresses)}


def seed_match(seeds, resolved=None):
    """_cassandra_service_is_seed and the other seeds' hosts; resolved: host -> getent result."""
    v = dict(NODE, cassandra_seeds=seeds, _cassandra_service_names=NAMES)
    v = task_vars(FIND, **v)
    v["cassandra_service_seed_ips"] = {"results": [dict((resolved or {}).get(s["host"], {"rc": 2, "stdout": ""}), item=s)
                                                   for s in v["_seeds"]]}
    v = task_vars(IS_SEED, **v)
    facts = {k: render(val, **v) for k, val in IS_SEED["ansible.builtin.set_fact"].items()}
    return facts["_cassandra_service_is_seed"], [s["host"] for s in facts["_cassandra_service_other_seeds"]]


@pytest.mark.parametrize("seeds, resolved, is_seed, others", [
    ("10.100.100.1,10.100.100.3:7000", {}, True, ["10.100.100.1"]),
    (["10.100.100.1", "10.100.100.2"], {}, False, ["10.100.100.1", "10.100.100.2"]),
    # found by any of its names, case and port left aside
    ("NODE3.example.com:7001,10.100.100.1", {}, True, ["10.100.100.1"]),
    ("node3", {}, True, []),
    ("172.17.0.3,[fe80::1]:7000", {}, True, ["fe80::1"]),
    ("[FE80::3]:7000,10.100.100.1", {}, True, ["10.100.100.1"]),
    # the loopback is this node, never another seed to probe
    ("127.0.0.1:7000", {}, True, []),
    ("", {}, False, []),
    # names it does not know, resolved on the node to one of its addresses (an alias, another spelling)
    ("seed-b.example.com,10.100.100.1", {"seed-b.example.com": getent("10.100.100.3")}, True, ["10.100.100.1"]),
    ("fe80:0:0:0:0:0:0:3", {"fe80:0:0:0:0:0:0:3": getent("fe80::3")}, True, []),
    ("seed-a.example.com", {"seed-a.example.com": getent("10.100.100.1")}, False, ["seed-a.example.com"]),
    # a prefix of one of its addresses is not this node
    ("10.100.100.33", {"10.100.100.33": getent("10.100.100.33")}, False, ["10.100.100.33"]),
])
def test_seed_or_not(seeds, resolved, is_seed, others):
    assert seed_match(seeds, resolved) == (is_seed, others)


def test_seeds_from_cassandra_config_only():
    # the service role alone: no cassandra_seeds, nothing to check
    v = task_vars(FIND, **dict(NODE, _cassandra_service_names=NAMES))
    assert v["_seeds"] == []


@pytest.mark.parametrize("seed, storage_port, port", [
    ({"host": "10.100.100.1", "port": None}, None, 7000),
    ({"host": "10.100.100.1", "port": None}, 7010, 7010),
    ({"host": "10.100.100.1", "port": 7001}, 7010, 7001),  # its own port wins
])
def test_probe_port(seed, storage_port, port):
    v = {"item": seed}
    if storage_port:
        v["cassandra_storage_port"] = storage_port
    assert int(render(PROBE["ansible.builtin.wait_for"]["port"], **v)) == port


def test_probes_are_read_only_and_never_fail():
    for t in (PROBE, find(NEW_SEED, "Resolve the seeds on this node")):
        assert t["check_mode"] is False
        assert t["failed_when"] is False


@pytest.mark.parametrize("check, seed, probed", [(True, True, True), (False, True, False), (True, False, False)])
def test_probe_only_a_new_seed(check, seed, probed):
    assert holds(PROBE, _cassandra_service_check_seed=check, _cassandra_service_is_seed=seed) is probed


def probed(*up, down=()):
    return {"results": [{"item": {"host": h, "port": None}, "state": "started", "elapsed": 0} for h in up]
            + [{"item": {"host": h, "port": None}, "failed": False, "msg": "Timeout when waiting for %s:7000" % h}
               for h in down]}


def host(name, new, names, seeds=(), cluster="Prod", seed=True):
    h = {"inventory_hostname": name, "_cassandra_service_new_node": new, "_cassandra_service_names": names,
         "_cassandra_service_seed_hosts": list(seeds), "_cassandra_service_cluster": cluster}
    if new:
        h["_cassandra_service_is_seed"] = seed
    return h


ME = host("node3", True, NAMES)
NODE1 = ["node1", "10.100.100.1"]


@pytest.mark.parametrize("results, others, resolved, refused", [
    (probed("10.100.100.1", down=["10.100.100.2"]), [], {}, ["10.100.100.1"]),
    (probed(down=["10.100.100.1", "10.100.100.2"]), [], {}, []),
    # a seed this run started for the first time (a cluster being created, serial: 1)
    (probed("10.100.100.1"), [host("node1", True, NODE1)], {}, []),
    # the same, listed by a name that resolves to it
    (probed("seed1.example.com"), [host("node1", True, NODE1)], {"seed1.example.com": getent("10.100.100.1")}, []),
    # a non-seed this run added answers: it joined a running cluster
    (probed("10.100.100.1"), [host("node1", True, NODE1, seed=False)], {}, ["10.100.100.1"]),
    # a host that had started before this run, in this node's seed list: a running cluster, answering or not
    (probed("10.100.100.1"), [host("node1", False, NODE1)], {}, ["10.100.100.1", "node1 started before"]),
    (probed(down=["10.100.100.1"]), [host("node1", False, NODE1)], {}, ["node1 started before"]),
    (probed(down=["seed1.example.com"]), [host("node1", False, NODE1)], {"seed1.example.com": getent("10.100.100.1")},
     ["node1 started before"]),
    # ... or with this node in its own seed list (e.g. the old seeds retired, down)
    (probed(down=["10.100.100.9"]), [host("node5", False, ["node5"], seeds=["10.100.100.3"])], {}, ["node5 started before"]),
    # not linked by the seed lists: another cluster that has the same name (e.g. the default one)
    (probed(down=["10.100.100.9"]), [host("node5", False, ["node5", "10.100.100.5"], seeds=["10.100.100.5"])], {}, []),
    (probed(down=["10.100.100.1"]), [host("node1", False, NODE1, cluster="Other")], {}, []),
    (probed("10.100.100.1"), [{"inventory_hostname": "node1"}], {}, ["10.100.100.1"]),  # not looked at (a later batch)
    ({"results": [{"skipped": True}]}, [], {}, []),
    ({"skipped": True}, [], {}, []),
])
def test_refused_when_the_cluster_runs(results, others, resolved, refused):
    hosts = dict((h["inventory_hostname"], h) for h in others + [ME])
    items = [r["item"] for r in results.get("results", []) if "item" in r]
    seed_ips = {"results": [dict(resolved.get(i["host"], {"rc": 2, "stdout": ""}), item=i) for i in items]}
    v = dict(cassandra_service_seeds_up=results, cassandra_service_seed_ips=seed_ips, groups={"all": list(hosts)},
             hostvars=hosts, inventory_hostname="node3", _cassandra_service_cluster="Prod", _cassandra_service_names=NAMES,
             _cassandra_service_other_seeds=items, _cassandra_service_check_seed=True, _cassandra_service_is_seed=True)
    v = task_vars(REFUSE, **v)
    assert v["_answering"] + [h + " started before" for h in v["_started"]] == refused
    assert holds(REFUSE, **v) is bool(refused)
    if refused:
        msg = render(REFUSE["ansible.builtin.fail"]["msg"], **v)
        assert "(%s)" % ", ".join(refused) in msg
        assert "Seeds don't bootstrap" in msg and "cassandra_service_allow_new_seed: true" in msg


@pytest.mark.parametrize("state, allow, checked", [
    ("started", False, True), ("restarted", False, True), ("stopped", False, False), ("started", True, False),
])
def test_checked_before_a_start(state, allow, checked):
    t = find(SERVICE_MAIN, "Refuse a new seed node in a running cluster")
    assert holds(t, cassandra_service_state=state, cassandra_service_allow_new_seed=allow) is checked
    # before anything is written
    names = [x.get("name") for x in SERVICE_MAIN]
    assert names.index(t["name"]) < names.index("Install the cassandra systemd unit")


# ---- cassandra_config: live seed reload ----

CONFIG_MAIN = load("cassandra_config", "main.yml")
SEEDS = find(CONFIG_MAIN, "Apply the seed list live, say what needs a restart")
RELOAD_BLOCK = find(CONFIG_MAIN, "Reload the seed list on the running node")
LIVE = find(CONFIG_MAIN, "Read the live seed list")
RELOAD = find(CONFIG_MAIN, "Reload the seed list")
NOT_RELOADED = find(CONFIG_MAIN, "Say the seed list could not be reloaded")
WARN = find(CONFIG_MAIN, "Warn that a restart is needed")

JMX = dict(cassandra_jmx_port=7199, cassandra_jmx_username="", cassandra_jmx_password="", cassandra_jmx_password_file="")


def seeds_vars(**variables):
    return task_vars(SEEDS, **dict(JMX, **variables))


@pytest.mark.parametrize("variables, login, hidden", [
    ({}, [], False),
    ({"cassandra_jmx_port": 7299}, None, False),
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/jmxremote.password"},
     ["-u", "ops", "-pwf", "/etc/cassandra/jmxremote.password"], False),
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password": "s3cret"}, ["-u", "ops", "-pw", "s3cret"], True),
    # the file wins over an inline password, which is then not on the command line
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password": "s3cret", "cassandra_jmx_password_file": "/pw"},
     ["-u", "ops", "-pwf", "/pw"], False),
    ({"cassandra_jmx_password": "s3cret"}, [], False),  # no user, no login (as cassandra_service)
])
def test_nodetool_commands(variables, login, hidden):
    v = seeds_vars(**variables)
    port = str(variables.get("cassandra_jmx_port", 7199))
    for t, command in ((LIVE, "getseeds"), (RELOAD, "reloadseeds")):
        argv = render(t["ansible.builtin.command"]["argv"], **v)
        if login is not None:
            assert argv == ["nodetool", "-p", port] + login + [command]
        assert argv[:3] == ["nodetool", "-p", port]
        assert render(t["no_log"], **v) is hidden
        assert t["failed_when"] is False  # said by the warning instead


GET = "Current list of seed node IPs, excluding the current node's IP: "
UPDATED = "Updated seed node IP list, excluding the current node's IP: "
NONE = "Seed node list does not contain any remote node IPs"


def reload_outcome(live, result):
    v = seeds_vars(cassandra_config_seeds_live=live, cassandra_config_seeds_reload=result)
    return (render("{{ %s }}" % RELOAD["changed_when"], **v), holds(RELOAD, **v), holds(NOT_RELOADED, **v))


def ok(stdout):
    return {"rc": 0, "stdout": stdout, "stderr": "", "stderr_lines": []}


@pytest.mark.parametrize("live, result, changed, reloaded, warned", [
    (ok(GET + "/10.100.100.1:7000"), ok(UPDATED + "/10.100.100.1:7000 /10.100.100.2:7000"), True, True, False),
    (ok(GET + "/10.100.100.1:7000 /10.100.100.2:7000"), ok(UPDATED + "/10.100.100.2:7000 /10.100.100.1:7000"), False, True, False),
    (ok(NONE), ok(UPDATED + "/10.100.100.1:7000"), True, True, False),
    (ok(GET + "/10.100.100.1:7000"), ok(UPDATED + "/10.100.100.1:7000"), False, True, False),
    (ok(NONE), ok(NONE), False, True, False),
    # names resolved by the node keep their name, even one holding the JMX user's name
    (ok(GET + "cassandra-seed1/10.100.100.1:7000"), ok(UPDATED + "cassandra-seed1/10.100.100.1:7000"), False, True, False),
    # a line before the answer (a JVM warning)
    (ok(GET + "/10.100.100.1:7000"), ok("Picked up JAVA_TOOL_OPTIONS: -Xss1m\n" + UPDATED + "/10.100.100.2:7000"),
     True, True, False),
    # the node could not read the new list (e.g. a newer series' cassandra.yaml): nodetool exits 0
    (ok(GET + "/10.100.100.1:7000"), ok("Failed to reload the seed node list."), False, True, True),
    ({"rc": 1, "stdout": "", "stderr": "nodetool: Failed to connect", "stderr_lines": ["nodetool: Failed to connect"]},
     {"changed": False, "skipped": True}, False, False, True),
    (ok(GET + "/10.100.100.1:7000"), {"rc": 1, "stdout": "", "stderr": "error: x", "stderr_lines": ["error: x"]},
     False, True, True),
])
def test_reload_changed_or_said(live, result, changed, reloaded, warned):
    assert reload_outcome(live, result) == (changed, reloaded, warned)


@pytest.mark.parametrize("live, result, said", [
    ({"rc": 1, "stdout": "", "stderr_lines": ["a", "nodetool: Failed to connect to '127.0.0.1:7199'"]}, {"skipped": True},
     "nodetool: Failed to connect to '127.0.0.1:7199'"),
    (ok(GET), ok("Failed to reload the seed node list."), "Failed to reload the seed node list."),
])
def test_what_went_wrong_is_said(live, result, said):
    v = task_vars(NOT_RELOADED, **seeds_vars(cassandra_config_seeds_live=live, cassandra_config_seeds_reload=result,
                                             inventory_hostname="node1"))
    assert "(%s)" % said in render(NOT_RELOADED["ansible.builtin.debug"]["msg"], **v)


@pytest.mark.parametrize("enabled, running, check_mode, reloaded", [
    (True, True, False, True), (True, False, False, False), (False, True, False, False), (True, True, True, False),
])
def test_reload_only_on_a_running_node(enabled, running, check_mode, reloaded):
    assert holds(RELOAD_BLOCK, cassandra_config_reload_seeds=enabled, _cassandra_config_running=running,
                 ansible_check_mode=check_mode) is reloaded
    # after the files, the JMX password file included, are written
    names = [x.get("name") for x in CONFIG_MAIN]
    assert names.index("Configure") < names.index("JMX users") < names.index(SEEDS["name"])
    assert [x.get("name") for x in SEEDS["block"]] == [RELOAD_BLOCK["name"], WARN["name"]]


DIFF_SCRIPT = find(CONFIG_MAIN, "Diff them against the live files")["ansible.builtin.command"]["argv"][2]
YAML = """cluster_name: 'Test Cluster'
seed_provider:
  - class_name: org.apache.cassandra.locator.SimpleSeedProvider
    parameters:
      - seeds: "%s"
concurrent_reads: %d
"""


def diff(tmp_path, live, new):
    (tmp_path / "live.yaml").write_text(live)
    (tmp_path / "new.yaml").write_text(new)
    return subprocess.run([sys.executable, "-c", DIFF_SCRIPT, str(tmp_path / "live.yaml"), str(tmp_path / "new.yaml")],
                          stdout=subprocess.PIPE, check=True).stdout.decode()


RELOADED = {"changed": True}
SKIPPED = {"changed": False, "skipped": True}


@pytest.mark.parametrize("new_seeds, reads, reload_on, reload, check_mode, files, restart", [
    ("10.100.100.1,10.100.100.2", 32, True, RELOADED, False, ["cassandra.yaml"], []),  # reloaded live
    ("10.100.100.1,10.100.100.2", 32, True, SKIPPED, True, ["cassandra.yaml"], []),  # --check: it would be
    # the live list did not move (e.g. only this node: Cassandra keeps the old one)
    ("10.100.100.1,10.100.100.2", 32, True, {"changed": False}, False, ["cassandra.yaml"], ["cassandra.yaml"]),
    ("10.100.100.1,10.100.100.2", 32, False, SKIPPED, False, ["cassandra.yaml"], ["cassandra.yaml"]),
    ("10.100.100.1,10.100.100.2", 64, True, RELOADED, False, ["cassandra.yaml"], ["cassandra.yaml"]),
    ("10.100.100.1", 64, True, SKIPPED, False, ["cassandra.yaml"], ["cassandra.yaml"]),
    ("10.100.100.1,10.100.100.2", 32, True, RELOADED, False, ["cassandra.yaml", "jvm-server.options"], ["jvm-server.options"]),
    ("10.100.100.1", 32, True, SKIPPED, False, [], []),  # owner or mode only: no file content to restart for
])
def test_restart_warning_leaves_out_a_seeds_only_change(tmp_path, new_seeds, reads, reload_on, reload, check_mode, files, restart):
    out = diff(tmp_path, YAML % ("10.100.100.1", 32), YAML % (new_seeds, reads))
    preview = {"results": [{"cassandra_config_file": "cassandra.yaml", "stdout": out, "stdout_lines": out.splitlines()},
                           {"cassandra_config_file": "jvm-server.options", "stdout": "x", "stdout_lines": ["x"]}]}
    v = task_vars(WARN, **seeds_vars(cassandra_config_preview=preview, cassandra_config_reload_seeds=reload_on,
                                     _cassandra_config_changes=files, cassandra_config_seeds_reload=reload,
                                     ansible_check_mode=check_mode))
    assert v["_restart"] == restart
    assert holds(WARN, cassandra_config_applied={"changed": True}, _cassandra_config_running=True, **v) is bool(restart)
