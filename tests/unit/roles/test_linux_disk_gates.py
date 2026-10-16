from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_linux's disk tuning on a running node: the tuned profile switch
# (it applies the profiles again, live) waits for a run with Cassandra stopped,
# like lvchange; and the directories it tunes are the ones cassandra_config
# writes (data_file_directories, by default [data_dir]).

import os

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

DISKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_linux", "tasks", "disks.yml")
DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_linux", "defaults", "main.yml")


def tasks():
    with open(DISKS) as f:
        out = []
        todo = list(yaml.safe_load(f))
        while todo:
            t = todo.pop(0)
            out.append(t)
            todo[0:0] = t.get("block", [])
        return out


def task(name):
    return next(t for t in tasks() if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_switch_off_by_default_under_a_running_cassandra():
    with open(DEFAULTS) as f:
        assert yaml.safe_load(f)["cassandra_linux_tuned_switch_running"] is False
    switch = task("Add it to the profiles tuned runs")
    assert "not _cassandra_disk_tuned_deferred | bool" in switch["when"]
    deferred = task("Pick the tuned profile keeping the Cassandra devices out")["ansible.builtin.set_fact"][
        "_cassandra_disk_tuned_deferred"]
    for running, asked, expected in ((True, False, True), (False, False, False), (True, True, False)):
        assert render(deferred, _cassandra_data_running=running, cassandra_linux_tuned_switch_running=asked) == expected


@pytest.mark.parametrize("lvchange_running, tuned_running, probed", [
    (False, False, True), (True, False, True), (False, True, True), (True, True, False)])
def test_running_cassandra_looked_for_whenever_a_gate_needs_it(lvchange_running, tuned_running, probed):
    when = task("Look for a running Cassandra")["when"]
    variables = dict(cassandra_data_disk_tuning=True, cassandra_data_lvchange_running=lvchange_running,
                     cassandra_linux_tuned_switch_running=tuned_running)
    assert all(render("{{ %s }}" % w, **variables) for w in when) == probed


@pytest.mark.parametrize("asked, running, now", [(False, "running", False), (False, "", True), (True, "running", True)])
def test_lvchange_waits_for_a_stopped_cassandra_unless_asked(asked, running, now):
    facts = task("Decide whether lvchange runs now (not under a running Cassandra)")["ansible.builtin.set_fact"]
    variables = dict(_cassandra_data_lvm_here=True, cassandra_data_lvchange_running=asked,
                     cassandra_data_running={"stdout": running})
    assert render(facts["_cassandra_data_lvchange_now"], **variables) is now
    assert render(facts["_cassandra_data_running"], **variables) is (running == "running")


def test_deferred_switch_said_with_its_command():
    keep = task("Keep how the data disks are tuned")
    variables = dict(_cassandra_disk_tuned={"name": "cassandra-disks", "active": ["virtual-guest"],
                                            "profiles": ["virtual-guest", "cassandra-disks"]},
                     _cassandra_disk_tuned_deferred=True, cassandra_data_tuned_file={"changed": True},
                     cassandra_data_tuned_switch={"skipped": True})
    assert render(keep["vars"]["_tuned_deferred"], **variables) is True
    assert render(keep["vars"]["_tuned_failed"], **variables) is False
    warnings = " ".join(keep["ansible.builtin.set_fact"]["_cassandra_disk_summary"]["warnings"].split())
    assert "'To do it: tuned-adm profile ' ~ _cassandra_disk_tuned.profiles | join(' ')" in warnings
    assert "if _tuned_deferred | bool and not _cassandra_disk_tuned_window | default(false) | bool" in warnings


def test_switch_timed_out_is_a_failure():
    keep = task("Keep how the data disks are tuned")
    variables = dict(_cassandra_disk_tuned={"name": "cassandra-disks"},
                     cassandra_data_tuned_switch={"failed": True, "msg": "timed out"})
    assert render(keep["vars"]["_tuned_failed"], **variables) is True


SUMMED = dict(
    inventory_hostname="node1", _cassandra_readahead_read_back=4, _cassandra_disk_items=[],
    _cassandra_disk_plan={"brief": ["/data/c/{data,commitlog} \u2192 vg-data \u2192 sdb"], "stacks": ["full"],
                          "warnings": [], "info": [], "notes": []},
    _cassandra_disk_lvm_failed=[], _cassandra_disk_tuned={}, _cassandra_disk_tuned_deferred=False,
    _cassandra_disk_changes_seen=[], _cassandra_disk_devices=[], _cassandra_data_lvchange_now=True,
    _cassandra_data_lvm_here=True, _cassandra_linux_check=False, cassandra_data_running={"stdout": ""},
    _cassandra_disk_lvm_window=[], _cassandra_disk_refused=[], cassandra_linux_tuned_switch_restart=True,
    cassandra_data_lvchange_restart=True,
    _tuned_warn="")  # (the collection's filter: not loaded here)


def summed(**changes):
    keep = task("Keep how the data disks are tuned")
    variables = dict(SUMMED, **changes)
    variables.update((k, render(v, **variables)) for k, v in keep["vars"].items() if k not in variables)
    return dict((k, render(v, **variables) if isinstance(v, str) else v)
                for k, v in keep["ansible.builtin.set_fact"]["_cassandra_disk_summary"].items())


def test_summary_of_a_node_with_nothing_to_say():
    s = summed()
    assert s["name"] == "node1" and s["kb"] == 4 and s["changed"] is False
    assert s["warnings"] == s["info"] == s["notes"] == []


def test_summary_of_a_node_says_the_problems_and_the_restart():
    failed = [{"kname": "dm-2", "lv": "vg/data"}]
    changes = [{"kname": "dm-2", "what": "ra"}]
    s = summed(_cassandra_disk_lvm_failed=failed, _cassandra_disk_changes_seen=changes, _cassandra_disk_items=[{"item": "x"}],
               cassandra_data_running={"stdout": "running"})
    assert s["changed"] is True
    assert s["warnings"] == ["lvchange failed on vg/data: its read-ahead is set now, kept by the udev rule"]
    assert s["info"] == ["optional: restart Cassandra to read the files it has open with the new read-ahead"]
    # not running (a new node), or under --check: no restart to ask for
    assert summed(_cassandra_disk_changes_seen=changes)["info"] == []
    assert summed(_cassandra_disk_changes_seen=changes, _cassandra_linux_check=True,
                  cassandra_data_running={"stdout": "running"})["info"] == []


def test_summary_of_a_node_with_lvm_kept_by_udev_under_a_running_cassandra():
    lv = [{"kname": "dm-2", "kind": "lvm", "os": False, "lv_read_ahead": "auto", "lv": "vg/data"}]
    s = summed(_cassandra_disk_devices=lv, _cassandra_data_lvchange_now=False)
    assert s["info"] == [u"the read-ahead of the LVM volumes is kept by the udev rule for now; LVM itself stores it"
                         u" at the next run with Cassandra stopped (apply_config: in the node's restart window) \u2014"
                         u" nothing to do"]
    s = summed(_cassandra_disk_devices=lv, _cassandra_data_lvchange_now=False, _cassandra_data_lvm_here=False)
    assert s["info"] == [] and "LVM may set them back" in s["notes"][0]


@pytest.mark.parametrize("variables, dirs", [
    ({"cassandra_data_dir": "/data/cassandra"}, ["/data/cassandra"]),
    # data_file_directories set: data_dir is not a data directory (cassandra_config's default only)
    ({"cassandra_data_file_directories": ["/d1/data", "/d2/data"], "cassandra_data_dir": "/var/lib/cassandra/data",
      "cassandra_commitlog_dir": "/c/commitlog"}, ["/d1/data", "/d2/data", "/c/commitlog"]),
    ({"cassandra_data_file_directories": "/d1/data", "cassandra_data_dir": "/var/lib/cassandra/data"}, ["/d1/data"]),
    ({}, []),
])
def test_tuned_directories_are_the_data_directories(variables, dirs):
    find = task("Find the filesystem of each Cassandra directory")
    assert render(find["vars"]["_tuned"], **variables) == dirs


def test_probe_skipped_is_not_running():
    # both opt-ins: no probe, its result skipped (no stdout)
    facts = task("Decide whether lvchange runs now (not under a running Cassandra)")["ansible.builtin.set_fact"]
    variables = dict(_cassandra_data_lvm_here=True, cassandra_data_lvchange_running=True,
                     cassandra_data_running={"skipped": True, "changed": False})
    assert render(facts["_cassandra_data_running"], **variables) is False
    assert render(facts["_cassandra_data_lvchange_now"], **variables) is True


@pytest.mark.parametrize("deferred, check_mode, switch, listed", [
    (True, False, {"skipped": True}, False),
    (False, False, {"failed": True, "msg": "timed out"}, False),
    (False, False, {"rc": 0, "changed": True}, True),
    (False, True, {"skipped": True}, True),
])
def test_profile_change_listed_only_when_done(deferred, check_mode, switch, listed):
    items = task("Compare the disk tuning")["ansible.builtin.set_fact"]["_cassandra_disk_items"]
    out = render(items, _cassandra_disk_changes_seen=[], _cassandra_disk_lvm_failed=[], _cassandra_disk_lvm_window=[],
                 _cassandra_disk_tuned_reload=False,
                 _cassandra_disk_tuned={"name": "cassandra-disks", "active": ["virtual-guest"],
                                        "profiles": ["virtual-guest", "cassandra-disks"]},
                 _cassandra_disk_tuned_deferred=deferred, _cassandra_linux_check=check_mode,
                 cassandra_data_tuned_switch=switch, cassandra_data_disk_udev={"changed": False})
    assert ("tuned profiles" in [i["item"] for i in out]) is listed


TUNED = {"name": "cassandra-disks", "active": ["virtual-guest"], "profiles": ["virtual-guest", "cassandra-disks"],
         "unmatched": []}


def test_summary_of_a_node_says_the_tuned_problems():
    deferred = summed(_cassandra_disk_tuned=TUNED, _cassandra_disk_tuned_deferred=True,
                      cassandra_data_tuned_file={"changed": True}, cassandra_data_tuned_switch={"skipped": True})
    assert deferred["warnings"] == [
        "tuned profile switch to virtual-guest cassandra-disks not done under a running Cassandra: it applies all of"
        " tuned's settings again, live. Until then, tuned sets the read-ahead of the Cassandra devices back at each"
        " boot. To do it: tuned-adm profile virtual-guest cassandra-disks on the node at a quiet time, or apply_config:"
        " it does it in the node's restart window."]
    updated = summed(_cassandra_disk_tuned=dict(TUNED, active=TUNED["profiles"]), _cassandra_disk_tuned_deferred=True,
                     cassandra_data_tuned_file={"changed": True}, cassandra_data_tuned_switch={"skipped": True})
    assert updated["warnings"] == [
        "tuned profile cassandra-disks updated, its reload not done under a running Cassandra: it applies all of"
        " tuned's settings again, live. Until then, tuned runs the profile it loaded (the new one from the next"
        " boot). To do it: tuned-adm profile virtual-guest cassandra-disks on the node at a quiet time, or apply_config:"
        " it does it in the node's restart window."]
    # its reload left for later by an earlier run (the profile written then): said again
    again = summed(_cassandra_disk_tuned=dict(TUNED, active=TUNED["profiles"]), _cassandra_disk_tuned_deferred=True,
                   _cassandra_disk_tuned_reload=True, cassandra_data_tuned_file={"changed": False},
                   cassandra_data_tuned_switch={"skipped": True})
    assert again["warnings"] == updated["warnings"]
    failed = summed(_cassandra_disk_tuned=TUNED, cassandra_data_tuned_switch={"rc": 1, "stderr": "no such profile\nmore"})
    assert failed["warnings"] == ["tuned-adm profile virtual-guest cassandra-disks failed: no such profile"]
    ok = summed(_cassandra_disk_tuned=dict(TUNED, unmatched=["sdb"]), cassandra_data_tuned_switch={"rc": 0})
    assert ok["warnings"] == ["tuned: no stable id for sdb: its profile still sets them"]
    assert ok["notes"] == ["tuned: runs virtual-guest cassandra-disks (cassandra-disks: nothing set on the Cassandra devices)"]
    # the conflict warning of the filter: one WARNING prefix, the summary's
    conflict = summed(_tuned_warn="WARNING - the active tuned profile (virtual-guest: [disk] readahead=>4096) sets sdb")
    assert conflict["warnings"] == ["the active tuned profile (virtual-guest: [disk] readahead=>4096) sets sdb"]


MAIN = os.path.join(os.path.dirname(DISKS), "main.yml")


def test_the_summary_reads_the_nodes_that_tuned_their_disks():
    with open(MAIN) as f:
        say = next(t for t in yaml.safe_load(f) if t.get("name") == "Say how the data disks are tuned")
    assert say["run_once"] is True and "when" not in [k for k in say if k == "when" and "manage" in str(say[k])]
    hostvars = {"node1": {"_cassandra_disk_summary": {"name": "node1"}}, "node2": {"_cassandra_disk_summary": {}},
                "node3": {}}
    nodes = render(say["vars"]["_nodes"], ansible_play_batch=["node1", "node2", "node3"], hostvars=hostvars)
    assert nodes == [{"name": "node1"}]
    when = "{{ (%s) }}" % ") and (".join(say["when"])
    assert render(when, _nodes=nodes) is True
    assert render(when, _nodes=[]) is False
    # apply_config's compare: in its view of the settings that differ instead
    assert render(when, _nodes=nodes, _cassandra_linux_quiet=True) is False
    # in a prepare of new nodes: said after its step lines (cassandra_service prepare_lines.yml)
    assert render(when, _nodes=nodes, _cassandra_prepare_lines=True) is False


# apply_config (_cassandra_linux_restart_window): what is not done under a running Cassandra goes to the node's
# restart window, compared with its reason; cassandra_linux_tuned_switch_restart / cassandra_data_lvchange_restart
# false: left for later, said

def test_the_restart_opt_ins_on_by_default():
    with open(DEFAULTS) as f:
        defaults = yaml.safe_load(f)
    assert defaults["cassandra_linux_tuned_switch_restart"] is True and defaults["cassandra_data_lvchange_restart"] is True


@pytest.mark.parametrize("deferred, window, restart, refused, expected", [
    (True, True, True, [], True), (True, False, True, [], False), (True, True, False, [], False),
    (False, True, True, [], False),
    # refused by tuned-adm in a restart window of this boot: not again
    (True, True, True, ["tuned virtual-guest cassandra-disks"], False), (True, True, True, ["lvm vg/data"], True)])
def test_the_tuned_switch_left_to_the_restart_window(deferred, window, restart, refused, expected):
    facts = task("Decide whether the tuned switch goes to the restart window")["ansible.builtin.set_fact"]
    variables = dict(_cassandra_disk_tuned=TUNED, _cassandra_disk_tuned_deferred=deferred,
                     cassandra_linux_tuned_switch_restart=restart, _cassandra_disk_refused=refused)
    if window:
        variables["_cassandra_linux_restart_window"] = True
    assert render(facts["_cassandra_disk_tuned_window"], **variables) is expected
    # no profile of the role's: nothing to switch
    variables["_cassandra_disk_tuned"] = {}
    assert render(facts["_cassandra_disk_tuned_window"], **variables) is False
    # (reset with the other facts of the run: an earlier run's would stay)
    reset = task("Keep the devices found under sysfs, and the read-ahead the kernel reads back")["ansible.builtin.set_fact"]
    assert reset["_cassandra_disk_tuned_window"] is False and reset["_cassandra_disk_tuned_reload"] is False


def test_a_reload_left_for_later_found_again():
    # the role's profile changed since tuned loaded it (the filter tells: cassandra_disk_tuned_profile's reload)
    fact = task("Decide whether the tuned switch goes to the restart window")["ansible.builtin.set_fact"][
        "_cassandra_disk_tuned_reload"]
    for profile, reload in ((dict(TUNED, reload=True), True), (dict(TUNED, reload=False), False), ({}, False)):
        assert render(fact, _cassandra_disk_tuned=profile) is reload
        switch = task("Add it to the profiles tuned runs")["when"][0]
        assert render("{{ %s }}" % switch, _cassandra_disk_tuned=dict(TUNED, active=TUNED["profiles"]),
                      cassandra_data_tuned_file={"changed": False}, _cassandra_disk_tuned_reload=reload) is reload


def test_the_times_tuned_loaded_and_the_profile_changed_read(tmp_path):
    import subprocess
    script = task("Read the active tuned profile")["ansible.builtin.shell"]
    part = script[script.index("echo \"now|"):]
    etc = tmp_path / "etc"
    (etc / "cassandra-disks").mkdir(parents=True)
    (etc / "active_profile").write_text("virtual-guest cassandra-disks\n")
    (etc / "cassandra-disks" / "tuned.conf").write_text("[main]\n")
    os.utime(str(etc / "active_profile"), (1000, 1000))
    os.utime(str(etc / "cassandra-disks" / "tuned.conf"), (2000, 2000))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 2500\n")  # the boot, in seconds
    part = "dir=%s\n" % etc + part.replace("/etc/tuned/", str(etc) + "/").replace("/proc/stat", str(tmp_path / "stat"))

    def run(started):
        (bin_dir / "systemctl").write_text("#!/bin/sh\necho '%s'\n" % started)  # its start, in us since the boot
        (bin_dir / "systemctl").chmod(0o755)
        return subprocess.run(["bash", "-c", part], env={"PATH": "%s:/usr/bin:/bin" % bin_dir},
                              capture_output=True, text=True, check=False).stdout.split()

    for never in ("", "0"):  # never active since boot: no time
        out = run(never)
        assert out[0].startswith("now|") and out[1:] == ["loaded|1000", "file|2000"]
    # tuned started after the last switch: its start is when it loaded them
    assert run("500000000")[1:] == ["loaded|3000", "file|2000"]
    # (the profile where the role writes it, the dir it found)
    (etc / "cassandra-disks" / "tuned.conf").unlink()
    assert run("")[1:] == ["loaded|1000"]


@pytest.mark.parametrize("lvm_here, now, window, restart, expected", [
    (True, False, True, True, ["dm-2"]), (True, True, True, True, []), (False, False, True, True, []),
    (True, False, False, True, []), (True, False, True, False, [])])
def test_lvchange_left_to_the_restart_window(lvm_here, now, window, restart, expected):
    facts = task("Note the LVM volumes left to the restart window")["ansible.builtin.set_fact"]
    devices = [{"kname": "dm-2", "kind": "lvm", "os": False, "lv_read_ahead": "auto", "lv": "vg/data"},
               {"kname": "dm-3", "kind": "lvm", "os": False, "lv_read_ahead": "8", "lv": "vg/done"},
               {"kname": "dm-4", "kind": "lvm", "os": True, "lv_read_ahead": "auto", "lv": "rhel/root"},
               {"kname": "sdb", "kind": "disk", "os": False}]
    variables = dict(_cassandra_disk_devices=devices, _cassandra_readahead_read_back=4, _cassandra_data_lvm_here=lvm_here,
                     _cassandra_data_lvchange_now=now, cassandra_data_lvchange_restart=restart, _cassandra_disk_refused=[])
    if window:
        variables["_cassandra_linux_restart_window"] = True
    assert [d["kname"] for d in render(facts["_cassandra_disk_lvm_window"], **variables)] == expected


def _items(**variables):
    items = task("Compare the disk tuning")["ansible.builtin.set_fact"]["_cassandra_disk_items"]
    base = dict(_cassandra_disk_changes_seen=[], _cassandra_disk_lvm_failed=[], _cassandra_disk_lvm_window=[],
                _cassandra_disk_tuned=TUNED, _cassandra_disk_tuned_deferred=True, _cassandra_disk_tuned_window=True,
                _cassandra_linux_check=True, _cassandra_disk_tuned_dir="/etc/tuned",
                cassandra_data_tuned_switch={"skipped": True}, cassandra_data_disk_udev={"changed": False},
                _cassandra_readahead_read_back=4, _cassandra_disk_tuned_reload=False)
    base.update(variables)
    return render(items, **base)


def test_compared_with_the_reason_of_the_restart():
    out = _items(cassandra_data_tuned_file={"changed": True},
                 _cassandra_disk_lvm_window=[{"lv": "vg/data", "lv_read_ahead": "auto"}])
    # (the profile itself written live: tuned reads it at the switch)
    assert out == [{"item": "/etc/tuned/cassandra-disks/tuned.conf", "before": "absent or different", "after": "present"},
                   {"item": "tuned profiles", "before": "virtual-guest", "after": "virtual-guest cassandra-disks",
                    "restart": "tuned profile"},
                   {"item": "LVM read-ahead vg/data (sectors)", "before": "auto", "after": "8", "restart": "LVM read-ahead"}]
    # 64 KB pages: 4 KB reads back as 0, LVM's "none" (cassandra_disk_lv_read_ahead)
    zero = _items(_cassandra_readahead_read_back=0, _cassandra_disk_lvm_window=[{"lv": "vg/data", "lv_read_ahead": "auto"}])
    assert zero[-1]["after"] == "none"
    # the real run, Cassandra running: not done, not reported (the window reports them)
    assert _items(_cassandra_linux_check=False, cassandra_data_tuned_file={"changed": False},
                  _cassandra_disk_lvm_window=[{"lv": "vg/data", "lv_read_ahead": "auto"}]) == []
    # a reload: the profile changed now, or since tuned loaded it (an earlier run left it for later)
    loaded = dict(TUNED, active=TUNED["profiles"])
    reload = {"item": "tuned profile cassandra-disks", "before": "loaded before its change", "after": "loaded again",
              "restart": "tuned profile"}
    assert _items(_cassandra_disk_tuned=loaded, cassandra_data_tuned_file={"changed": True})[1:] == [reload]
    assert _items(_cassandra_disk_tuned=loaded, cassandra_data_tuned_file={"changed": False},
                  _cassandra_disk_tuned_reload=True) == [reload]
    assert _items(_cassandra_disk_tuned=loaded, cassandra_data_tuned_file={"changed": False}) == []
    # ... done with Cassandra stopped (the window, a resumed run): said as done
    assert _items(_cassandra_disk_tuned=loaded, cassandra_data_tuned_file={"changed": False}, _cassandra_disk_tuned_reload=True,
                  _cassandra_linux_check=False, _cassandra_disk_tuned_deferred=False, _cassandra_disk_tuned_window=False,
                  cassandra_data_tuned_switch={"rc": 0}) == [dict((k, v) for k, v in reload.items() if k != "restart")]
    # the opt-out under --check: deferred, not listed (the warning says it)
    assert _items(_cassandra_disk_tuned_window=False) == []
    # the window itself (Cassandra stopped): done, listed as any change
    done = _items(_cassandra_linux_check=False, _cassandra_disk_tuned_deferred=False, _cassandra_disk_tuned_window=False,
                  cassandra_data_tuned_switch={"rc": 0, "changed": True})
    assert done == [{"item": "tuned profiles", "before": "virtual-guest", "after": "virtual-guest cassandra-disks"}]


def test_no_warning_for_what_the_window_does():
    window = summed(_cassandra_disk_tuned=TUNED, _cassandra_disk_tuned_deferred=True, _cassandra_disk_tuned_window=True,
                    cassandra_data_tuned_file={"changed": True}, cassandra_data_tuned_switch={"skipped": True})
    assert window["warnings"] == []
    # the opt-out under apply_config: said, the command alone
    out = summed(_cassandra_disk_tuned=TUNED, _cassandra_disk_tuned_deferred=True, _cassandra_disk_tuned_window=False,
                 cassandra_linux_tuned_switch_restart=False, cassandra_data_tuned_file={"changed": True},
                 cassandra_data_tuned_switch={"skipped": True})
    assert out["warnings"][0].endswith("To do it: tuned-adm profile virtual-guest cassandra-disks on the node at a quiet"
                                       " time (cassandra_linux_tuned_switch_restart is false).")
    # LVM left to the window: no "kept by the udev rule for now"
    lv = [{"kname": "dm-2", "kind": "lvm", "os": False, "lv_read_ahead": "auto", "lv": "vg/data"}]
    s = summed(_cassandra_disk_devices=lv, _cassandra_data_lvchange_now=False, _cassandra_disk_lvm_window=lv)
    assert s["info"] == [] and s["warnings"] == []


@pytest.mark.parametrize("live, root, stand_ins, here", [
    (True, "/sys/block", False, True), (False, "/sys/block", False, False), (False, "/tmp/fake-sys/block", False, False),
    (False, "/tmp/fake-sys/block", True, True),
    # stand-ins never on the real devices
    (False, "/sys/block", True, False)])
def test_tuned_and_lvm_driven_on_real_devices_live_or_on_test_stand_ins(live, root, stand_ins, here):
    facts = task("Decide whether the disks are tuned here")["ansible.builtin.set_fact"]
    variables = dict(_cassandra_linux_live=live, cassandra_sysfs_block_root=root)
    if stand_ins:
        variables["_cassandra_linux_stand_ins"] = True
    assert render(facts["_cassandra_data_tuned_lvm_here"], **variables) is here
    assert "_cassandra_data_tuned_lvm_here | bool" in task("Decide whether LVM keeps the read-ahead of its volumes")[
        "ansible.builtin.set_fact"]["_cassandra_data_lvm_here"]
    assert "_cassandra_data_tuned_lvm_here | bool" in task("Pick the tuned profile keeping the Cassandra devices out")[
        "ansible.builtin.set_fact"]["_cassandra_disk_tuned"]


def test_kernel_settings_applied_again_only_live():
    # (stand-ins in a container: its sysctls are the host's)
    assert "_cassandra_linux_live | bool" in task("Apply the kernel settings again")["when"]


def test_what_a_window_refused_said_not_restarted_for_again():
    # tuned-adm failed in a restart window of this boot: the WARNING again, its cause said
    out = summed(_cassandra_disk_tuned=TUNED, _cassandra_disk_tuned_deferred=True, _cassandra_disk_tuned_window=False,
                 _cassandra_disk_refused=["tuned virtual-guest cassandra-disks"], cassandra_data_tuned_file={"changed": False},
                 cassandra_data_tuned_switch={"skipped": True})
    assert out["warnings"][0].endswith("on the node at a quiet time (tuned-adm failed in its restart window, this boot).")
    # lvchange failed there: said with its command, not the "nothing to do" line
    lv = [{"kname": "dm-2", "kind": "lvm", "os": False, "lv_read_ahead": "auto", "lv": "vg/data"}]
    out = summed(_cassandra_disk_devices=lv, _cassandra_data_lvchange_now=False, _cassandra_disk_refused=["lvm vg/data"])
    assert out["warnings"] == ["lvchange --readahead 8 vg/data failed in its restart window (this boot): its read-ahead kept"
                               " by the udev rule, no restart for it again; once the volume group is fixed, run it on the"
                               " node"]
    assert out["info"] == []


def test_the_lvm_opt_out_said_with_its_command():
    lv = [{"kname": "dm-2", "kind": "lvm", "os": False, "lv_read_ahead": "auto", "lv": "vg/data"}]
    out = summed(_cassandra_disk_devices=lv, _cassandra_data_lvchange_now=False, cassandra_data_lvchange_restart=False)
    assert out["info"] == ["the read-ahead of the LVM volumes is kept by the udev rule for now (cassandra_data_lvchange_restart"
                           " is false); for LVM to keep it: lvchange --readahead 8 vg/data on the node"]
    assert out["warnings"] == []


def test_the_window_notes_what_it_refused():
    note = task("Note what the restart window could not do")
    assert note["ansible.builtin.copy"]["dest"] == "/run/cassandra-apply-config.refused"
    assert note["when"] == "_cassandra_linux_in_window | default(false) | bool"

    def lines(failed, switch):
        return render(note["vars"]["_lines"], _cassandra_disk_lvm_failed=failed, _cassandra_disk_tuned=TUNED,
                      cassandra_data_tuned_switch=switch)

    assert lines([{"lv": "vg/data"}], {"rc": 1}) == ["lvm vg/data", "tuned virtual-guest cassandra-disks"]
    # nothing refused: an empty record (the next runs restart for them again)
    assert lines([], {"rc": 0}) == [] and lines([], {"skipped": True}) == []
    # a tuned-adm that timed out (no rc): refused too
    assert lines([], {"failed": True, "msg": "timed out"}) == ["tuned virtual-guest cassandra-disks"]
    content = note["ansible.builtin.copy"]["content"]
    assert render(content, _lines=["lvm vg/data", "tuned a b"]) == "lvm vg/data\ntuned a b\n"
    # read by every run (the plan and the OS play word it alike)
    read = task("Read what an earlier restart window could not do")
    assert read["ansible.builtin.command"] == "cat /run/cassandra-apply-config.refused"
    assert read["when"] == "cassandra_data_disk_tuning | bool" and read["check_mode"] is False
    lvm = task("Note the LVM volumes left to the restart window")["ansible.builtin.set_fact"]["_cassandra_disk_lvm_window"]
    devices = [{"kname": "dm-2", "kind": "lvm", "os": False, "lv_read_ahead": "auto", "lv": "vg/data"},
               {"kname": "dm-3", "kind": "lvm", "os": False, "lv_read_ahead": "auto", "lv": "vg/other"}]
    left = render(lvm, _cassandra_disk_devices=devices, _cassandra_readahead_read_back=4, _cassandra_data_lvm_here=True,
                  _cassandra_data_lvchange_now=False, cassandra_data_lvchange_restart=True,
                  _cassandra_linux_restart_window=True, _cassandra_disk_refused=["lvm vg/data"])
    assert [d["lv"] for d in left] == ["vg/other"]


def test_the_stand_ins_checked_before_any_use(tmp_path):
    import subprocess
    check = task("Check the stand-ins are the tools run (tests)")
    assert check["when"] == "_cassandra_linux_stand_ins | default(false) | bool"
    for where, rc in (("local/sbin", 0), ("sbin", 1)):
        d = tmp_path / where
        d.mkdir(parents=True)
        for c in ("tuned-adm", "lvchange"):
            (d / c).write_text("#!/bin/sh\n")
            (d / c).chmod(0o755)
        script = check["ansible.builtin.shell"].replace("/usr/local/*", str(tmp_path / "local") + "/*")
        assert subprocess.run(["bash", "-c", script], env={"PATH": "%s:/usr/bin:/bin" % d}, capture_output=True,
                              check=False).returncode == rc, where
    names = [t.get("name") for t in tasks()]
    assert names.index("Check the stand-ins are the tools run (tests)") < names.index("Read the LVM volumes") \
        < names.index("Read the active tuned profile")


def test_an_lvchange_that_times_out_is_taken_as_refused():
    lvchange = task("Set the read-ahead of the LVM volumes (kept in their metadata)")
    assert lvchange["timeout"] == 120 and lvchange["ignore_errors"] is True
    failed = task("Write down the udev rule lines")["vars"]["_lvm_failed"]
    results = {"results": [{"item": {"lv": "vg/a"}, "rc": 0}, {"item": {"lv": "vg/b"}, "rc": 5},
                           {"item": {"lv": "vg/c"}, "failed": True, "msg": "The task timed out"}]}
    assert render(failed, cassandra_data_lvchange=results) == [{"lv": "vg/b"}, {"lv": "vg/c"}]
    assert render(failed, cassandra_data_lvchange={"skipped": True, "results": []}) == []


def test_the_shell_scripts_split_as_ansible_splits_them():
    # (a free-form argument with an odd quote, e.g. in a comment, fails the play at run time)
    from ansible.parsing.splitter import split_args
    scripts = [t["ansible.builtin.shell"] for t in tasks() if isinstance(t.get("ansible.builtin.shell"), str)]
    assert len(scripts) > 5
    for script in scripts:
        split_args(script)


def test_a_switch_done_dates_the_profile_as_loaded(tmp_path):
    import subprocess
    date = task("Date the profile as loaded")
    # the file the read of tuned stats ($dir/cassandra-disks/tuned.conf), the one the template writes
    assert date["environment"]["F"] == task("The tuned profile")["ansible.builtin.template"]["dest"]
    script = date["ansible.builtin.shell"]
    profile, active = tmp_path / "tuned.conf", tmp_path / "active_profile"
    profile.write_text("x")
    active.write_text("y")
    os.utime(str(active), (1000, 1000))
    subprocess.run(["sh", "-c", script.replace("/etc/tuned/active_profile", str(active))], env={"F": str(profile),
                   "PATH": "/usr/bin:/bin"}, check=True)
    assert int(profile.stat().st_mtime) == 1000
    # no active_profile: dated long ago (never taken for a change since tuned loaded it)
    active.unlink()
    subprocess.run(["sh", "-c", script.replace("/etc/tuned/active_profile", str(active))], env={"F": str(profile),
                   "PATH": "/usr/bin:/bin"}, check=True)
    assert int(profile.stat().st_mtime) == 1
    names = [t.get("name") for t in tasks()]
    assert names.index("Add it to the profiles tuned runs") < names.index("Date the profile as loaded")
    for switch, dated in (({"rc": 0}, True), ({"rc": 1}, False), ({"skipped": True}, False), ({"failed": True}, False)):
        assert render("{{ %s }}" % date["when"], cassandra_data_tuned_switch=switch) is dated


def test_lvchange_killed_on_the_node_at_its_timeout():
    argv = task("Set the read-ahead of the LVM volumes (kept in their metadata)")["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["timeout", "110", "lvchange"]
