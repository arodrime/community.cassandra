from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# status: the ring read by address (matched with the inventory by address), then each node shown by its name
# too (-e status_resolve_ip, true by default), resolved on the node the ring was read from, bounded in time; an
# address without a name shown alone. The other users of cassandra_status read no name.

import glob
import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")
with open(os.path.join(TOP, "playbooks", "status.yml"), encoding="utf-8") as f:
    TASKS = dict((t["name"], t) for t in yaml.safe_load(f)[1]["tasks"])
RESOLVE = TASKS["Resolve the addresses of the ring on {{ _view.from }}"]


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_the_ring_is_read_by_address():
    for name in ("Read the ring from {{ _from }}", "Read the ring from the other nodes ({{ _from }} did not answer)"):
        assert "resolve_ip" not in TASKS[name]["community.cassandra.cassandra_status"]
    # nor anywhere else: health_check, preflight... read addresses
    for path in glob.glob(os.path.join(TOP, "playbooks", "*.yml")) + glob.glob(os.path.join(TOP, "roles", "*", "tasks", "*.yml")):
        with open(path, encoding="utf-8") as f:
            assert "resolve_ip" not in f.read() or path.endswith("status.yml"), path


def test_names_resolved_on_the_node_read_bounded_and_on_by_default():
    assert RESOLVE["delegate_to"] == "{{ _view.from }}" and RESOLVE["run_once"] is True
    assert RESOLVE["when"] == "status_resolve_ip | default(true) | bool"
    assert RESOLVE["check_mode"] is False and RESOLVE["changed_when"] is False
    assert RESOLVE["failed_when"] is False and RESOLVE["ignore_errors"] is True and RESOLVE["timeout"] == 60
    view = {"from": "n1", "result": {"cluster_status": {
        "dc1": {"nodes": [{"address": "10.0.0.1"}, {"address": "0:0:0:0:0:0:0:3"}]}, "dc2": {"nodes": []}}}}
    ring = render(RESOLVE["vars"]["_ring"], _view=view)
    command = render(RESOLVE["ansible.builtin.shell"], _ring=ring)
    # each address its own getent, 3 s at most, 16 at a time; none (-r): no getent enumerating every host
    assert command == r"printf '%s\n' 10.0.0.1 0:0:0:0:0:0:0:3 | xargs -r -n 1 -P 16 timeout 3 getent hosts"


def test_getent_lines_to_names():
    show = TASKS["Show the ring"]
    assert "names=_names" in show["ansible.builtin.debug"]["msg"]
    out = {"stdout_lines": ["10.0.0.1        db1.example.com db1", "::3   db3.example.com", "", "garbage"]}
    assert render(show["vars"]["_names"], cassandra_status_names=out) == {
        "10.0.0.1": "db1.example.com", "::3": "db3.example.com"}
    # skipped (status_resolve_ip=false) or timed out: no name, the addresses alone
    assert render(show["vars"]["_names"], cassandra_status_names={"skipped": True}) == {}
    assert render(show["vars"]["_names"], cassandra_status_names={"failed": True, "msg": "The task timed out"}) == {}
