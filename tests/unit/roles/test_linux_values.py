from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_linux's values that leave a node's settings as they are (rc10): THP (values, kernel, keep), swap,
# limits, the RedHat nproc line, chrony's servers; their stock defaults unchanged.

import os

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

COLLECTION = os.path.join(os.path.dirname(__file__), "..", "..", "..")
ROLE = os.path.join(COLLECTION, "roles", "cassandra_linux")


def defaults():
    with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
        return yaml.safe_load(f)


def task(name, path):
    todo = load_tasks(os.path.join(ROLE, "tasks", path))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo[0:0] = t.get("block", [])
    raise KeyError(name)


def block(name, path):
    return next(t for t in load_tasks(os.path.join(ROLE, "tasks", path)) if t.get("name") == name)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def when(t, **variables):
    conditions = t["when"] if isinstance(t["when"], list) else [t["when"]]
    return all(render("{{ %s }}" % c, **variables) in (True, "True") for c in conditions)


def test_stock_defaults_unchanged():
    d = defaults()
    assert (d["cassandra_linux_thp_enabled"], d["cassandra_linux_thp_defrag"]) == ("never", "never")
    assert d["cassandra_linux_swap_off"] is True and d["cassandra_linux_nproc_all"] is True
    assert d["cassandra_linux_limits_file"] == "/etc/security/limits.d/cassandra.conf"
    assert d["cassandra_linux_timesync_servers"] == []
    assert (d["cassandra_data_readahead_kb"], d["cassandra_data_scheduler"], d["cassandra_data_lvm_readahead"]) == (4, "none", "")


@pytest.mark.parametrize("enabled, defrag, unit, unset", [
    ("never", "never", {"enabled": "never", "defrag": "never"}, False),
    ("madvise", "kernel", {"enabled": "madvise"}, False),
    ("keep", "always", {"defrag": "always"}, False),
    ("kernel", "kernel", {}, True),  # its unit removed
    ("kernel", "keep", {}, False),  # (keep: nothing touched)
    ("keep", "keep", {}, False)])
def test_thp_unit_values(enabled, defrag, unit, unset):
    t = task("Decide how THP is set", "os.yml")["ansible.builtin.set_fact"]
    v = {"cassandra_linux_thp_enabled": enabled, "cassandra_linux_thp_defrag": defrag}
    assert render(t["_cassandra_linux_thp_set"], **v) == unit
    assert render(t["_cassandra_linux_thp_unset"], **v) in (unset, str(unset))


def test_thp_unit_started_while_a_value_differs():
    t = task("Ensure service to disable THP is started", "os.yml")
    base = {"_cassandra_linux_live": True, "_cassandra_linux_check": False, "thp": {"changed": False}}
    for wanted, before, started in (({"enabled": "never", "defrag": "never"}, ("always", "never"), True),
                                    ({"enabled": "never", "defrag": "never"}, ("never", "madvise"), True),
                                    ({"enabled": "never", "defrag": "never"}, ("never", "never"), False),
                                    ({"defrag": "madvise"}, ("always", "madvise"), False),
                                    ({"enabled": "madvise"}, ("always", "never"), True),
                                    ({"enabled": "never"}, ("", ""), False)):  # no THP there
        v = dict(base, _cassandra_linux_thp_set=wanted,
                 _cassandra_linux_before={"thp|enabled": before[0], "thp|defrag": before[1]})
        assert when(t, **v) is started, (wanted, before)


@pytest.mark.parametrize("name, variable", [
    ("Disable Swap", "cassandra_linux_swap_off"), ("Remove swap partitions", "cassandra_linux_swap_off"),
    ("Extra Limit for RH Derived Distros", "cassandra_linux_nproc_all")])
def test_switches_off_skip_their_task(name, variable):
    t = task(name, "os.yml")
    assert any(variable in str(c) for c in (t["when"] if isinstance(t["when"], list) else [t["when"]]))


def test_limits_file_and_none():
    t = task("Limits of the cassandra user", "limits.yml")
    assert t["ansible.builtin.copy"]["dest"] == "{{ cassandra_linux_limits_file }}"
    assert not when(t, cassandra_linux_limits={})
    assert when(t, cassandra_linux_limits={"nofile": 1})


def test_value_typo_refused_naming_the_values():
    t = task("Check the values of the THP and disk settings", "os.yml")
    item = {"name": "cassandra_linux_thp_enabled", "value": "alwyas", "allowed": ["always", "madvise", "never", "kernel", "keep"]}
    assert render("{{ %s }}" % t["ansible.builtin.assert"]["that"], item=item) is False
    assert " ".join(render(t["ansible.builtin.assert"]["fail_msg"], item=item).split()) == (
        "cassandra_linux_thp_enabled must be always, madvise, never, kernel or keep, not alwyas")
    lvm = {"name": "cassandra_data_lvm_readahead", "value": "x", "allowed": ["", "auto"]}
    assert " ".join(render(t["ansible.builtin.assert"]["fail_msg"], item=lvm).split()) == (
        'cassandra_data_lvm_readahead must be "" or auto, not x')
    ra = task("Check cassandra_data_readahead_kb", "os.yml")["ansible.builtin.assert"]["that"]
    assert [render("{{ %s }}" % ra, cassandra_data_readahead_kb=v) for v in (4, "kernel", "4k")] == [True, True, False]


def chrony(lines, servers):
    """The loops of the chrony tasks: (lines commented out, lines added)."""
    v = {"cassandra_linux_chrony_lines": {"stdout_lines": lines}, "cassandra_linux_timesync_servers": servers}
    v["_wanted"] = render(block("Time servers (chrony)", "timesync.yml")["vars"]["_wanted"], **v)
    comment = task("Comment out the server lines not listed", "timesync.yml")
    add = task("Add the server lines listed", "timesync.yml")
    v1 = dict(v, _found_wanted=render(comment["vars"]["_found_wanted"], **v))
    v2 = dict(v, _found=render(add["vars"]["_found"], **v))
    return render(comment["loop"], **v1), render(add["loop"], **v2)


def test_chrony_servers():
    stock = ["pool 2.rhel.pool.ntp.org iburst"]
    assert chrony(stock, ["server ntp1.example.org iburst"]) == (stock, ["server ntp1.example.org iburst"])
    # the same words, other spaces: nothing to do
    assert chrony(["server  ntp1.example.org   iburst"], ["server ntp1.example.org iburst"]) == ([], [])
    t = task("Add the server lines listed", "timesync.yml")["ansible.builtin.lineinfile"]
    # (a commented-out one is uncommented in its place)
    regexp = render(t["regexp"], item="server ntp1.example.org iburst")
    assert regexp == r"^\s*#?\s*server\s+ntp1\.example\.org\s+iburst\s*$"
    stop = task("Refuse time servers for another time sync than chrony", "timesync.yml")
    assert when(stop, _cassandra_timesync_service="systemd-timesyncd")
    assert not when(stop, _cassandra_timesync_service="chronyd")


def test_service_start_method():
    root = os.path.join(ROLE, "..", "cassandra_service")
    with open(os.path.join(root, "defaults", "main.yml")) as f:
        assert yaml.safe_load(f)["cassandra_service_start"] == "unit"
    with open(os.path.join(root, "vars", "main.yml")) as f:
        own = yaml.safe_load(f)["_cassandra_service_own_unit"]
    for manage, start, written in ((True, "unit", True), (True, "package_script", False), (False, "unit", False)):
        assert render(own, cassandra_service_unit_manage=manage, cassandra_service_start=start) is written


def test_cqlsh_source_and_install_method():
    root = os.path.join(ROLE, "..", "cassandra_install")
    with open(os.path.join(root, "defaults", "main.yml")) as f:
        assert yaml.safe_load(f)["cassandra_cqlsh_source"] == "package"
    with open(os.path.join(root, "vars", "main.yml")) as f:
        managed = yaml.safe_load(f)["_cassandra_cqlsh_managed"]
    for manage, source, done in ((True, "package", True), (True, "external", False), (False, "package", False)):
        assert render(managed, cassandra_cqlsh_python_manage=manage, cassandra_cqlsh_source=source) is done
    check = next(t for t in load_tasks(os.path.join(root, "tasks", "install.yml")) if t.get("name") == "Check cassandra_cqlsh_source")
    a = check["ansible.builtin.assert"]
    ok = dict(cassandra_cqlsh_source="external", cassandra_cqlsh_command="/opt/c/bin/cqlsh")
    assert all(render("{{ %s }}" % c, **ok) for c in a["that"])
    bad = {"cassandra_cqlsh_source": "external", "cassandra_cqlsh_command": "cqlsh"}
    assert not all(render("{{ %s }}" % c, **bad) for c in a["that"])
    assert "set cassandra_cqlsh_command to the full path" in render(a["fail_msg"], **bad)
    # system: the repositories of the node, none added nor removed
    repo = os.path.join(ROLE, "..", "cassandra_repository", "tasks", "main.yml")
    for t in load_tasks(repo):
        if t.get("name") in ("Configure the Cassandra repository", "Remove the Cassandra repositories (packages)"):
            assert not when(t, cassandra_repository_manage=True, cassandra_install_method="system")


# --- rc10 audit round 1 ---

def role_task(role, name, path):
    todo = load_tasks(os.path.join(ROLE, "..", role, "tasks", path))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo[0:0] = t.get("block", [])
    raise KeyError(name)


def test_limits_block_left_with_no_limits():
    t = task("Remove the block earlier versions of the role wrote to limits.conf", "os.yml")
    assert not when(t, cassandra_linux_limits={}) and when(t, cassandra_linux_limits={"nofile": 1})


def test_package_script_refused_without_the_sysv_generator():
    look = role_task("cassandra_service", "Look for the SysV generator (package_script)", "main.yml")
    assert look["check_mode"] is False and when(look, cassandra_service_start="package_script")
    refuse = role_task("cassandra_service", "Refuse the package's init script where systemd cannot start it", "main.yml")
    a = refuse["ansible.builtin.assert"]
    assert not render("{{ %s }}" % a["that"], cassandra_service_sysv_generator={"stdout": ""})
    assert "Set cassandra_service_start: unit" in " ".join(render(a["fail_msg"], inventory_hostname="n1").split())


def test_init_script_boot_state_asked_to_the_init_system():
    keep = role_task("cassandra_service", "Keep the snapshot", "main.yml")["ansible.builtin.set_fact"]
    enabled = keep["_cassandra_service_before"]["enabled"]
    status = {"LoadState": "loaded", "UnitFileState": "generated"}
    assert render(enabled, _status=status, cassandra_service_sysv_enabled={"stdout": "enabled\n"}) == "enabled"
    assert render(enabled, _status=dict(status, UnitFileState="disabled"), cassandra_service_sysv_enabled={}) == "disabled"


def test_replace_way_out_for_the_init_script():
    t = role_task("cassandra_operation", "The replacement gets the role's unit", "replace_unit_check.yml")
    msg = " ".join(render(t["ansible.builtin.assert"]["fail_msg"], cassandra_service_unit_manage=True,
                          cassandra_service_start="package_script", inventory_hostname="n9").split())
    assert "set cassandra_service_start: unit in the host_vars of n9" in msg
    # the stop itself: the init script refused, the role's unit accepted
    that = t["ansible.builtin.assert"]["that"]

    def holds(**variables):
        return all(render("{{ %s }}" % c, **variables) for c in that)
    assert not holds(cassandra_service_unit_manage=True, cassandra_service_start="package_script")
    assert holds(cassandra_service_unit_manage=True, cassandra_service_start="unit")
    assert not holds(cassandra_service_unit_manage=False, cassandra_service_start="unit")
    # the kept parts' lines named in the way out
    msg = " ".join(render(t["ansible.builtin.assert"]["fail_msg"], cassandra_service_unit_manage=False,
                          cassandra_service_start="unit", inventory_hostname="n9",
                          cassandra_import_kept=["unit", "thp"]).split())
    assert ("the other *_manage: false lines the dead node had, cassandra_import_kept, cassandra_linux_thp_enabled,"
            " cassandra_linux_thp_defrag) from its host_vars") in msg
    # only the unit kept: no empty list left in the line
    msg = " ".join(render(t["ansible.builtin.assert"]["fail_msg"], cassandra_service_unit_manage=False,
                          cassandra_service_start="unit", inventory_hostname="n9", cassandra_import_kept=["unit"]).split())
    assert "the dead node had, cassandra_import_kept) from its host_vars" in msg


def test_replace_unit_checked_before_the_question():
    """The init script refused in the checks, before the question and the reset (hostile S16: it came after both)."""
    with open(os.path.join(COLLECTION, "playbooks", "replace_node.yml")) as f:
        plays = yaml.safe_load(f)
    check = next(p for p in plays if p.get("name") == "Check the replacement")
    names = []

    def walk(tasks):
        for task in tasks:
            names.append((task.get("name"), (task.get("ansible.builtin.include_role") or {}).get("tasks_from")))
            walk(task.get("block") or [])
    walk(check["tasks"])
    at = names.index(("The replacement gets the role's unit", "replace_unit_check.yml"))
    assert at < names.index(("Show the plan and confirm the replacement", "screen.yml"))
    assert at < names.index(("Reset the host first (cassandra_replace_node_reset)", "reset_node.yml"))
    with open(os.path.join(COLLECTION, "roles", "cassandra_operation", "tasks", "action_replace.yml")) as f:
        assert "cassandra_service_start" not in f.read()


def test_no_kernel_settings_no_directory_for_their_file():
    # (e2e: an imported node with none, cassandra_linux_sysctl: {}, on an image without /etc/sysctl.d: every --check
    # and drift showed the directory to create, which no real run did) no file written: no directory either
    directory = task("Directory of the kernel settings file", "os.yml")
    assert when(directory, cassandra_linux_sysctl={}) is False
    assert when(directory, cassandra_linux_sysctl=defaults()["cassandra_linux_sysctl"]) is True
