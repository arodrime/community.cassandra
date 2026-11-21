from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: a ring node it could not read fails the import (strict), unless
# import_allow_unread: the roles would give it the group variables unchecked.

import os

import pytest
import yaml


from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
from ansible_collections.community.cassandra.tests.unit.import_nodes import node
from ansible_collections.community.cassandra.tests.unit.helpers import render_as_given, trust_as_template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")
with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
WRITE = next(p for p in PLAYS if p["name"] == "Write the inventory")
STOP = next(t for t in WRITE["tasks"] if t["name"] == "Stop on a failed self-check")


def render(template, **variables):
    return render_as_given(template, variables)


NODES = [{"address": "10.0.0.1", "read": True}, {"address": "10.0.0.2", "read": False}]
LAYOUT = {"names": {"10.0.0.1": "n1", "10.0.0.2": "n2"}}
CHECK = {"n1": {"differences": [], "notes": []}}


def ok(nodes, **extra):
    variables = dict(_nodes=nodes, _layout=LAYOUT, _self_check=CHECK, **extra)
    variables["_unchecked"] = trust_as_template(WRITE["vars"]["_unchecked"])
    return render(WRITE["vars"]["_self_check_ok"], **variables)


@pytest.mark.parametrize("nodes, extra, expected", [
    (NODES[:1], {}, True),
    (NODES, {}, False),  # n2 not read: not valid
    (NODES, {"import_allow_unread": True}, True),
])
def test_unread_ring_node_fails_the_check(nodes, extra, expected):
    assert ok(nodes, **extra) is expected


def test_report_and_stop_name_them():
    load_collection_filters()
    layout = {"cluster_group": "c", "names": LAYOUT["names"],
              "nodes": [{"name": "n1", "read": True, "hand_edits": []},
                        {"name": "n2", "read": False, "reason": "unreachable", "hand_edits": []}]}
    report_file = trust_as_template(WRITE["vars"]["_report_args"]["report_file"])
    variables = dict(_nodes=NODES, _layout=layout, _dir="/inv", _report_dir="/reports/c", ansible_check_mode=False,
                     _self_check=CHECK, _newline="\n", import_allow_unread=False)
    variables["_report_args"] = {"report_file": report_file, "allow_unread": False, "inventory": "", "hosts": "",
                                 "cwd": "",
                                 "check": trust_as_template(WRITE["vars"]["_report_args"]["check"])}
    msg = render(STOP["ansible.builtin.fail"]["msg"], **variables).split("\n")
    assert msg[0] == u"SELF-CHECK FAILED \u2014 1 node not read, so not checked"
    assert "  1 node not read, so not checked: n2 (unreachable)." in msg
    assert "  The inventory: written for inspection only, marked NOT VALID" in msg
    variables["ansible_check_mode"] = True
    msg = render(STOP["ansible.builtin.fail"]["msg"], **variables).split("\n")
    assert "  --check: the inventory not written" in msg
    assert "allow_unread=_report_args.allow_unread" in WRITE["vars"]["_report"]
    assert "import_strict | default(true) | bool" in STOP["when"]


def test_air_gapped_node_noted():
    notes = node({"import_cluster_repos_answer": False, "import_cluster_newer_files": []})["notes"]
    assert [n for n in notes if "cassandra_offline: true" in n]
    assert not [n for n in node({"import_cluster_repos_answer": True})["notes"] if "cassandra_offline: true" in n]


READ = next(p for p in PLAYS if p["name"] == "Read every node")
READ_TASKS = READ["tasks"]


def test_a_jvm_not_the_units_is_noted_and_not_read_for_the_proofs():
    # a JVM started by hand (outside the unit's control group) or with an unreadable environment:
    # its arguments are not read for the proofs (none read: a difference stays one), said under the node
    why = "not started by the cassandra unit (pid 42 is not in its control group)"
    notes = node({"import_cluster_unit": {"jvm_untrusted": why}})["notes"]
    assert "the running JVM's arguments and environment not used to prove the roles' files give it the same: " + why \
        in notes
    assert not [n for n in node({"import_cluster_unit": {"jvm_untrusted": ""}})["notes"] if "not used to prove" in n]
    read = next(t for t in READ_TASKS if t.get("name") == "Read the running Cassandra (conf dir, Cassandra and Java"
                " versions)")["ansible.builtin.shell"]
    assert "/files/jvm_proof.sh" in read and 'echo "jvm_untrusted=$(jvm_untrusted "$pid"' in read
    files = next(t for t in READ_TASKS if t.get("name", "").startswith("Read the config files, when they were"))
    assert "import_cluster_unit.jvm_untrusted | default('') == ''" in files["vars"]["_wanted"]


@pytest.mark.parametrize("task, field", [
    ("Read the running Cassandra (conf dir, Cassandra and Java versions)", ("environment", "IMPORT_CLUSTER_OS_FAMILY")),
    ("Read the config files, when they were written, the JVM's arguments, the repository files and the unit",
     ("vars", "_repo")),
])
def test_a_node_not_reached_has_no_facts_and_no_templating_error(task, field):
    # ignore_unreachable: the node's later tasks run without its facts and end unreachable, the reason the report
    # gives, never a templating error ('dict object' has no attribute 'os_family')
    template = next(t for t in READ["tasks"] if t["name"] == task)[field[0]][field[1]]
    assert render(template, ansible_facts={}, import_cluster_repo_files=[]) in ("", [], "[]")
