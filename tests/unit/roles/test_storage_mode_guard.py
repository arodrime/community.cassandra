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


def test_the_guard_says_why_and_the_way_out():
    tasks = _load("roles/cassandra_operation/tasks/storage_mode_guard.yml")
    assert tasks[0]["ansible.builtin.include_tasks"] == "storage_mode_read.yml"
    check = tasks[1]
    # once for the nodes of the play: one refused stops the run before any node is restarted
    assert check["run_once"] is True and check["vars"]["cassandra_output"] is True

    def run(hosts, **extra):
        hostvars = {}
        for name, (host, inventory) in hosts.items():
            hostvars[name] = dict(host, cassandra_version="50x", cassandra_storage_compatibility_mode=inventory)
        variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_version="50x",
                         **extra)
        return (_render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
                _render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n"))
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
        "WAY OUT  On n2, set cassandra_storage_compatibility_mode: NONE in the inventory (apply_config then writes it)."
        " Then run this again."]
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
    # the filter's address (the read-first way out names it) is the same
    from ansible_collections.community.cassandra.plugins.filter.cassandra_storage_mode import _cql_host
    assert [_cql_host(None), _cql_host("10.0.0.5", "x"), _cql_host("0.0.0.0", "10.0.0.6")] == [
        "127.0.0.1", "10.0.0.5", "10.0.0.6"]
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
    tasks = _load("roles/cassandra_operation/tasks/storage_mode_guard.yml")
    check = next(t for t in tasks if t["name"] == "The restarts move the storage_compatibility_mode Cassandra runs one step at most")
    assert check["run_once"] is True and check["vars"]["cassandra_output"] is True

    def run(hosts, **extra):
        hostvars = {}
        for name, (host, inventory) in hosts.items():
            hostvars[name] = dict(host, cassandra_version="50x", cassandra_storage_compatibility_mode=inventory)
        variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_version="50x",
                         **extra)
        return (_render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
                _render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n"))
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
    # NONE is never back; the file not changed since its start (it tells the mode); the mode read; stopped: nothing
    assert cassandra_storage_mode_unsure(_host("UPGRADING", more=CHANGED), "NONE") == {}
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
    # given for another node only, or not a mode: still not known
    assert cassandra_storage_mode_unsure(dict(given, cassandra_storage_mode_running="n2:NONE"), "UPGRADING")
    assert cassandra_storage_mode_unsure(dict(given, cassandra_storage_mode_running="n1:NONNE"), "UPGRADING")["rows"][-1] \
        == "  n1  -e cassandra_storage_mode_running gives NONNE: not a mode (CASSANDRA_4, UPGRADING or NONE)"


def test_unsure_says_what_could_not_be_read_and_the_way_out():
    tasks = _load("roles/cassandra_operation/tasks/storage_mode_guard.yml")
    names = [t["name"] for t in tasks]
    # after the evidence-based checks (they say more when the mode is known)
    check = tasks[names.index("The restarts move no node back while the mode it runs is not known")]
    assert names.index("The restarts move the storage_compatibility_mode Cassandra runs one step at most") < names.index(
        check["name"])
    assert check["run_once"] is True and check["vars"]["cassandra_output"] is True

    def run(hosts, **extra):
        hostvars = {}
        for name, (host, inventory) in hosts.items():
            hostvars[name] = dict(host, inventory_hostname=name, cassandra_version="50x",
                                  cassandra_storage_compatibility_mode=inventory)
        variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_version="50x",
                         **extra)
        return (_render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
                _render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n"))
    assert run({"n1": (_host("UPGRADING", logged="CASSANDRA_4", more=CHANGED), "UPGRADING")},
               _cassandra_storage_mode_written=True)[0] is True
    # apply_config, the role: UPGRADING to be written, the mode it runs not read
    n1 = dict(_host("UPGRADING", more=CHANGED), cassandra_storage_mode_cql=CQL_FAILED, cassandra_rpc_address="10.0.0.5")
    n2 = dict(_host("CASSANDRA_4", more=("pending=yes", "changed=time")), cassandra_storage_mode_cql={"skipped": True})
    ok, msg = run({"n1": (n1, "UPGRADING"), "n2": (n2, "UPGRADING"), "n3": (_host("UPGRADING", logged="UPGRADING"),
                                                                            "UPGRADING")},
                  _cassandra_storage_mode_written=True)
    assert ok is False and msg == [
        "A restart could take n1, n2 back to an earlier storage_compatibility_mode than the one Cassandra runs, which"
        " could not be read (5.0 moves CASSANDRA_4, UPGRADING, NONE, never back). Nothing was changed.",
        "  n1  its cassandra.yaml would have UPGRADING (the inventory's)",
        "  n1  its cassandra.yaml changed since Cassandra started (its checksum is not the one recorded at that start)",
        "  n1  CQL: not read (cqlsh on 10.0.0.5:9042: Connection error: ('Unable to connect to any servers'))",
        "  n1  system.log: no storage_compatibility_mode logged since its start",
        "  n2  its cassandra.yaml would have UPGRADING (the inventory's)",
        "  n2  its cassandra.yaml changed since Cassandra started (its mtime or ctime is after that start)",
        "  n2  CQL: not read (cqlsh on 127.0.0.1:9042)",
        "  n2  system.log: no storage_compatibility_mode logged since its start",
        "WAY OUT  Let this run read the mode over CQL: on n1, cqlsh on 10.0.0.5:9042, on n2, cqlsh on 127.0.0.1:9042, as"
        " the cluster's CQL user (cassandra_cql_username, cassandra_cql_password; cassandra_cqlsh_command for a cqlsh"
        " outside root's PATH). Or read it there by hand (SELECT value FROM system_views.settings WHERE name ="
        " 'storage_compatibility_mode'; null: CASSANDRA_4) and give it: -e"
        " cassandra_storage_mode_running=n1:<its mode>,n2:<its mode>. Then run this again."]
    # a restart alone (rolling_restart): the file's mode; NONE in the file never refused
    ok, msg = run({"n1": (n1, "NONE")})
    assert ok is False and msg[1] == "  n1  its cassandra.yaml has UPGRADING"
    assert run({"n1": (dict(n1, cassandra_storage_mode_read={"stdout_lines": [
        "yaml_read=yes", "yaml=NONE", "running=yes", "pending=yes", "changed=time"]}), "NONE")})[0] is True
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
    assert run({"n1": (dict(n1, cassandra_storage_mode_running="n1:CASSANDRA_4"), "UPGRADING")},
               _cassandra_storage_mode_written=True)[0] is True


def test_the_read_says_how_the_file_changed_and_the_way_out_variable_is_known():
    read = next(t for t in _load("roles/cassandra_operation/tasks/storage_mode_read.yml")
                if t["name"] == "Read storage_compatibility_mode in its cassandra.yaml and in the running Cassandra")
    shell = read["ansible.builtin.shell"]
    assert "changed=record" in shell and "changed=time" in shell and 'echo "started_sha1=$rsum"' in shell
    assert "grep -o 'apache-cassandra-[0-9][0-9.]*[0-9]'" in shell and 'echo "jvm_version=${v#apache-cassandra-}"' in shell
    assert "printf 'pending=yes\\nchanged=%s\\n' \"$changed\"" in shell
    variables = _load("roles/cassandra_operation/vars/main.yml")["_cassandra_playbook_variables"]
    assert "cassandra_storage_mode_running" in variables
