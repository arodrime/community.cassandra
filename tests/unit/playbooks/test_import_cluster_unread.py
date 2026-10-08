from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: a ring node it could not read fails the import (strict), unless
# import_cluster_allow_unread: the roles would give it the group variables unchecked.

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

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")
with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)
WRITE = next(p for p in PLAYS if p["name"] == "Write the inventory")
STOP = next(t for t in WRITE["tasks"] if t["name"] == "Stop on a failed self-check")


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


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
    (NODES, {"import_cluster_allow_unread": True}, True),
])
def test_unread_ring_node_fails_the_check(nodes, extra, expected):
    assert ok(nodes, **extra) is expected


def test_report_and_stop_name_them():
    variables = dict(_nodes=NODES, _layout=LAYOUT, _dir="/inv", _report_dir="/reports/c", ansible_check_mode=False)
    variables["_unchecked"] = trust_as_template(WRITE["vars"]["_unchecked"])
    msg = render(STOP["ansible.builtin.fail"]["msg"], **variables)
    assert "n2 not read" in msg and "See TO DO above, and DETAILS in /reports/c/report.txt." in msg
    variables["ansible_check_mode"] = True  # --check writes no report
    assert "See TO DO above." in render(STOP["ansible.builtin.fail"]["msg"], **variables)
    assert "allow_unread=_report_args.allow_unread" in WRITE["vars"]["_report"]
    assert "import_cluster_strict | default(true) | bool" in STOP["when"]


def test_air_gapped_node_noted():
    todo = [t for p in PLAYS for t in p.get("tasks", [])]
    match = None
    while todo:
        t = todo.pop(0)
        if t.get("name") == "Match the ring with the hosts":
            match = t["vars"]
        todo += t.get("block", [])
    hv = {"import_cluster_repos_answer": False, "import_cluster_newer_files": []}
    notes = render(match["_node"]["notes"], _hv=hv, _read=True, _versions="v", _install="i", _started="s",
                   _host="10.0.0.1", item={"address": "10.0.0.1"}, _repo={"manage": True})
    assert [n for n in notes if "cassandra_offline: true" in n]
