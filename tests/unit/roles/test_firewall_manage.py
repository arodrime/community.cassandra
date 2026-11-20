from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_firewall_manage: false leaves the firewall as it is (nothing
# installed, started or opened); cassandra_firewall_enabled: false leaves the node without one. import_cluster
# carries the firewall of the nodes it reads (rc10), and keeps it as it is on the nodes it could not read.

import os
import subprocess

import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_inventory_layout
from ansible_collections.community.cassandra.tests.unit.import_nodes import node
from ansible_collections.community.cassandra.tests.unit.playbooks.test_apply_config_permissions import (
    find, load, render, with_task_vars, walk)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def test_role_does_nothing_when_not_managed():
    with open(os.path.join(ROOT, "roles", "cassandra_firewall", "tasks", "main.yml")) as f:
        main = yaml.safe_load(f)
    setup = next(t for t in main if t["name"] == "Set up the firewall")
    assert setup["ansible.builtin.include_tasks"] == "firewall.yml"
    assert setup["when"] == ["cassandra_firewall_manage | bool", "cassandra_firewall_enabled | bool"]
    none = next(t for t in main if t["name"] == "Leave the node without a firewall")
    assert none["when"] == ["cassandra_firewall_manage | bool", "not cassandra_firewall_enabled | bool"]
    # every change is in firewall.yml or no_firewall.yml
    assert [t for t in main if set(t) & {"ansible.builtin.package", "ansible.builtin.service"}] == []
    with open(os.path.join(ROOT, "roles", "cassandra_firewall", "defaults", "main.yml")) as f:
        defaults = yaml.safe_load(f)
    assert defaults["cassandra_firewall_manage"] is True and defaults["cassandra_firewall_enabled"] is True


def test_import_keeps_the_firewall_of_the_nodes_not_read():
    hv = {"import_cluster_keep": {}, "import_cluster_medusa": {}}
    assert node(hv)["keep"] == {"cassandra_dsbulk_install": False}  # read: carried (cassandra_os)
    assert node({}, read=False)["keep"]["cassandra_firewall_manage"] is False
    base = {"dc": "dc1", "rack": "r1", "read": True, "vars": {"cassandra_cluster_name": "c"}, "hand_edits": [],
            "normalized": [], "notes": []}
    layout = cassandra_inventory_layout([dict(base, name="n1", address="10.0.0.1",
                                              keep={"cassandra_firewall_manage": False})], "c")
    assert layout["host_vars"]["n1"] == {"cassandra_firewall_manage": False}  # nodes added later get the role's
    assert "the firewall (its package, service and ports), or none (cassandra_firewall_manage: false)" in layout["report"]


def _firewall():
    return [t.get("name") for t in walk(load("roles", "cassandra_firewall", "tasks", "firewall.yml"))]


def _variables(**inventory):
    variables = dict(load("roles", "cassandra_firewall", "defaults", "main.yml"))
    variables.update(load("roles", "cassandra_firewall", "vars", "main.yml"))
    variables.update(inventory)
    return variables


def test_the_ports_are_open_before_the_firewall_starts_and_never_closed_first():
    names = _firewall()
    start = names.index("Ensure firewall service is started")
    for name in ("Ensure ports for cassandra open (RedHat)", "Ensure ports for cassandra open (Debian)",
                 "Open them to their sources (RedHat)", "Open them to their sources (Debian)"):
        assert names.index(name) < start, name
    # to their sources first, then closed to everyone else: no moment with the port closed to them
    for family in ("RedHat", "Debian"):
        assert names.index("Open them to their sources (%s)" % family) < names.index("Close them to everyone (%s)" % family)


def test_the_node_s_moved_ports_are_said_not_opened():
    # (opened to everyone, an imported node reaching them through a zone of its own got them on its public side:
    # a change after the import)
    unopened = "{{ _cassandra_firewall_own_unopened }}"
    assert render(unopened, _variables()) == []
    moved = _variables(cassandra_storage_port=7100, cassandra_native_transport_port=9142, cassandra_jmx_port=7299,
                       cassandra_local_jmx=False)
    assert render(unopened, moved) == ["7100/tcp (cassandra_storage_port)", "9142/tcp (cassandra_native_transport_port)",
                                       "7299/tcp (cassandra_jmx_port)"]
    assert render(unopened, dict(moved, open_ports=["7100/tcp"], cassandra_firewall_port_sources={"7299/tcp": ["10.0.0.1"]})) \
        == ["9142/tcp (cassandra_native_transport_port)"]
    # said as set (not as listened on), one fact per line, the way out on its own line
    task = find(load("roles", "cassandra_firewall", "tasks", "firewall.yml"), "Say the Cassandra ports the role does not open")
    said = render(task["ansible.builtin.debug"]["msg"], with_task_vars(task, dict(moved, inventory_hostname="n1")))
    assert said == ["WARNING  n1: Cassandra is set to use 7100/tcp (cassandra_storage_port), 9142/tcp"
                    " (cassandra_native_transport_port), 7299/tcp (cassandra_jmx_port), not in open_ports nor among the"
                    " ports open to some sources.",
                    "The firewall role does not open them.",
                    "Way out: add them to open_ports, unless the node reaches them by other means."]


def test_the_rich_rules_written_offline_are_one_per_line(tmp_path):
    # (firewalld stopped: the rules go through firewall-offline-cmd, one line each; two sources once gave one line
    # with a literal \n, which firewalld refused)
    sources = {"7199/tcp": ["10.0.9.0/24", "10.0.10.5"], "9404/udp": ["fd00::1"]}
    variables = _variables(cassandra_firewall_port_sources=sources, open_ports=["22/tcp", "7199/tcp", "9042/tcp"])
    rules = render("{{ _cassandra_firewall_rich_rules }}", variables)
    assert [r["rule"] for r in rules] == [
        'rule family="ipv4" source address="10.0.9.0/24" port port="7199" protocol="tcp" accept',
        'rule family="ipv4" source address="10.0.10.5" port port="7199" protocol="tcp" accept',
        'rule family="ipv6" source address="fd00::1" port port="9404" protocol="udp" accept']
    task = find(load("roles", "cassandra_firewall", "tasks", "firewall.yml"),
                "Ensure ports for cassandra open, before firewalld starts (RedHat)")
    variables = with_task_vars(task, variables)
    env = dict((k, render(v, variables)) for k, v in task["environment"].items())
    assert env["RULES"].split("\n") == [r["rule"] for r in rules]
    # the shell itself, against a firewall-offline-cmd that has nothing yet: each rule added as it is
    fake = tmp_path / "firewall-offline-cmd"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LOG"\ncase "$2" in --query-*) exit 1;; esac\nexit 0\n')
    fake.chmod(0o755)
    log = tmp_path / "log"
    out = subprocess.run(["bash", "-c", task["ansible.builtin.shell"]], capture_output=True, text=True,
                         env=dict(env, PATH="%s:/usr/bin:/bin" % tmp_path, LOG=str(log)))
    assert (out.returncode, out.stdout) == (0, "changed=1\n")
    calls = log.read_text().splitlines()
    for r in rules:
        assert "-q --add-rich-rule=" + r["rule"] in calls
    assert "-q --add-port=22/tcp" in calls and "-q --add-port=9042/tcp" in calls and "-q --add-port=7199/tcp" not in calls
    # a single source given as a string, none: as a list of one, nothing
    one = _variables(cassandra_firewall_port_sources={"7199/tcp": "10.0.9.5", "9404/tcp": None})
    assert [r["source"] for r in render("{{ _cassandra_firewall_rich_rules }}", one)] == ["10.0.9.5"]
    # the live path takes the same rules
    live = find(load("roles", "cassandra_firewall", "tasks", "firewall.yml"), "Open them to their sources (RedHat)")
    assert live["loop"] == "{{ _cassandra_firewall_rich_rules }}" and live["ansible.posix.firewalld"]["rich_rule"] == "{{ item.rule }}"


def test_a_firewall_starting_or_reloading_gets_its_ports_live():
    tasks = load("roles", "cassandra_firewall", "tasks", "firewall.yml")
    stopped = load("roles", "cassandra_firewall", "vars", "main.yml")["_cassandra_firewall_stopped"]
    offline = find(tasks, "Ensure ports for cassandra open, before firewalld starts (RedHat)")
    live = find(tasks, "Ensure ports for cassandra open (RedHat)")
    for state, written_offline in (("inactive", True), ("unknown", True), ("activating", False), ("reloading", False),
                                   ("active", False)):
        variables = {"ansible_facts": {"os_family": "RedHat"}, "_cassandra_firewall_before": {"active": state},
                     "_cassandra_firewall_check": False, "_cassandra_firewall_stopped": stopped}
        assert all(render("{{ %s }}" % w, variables) for w in offline["when"]) is written_offline, state
        assert all(render("{{ %s }}" % w, variables) for w in live["when"]) is not written_offline, state
