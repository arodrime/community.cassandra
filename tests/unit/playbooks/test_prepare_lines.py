from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The prepare of new nodes (add_node all at once, replace_node and
# add_datacenter through action_add.yml, create_cluster) says a line per step
# (prepare_lines.yml), from what each role's change report kept: rendered here
# from the facts the roles leave, each step's line there; the roles' own
# disk summary and old system keyspace note held back for it.

import os
import warnings

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

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


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def named(tasks, name):
    return next(t for t in walk(tasks) if t.get("name") == name)


def trusted(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trusted(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trusted(v) for v in value]
    return value


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=trusted(variables)).template(trusted(template))


def render_all(value, **variables):
    if isinstance(value, dict):
        return dict((k, render_all(v, **variables)) for k, v in value.items())
    return render(value, **variables)


LINES = load("roles", "cassandra_service", "tasks", "prepare_lines.yml")
KEEP, SAY, DISKS, WARN = (named(LINES, n) for n in (
    "Keep what the prepare did", "Say what the prepare did", "Say how the data disks are tuned",
    "Warn about an old system keyspace left outside the data directories"))
SHARED = named(LINES, "Say what the prepare did, how the disks are tuned, what it left")["vars"]  # the block's
REPORT = named(load("roles", "cassandra_change_report", "tasks", "main.yml"), "Keep what changed ({{ cassandra_change_report_role }})")
DISK = {"name": "", "kb": "4", "changed": False, "brief": ["sdb (ssd): read-ahead 4 KB"], "stacks": [], "info": [],
        "warnings": [], "notes": []}


def report(seen, role, items, state=None):
    """What cassandra_change_report keeps for a role (its own task rendered)."""
    variables = {"_cassandra_change_report_seen": seen, "cassandra_change_report_role": role,
                 "cassandra_change_report_items": items}
    if state is not None:
        variables["cassandra_change_report_state"] = state
    return render(REPORT["ansible.builtin.set_fact"]["_cassandra_change_report_seen"], **variables)


INSTALL = named(load("roles", "cassandra_install", "tasks", "report.yml"), "Report")["vars"]
LINUX = named(load("roles", "cassandra_linux", "tasks", "report.yml"), "Report")["vars"]
REPOSITORY = named(load("roles", "cassandra_repository", "tasks", "main.yml"), "Keep what was done with the repository")
PACKAGES = {"cassandra": [{"version": "5.0.9"}], "openjdk-17-jre-headless": [{"version": "17.0.12+7-1~deb12u1"}]}


def install_state(**variables):
    """The state cassandra_install's report task gives the change report (rendered)."""
    variables = dict({"ansible_facts": {"packages": PACKAGES}, "cassandra_install_java": True,
                      "cassandra_java_package": "openjdk-17-jre-headless", "_cassandra_java_home": "",
                      "cassandra_java_version": 17}, **variables)
    return render_all(INSTALL["cassandra_change_report_state"], **dict(INSTALL, **variables))


def outcome(**variables):
    """_cassandra_repository_outcome as cassandra_repository's task renders it."""
    variables = dict({"cassandra_repository_manage": True, "cassandra_install_method": "repository"}, **variables)
    return render(REPOSITORY["ansible.builtin.set_fact"]["_cassandra_repository_outcome"], **variables)


def roles_ran(name, firewall=False, medusa=False):
    """The facts the roles of a prepare leave on a node: each role's change report (with the state their own
    report tasks render), the repository's outcome, the disk summary, the old system keyspace cassandra_config
    found."""
    seen = report({}, "cassandra_install", [{"item": "package cassandra", "before": "absent", "after": "5.0.9"}],
                  install_state())
    linux = [{"item": "swap", "before": "on", "after": "off"},
             {"item": "sysctl vm.max_map_count (live)", "before": "65530", "after": "1048575"},
             {"item": "read-ahead sdb", "before": "128", "after": "4"}]
    seen = report(seen, "cassandra_linux", linux, render_all(
        LINUX["cassandra_change_report_state"], _cassandra_disk_items=linux[2:]))
    seen = report(seen, "cassandra_config", [
        {"item": "/etc/cassandra/cassandra.yaml", "before": "current", "after": "40 line(s) changed", "diff": "+secret: x"},
        {"item": "/etc/cassandra/jvm-server.options", "before": "current", "after": "3 line(s) changed", "diff": "-a\n+b"}])
    if firewall:
        seen = report(seen, "cassandra_firewall", [{"item": "port 7000", "before": "closed", "after": "open"}])
    if medusa:
        seen = report(seen, "cassandra_medusa", [{"item": "Medusa", "before": "absent", "after": "0.24.1"}])
    return {"inventory_hostname": name, "_cassandra_change_report_seen": seen, "_cassandra_repository_outcome": outcome(),
            "_cassandra_disk_summary": dict(DISK, name=name, changed=True),
            "_cassandra_join_seeds": "10.0.0.1,10.0.0.2", "cassandra_manage_firewall": firewall,
            "cassandra_medusa_enabled": medusa,
            "_cassandra_config_left": ["/var/lib/cassandra/data"], "_cassandra_config_own_dirs": ["/data/cassandra"]}


def kept(facts, hosts):
    done = render_all(KEEP["ansible.builtin.set_fact"]["_cassandra_prepare_done"], **dict(KEEP["vars"], **dict(
        facts, _cassandra_prepare_hosts=hosts, ansible_play_batch=hosts)))
    assert render("{{ %s }}" % KEEP["when"], **dict(KEEP["vars"], **dict(facts, _cassandra_prepare_hosts=hosts)))
    return done


def said(task, hostvars, hosts, check=False, verbosity=0):
    variables = dict(SHARED, **task["vars"])
    variables = dict(variables, hostvars=hostvars, _cassandra_prepare_hosts=hosts, ansible_play_batch=hosts,
                     ansible_check_mode=check, ansible_verbosity=verbosity)
    when = task["when"] if isinstance(task["when"], list) else [task["when"]]
    if not all(render("{{ %s }}" % w, **variables) for w in when):
        return None
    return render(task["ansible.builtin.debug"]["msg"], **variables)


def test_the_change_report_keeps_each_role_without_its_diff():
    seen = roles_ran("node4")["_cassandra_change_report_seen"]
    assert sorted(seen) == ["cassandra_config", "cassandra_install", "cassandra_linux"]
    assert all(set(i) == {"item", "before", "after"} for r in seen.values() for i in r["items"])  # no diff, no secret
    assert seen["cassandra_install"]["state"]["cassandra"] == "5.0.9"
    assert seen["cassandra_config"]["state"] == {}
    # kept whether the list is shown or not: no condition on the quiet flag
    assert "when" not in REPORT


def test_each_step_of_the_prepare_says_its_line():
    hosts = ["node4", "node5"]
    hostvars = dict((h, roles_ran(h, firewall=True, medusa=True)) for h in hosts)
    for h in hosts:
        hostvars[h]["_cassandra_prepare_done"] = kept(hostvars[h], hosts)
    assert said(SAY, hostvars, hosts) == [
        "node4, node5  repository  ok (already set)",
        "node4, node5  install     cassandra 5.0.9 (installed now)",
        "node4, node5  java        17.0.12 (already installed)",
        "node4, node5  os tuning   2 changes",  # the disk's change on the data disks line
        "node4, node5  data disks  read-ahead 4 KB set now",
        "node4, node5  config      2 files written",
        "node4, node5  firewall    1 change",  # (opened its ports)
        "node4, node5  medusa      Medusa 0.24.1 installed now",
        "node4, node5  seeds       joins with 10.0.0.1, 10.0.0.2"]
    assert SAY["run_once"] is True and SAY["vars"]["cassandra_output"] is True
    assert SAY["vars"]["cassandra_output_info"] is True  # the progress colour (ops callback)
    # --check: what a real run would do
    check = said(SAY, hostvars, hosts, check=True)
    assert "node4, node5  config      would write 2 files" in check
    assert "node4, node5  data disks  read-ahead 4 KB would be set" in check


def test_firewall_and_medusa_off_and_a_step_not_run():
    facts = roles_ran("node4")
    del facts["_cassandra_change_report_seen"]["cassandra_linux"]  # cassandra_linux_manage: false
    done = kept(facts, ["node4"])
    assert "cassandra_linux" not in done["ran"] and done["firewall"] == "off" and done["medusa"] == "off"
    lines = said(SAY, {"node4": {"_cassandra_prepare_done": done}}, ["node4"])
    assert "node4  os tuning   not managed (cassandra_linux_manage: false)" in lines
    assert "node4  firewall    off (cassandra_manage_firewall: false)" in lines
    assert "node4  medusa      off (cassandra_medusa_enabled: false)" in lines
    # cassandra_manage_firewall on, the role's own switch off: no report of it
    done = kept(dict(roles_ran("node4"), cassandra_manage_firewall=True), ["node4"])
    assert done["firewall"] == "not managed"


def test_only_the_nodes_prepared():
    hostvars = {"node4": dict(roles_ran("node4")), "node5": {}}  # node5 joined already (an earlier run)
    hostvars["node4"]["_cassandra_prepare_done"] = kept(hostvars["node4"], ["node4"])
    variables = dict(KEEP["vars"], **dict(roles_ran("node5"), _cassandra_prepare_hosts=["node4"]))
    assert not render("{{ %s }}" % KEEP["when"], **variables)
    assert said(SAY, hostvars, ["node4", "node5"])[0] == "node4  repository  ok (already set)"
    assert said(SAY, {"node5": {}}, ["node5"]) is None  # nobody prepared: nothing said


def test_the_disk_summary_after_the_lines_only_when_there_is_something_to_see():
    def hostvars(**disk):
        return {"node4": {"_cassandra_prepare_done": {"name": "node4", "disks": dict(DISK, name="node4", **disk)}}}

    assert said(DISKS, hostvars(), ["node4"]) is None  # already set: its step line says it
    assert said(DISKS, hostvars(), ["node4"], verbosity=1)[0].startswith("Data disks: ")  # -v: as before
    assert said(DISKS, hostvars(changed=True), ["node4"])[0] == "Data disks: read-ahead 4 KB, set now on 1 node"
    assert said(DISKS, hostvars(warnings=["sdb: no scheduler"]), ["node4"])[-1] == "WARNING - sdb: no scheduler (node4)"
    assert said(DISKS, hostvars(info=["sdc not tuned yet"]), ["node4"]) is not None
    assert said(DISKS, hostvars(changed="False"), ["node4"]) is None  # a string from an older Ansible


def test_the_old_system_keyspace_warning_once_for_its_nodes():
    hosts = ["node4", "node5", "node6"]
    hostvars = dict((h, roles_ran(h)) for h in hosts)
    hostvars["node6"]["_cassandra_config_left"] = []
    for h in hosts:
        hostvars[h]["_cassandra_prepare_done"] = kept(hostvars[h], hosts)
    lines = said(WARN, hostvars, hosts)
    assert [line for line in lines if line.startswith("WARNING")] == [
        "WARNING - node4, node5: an old system keyspace in /var/lib/cassandra/data, not one of the data directories the"
        " config uses (/data/cassandra)"]
    hostvars["node4"]["_cassandra_prepare_done"]["left"] = hostvars["node5"]["_cassandra_prepare_done"]["left"] = []
    assert said(WARN, hostvars, hosts) is None


def test_the_roles_hold_back_what_the_lines_say():
    linux = named(load("roles", "cassandra_linux", "tasks", "main.yml"), "Say how the data disks are tuned")
    assert "not _cassandra_prepare_lines | default(false) | bool" in linux["when"]
    config = named(load("roles", "cassandra_config", "tasks", "main.yml"), "Say what a new node leaves as it is")
    assert "not _cassandra_prepare_lines | default(false) | bool" in config["when"]
    # outside a prepare: the same WARNING, for its node
    msg = render(config["ansible.builtin.debug"]["msg"], inventory_hostname="node4",
                 _cassandra_config_left=["/var/lib/cassandra/data"], _cassandra_config_own_dirs=["/data/cassandra"])
    assert msg[0].startswith("WARNING - node4: an old system keyspace in /var/lib/cassandra/data")


def test_every_prepare_says_its_lines():
    # add_node: all the new nodes at once, then the lines for the ones prepared
    plays = load("playbooks", "add_node.yml")
    prepare = next(p for p in plays if p["name"] == "Prepare the new nodes, all at once")
    tasks = prepare["tasks"]
    block = next(t for t in tasks if "block" in t)["block"]
    # after the block: what only prints never goes to its rescue (a run_once failing there drops hosts, 2.19+)
    assert "Say what the prepare did" not in [t["name"] for t in walk(block)]
    step = tasks[-1]
    assert step["name"] == "Say what the prepare did" and "block" in tasks[-2]
    assert step["ansible.builtin.include_role"]["tasks_from"] == "prepare_lines.yml" and "when" not in step
    assert "cassandra_new_node_state" in step["vars"]["_cassandra_prepare_hosts"]
    roles = named(block, "Prepare this node")
    assert roles["vars"]["_cassandra_prepare_lines"] is True
    assert roles["vars"]["_cassandra_change_report_quiet"] == "{{ ansible_verbosity | int == 0 }}"  # -v: the lists too
    # replace_node, add_datacenter: one node at a time, through action_add.yml
    add = load("roles", "cassandra_service", "tasks", "action_add.yml")
    assert [t["name"] for t in add[:2]] == ["Prepare the node", "Say what the prepare did"]
    assert add[0]["vars"]["_cassandra_prepare_lines"] is True
    assert add[1]["ansible.builtin.include_tasks"] == "prepare_lines.yml" and add[1]["when"] == add[0]["when"]
    # create_cluster: every node, its roles quiet but with -v
    plays = load("playbooks", "create_cluster.yml")
    play = next(p for p in plays if p["name"] == "Prepare the nodes")
    assert play["vars"]["_cassandra_prepare_lines"] is True
    assert play["vars"]["_cassandra_change_report_quiet"] == "{{ ansible_verbosity | int == 0 }}"
    assert play["post_tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "prepare_lines.yml"
    assert [t["name"] for t in play["pre_tasks"]][-2:] == ["Say what the run does", "Forget what an earlier prepare did"]


def test_a_prepare_forgets_an_earlier_one():
    prepare = load("roles", "cassandra_service", "tasks", "action_add_prepare.yml")
    names = [t["name"] for t in prepare]
    assert names.index("Forget what an earlier prepare did") < names.index("Configure the package repository")
    assert named(prepare, "Forget what an earlier prepare did")["ansible.builtin.include_tasks"] == "prepare_reset.yml"
    create = next(p for p in load("playbooks", "create_cluster.yml") if p["name"] == "Prepare the nodes")
    assert named(create["pre_tasks"], "Forget what an earlier prepare did")["ansible.builtin.include_role"][
        "tasks_from"] == "prepare_reset.yml"
    reset = load("roles", "cassandra_service", "tasks", "prepare_reset.yml")[0]["ansible.builtin.set_fact"]
    assert reset == {"_cassandra_change_report_seen": {}, "_cassandra_repository_outcome": "",
                     "_cassandra_config_left": [], "_cassandra_config_own_dirs": []}


def test_the_roles_own_tasks_give_what_the_lines_read():
    # cassandra_repository: what it did, under every switch (its task rendered: a template error stops the role)
    assert outcome(cassandra_repository_manage=False) == "off"
    assert outcome(cassandra_install_method="packages") == "packages"
    assert outcome() == "ok"
    assert outcome(_cassandra_repository_apt={"changed": True}) == "set"
    assert outcome(_cassandra_repository_yum={"changed": True}) == "set"
    assert outcome(_cassandra_repository_old={"changed": True, "results": []}) == "set"
    assert outcome(_cassandra_repository_apt={"changed": False, "skipped": True},
                   _cassandra_repository_python_debian={"rc": 1}) == "set"  # --check, python3-debian missing
    # cassandra_install: the versions read from the packages
    state = install_state()
    assert state["cassandra"] == "5.0.9"
    assert state["java"] == {"managed": True, "package": "openjdk-17-jre-headless", "version": "17.0.12+7-1~deb12u1",
                             "home": "", "series": "17"}
    assert install_state(ansible_facts={"packages": {}})["cassandra"] == ""  # --check on a blank host
    # cassandra_linux: how many of its items are the disks'
    assert int(render_all(LINUX["cassandra_change_report_state"], _cassandra_disk_items=[{}, {}])["disk_changes"]) == 2
    assert int(render_all(LINUX["cassandra_change_report_state"])["disk_changes"]) == 0


def test_what_only_prints_never_stops_the_run():
    for task in (KEEP, SAY, DISKS, WARN):
        assert task["ignore_errors"] is True, task["name"]


# read-only, or a step of another (the download, then its install)
READS = ("_metadata", "_key_list", "_gpg", "_gpg_check", "_python_debian", "_key_download", "_auth_mark")


def test_every_change_of_the_repository_role_counts():
    tasks = [t for f in ("repository.yml", "remove_apt_credentials.yml")
             for t in walk(load("roles", "cassandra_repository", "tasks", f))]
    # every task that may change the node registers its result (else the outcome can't see it)
    for t in tasks:
        quiet = t.get("changed_when") is False or str(t.get("check_mode")) == "False" or "block" in t
        quiet = quiet or any(k in t for k in ("ansible.builtin.set_fact", "ansible.builtin.assert", "ansible.builtin.fail",
                                              "ansible.builtin.debug", "ansible.builtin.include_tasks"))
        quiet = quiet or t.get("name") in ("Ensure the key directory exists", "Install gpg to read them (RedHat)",
                                           "Remove the downloaded file", "Update the apt cache (Debian & Ubuntu)")
        assert quiet or "register" in t, t["name"]
    registered = [t["register"] for t in tasks if "register" in t and not t["register"].endswith(READS)]
    for name in registered:  # what it writes or removes, all of it read by the outcome
        assert name in REPOSITORY["ansible.builtin.set_fact"]["_cassandra_repository_outcome"], name
    for name in registered:
        assert outcome(**{name: {"changed": True}}) == "set", name


def test_a_pinned_java_package_keeps_its_version():
    state = install_state(cassandra_java_package="openjdk-17-jre-headless=17.0.12*")
    assert state["java"]["package"] == "openjdk-17-jre-headless"
    assert state["java"]["version"] == "17.0.12+7-1~deb12u1"
