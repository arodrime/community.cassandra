from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# 5.0: no restart into an earlier storage_compatibility_mode than the one Cassandra runs (a cassandra.yaml edited
# back by hand, an inventory behind the node): cassandra_config (apply_config, site.yml, the upgrade's writes),
# rolling_restart and rolling_reboot refuse it, on evidence only (the mode read over CQL or logged at its start).

import os

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import (
    cassandra_storage_mode_back, cassandra_storage_mode_jump, cassandra_storage_mode_unsure)
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _load(path):
    with open(os.path.join(TOP, path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _host(yaml_mode, logged=None, cql=None, more=()):
    lines = ["file=/etc/cassandra/conf/cassandra.yaml", "yaml_read=yes", "yaml=" + yaml_mode, "running=yes"] + list(more)
    if logged:
        lines.append("logged=" + logged)
    host = {"inventory_hostname": "n1", "cassandra_storage_mode_read": {"stdout_lines": lines}}
    if cql:
        host["cassandra_storage_mode_cql"] = {"changed": True, "transformed_output": "value\n----\n %s\n\n(1 rows)" % cql}
    return host


def test_back_on_evidence_only():
    # it runs NONE (logged, or read over CQL), UPGRADING to be written: back
    assert cassandra_storage_mode_back(_host("NONE", logged="NONE"), "UPGRADING") == "NONE"
    assert cassandra_storage_mode_back(_host("NONE", cql="NONE"), "UPGRADING") == "NONE"
    # a restart alone (no target): its file edited back to UPGRADING, it runs NONE
    assert cassandra_storage_mode_back(_host("UPGRADING", logged="NONE")) == "NONE"
    assert cassandra_storage_mode_back(_host("", cql="UPGRADING")) == "UPGRADING"  # no such line: CASSANDRA_4
    # forward or the same: nothing
    assert cassandra_storage_mode_back(_host("CASSANDRA_4", logged="CASSANDRA_4"), "UPGRADING") == ""
    assert cassandra_storage_mode_back(_host("NONE", logged="NONE"), "NONE") == ""
    assert cassandra_storage_mode_back(_host("UPGRADING", logged="CASSANDRA_4")) == ""
    # the mode it runs not known (nothing logged, CQL not read): never refused
    assert cassandra_storage_mode_back(_host("UPGRADING"), "CASSANDRA_4") == ""
    failed = dict(_host("UPGRADING"), cassandra_storage_mode_cql={"failed": True, "msg": "module execution failed"})
    assert cassandra_storage_mode_back(failed, "CASSANDRA_4") == ""


def _render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


GUARD = "roles/cassandra_operation/tasks/storage_mode_guard.yml"
BACK = "The restarts keep the storage_compatibility_mode Cassandra runs, or a later one"
JUMP = "The restarts move the storage_compatibility_mode Cassandra runs one step at most"
UNSURE = "The restarts move no node back while the mode it runs is not known"
GIVEN = "The mode given with -e is taken only where nothing else tells it"
KEPT = "The run keeps the storage_compatibility_mode of each cassandra.yaml"


def guard(name, hosts, **extra):
    """One check of the guard, rendered for hosts {name: (its variables, its inventory's mode)} -> (passes, its
    message lines). The extra variables: the guard's (_cassandra_storage_mode_written...), -e ones (on each node too)."""
    check = next(t for t in _load(GUARD) if t["name"] == name)
    assert check["run_once"] is True and check["vars"]["cassandra_output"] is True
    hostvars = {}
    for node, (host, inventory) in hosts.items():
        hostvars[node] = dict(host, inventory_hostname=node, cassandra_version="50x",
                              cassandra_storage_compatibility_mode=inventory)
        if "cassandra_storage_mode_running" in extra:
            hostvars[node]["cassandra_storage_mode_running"] = extra["cassandra_storage_mode_running"]
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars),
                     ansible_play_hosts_all=list(hostvars), cassandra_version="50x", **extra)
    return (_render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
            _render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n"))


def test_the_guard_says_why_and_the_way_out():
    tasks = _load(GUARD)
    assert tasks[0]["ansible.builtin.include_tasks"] == "storage_mode_read.yml"
    # once for the nodes of the play: one refused stops the run before any node is restarted

    def run(hosts, **extra):
        return guard(BACK, hosts, **extra)
    # every node fine (a passing run renders its message too, as ansible-core 2.19+ does)
    ok, msg = run({"n1": (_host("NONE", logged="NONE"), "NONE")}, _cassandra_storage_mode_written=True)
    assert ok is True
    # cassandra_config: the inventory behind the mode it runs
    ok, msg = run({"n1": (_host("NONE", logged="NONE"), "NONE"), "n2": (_host("NONE", logged="NONE"), "UPGRADING")},
                  _cassandra_storage_mode_written=True)
    assert ok is False and msg == [
        "A restart would take n2 back to an earlier storage_compatibility_mode than the one Cassandra runs (5.0 moves"
        " CASSANDRA_4, UPGRADING, NONE, never back). Nothing was changed.",
        "  n2  runs NONE, its cassandra.yaml would have UPGRADING",
        "WAY OUT  On n2, set cassandra_storage_compatibility_mode: NONE in the inventory (its cassandra.yaml has it)."
        " Then run this again."]
    # its file one step behind the mode it runs: apply_config writes it
    ok, msg = run({"n2": (_host("UPGRADING", logged="NONE"), "UPGRADING")}, _cassandra_storage_mode_written=True)
    assert msg[-1] == ("WAY OUT  On n2, set cassandra_storage_compatibility_mode: NONE in the inventory (apply_config"
                       " then writes it). Then run this again.")
    # a restart alone: its file edited back by hand, the inventory right: apply_config writes it back
    ok, msg = run({"n1": (_host("NONE", logged="NONE"), "NONE"), "n2": (_host("UPGRADING", logged="NONE"), "NONE")})
    assert ok is False and msg[1:] == [
        "  n2  runs NONE, its cassandra.yaml has UPGRADING",
        "WAY OUT  On n2, run apply_config --limit n2 (it writes NONE back). Then run this again."]
    # the inventory behind too: set it first
    ok, msg = run({"n2": (_host("UPGRADING", logged="NONE"), "UPGRADING")})
    assert msg[-1] == ("WAY OUT  On n2, set cassandra_storage_compatibility_mode: NONE in the inventory, then run"
                       " apply_config --limit n2 (it writes it back). Then run this again.")


def test_where_it_runs():
    # cassandra_config: after the compare (its identity check first), before the question and any write
    block = next(t for t in _load("roles/cassandra_config/tasks/main.yml") if t.get("name") == "Configure")["block"]
    names = [t["name"] for t in block]
    assert names.index("Compare the new files with the live ones") < names.index(
        "Refuse a storage_compatibility_mode before the one Cassandra runs, or two steps after it") < names.index(
        "Show the changes and confirm them") < names.index("Write the files and directories")
    guard = block[names.index("Refuse a storage_compatibility_mode before the one Cassandra runs, or two steps after it")]
    assert guard["ansible.builtin.include_role"]["tasks_from"] == "storage_mode_guard.yml"
    assert guard["vars"]["_cassandra_storage_mode_written"] is True
    # rolling_restart, rolling_reboot, update_java: before the question
    for playbook, play_name in (("playbooks/rolling_restart.yml", "Set up the progress file"),
                                ("playbooks/rolling_reboot.yml", "Set up the progress file"),
                                ("playbooks/update_java.yml", "Check the Java change")):
        play = next(p for p in _load(playbook) if p.get("name") == play_name)
        names = [t["name"] for t in play["tasks"]]
        guard = "Refuse a restart into another storage_compatibility_mode than one step from the one it runs"
        assert names.index(guard) < names.index("Show the plan and confirm the run"), playbook
        assert play["tasks"][names.index(guard)]["ansible.builtin.include_role"]["tasks_from"] == "storage_mode_guard.yml"


def test_the_cql_read_asks_the_node_itself_and_says_when_it_could_not():
    tasks = _load("roles/cassandra_operation/tasks/storage_mode_read.yml")
    read = next(t for t in tasks if t["name"].startswith("Read the storage_compatibility_mode the running"))
    cql = read["community.cassandra.cassandra_cqlsh"]
    assert cql["cqlsh_host"] == "{{ _cassandra_storage_mode_cql_host }}"
    assert cql["cqlsh_port"] == "{{ cassandra_native_transport_port | default(9042) }}"
    assert cql["ssl"] == "{{ _cassandra_storage_mode_cql_ssl }}"
    defaults = _load("roles/cassandra_operation/vars/main.yml")
    for variables, host, ssl in (({}, "127.0.0.1", False),
                                 ({"cassandra_rpc_address": "10.0.0.5", "cassandra_listen_address": "10.0.0.6"}, "10.0.0.5", False),
                                 ({"cassandra_rpc_address": "0.0.0.0", "cassandra_listen_address": "10.0.0.6"}, "10.0.0.6", False),
                                 ({"cassandra_client_encryption_enabled": True}, "127.0.0.1", True),
                                 ({"cassandra_client_encryption_enabled": True,
                                   "cassandra_client_encryption_optional": "true"}, "127.0.0.1", False)):
        assert str(_render(defaults["_cassandra_storage_mode_cql_host"], **variables)).strip() == host
        assert str(_render(defaults["_cassandra_storage_mode_cql_ssl"], **variables)).strip() in (str(ssl), str(ssl).lower())
    # an unset address read as None: never asked
    for variables in ({"cassandra_rpc_address": None, "cassandra_listen_address": "10.0.0.6"},
                      {"cassandra_rpc_address": "None"}):
        assert str(_render(defaults["_cassandra_storage_mode_cql_host"], **variables)).strip() in ("10.0.0.6", "127.0.0.1")
        assert str(_render(defaults["_cassandra_storage_mode_cql_host"], **variables)).strip() != "None"
    # the filter's address (the read-first way out names it) is the same
    from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import _cql_host
    assert [_cql_host(None), _cql_host("10.0.0.5", "x"), _cql_host("0.0.0.0", "10.0.0.6"), _cql_host("None", "10.0.0.6")] == [
        "127.0.0.1", "10.0.0.5", "10.0.0.6", "10.0.0.6"]
    # not read over CQL: said, once a run per node, with what tells the mode instead
    say = next(t for t in tasks if t["name"] == "Say that the mode Cassandra runs was not read over CQL")
    failed = {"failed": True, "msg": "module execution failed", "rc": 1,
              "err": "Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'cqlshlib'\n"}
    base = dict(say["vars"], inventory_hostname="n1", _cassandra_storage_mode_cql_host="10.0.0.5",
                _cassandra_storage_mode_cql_ssl=False, cassandra_storage_mode_cql=failed)
    raw = str(_render(say["ansible.builtin.debug"]["msg"], cassandra_storage_mode_read={
        "stdout_lines": ["yaml=NONE", "running=yes", "logged=NONE"]}, **base))
    assert raw.startswith("WARNING  n1: ")
    assert " ".join(raw.split()).split(": ", 1)[1] == (
        "the storage_compatibility_mode Cassandra runs could not be read over CQL (cqlsh on 10.0.0.5:9042:"
        " ModuleNotFoundError: No module named 'cqlshlib'); the line it logged at its start tells it instead.")
    msg = " ".join(str(_render(say["ansible.builtin.debug"]["msg"], cassandra_storage_mode_read={
        "stdout_lines": ["yaml=NONE", "running=yes"]}, **base)).split())
    assert msg.endswith("nothing logged since its start either: its cassandra.yaml tells it unless the file changed"
                        " since that start.")
    assert "not _cassandra_storage_mode_cql_said | default(false) | bool" in say["when"]


def test_jump_on_evidence_only():
    # runs CASSANDRA_4 (logged or read over CQL), NONE to be written or in its file: two steps
    assert cassandra_storage_mode_jump(_host("UPGRADING", logged="CASSANDRA_4"), "NONE") == "CASSANDRA_4"
    assert cassandra_storage_mode_jump(_host("NONE", cql="CASSANDRA_4")) == "CASSANDRA_4"
    assert cassandra_storage_mode_jump(_host("NONE", cql="null")) == "CASSANDRA_4"  # no such line at its start
    # one step, the same, back (the other check), not known: nothing
    assert cassandra_storage_mode_jump(_host("UPGRADING", logged="CASSANDRA_4")) == ""
    assert cassandra_storage_mode_jump(_host("UPGRADING", logged="UPGRADING"), "NONE") == ""
    assert cassandra_storage_mode_jump(_host("NONE", logged="NONE"), "CASSANDRA_4") == ""
    assert cassandra_storage_mode_jump(_host("NONE"), "NONE") == ""


def test_the_jump_says_why_and_the_way_out():
    def run(hosts, **extra):
        return guard(JUMP, hosts, **extra)
    assert run({"n1": (_host("UPGRADING", logged="CASSANDRA_4"), "UPGRADING")}, _cassandra_storage_mode_written=True)[0]
    # apply_config, the role: the file at UPGRADING (written ahead of the node), NONE in the inventory
    ok, msg = run({"n1": (_host("UPGRADING", logged="CASSANDRA_4"), "NONE"),
                   "n2": (_host("UPGRADING", logged="UPGRADING"), "NONE")}, _cassandra_storage_mode_written=True)
    assert ok is False and msg == [
        "A restart would take n1 from CASSANDRA_4 straight to NONE (5.0 moves CASSANDRA_4, UPGRADING, NONE, one step"
        " at a time, each node restarted in UPGRADING before NONE). Nothing was changed.",
        "  n1  runs CASSANDRA_4, its cassandra.yaml would have NONE",
        "WAY OUT  On n1, run rolling_restart --limit n1 (it restarts it in the UPGRADING its cassandra.yaml has, one"
        " step). Then run this again."]
    # apply_config, the role: the file still at CASSANDRA_4 (NONE set in the inventory straight away): a restart of
    # that file would not move it, the inventory goes through UPGRADING
    ok, msg = run({"n1": (_host("CASSANDRA_4", logged="CASSANDRA_4"), "NONE")}, _cassandra_storage_mode_written=True)
    assert ok is False and msg[1:] == [
        "  n1  runs CASSANDRA_4, its cassandra.yaml would have NONE",
        "WAY OUT  On n1, set cassandra_storage_compatibility_mode: UPGRADING in the inventory, then run apply_config"
        " --limit n1 (it writes UPGRADING and restarts it), then set NONE in the inventory again. Then run this again."]
    # a restart alone (rolling_restart, rolling_reboot, update_java): its file at NONE, by hand
    ok, msg = run({"n1": (_host("NONE", logged="CASSANDRA_4"), "NONE")})
    assert ok is False and msg[1:] == [
        "  n1  runs CASSANDRA_4, its cassandra.yaml has NONE",
        "WAY OUT  On n1, set storage_compatibility_mode: UPGRADING in /etc/cassandra/conf/cassandra.yaml by hand"
        " (apply_config never moves its NONE back), then run rolling_restart --limit n1 (it restarts it in"
        " UPGRADING). Then run this again."]


def test_apply_config_refuses_before_its_plan_warns():
    # the guard runs in the config preview (Compare the config), before the plan and its warnings (point of no return)
    plays = _load("playbooks/apply_config.yml")
    names = [t["name"] for p in plays for t in p.get("tasks", [])]
    assert names.index("Compare the config") < names.index("Put the settings that differ side by side") < names.index(
        "Show the plan and confirm the run")


# its cassandra.yaml changed since its start (by the record of that start, or by its times), nothing logged since
CHANGED = ("pending=yes", "changed=record")
CQL_FAILED = {"failed": True, "rc": 1, "err": "Connection error: ('Unable to connect to any servers')\n"}


def test_unsure_refuses_a_move_that_could_be_back():
    # nothing tells the mode it runs, its file changed since its start: any mode before NONE could be back
    for target in ("UPGRADING", "CASSANDRA_4"):
        assert cassandra_storage_mode_unsure(_host("UPGRADING", more=CHANGED), target)["target"] == target
    assert cassandra_storage_mode_unsure(_host("UPGRADING", more=CHANGED))["target"] == "UPGRADING"  # a restart alone
    assert cassandra_storage_mode_unsure(_host("", more=CHANGED))["target"] == "CASSANDRA_4"  # no such line
    # its file's time after now, its start not read: not known either
    for more in (["future=yes"], ["start_read=no"], ["start_read=after-now"]):
        assert cassandra_storage_mode_unsure(_host("CASSANDRA_4", more=more)), more
    # a 4.x Cassandra (the preview of an upgrade to 5.0): no such mode, its sstables in the 4.x format: nothing
    assert cassandra_storage_mode_unsure(_host("", more=CHANGED + ("jvm_version=4.1.5",)), "CASSANDRA_4") == {}
    assert cassandra_storage_mode_unsure(_host("", more=CHANGED + ("jvm_version=5.0.2",)), "CASSANDRA_4")
    # NONE too: it may run CASSANDRA_4 (an apply_config cut after its write of UPGRADING), NONE then two steps on
    assert cassandra_storage_mode_unsure(_host("UPGRADING", more=CHANGED), "NONE")["target"] == "NONE"
    assert cassandra_storage_mode_unsure(_host("NONE", more=CHANGED))["target"] == "NONE"
    # the file not changed since its start (it tells the mode); the mode read; stopped: nothing
    assert cassandra_storage_mode_unsure(_host("UPGRADING"), "CASSANDRA_4") == {}
    assert cassandra_storage_mode_unsure(_host("UPGRADING", logged="UPGRADING", more=CHANGED), "CASSANDRA_4") == {}
    assert cassandra_storage_mode_unsure(_host("UPGRADING", cql="UPGRADING", more=CHANGED)) == {}
    stopped = {"inventory_hostname": "n1",
               "cassandra_storage_mode_read": {"stdout_lines": ["yaml_read=yes", "yaml=", "running=no"]}}
    assert cassandra_storage_mode_unsure(stopped, "CASSANDRA_4") == {}
    # the mode read by hand, given: then the evidence-based checks decide (NONE given, UPGRADING to be written: back)
    given = dict(_host("UPGRADING", more=CHANGED), cassandra_storage_mode_running="n1:NONE,n2:UPGRADING")
    assert cassandra_storage_mode_unsure(given, "UPGRADING") == {}
    assert cassandra_storage_mode_back(given, "UPGRADING") == "NONE"
    assert cassandra_storage_mode_back(dict(given, cassandra_storage_mode_running={"n1": "UPGRADING"}), "UPGRADING") == ""
    assert cassandra_storage_mode_jump(dict(given, cassandra_storage_mode_running="n1:CASSANDRA_4"), "NONE") == "CASSANDRA_4"
    # given for another node only, or not a mode: still not known (the -e check says why first)
    assert cassandra_storage_mode_unsure(dict(given, cassandra_storage_mode_running="n2:NONE"), "UPGRADING")
    assert cassandra_storage_mode_unsure(dict(given, cassandra_storage_mode_running="n1:NONNE"), "UPGRADING")


def test_unsure_says_what_could_not_be_read_and_the_way_out():
    names = [t["name"] for t in _load(GUARD)]
    # after the evidence-based checks (they say more when the mode is known)
    assert names.index(JUMP) < names.index(UNSURE)

    def run(hosts, **extra):
        return guard(UNSURE, hosts, **extra)
    assert run({"n1": (_host("UPGRADING", logged="CASSANDRA_4", more=CHANGED), "UPGRADING")},
               _cassandra_storage_mode_written=True)[0] is True
    # apply_config, the role: UPGRADING to be written, the mode it runs not read
    n1 = dict(_host("UPGRADING", more=CHANGED), cassandra_storage_mode_cql=CQL_FAILED, cassandra_rpc_address="10.0.0.5")
    n2 = dict(_host("CASSANDRA_4", more=("pending=yes", "changed=time")), cassandra_storage_mode_cql={"skipped": True})
    ok, msg = run({"n1": (n1, "UPGRADING"), "n2": (n2, "UPGRADING"), "n3": (_host("UPGRADING", logged="UPGRADING"),
                                                                            "UPGRADING")},
                  _cassandra_storage_mode_written=True)
    assert ok is False and msg == [
        "A restart could move n1, n2 back, or two steps on. Nothing was changed.",
        "The storage_compatibility_mode Cassandra runs there could not be read.",
        "5.0 moves CASSANDRA_4, UPGRADING, NONE: one step at a time, never back.",
        "  n1  its cassandra.yaml would have UPGRADING (the inventory's)",
        "  n1  its cassandra.yaml changed since Cassandra started (its checksum is not the one recorded at that start)",
        "  n1  CQL: not read (cqlsh on 10.0.0.5:9042: Connection error: ('Unable to connect to any servers'))",
        "  n1  system.log: no storage_compatibility_mode logged since its start",
        "  n2  its cassandra.yaml would have UPGRADING (the inventory's)",
        "  n2  its cassandra.yaml changed since Cassandra started (its mtime or ctime is after that start)",
        "  n2  CQL: not read (cqlsh on 127.0.0.1:9042)",
        "  n2  system.log: no storage_compatibility_mode logged since its start",
        "WAY OUT  Read the mode Cassandra runs on each node, and give it to this run:",
        "  1. On each node, as the cluster's CQL user (null means CASSANDRA_4):",
        "     SELECT value FROM system_views.settings WHERE name = 'storage_compatibility_mode';",
        "     n1  cqlsh on 10.0.0.5:9042",
        "     n2  cqlsh on 127.0.0.1:9042",
        "  2. Run this again with:",
        "     -e cassandra_storage_mode_running=n1:<its mode>,n2:<its mode>",
        "  Or let this run read it over CQL:",
        "  1. Set cassandra_cql_username and cassandra_cql_password.",
        "  2. For a cqlsh outside root's PATH, set cassandra_cqlsh_command.",
        "  3. Run this again."]
    # one line per fact: none longer than a terminal's
    assert max(len(line) for line in msg if not line.startswith("  n")) <= 120
    # a restart alone (rolling_restart): the file's mode; NONE in the file too (it may run CASSANDRA_4)
    ok, msg = run({"n1": (n1, "NONE")})
    assert ok is False and msg[3] == "  n1  its cassandra.yaml has UPGRADING"
    ok, msg = run({"n1": (dict(n1, cassandra_storage_mode_read={"stdout_lines": [
        "yaml_read=yes", "yaml=NONE", "running=yes", "pending=yes", "changed=time"]}), "NONE")})
    assert ok is False and msg[3] == "  n1  its cassandra.yaml has NONE"
    # cassandra.yaml written back as Cassandra started with it (the record of its start has its checksum): the mode it
    # runs is the one written, no refusal (apply_config: no restart, its seeds reloaded)
    import hashlib
    rendered = "storage_compatibility_mode: UPGRADING\n"
    started = dict(n1, cassandra_storage_mode_read={"stdout_lines": n1["cassandra_storage_mode_read"]["stdout_lines"]
                                                    + ["started_sha1=" + hashlib.sha1(rendered.encode()).hexdigest()]})
    assert run({"n1": (dict(started, _cassandra_config_new={"cassandra.yaml": rendered}), "UPGRADING")},
               _cassandra_storage_mode_written=True)[0] is True
    assert run({"n1": (dict(started, _cassandra_config_new={"cassandra.yaml": "storage_compatibility_mode: CASSANDRA_4\n"}),
                       "CASSANDRA_4")}, _cassandra_storage_mode_written=True)[0] is False
    assert run({"n1": (dict(started, _cassandra_config_new={"cassandra.yaml": rendered}), "UPGRADING")})[0] is False
    # the mode given by hand: not refused here (the evidence-based checks decide)
    assert run({"n1": (n1, "UPGRADING")}, _cassandra_storage_mode_written=True,
               cassandra_storage_mode_running="n1:CASSANDRA_4")[0] is True


def test_the_read_says_how_the_file_changed_and_the_way_out_variable_is_known():
    read = next(t for t in _load("roles/cassandra_operation/tasks/storage_mode_read.yml")
                if t["name"] == "Read storage_compatibility_mode in its cassandra.yaml and in the running Cassandra")
    shell = read["ansible.builtin.shell"]
    assert "changed=record" in shell and "changed=time" in shell and 'echo "started_sha1=$rsum"' in shell
    assert "grep -o 'apache-cassandra-[0-9][0-9.]*[0-9]'" in shell and 'echo "jvm_version=${v#apache-cassandra-}"' in shell
    assert "printf 'pending=yes\\nchanged=%s\\n' \"$changed\"" in shell
    # the CassandraDaemon of its own PID namespace (as config_snapshot.yml), not a container's on the same host
    assert 'me=$(readlink /proc/self/ns/pid 2>/dev/null)' in shell
    assert '[ -z "$me" ] || [ "$(readlink /proc/$p/ns/pid 2>/dev/null)" = "$me" ] || continue' in shell
    variables = _load("roles/cassandra_operation/vars/main.yml")["_cassandra_playbook_variables"]
    assert "cassandra_storage_mode_running" in variables


def test_the_back_way_out_writes_by_hand_what_apply_config_cannot():
    # its file two steps behind the mode it runs, or without the line: cassandra_config's identity check would refuse
    # apply_config's write (one step forward from a line only): by hand, the inventory too when behind
    ok, msg = guard(BACK, {"n1": (_host("CASSANDRA_4", logged="NONE"), "NONE")})
    assert msg[1:] == [
        "  n1  runs NONE, its cassandra.yaml has CASSANDRA_4",
        "WAY OUT  On n1, set storage_compatibility_mode: NONE in /etc/cassandra/conf/cassandra.yaml by hand (apply_config"
        " moves it only one step forward, from a storage_compatibility_mode line). Then run this again."]
    ok, msg = guard(BACK, {"n1": (_host("", logged="UPGRADING"), "CASSANDRA_4")})
    assert msg[1:] == [
        "  n1  runs UPGRADING, its cassandra.yaml has CASSANDRA_4 (no such line)",
        "WAY OUT  On n1, set storage_compatibility_mode: UPGRADING in /etc/cassandra/conf/cassandra.yaml by hand"
        " (apply_config moves it only one step forward, from a storage_compatibility_mode line), and set"
        " cassandra_storage_compatibility_mode: UPGRADING in the inventory. Then run this again."]
    ok, msg = guard(BACK, {"n1": (_host("", logged="UPGRADING"), "CASSANDRA_4")}, _cassandra_storage_mode_written=True)
    assert msg[-1].startswith("WAY OUT  On n1, set storage_compatibility_mode: UPGRADING in /etc/cassandra/conf/cassandra.yaml"
                              " by hand")


def test_the_jump_way_out_without_the_line_is_by_hand():
    # no storage_compatibility_mode line (CASSANDRA_4): apply_config would not write UPGRADING from it
    ok, msg = guard(JUMP, {"n1": (_host("", logged="CASSANDRA_4"), "NONE")}, _cassandra_storage_mode_written=True)
    assert ok is False and msg[1:] == [
        "  n1  runs CASSANDRA_4, its cassandra.yaml would have NONE",
        "WAY OUT  On n1, set storage_compatibility_mode: UPGRADING in /etc/cassandra/conf/cassandra.yaml by hand"
        " (apply_config moves it only from a storage_compatibility_mode line), then run rolling_restart --limit n1 (it"
        " restarts it in UPGRADING). Then run this again."]


def test_a_mode_given_with_e_is_taken_only_where_nothing_else_tells_it():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import (
        cassandra_storage_mode_given_unknown, cassandra_storage_mode_given_wrong)
    # it runs UPGRADING (logged, over CQL, or its file unchanged since its start): NONE given is refused, not taken
    for host in (_host("UPGRADING", logged="UPGRADING"), _host("UPGRADING", cql="UPGRADING"), _host("UPGRADING")):
        given = dict(host, cassandra_storage_mode_running="n1:NONE")
        assert cassandra_storage_mode_back(given, "UPGRADING") == ""
        assert cassandra_storage_mode_given_wrong(given).startswith("  n1  -e gives NONE, Cassandra runs UPGRADING (")
    assert cassandra_storage_mode_given_wrong(dict(_host("UPGRADING"), cassandra_storage_mode_running="n1:NONE")) == (
        "  n1  -e gives NONE, Cassandra runs UPGRADING (its cassandra.yaml has it, unchanged since Cassandra started)")
    # the same mode, or nothing else tells it: taken
    assert cassandra_storage_mode_given_wrong(dict(_host("UPGRADING"), cassandra_storage_mode_running="n1:upgrading")) == ""
    assert cassandra_storage_mode_given_wrong(dict(_host("UPGRADING", more=CHANGED), cassandra_storage_mode_running="n1:NONE")) == ""
    assert cassandra_storage_mode_given_wrong(dict(_host("UPGRADING", more=CHANGED), cassandra_storage_mode_running="n1:NONNE")) \
        == "  n1  -e gives NONNE: not a mode (CASSANDRA_4, UPGRADING or NONE)"
    # items that name no node of the run: said, never left aside
    assert cassandra_storage_mode_given_unknown("UPGRADING,10.0.0.5:NONE,n1:NONE,fe80::1:NONE", ["n1"]) == [
        "  UPGRADING  not node:MODE",
        "  10.0.0.5  -e gives NONE: no such node in this run (its inventory name)",
        "  fe80::1  -e gives NONE: no such node in this run (its inventory name)"]
    assert cassandra_storage_mode_given_unknown({"n1": "NONE"}, ["n1"]) == []
    # the check: first, before the others
    names = [t["name"] for t in _load(GUARD)]
    assert names.index(GIVEN) < names.index(BACK)
    ok, msg = guard(GIVEN, {"n1": (_host("UPGRADING", logged="UPGRADING"), "UPGRADING")},
                    cassandra_storage_mode_running="n1:NONE,n9:NONE")
    assert ok is False and msg == [
        "-e cassandra_storage_mode_running gives a mode this run cannot take (node:MODE, the inventory name of a node of"
        " this run, where the mode it runs is not read). Nothing was changed.",
        "  n1  -e gives NONE, Cassandra runs UPGRADING (logged at its start)",
        "  n9  -e gives NONE: no such node in this run (its inventory name)",
        "WAY OUT  Fix or drop these items of -e cassandra_storage_mode_running. Then run this again."]
    assert guard(GIVEN, {"n1": (_host("UPGRADING", more=CHANGED), "UPGRADING")},
                 cassandra_storage_mode_running="n1:NONE")[0] is True
    # a row built from it says so, never as observed
    given = dict(_host("UPGRADING", more=CHANGED), cassandra_storage_mode_running="n1:NONE")
    ok, msg = guard(BACK, {"n1": (given, "NONE")})
    assert msg[1:] == [
        "  n1  runs NONE (given with -e), its cassandra.yaml has UPGRADING",
        "WAY OUT  On n1, run apply_config --limit n1 -e cassandra_storage_mode_running=n1:NONE (it writes NONE back)."
        " Then run this again."]


def test_update_java_refuses_a_storage_mode_change_and_checks_only_its_nodes():
    play = next(p for p in _load("playbooks/update_java.yml") if p.get("name") == "Check the Java change")
    names = [t["name"] for t in play["tasks"]]
    guard_task = play["tasks"][names.index("Refuse a restart into another storage_compatibility_mode than one step from"
                                           " the one it runs")]
    assert names.index("Group the nodes to do") < names.index(guard_task["name"])
    assert guard_task["vars"] == {
        "_cassandra_storage_mode_hosts": "{{ groups['cassandra_update_java_True'] | default([]) }}",
        "_cassandra_storage_mode_written": True, "_cassandra_storage_mode_kept": True}
    # it runs UPGRADING, NONE already in the inventory: update_java would write NONE, unshown; refused before the
    # question (the back check alone passes it: one step on)
    hosts = {"n1": (_host("UPGRADING", logged="UPGRADING"), "NONE"), "n2": (_host("NONE", logged="NONE"), "NONE"),
             "n3": (_host("", logged="CASSANDRA_4"), "CASSANDRA_4")}
    assert guard(BACK, hosts, _cassandra_storage_mode_written=True)[0] is True
    ok, msg = guard(KEPT, hosts, _cassandra_storage_mode_kept=True)
    assert ok is False and msg == [
        "This run would change storage_compatibility_mode on n1: it writes the inventory's config before each restart,"
        " without showing it. Nothing was changed.",
        "  n1  inventory NONE, its cassandra.yaml UPGRADING",
        "WAY OUT  Run apply_config first (it shows the change and its warning). Then run this again."]
    # behind too (refused before Java is installed); only the nodes update_java restarts
    hosts = {"n1": (_host("NONE", logged="NONE"), "UPGRADING"), "n2": (_host("NONE", logged="NONE"), "NONE")}
    assert guard(KEPT, hosts, _cassandra_storage_mode_kept=True)[1][1] == "  n1  inventory UPGRADING, its cassandra.yaml NONE"
    assert guard(KEPT, hosts, _cassandra_storage_mode_kept=True, _cassandra_storage_mode_hosts=["n2"])[0] is True
    assert guard(BACK, hosts, _cassandra_storage_mode_written=True, _cassandra_storage_mode_hosts=["n2"])[0] is True
