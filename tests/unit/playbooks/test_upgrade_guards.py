from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# upgrade, found on a 4.1 -> 5.0 run: the prepare's config copy holds the files (copy's force: false copied
# none from a directory), the canary and rolling phases refuse nodes without the pre-upgrade snapshot, the
# sstables phase refuses a 5.0 target not in storage_compatibility_mode NONE, the rolling phase says the next
# mode step from the inventory (a re-run too), --check says what a real run would do, cleanup suggests no way
# back, a node down with the target package installed is pointed to apply_config, and apply_config's plan says
# NONE is the point of no return.

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
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_upgrade_from="4.1.12")
    assert render(check["vars"]["_unprepared"], **variables) == ["n2"]
    assert text(check["ansible.builtin.assert"]["fail_msg"], **variables) == (
        "No snapshot pre-upgrade-4.1.12 on n2 (the prepare phase takes it). Way out: ansible-playbook "
        "community.cassandra.upgrade -e cassandra_upgrade_phase=prepare, then this phase again. Nothing was done.")
    hostvars["n2"]["cassandra_upgrade_prepared"]["stdout"] = LISTED
    assert render(check["vars"]["_unprepared"], **variables) == []


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

    hostvars = {"n1": node("n1", 0), "n2": node("n2", 1, "5.0.9"), "n3": node("n3", 1, "4.1.12")}
    variables = dict(check["vars"], hostvars=hostvars, ansible_play_hosts=list(hostvars), cassandra_package_version="5.0.9")
    assert render(check["vars"]["_on_target"], **variables) == ["n2"]
    assert text(check["ansible.builtin.assert"]["fail_msg"], **variables) == (
        "nodetool version failed on n2 (%s); nodetool version failed on n3 (%s). Package cassandra 5.0.9 (the target) "
        "is installed on n2: run ansible-playbook community.cassandra.apply_config --limit n2 (it writes the "
        "config of 5.0.9 and leaves Cassandra stopped there), then start Cassandra there (systemctl start cassandra), "
        "then run this phase again. A node not running Cassandra: start it "
        "(systemctl start cassandra there, or apply_config --limit <node>, which starts a node whose unit failed), "
        "then run this phase again. A node already upgraded is skipped. Nothing was done." % (REFUSED, REFUSED))
    del hostvars["n3"]
    variables["ansible_play_hosts"] = list(hostvars)
    hostvars["n2"]["cassandra_upgrade_installed"]["stdout"] = "5.0.9-1"  # a Debian revision
    msg = text(check["ansible.builtin.assert"]["fail_msg"], **variables)
    # apply_config leaves a node stopped by the upgrade's drain stopped (its unit not failed): the start is said
    assert "apply_config --limit n2 (it writes" in msg and "A node not running Cassandra" not in msg
    assert "then start Cassandra there (systemctl start cassandra), then run this phase again" in msg

