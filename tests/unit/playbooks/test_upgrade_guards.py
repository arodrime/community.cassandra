from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# upgrade, found on a 4.1 -> 5.0 run: the prepare's config copy holds the files (copy's force: false copied
# none from a directory), the canary and rolling phases refuse nodes without the pre-upgrade snapshot, the
# sstables phase refuses a 5.0 target not in storage_compatibility_mode NONE, the rolling phase says the next
# mode step from the inventory (a re-run too), --check says what a real run would do, cleanup suggests no way
# back, a node down with the target package installed is pointed to apply_config, and apply_config's plan says
# NONE is the point of no return.

import hashlib
import json
import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()  # the community.cassandra filters, whatever ran before

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as _f:
    PLAYS = yaml.safe_load(_f)


def play(name):
    return next(p for p in PLAYS if p.get("name") == name)


def task(play_name, name):
    return next(t for t in play(play_name)["tasks"] if t.get("name") == name)


def names(play_name):
    return [t["name"] for t in play(play_name)["tasks"]]


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def text(template, **variables):
    return " ".join(str(render(template, **variables)).split())


def test_prepare_copies_the_config_files_and_checks_the_copy(tmp_path):
    import subprocess
    copy = task("Prepare", "Copy the config")["ansible.builtin.copy"]
    assert "force" not in copy  # force: false copies nothing from a directory (an empty copy, said done)
    assert copy["remote_src"] is True and copy["src"].endswith("/") and copy["dest"].endswith("/")
    order = names("Prepare")
    assert (order.index("Look for an earlier config copy") < order.index("Copy the config")
            < order.index("Compare the config copy with the config") < order.index("Check the config copy holds every file of the config")
            < order.index("Say what the next phase is"))
    # an earlier prepare's copy (with its cassandra.yaml) is kept: the canary's config may be the new one by then
    assert task("Prepare", "Copy the config")["when"] == "not cassandra_upgrade_conf_copied.stat.exists"
    assert task("Prepare", "Say the config copy is already there")["when"] == "cassandra_upgrade_conf_copied.stat.exists"

    # the comparison, run: every file of the config, by name (and size for a copy made now)
    compare = task("Prepare", "Compare the config copy with the config")
    conf, link = tmp_path / "default.conf", tmp_path / "conf"
    (conf / "triggers").mkdir(parents=True)
    for name, text in (("cassandra.yaml", "a: 1\n"), ("jvm-server.options", "-Xss256k\n"), ("triggers/README.txt", "x")):
        (conf / name).write_text(text)
    link.symlink_to(conf)  # /etc/cassandra/conf of the RPM: an alternatives link
    copy_dir = tmp_path / "conf.pre-upgrade-4.1.12"

    (conf / "my notes.txt").write_text("a name with a space")
    (conf / "cassandra.yaml.12345.2026-11-09@10:00:00~").write_text("an Ansible backup")  # left aside

    def compared(fresh="yes"):
        env = {"SRC": str(link), "COPY": str(copy_dir), "FRESH": fresh, "PATH": os.environ["PATH"],
               "TMPDIR": str(tmp_path)}
        out = subprocess.run(["/bin/sh", "-c", compare["ansible.builtin.shell"]], env=env, stdout=subprocess.PIPE,
                             universal_newlines=True, check=True)
        return out.stdout.split("\n")[:-1]
    subprocess.run(["cp", "-a", str(conf) + "/.", str(copy_dir)], check=True)
    (copy_dir / "cassandra.yaml.12345.2026-11-09@10:00:00~").unlink()
    assert compared() == []
    (copy_dir / "triggers" / "README.txt").unlink()
    (copy_dir / "my notes.txt").unlink()
    (copy_dir / "cassandra.yaml").write_text("a: 12\n")
    assert compared() == ["missing my notes.txt", "missing triggers/README.txt", "size cassandra.yaml"]
    assert compared(fresh="no") == ["missing my notes.txt", "missing triggers/README.txt"]  # an earlier copy: its sizes may differ
    # a tree find cannot read: which one
    os.chmod(str(copy_dir / "triggers"), 0)
    try:
        if os.geteuid() != 0:
            assert compared()[0].startswith("unreadable copy %s: " % copy_dir)
    finally:
        os.chmod(str(copy_dir / "triggers"), 0o755)
    assert sorted(os.listdir(str(tmp_path))) == ["conf", "conf.pre-upgrade-4.1.12", "default.conf"]  # its temp dir removed

    check = task("Prepare", "Check the config copy holds every file of the config")
    assert check["when"] == "not ansible_check_mode"
    variables = dict(check["vars"], inventory_hostname="n1", cassandra_conf_dir="/etc/cassandra/conf",
                     cassandra_upgrade_from="4.1.12", cassandra_upgrade_conf_compare={"stdout_lines": ["missing cassandra.yaml"]},
                     _cassandra_run_command_args={"inventory": None, "default_inventory": "", "hosts": "", "cwd": None})
    assert render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n") == [
        "n1: the config copy /etc/cassandra/conf.pre-upgrade-4.1.12 is not whole (/etc/cassandra/conf has more, or other"
        " sizes). The snapshot pre-upgrade-4.1.12 is taken; no node was upgraded.",
        "  missing cassandra.yaml",
        "WAY OUT  remove /etc/cassandra/conf.pre-upgrade-4.1.12 on n1 (rm -r), then run the prepare phase again:",
        "  ansible-playbook community.cassandra.upgrade -e cassandra_upgrade_phase=prepare"]
    # the config itself not read whole: said so, the way out on it (not on the copy)
    variables["cassandra_upgrade_conf_compare"] = {"stdout_lines": [
        "unreadable config /etc/cassandra/conf: find: './x': Permission denied"]}
    lines = render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n")
    assert lines[0] == ("n1: the config /etc/cassandra/conf could not be read whole, the copy is not checked. The snapshot"
                        " pre-upgrade-4.1.12 is taken; no node was upgraded.")
    assert lines[2].startswith("WAY OUT  make /etc/cassandra/conf on n1 readable")
    variables["cassandra_upgrade_conf_compare"] = {"stdout_lines": ["unreadable copy /etc/cassandra/conf.pre-upgrade-4.1.12: x"]}
    lines = render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n")
    assert lines[0].startswith("n1: the config copy /etc/cassandra/conf.pre-upgrade-4.1.12 could not be read whole.")
    assert lines[2].startswith("WAY OUT  remove /etc/cassandra/conf.pre-upgrade-4.1.12 on n1")


def test_prepare_under_check_says_what_a_real_run_would_do():
    say = task("Prepare", "Say what the next phase is")["ansible.builtin.debug"]["msg"]
    variables = dict(cassandra_upgrade_from="4.1.12", cassandra_conf_dir="/etc/cassandra/conf",
                     ansible_play_hosts=["n1"], ansible_play_hosts_all=["n1"])
    assert text(say, ansible_check_mode=True, **variables) == (
        "--check: nothing was done. A real run would take the snapshots pre-upgrade-4.1.12 and copy the config to "
        "/etc/cassandra/conf.pre-upgrade-4.1.12.")
    assert text(say, ansible_check_mode=False, **variables) == (
        "Snapshots pre-upgrade-4.1.12 and config copies in /etc/cassandra/conf.pre-upgrade-4.1.12. "
        "Next: -e cassandra_upgrade_phase=canary.")


LISTED = ("Snapshot Details: \n"
          "Snapshot name     Keyspace name Column family name True size Size on disk\n"
          "pre-upgrade-4.1.12 ks1          t1                 0 bytes   1.2 KiB\n")


def test_canary_and_rolling_refuse_a_node_without_its_pre_upgrade_snapshot():
    order = names("Upgrade checks")
    assert order.index("Note the versions") < order.index("List the snapshots of the nodes to upgrade") \
        < order.index("Every node to upgrade has its pre-upgrade snapshot")
    listing = task("Upgrade checks", "List the snapshots of the nodes to upgrade")
    assert listing["check_mode"] is False  # read under --check too: the canary --check before prepare refuses
    assert "not cassandra_upgrade_done | bool" in listing["when"]
    check = task("Upgrade checks", "Every node to upgrade has its pre-upgrade snapshot")
    assert check["run_once"] is True and check["when"] == "_phase in ['canary', 'rolling']"
    hostvars = {"n1": {"inventory_hostname": "n1", "cassandra_upgrade_from": "4.1.12",
                       "cassandra_upgrade_prepared": {"stdout": LISTED}},
                "n2": {"inventory_hostname": "n2", "cassandra_upgrade_from": "4.1.12",
                       "cassandra_upgrade_prepared": {"stdout": "Snapshot Details: \nThere are no snapshots\n"}},
                "n3": {"inventory_hostname": "n3", "cassandra_upgrade_from": "5.0.9",  # upgraded: not listed
                       "cassandra_upgrade_prepared": {"skipped": True}}}
    # the way out: the run's own command (its inventory, its cluster), as the other way outs
    run = {"inventory": ["/inv/a.yml"], "default_inventory": "", "hosts": "staging_b", "cwd": "/inv"}
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_upgrade_from="4.1.12",
                     _cassandra_run_command_args=run)
    assert render(check["vars"]["_unprepared"], **variables) == ["n2"]
    assert render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n") == [
        "No snapshot pre-upgrade-4.1.12 on n2 (the prepare phase takes it). Nothing was done.",
        "WAY OUT  run the prepare phase, then this phase again:",
        "  ansible-playbook -i a.yml community.cassandra.upgrade -e cassandra_hosts=staging_b -e cassandra_upgrade_phase=prepare"]
    hostvars["n2"]["cassandra_upgrade_prepared"]["stdout"] = LISTED
    assert render(check["vars"]["_unprepared"], **variables) == []


def _modes(modes, from_series="41x", copies=()):
    """[(inventory mode, cassandra.yaml mode, running mode[, pending])] -> hostvars n1.. (inventory None: not set;
    cassandra.yaml None: not read; running None: Cassandra not running; a running mode other than the yaml's: logged,
    the file changed since; pending True: the file changed since its start, nothing logged); every node coming from
    from_series, with those config copies."""
    hostvars = {}
    for i, mode in enumerate(modes, 1):
        inventory, yaml_mode, running, pending = (tuple(mode) + (None, None, False))[:4]
        lines = ["yaml_read=yes", "yaml=" + yaml_mode] if yaml_mode is not None else ["yaml_read=no"]
        if running is None:
            lines.append("running=no")
        else:
            lines.append("running=yes")
            if running != yaml_mode:
                lines.append("logged=" + running)
            elif pending:
                lines.append("pending=yes")
        h = {"inventory_hostname": "n%d" % i, "cassandra_upgrade_from_series": from_series, "cassandra_upgrade_done": True,
             "cassandra_storage_mode_read": {"stdout_lines": lines},
             "cassandra_upgrade_copies": {"stdout_lines": list(copies)}}
        if inventory is not None:
            h["cassandra_storage_compatibility_mode"] = inventory
        hostvars["n%d" % i] = h
    return hostvars


RUN = {"inventory": ["/inv/a.yml"], "default_inventory": "", "hosts": "", "cwd": "/inv"}


def _sstables(*modes, **kwargs):
    check = task("Upgrade checks", "The sstables are rewritten once storage_compatibility_mode is NONE")
    extra = kwargs.pop("extra", {})  # node -> lines added to what it read
    hostvars = _modes(modes, **kwargs)
    for node, lines in extra.items():
        hostvars[node]["cassandra_storage_mode_read"]["stdout_lines"] += lines
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_package_version="5.0.9",
                     _cassandra_run_command_args=RUN)
    return (render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables),
            render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n"))


def test_sstables_refuse_a_50_target_until_storage_compatibility_mode_is_none():
    read = task("Upgrade checks", "Read storage_compatibility_mode on the nodes")
    check = task("Upgrade checks", "The sstables are rewritten once storage_compatibility_mode is NONE")
    assert read["ansible.builtin.include_role"]["tasks_from"] == "storage_mode_read.yml"
    assert check["when"] == read["when"] == ["_phase == 'sstables'", "cassandra_version == '50x'"]
    assert check["run_once"] is True
    assert _sstables(("NONE", "NONE", "NONE"), (None, "NONE", "NONE"))[0] is True
    ok, msg = _sstables(("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4"), ("NONE", "NONE", "NONE"))
    assert ok is False and msg == [
        "storage_compatibility_mode is not NONE on n1: in a mode before NONE, 5.0 writes the sstables in the 4.x format,"
        " the sstables phase would rewrite them in it. Nothing was done.",
        "  node  inventory    cassandra.yaml  running",
        "  n1    CASSANDRA_4  CASSANDRA_4     CASSANDRA_4",
        "WAY OUT  Now set cassandra_storage_compatibility_mode: UPGRADING in the inventory and run apply_config, then"
        " NONE and apply_config again (NONE is the point of no return). Then run this phase again."]
    # the inventory at NONE, apply_config not run: the nodes still read and run CASSANDRA_4
    ok, msg = _sstables(("NONE", "CASSANDRA_4", "CASSANDRA_4"))
    assert ok is False and msg[2] == "  n1    NONE       CASSANDRA_4     CASSANDRA_4"
    ok, msg = _sstables(("UPGRADING", "UPGRADING", "UPGRADING"))
    assert ok is False and msg[-1].startswith("WAY OUT  Now set cassandra_storage_compatibility_mode: NONE in the inventory")


def test_sstables_refuse_a_mode_not_read_unknown_or_a_restart_pending():
    # its cassandra.yaml not read, Cassandra not running: refused, never taken as NONE; what was not read named
    ok, msg = _sstables(("NONE", None, None))
    assert ok is False and msg[2] == "  n1    NONE       not read        no Cassandra process found"
    assert ("On n1, neither its cassandra.yaml nor the mode it runs (no Cassandra process found) could be read: the cassandra.yaml"
            " of its cassandra_conf_dir must be readable.") in msg[-1]
    # NONE written by apply_config, the node started before that (the log rolled over: no logged mode): a restart
    ok, msg = _sstables(("NONE", "NONE", "NONE", True))
    assert ok is False and msg[2] == "  n1    NONE       NONE            restart pending (its cassandra.yaml changed after Cassandra started)"
    assert msg[0] == "A restart is pending on n1 (its cassandra.yaml changed after Cassandra started). Nothing was done."
    assert msg[-1] == ("WAY OUT  Restart n1, so it runs the mode its cassandra.yaml has: ansible-playbook -i a.yml"
                       " community.cassandra.rolling_restart --limit n1. Then run this phase again.")
    # it logged UPGRADING at its start, NONE written since: the restart too
    ok, msg = _sstables(("NONE", "NONE", "UPGRADING"))
    assert ok is False and "Restart n1, so it runs the mode its cassandra.yaml has" in msg[-1]
    # a file time after now on n2, a restart pending on n1: both restarted, n2 touched first
    ok, msg = _sstables(("NONE", "NONE", "NONE", True), ("NONE", "NONE", "NONE"), extra={"n2": ["future=yes"]})
    assert ok is False and msg[-1] == (
        "WAY OUT  Set the clock of n2 right (its cassandra.yaml has a time after now), then on it: touch cassandra.yaml."
        " Restart n1, n2, so they run the mode their cassandra.yaml has: ansible-playbook -i a.yml"
        " community.cassandra.rolling_restart --limit n1,n2. Then run this phase again.")
    # NONE in the inventory and running, its cassandra.yaml edited back to UPGRADING by hand (a 5.0 cluster): the
    # inventory's mode written back, never a restart into UPGRADING nor "nothing to rewrite"
    patch = dict(from_series="50x", copies=("5.0.8",))
    ok, msg = _sstables(("NONE", "UPGRADING", "NONE"), **patch)
    assert ok is False and msg == [
        "On n1, cassandra.yaml has UPGRADING, before the inventory's NONE and the NONE it runs. Nothing was done.",
        "  node  inventory  cassandra.yaml  running",
        "  n1    NONE       UPGRADING       NONE",
        "WAY OUT  Write the inventory's mode back to the cassandra.yaml of n1 (apply_config restarts it): ansible-playbook -i a.yml"
        " community.cassandra.apply_config --limit n1. Then run this phase again."]
    # nothing logged (its log rolled over), its cassandra.yaml changed since its start: the mode it runs is not
    # known (it may run the file's), so no write back: the step and the restart. The inventory jumped to NONE, the
    # file at CASSANDRA_4: the step to UPGRADING first, never apply_config straight to NONE
    ok, msg = _sstables(("NONE", "CASSANDRA_4", "CASSANDRA_4", True))
    assert ok is False and "Write the inventory's mode back" not in "\n".join(msg)
    assert "Now set cassandra_storage_compatibility_mode: UPGRADING in the inventory" in msg[-1]
    assert "Restart n1, so it runs the mode its cassandra.yaml has" in msg[-1]
    ok, msg = _sstables(("NONE", "UPGRADING", "UPGRADING", True), **patch)
    assert ok is False and "Write the inventory's mode back" not in msg[-1] and "Restart n1" in msg[-1]
    # two steps back (CASSANDRA_4 where NONE was, NONE logged): apply_config refuses that move, by hand
    ok, msg = _sstables(("NONE", "CASSANDRA_4", "NONE"), **patch)
    assert ok is False and msg[-1] == (
        "WAY OUT  On n1, set storage_compatibility_mode: NONE back in cassandra.yaml by hand (apply_config moves it"
        " one step at a time, it refuses this one). Then run this phase again.")
    # a typo in the inventory: not taken as NONE
    ok, msg = _sstables(("NONNE", "NONE", "NONE"))
    assert ok is False and msg[2] == "  n1    NONNE (unknown)  NONE            NONE"
    assert msg[-1].startswith("WAY OUT  Set cassandra_storage_compatibility_mode for n1 to CASSANDRA_4, UPGRADING or NONE.")


def test_sstables_after_none_without_a_restart_since_midnight():
    # NONE in the yaml, the node started after that change (its log rolled over since: nothing logged): it runs NONE
    hostvars = _modes([("NONE", "NONE", "NONE")])
    assert "logged=NONE" not in hostvars["n1"]["cassandra_storage_mode_read"]["stdout_lines"]
    assert _sstables(("NONE", "NONE", "NONE"), ("NONE", "NONE", "NONE"))[0] is True


def test_a_5_0_patch_upgrade_is_told_there_is_nothing_to_rewrite():
    # a 5.0 cluster kept on CASSANDRA_4, 5.0.8 -> 5.0.9: the sstables phase refused, nothing to rewrite (cleanup next)
    patch = dict(from_series="50x", copies=("4.1.12", "5.0.8"))
    ok, msg = _sstables(("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4"), **patch)
    assert msg[0] == "storage_compatibility_mode is not NONE on n1: its sstables are in the format it writes. Nothing was done."
    assert ok is False and msg[-1] == (
        "WAY OUT  SSTables keep the 4.x format while storage_compatibility_mode is not NONE: nothing to rewrite. Skip this"
        " phase (cleanup next), or move to NONE first (point of no return) if you want the 5.0 format.")
    # one node of three at NONE already, the others at UPGRADING (one mode among those behind): the step that
    # finishes the move, from the number of nodes of the run
    ok, msg = _sstables(("UPGRADING", "UPGRADING", "UPGRADING"), ("UPGRADING", "UPGRADING", "UPGRADING"),
                        ("NONE", "NONE", "NONE"), **patch)
    assert ok is False and "To finish the move to NONE: now set cassandra_storage_compatibility_mode: NONE" in msg[-1]
    # its rolling text is not steered to NONE
    assert next_phase("rolling", modes=["CASSANDRA_4"], **patch) == "Every node runs 5.0.9. Then -e cassandra_upgrade_phase=sstables."
    # every node on 5.0 by then (a rolling phase run again, the sstables phase), the copy of 4.1 names the origin
    from_41 = dict(from_series="50x", copies=("4.1.12",))
    assert "Now set cassandra_storage_compatibility_mode: UPGRADING" in _sstables(
        ("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4"), **from_41)[1][-1]
    assert "UPGRADING" in next_phase("rolling", modes=["CASSANDRA_4"], **from_41)


def test_sstables_refused_first_while_the_rolling_phase_is_not_finished():
    order = names("Upgrade checks")
    assert order.index("The rolling phase is finished") < order.index("Read storage_compatibility_mode on the nodes")
    check = task("Upgrade checks", "The rolling phase is finished")
    assert check["when"] == "_phase == 'sstables'" and check["run_once"] is True
    hostvars = _modes([("CASSANDRA_4", "CASSANDRA_4", "CASSANDRA_4")] * 3)
    hostvars["n2"]["cassandra_upgrade_done"] = False
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_package_version="5.0.9",
                     _cassandra_run_command_args=RUN)
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is False
    assert render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n") == [
        "The rolling phase is not finished: n2 runs another version than 5.0.9. Nothing was done.",
        "WAY OUT  run the rolling phase, then this phase again:",
        "  ansible-playbook -i a.yml community.cassandra.upgrade -e cassandra_upgrade_phase=rolling"]
    hostvars["n2"]["cassandra_upgrade_done"] = True
    assert render("{{ %s }}" % check["ansible.builtin.assert"]["that"], **variables) is True


def test_the_storage_mode_read_script(tmp_path):
    import subprocess
    import time
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "storage_mode_read.yml"), encoding="utf-8") as f:
        read = yaml.safe_load(f)[1]
    assert read["check_mode"] is False and read["register"] == "cassandra_storage_mode_read"
    script = read["ansible.builtin.shell"]
    assert "[[:" not in script  # mawk and busybox awk/sed alike
    path = tmp_path / "cassandra.yaml"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pgrep").write_text("#!/bin/sh\nexit 1\n")  # no Cassandra running here
    (bin_dir / "pgrep").chmod(0o755)

    def run(content, sh_script=script):
        if content is not None:
            path.write_text(content)
        env = {"F": str(path), "PATH": "%s:%s" % (bin_dir, os.environ["PATH"])}
        out = subprocess.run(["/bin/sh", "-c", sh_script], env=env, stdout=subprocess.PIPE, universal_newlines=True,
                             check=True)
        assert out.stdout.split()[0] == "file=%s" % path
        return out.stdout.split()[1:]
    assert run(None) == ["yaml_read=no", "running=no"]  # no file: not read
    assert run("cluster_name: x\nstorage_compatibility_mode: UPGRADING  # the step\n")[:2] == ["yaml_read=yes", "yaml=UPGRADING"]
    assert run("# storage_compatibility_mode: NONE\ncluster_name: x\n")[:2] == ["yaml_read=yes", "yaml="]
    # a running Cassandra: a process of this test stands for it, started after the file was written
    path.write_text("storage_compatibility_mode: NONE\n")
    time.sleep(1.2)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        time.sleep(1.2)
        logs = tmp_path / "log"
        logs.mkdir()
        assert "/var/log/cassandra" in script
        fake = script.replace("$(pgrep -x java)", str(proc.pid)).replace(
            "grep -qx org.apache.cassandra.service.CassandraDaemon", "true").replace("/var/log/cassandra", str(logs))
        out = run(None, fake)
        assert "running=yes" in out and not [x for x in out if x.split("=")[0] in ("pending", "future", "start_read")]
        # the record of the config it started with (config_snapshot.yml), of this JVM: by content
        with open("/proc/%d/stat" % proc.pid) as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        jvm = {"pid": str(proc.pid), "start": round(time.time() - up + ticks / float(os.sysconf("SC_CLK_TCK")), 2)}
        record = tmp_path / "record.json"

        def with_record(content, pid=jvm["pid"]):
            record.write_text(json.dumps({"/etc/x//../cassandra/cassandra.yaml": hashlib.sha1(content).hexdigest(),
                                          "/etc/systemd/system/cassandra.service": "0" * 40,
                                          "_jvm": dict(jvm, pid=pid)}, indent=4, sort_keys=True))
            return fake.replace('"$R"', '"%s"' % record)
        none = b"storage_compatibility_mode: NONE\n"
        os.chmod(str(path), 0o640)  # metadata only (ctime moves, mtime not)
        # with the record of this JVM: by content, not pending
        assert not [x for x in run(None, with_record(none)) if x.split("=")[0] in ("pending", "future", "start_read")]
        # no record, nothing logged: by mtime and ctime, pending (a restart clears it)
        assert "pending=yes" in run(None, fake)
        # the mode it logged at its start (its system.log written since): decides instead, no pending line
        (logs / "system.log").write_text("INFO Node configuration:[a=1; storage_compatibility_mode=NONE; z=2]\n")
        out = run(None, fake)
        assert "logged=NONE" in out and "pending=yes" not in out
        # a system.log not written since this JVM started (an earlier start, another log): not taken
        old = time.time() - 86400
        os.utime(str(logs / "system.log"), (old, old))
        out = run(None, fake)
        assert not [x for x in out if x.startswith("logged=")] and "pending=yes" in out
        path.write_text("storage_compatibility_mode: NONE\n")  # written after it started
        assert "pending=yes" in run(None, fake)
        assert "pending=yes" not in run(None, with_record(none))  # same content
        assert "pending=yes" in run(None, with_record(b"storage_compatibility_mode: UPGRADING\n"))  # another
        # a file copied in with an older mtime (cp -p, mv, rsync -a, tar x): its ctime is new, pending with or
        # without the record
        os.utime(str(path), (old, old))
        assert "pending=yes" in run(None, fake)
        assert "pending=yes" in run(None, with_record(b"storage_compatibility_mode: UPGRADING\n"))
        # a record of another JVM: as none (by mtime and ctime)
        assert "pending=yes" in run(None, with_record(none, pid="1"))
        os.utime(str(path), (time.time() + 3600, time.time() + 3600))  # a time after now (a clock set back)
        out = run(None, fake)
        assert "future=yes" in out and "pending=yes" not in out
        # its start not computed (no btime): not read, never taken as not pending
        assert "start_read=no" in run(None, fake.replace("/proc/stat 2>/dev/null", "/nonexistent 2>/dev/null"))
        # its start after now (a boot time a virtual machine got wrong): not read either
        assert "start_read=after-now" in run(None, fake.replace("printf \"%d\", b + t / h", "printf \"%d\", b + t / h + 86400"))
    finally:
        proc.kill()
    # the logged mode, when there: the last "Node configuration" line Cassandra logged
    assert "Node configuration:" in script and "-Dcassandra[.]logdir=" in script
    log = "INFO Node configuration:[a=1; storage_compatibility_mode=%s; z=2]\n"
    sed = subprocess.run(["sh", "-c", "grep -a 'Node configuration:\\[' | tail -1 | sed -n "
                          "'s/.*[[; ]storage_compatibility_mode=\\([A-Z_0-9]*\\).*/\\1/p'"],
                         input=(log % "CASSANDRA_4") + (log % "NONE"), stdout=subprocess.PIPE, universal_newlines=True,
                         check=True)
    assert sed.stdout.strip() == "NONE"


def next_phase(phase, check=False, modes=None, version="50x", read=None, **kwargs):
    say = task("Summary", "Say what the next phase is")
    modes = modes or [None]
    hostvars = _modes(list(zip(modes, read or modes, read or modes)), **kwargs)
    variables = dict(say["vars"], hostvars=hostvars, ansible_play_hosts_all=list(hostvars), cassandra_version=version,
                     cassandra_upgrade_phase=phase, cassandra_package_version="5.0.9", ansible_check_mode=check)
    return text(say["ansible.builtin.debug"]["msg"], **variables)


def test_rolling_says_the_next_mode_step_from_the_inventory_a_re_run_too():
    # a rolling phase run again once every node runs 5.0 (cassandra_upgrade_from_series 50x by then): the same step
    assert "cassandra_upgrade_from_series" not in str(task("Summary", "Say what the next phase is"))
    assert next_phase("rolling", modes=["CASSANDRA_4"] * 3) == (
        "Every node runs 5.0.9. Now set cassandra_storage_compatibility_mode: UPGRADING in the inventory and run "
        "apply_config, then NONE and apply_config again (NONE is the point of no return). "
        "Then -e cassandra_upgrade_phase=sstables.")
    assert next_phase("rolling", modes=["UPGRADING"] * 3) == (
        "Every node runs 5.0.9. Now set cassandra_storage_compatibility_mode: NONE in the inventory and run "
        "apply_config (NONE is the point of no return). Then -e cassandra_upgrade_phase=sstables.")
    assert next_phase("rolling", modes=["NONE", None]) == "Every node runs 5.0.9. Then -e cassandra_upgrade_phase=sstables."
    assert next_phase("rolling", modes=["CASSANDRA_4"], version="41x") == (
        "Every node runs 5.0.9. Then -e cassandra_upgrade_phase=sstables.")
    # the inventory at NONE, a node still reading UPGRADING (apply_config not run): the step to NONE again
    assert next_phase("rolling", modes=["NONE", "NONE"], read=["UPGRADING", "NONE"]) == (
        "Every node runs 5.0.9. Now set cassandra_storage_compatibility_mode: NONE in the inventory and run "
        "apply_config (NONE is the point of no return). Then -e cassandra_upgrade_phase=sstables.")
    read = task("Summary", "Read storage_compatibility_mode on the nodes")
    assert read["when"] == ["cassandra_upgrade_phase == 'rolling'", "cassandra_version == '50x'", "not ansible_check_mode"]


def test_check_mode_says_what_a_real_run_would_do():
    assert next_phase("canary", check=True).startswith("--check: nothing was done. A real run upgrades the canary;")
    assert next_phase("rolling", check=True, modes=["CASSANDRA_4"]).startswith(
        "--check: nothing was done. A real run upgrades the nodes listed above. Now set cassandra_storage_compatibility_mode: UPGRADING")
    assert next_phase("sstables", check=True) == (
        "--check: nothing was done. A real run rewrites the sstables of the nodes listed above. "
        "Then -e cassandra_upgrade_phase=cleanup.")
    assert next_phase("sstables") == "sstables rewritten. Last: -e cassandra_upgrade_phase=cleanup."
    for phase in ("canary", "rolling", "sstables"):
        assert "nothing was done" in next_phase(phase, check=True) and "nothing was done" not in next_phase(phase)


def test_cleanup_suggests_no_way_back():
    say = task("Cleanup", "Say what is left to do")
    variables = dict(say["vars"], cassandra_conf_dir="/etc/cassandra/conf")
    real = text(say["ansible.builtin.debug"]["msg"], ansible_check_mode=False, **variables)
    assert "roll back" not in real
    assert real == ("Upgrade over. Resume backups and repairs. The config copies (/etc/cassandra/conf.pre-upgrade-*) "
                    "are kept, for reference: with the pre-upgrade snapshots removed they are no way back to the former "
                    "version. Remove them when you no longer need them.")
    assert text(say["ansible.builtin.debug"]["msg"], ansible_check_mode=True, **variables) == (
        "--check: nothing was removed. A real run removes the pre-upgrade snapshots listed above.")


REFUSED = "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused (Connection refused)'."


def test_a_down_node_with_the_target_package_is_pointed_to_apply_config():
    # a rolling phase interrupted after the node's package upgrade: systemctl start fails on the former config
    read = task("Upgrade checks", "Read the installed Cassandra package where nodetool failed")
    assert read["check_mode"] is False and read["failed_when"] is False
    assert read["when"] == "cassandra_upgrade_running.rc | default(0) != 0"
    order = names("Upgrade checks")
    assert order.index("Read the installed Cassandra package where nodetool failed") < order.index("Every node answers nodetool")
    check = task("Upgrade checks", "Every node answers nodetool")

    def node(name, rc, installed=None):
        h = {"inventory_hostname": name, "cassandra_package_version": "5.0.9",
             "cassandra_upgrade_running": {"rc": rc, "stdout": "ReleaseVersion: 4.1.12" if rc == 0 else "",
                                           "stderr": "" if rc == 0 else REFUSED + "\n"}}
        if installed is not None:
            h["cassandra_upgrade_installed"] = {"rc": 0, "stdout": installed}
        return h

    run = {"inventory": ["/inv/a.yml"], "default_inventory": "", "hosts": "staging_b", "cwd": "/inv"}
    hostvars = {"n1": node("n1", 0), "n2": node("n2", 1, "installed 5.0.9"), "n3": node("n3", 1, "installed 4.1.12"),
                "n4": node("n4", 1, "config-files 5.0.9")}  # dpkg: removed, its config kept: not installed
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_package_version="5.0.9",
                     _phase="rolling", _cassandra_run_command_args=run)
    assert render(check["vars"]["_on_target"], **variables) == ["n2"]
    assert render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n") == [
        "nodetool version failed on n2, n3, n4. Nothing was done.",
        "  n2  package 5.0.9  " + REFUSED,
        "  n3  package 4.1.12  " + REFUSED,
        "  n4  package not installed  " + REFUSED,
        "WAY OUT  on n2 (the target installed): write its config, ansible-playbook -i a.yml community.cassandra.apply_config"
        " -e cassandra_hosts=staging_b --limit n2, then start Cassandra there (systemctl start cassandra)",
        "  on n3, n4: start Cassandra there (systemctl start cassandra; apply_config --limit <node> starts a node whose"
        " unit failed)",
        "  then run this phase again (a node already upgraded is skipped):",
        "  ansible-playbook -i a.yml community.cassandra.upgrade -e cassandra_hosts=staging_b -e cassandra_upgrade_phase=rolling"]
    assert check["vars"]["cassandra_output"] is True
    # a node already upgraded is skipped by the canary and rolling phases only
    variables["_phase"] = "sstables"
    assert "  then run this phase again:" in render(check["ansible.builtin.assert"]["fail_msg"], **variables).split("\n")
    variables["_phase"] = "rolling"
    del hostvars["n3"], hostvars["n4"]
    variables["ansible_play_hosts"] = list(hostvars)
    hostvars["n2"]["cassandra_upgrade_installed"]["stdout"] = "installed 5.0.9-1"  # a Debian revision
    msg = render(check["ansible.builtin.assert"]["fail_msg"], **variables)
    assert "WAY OUT  on n2 (the target installed)" in msg and "on n3" not in msg
    assert read["ansible.builtin.command"]["argv"].count("installed %{VERSION}") == 1
    assert "${db:Status-Status} ${Version}" in read["ansible.builtin.command"]["argv"]


def test_apply_config_says_none_is_the_point_of_no_return():
    with open(os.path.join(TOP, "playbooks", "apply_config.yml"), encoding="utf-8") as f:
        content = f.read()
    assert ("cassandra_apply_config_no_return: \"{{ _nodes | community.cassandra.cassandra_apply_config_no_return }}\""
            in content)
    plays = yaml.safe_load(content)
    screen = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Show the plan and confirm the run")
    warnings = screen["vars"]["cassandra_screen"]["warnings"]

    def said(no_return):
        out = render(warnings, _nodes=[], cassandra_apply_config_no_return=no_return, ansible_check_mode=False,
                     _cassandra_apply_config_refused=[])
        return [w for w in out if w["text"]]
    assert said(["n1", "n2"]) == [{"label": "n1, n2", "text": (
        "storage_compatibility_mode moves to NONE: the point of no return. Once a node runs with it, it cannot go back"
        " to UPGRADING or CASSANDRA_4, nor to its former Cassandra version.")}]
    assert said([]) == []


def test_the_nodes_outside_the_canary_are_said_so():
    # a canary run that stops: the other nodes are left to the rolling phase, not "left out by --limit"
    from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import cassandra_stop_block
    note = task("Pick the nodes and their order", "Note the nodes the canary phase leaves to the rolling phase")
    assert note["when"] == ["cassandra_upgrade_phase == 'canary'", "inventory_hostname != _canary"]
    result = note["ansible.builtin.set_fact"]["cassandra_op_result"]
    lines = cassandra_stop_block([{"node": "n1", "result": result}, {"node": "n2", "result": "upgrade FAILED after 5s: x"},
                                  {"node": "n3", "result": result}], "n2", "boom", operation="upgrade", resume=False)
    assert "n1, n3 not touched (left to the rolling phase)" in [" ".join(x.split()) for x in lines]
    assert not any("--limit" in x for x in lines)


def test_the_summary_play_reads_with_the_role_defaults_and_lists_every_node_of_the_run():
    order = names("Summary")
    assert order.index("Load the role defaults") < order.index("Print the summary") \
        < order.index("Read storage_compatibility_mode on the nodes")
    load = task("Summary", "Load the role defaults")
    assert "cassandra_config" in load["loop"] and load["ansible.builtin.include_role"]["public"] is True
    summary = task("Summary", "Print the summary")
    # the canary's play holds the canary alone: the other nodes of the run (left to the rolling phase) listed too
    assert summary["vars"]["cassandra_service_summary_hosts"] == "{{ cassandra_upgrade_run_nodes | default(ansible_play_hosts_all) }}"
    # every play that reads the nodes' storage_compatibility_mode has the role defaults (cassandra_conf_dir)
    for play in ("Upgrade checks", "Summary"):
        assert "Load the role defaults" in names(play)


def test_prepare_says_where_it_did_not_go_through():
    say = task("Prepare", "Say what the next phase is")["ansible.builtin.debug"]["msg"]
    variables = dict(cassandra_upgrade_from="4.1.12", cassandra_conf_dir="/etc/cassandra/conf", ansible_check_mode=False)
    assert text(say, ansible_play_hosts=["n1", "n3"], ansible_play_hosts_all=["n1", "n2", "n3"], **variables) == (
        "Prepare did not go through on n2 (above): run the prepare phase again once that is fixed, before the canary"
        " phase.")
    assert text(say, ansible_play_hosts=["n1"], ansible_play_hosts_all=["n1"], **variables).startswith("Snapshots")
