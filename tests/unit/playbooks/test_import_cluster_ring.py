from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster reading the ring: nodetool must read the JMX password file
# (cassandra's, 0400), a failure must say why, and the nodes found in the
# ring (add_host) must not hide the given ones.

import os
import subprocess

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import cassandra_import_error

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name):
    return next(t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == name)


def test_nodetool_runs_with_become():
    # every play that runs nodetool, the ring read included
    for play in PLAYS:
        if "nodetool" in yaml.safe_dump(play) or "cassandra_status" in yaml.safe_dump(play):
            assert play.get("become") is True, play["name"]


def test_failure_shows_each_node_error():
    stop = task("Stop if no node answered")
    hostvars = {
        "n1": {"import_cluster_ring": {"failed": True, "msg": "Unable to determine Cassandra version: ",
                                       "stderr": "error: ******** (Permission denied)\n-- StackTrace --\n..."}},
        "n2": {},  # unreachable: the task never ran there
        # the module said it already: not twice
        "n3": {"import_cluster_ring": {"failed": True, "msg": "JMX login refused (Invalid username or password): check",
                                       "stderr": "error: Invalid username or password\n-- StackTrace --\n..."}},
    }
    variables = {"import_cluster_given": ["n1", "n2", "n3"], "hostvars": hostvars}
    templar = Templar(loader=DataLoader(), variables=variables)
    errors = templar.template(trust_as_template(stop["vars"]["_errors"]))
    assert errors == ["n1: Unable to determine Cassandra version:  error: ******** (Permission denied)",
                      "n2: unreachable", "n3: JMX login refused (Invalid username or password): check"]


def test_given_nodes_found_when_a_discovered_node_comes_first():
    # add_host puts the nodes found in the ring in groups['all'], maybe first;
    # only the given ones have import_cluster_given
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    hostvars = {"10.0.0.2": {}, "node1": {"import_cluster_given": ["node1"]}}
    templar = Templar(loader=DataLoader(), variables={"groups": {"all": ["10.0.0.2", "node1"]}, "hostvars": hostvars})
    assert templar.template(trust_as_template(write["vars"]["_given"])) == ["node1"]


def test_inventory_written_without_become():
    # the nodes' plays become: sudo on the controller would fail (password) or write root's files
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    assert write.get("become") is False


def test_node_read_failure_is_reported_without_values():
    # the no_log step is rescued: its reason, kept for the report, shows no value
    block = task("Read this node's config")
    assert [t["name"] for t in block["block"]] == ["Check cassandra.yaml was read", "Turn them into cassandra_config variables"]
    keep = block["rescue"][0]["ansible.builtin.set_fact"]["import_cluster_error"]
    variables = {"ansible_failed_task": {"name": "Turn them into cassandra_config variables"},
                 "ansible_failed_result": {"failed": True, "msg": "templating failed: cassandra_config_import: "
                                           "cassandra.yaml, KeyError in _read_line(), line 3 (message hidden: it may "
                                           "show a value read from the nodes)"}}
    # the filter branch of _why (Templar here does not load the collection's filters)
    assert "cassandra_import_error" in block["rescue"][0]["vars"]["_why"]
    variables["_why"] = cassandra_import_error(variables["ansible_failed_result"])
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template(keep)) == (
        '"Turn them into cassandra_config variables" failed: cassandra_config_import: cassandra.yaml, KeyError in _read_line(), '
        'line 3 (message hidden: it may show a value read from the nodes)')
    # and the report gives it as the reason the node was not read
    match = next(t for t in task("Build the inventory")["block"] if t["name"] == "Match the ring with the hosts")
    hv = {"import_cluster_running": {"rc": 0}, "import_cluster_version": "4.1.12", "import_cluster_error": "boom"}
    templar = Templar(loader=DataLoader(), variables={"_hv": hv})
    assert templar.template(trust_as_template(match["vars"]["_node"]["reason"])) == "boom"


def test_node_read_needs_a_supported_series_and_its_cassandra_yaml():
    # a skipped loop registers results too: an unsupported version must not reach the filter
    block = task("Read this node's config")
    variables = {"import_cluster_series": "30x",
                 "import_cluster_slurp": {"skipped": True, "results": [{"skipped": True, "item": "cassandra.yaml"}]}}
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ " + block["when"] + " }}")) is False
    # without cassandra.yaml the role defaults (cluster name, seeds) would be imported
    check = block["block"][0]
    assert check["name"] == "Check cassandra.yaml was read"
    variables = {"import_cluster_conf_dir": "/etc/cassandra/conf", "import_cluster_slurp": {"results": [
        {"item": "cassandra-env.sh", "content": "eA=="},
        {"item": "cassandra.yaml", "failed": True, "msg": "file not found: /etc/cassandra/conf/cassandra.yaml"}]}}
    variables["_yaml"] = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(check["vars"]["_yaml"]))
    templar = Templar(loader=DataLoader(), variables=variables)
    assert templar.template(trust_as_template("{{ " + check["ansible.builtin.assert"]["that"] + " }}")) is False
    assert templar.template(trust_as_template(check["ansible.builtin.assert"]["fail_msg"])) == (
        "file not found: /etc/cassandra/conf/cassandra.yaml")
    # the reason kept for the report is the assert's message
    rescue = {"ansible_failed_task": {"name": "Check cassandra.yaml was read"},
              "ansible_failed_result": {"failed": True, "msg": "file not found: /etc/cassandra/conf/cassandra.yaml"}}
    why = Templar(loader=DataLoader(), variables=rescue).template(trust_as_template(block["rescue"][0]["vars"]["_why"]))
    assert why == "file not found: /etc/cassandra/conf/cassandra.yaml"


def _status(*addresses):
    return {"cluster_status": {"dc1": {"nodes": [{"address": a} for a in addresses]}}}


def test_stops_on_nodes_of_several_clusters():
    # without -i <node>, every host of the inventory is given: two clusters' nodes stop the import
    stop = task("Stop on nodes of several clusters")
    hostvars = {"a1": {"import_cluster_ring": _status("10.0.0.1", "10.0.0.2")},
                "a2": {"import_cluster_ring": _status("10.0.0.2", "10.0.0.1")},
                "b1": {"import_cluster_ring": _status("10.0.1.1")},
                "down": {"import_cluster_ring": {"failed": True}}, "gone": {}}
    for given, apart in ((["a1", "a2", "down", "gone"], []), (["a1", "b1", "a2"], ["b1"]), (["b1", "a1", "a2"], ["a1", "a2"])):
        variables = {"import_cluster_given": given, "hostvars": hostvars}
        variables["_rings"] = Templar(loader=DataLoader(), variables=variables).template(
            trust_as_template(stop["vars"]["_rings"]))
        assert Templar(loader=DataLoader(), variables=variables).template(
            trust_as_template(stop["vars"]["_apart"])) == apart, given
    assert stop["run_once"] is True


def test_limit_refused_and_the_nodes_read():
    # --limit would leave out the nodes found and localhost (the inventory written nowhere, rc 0)
    stop = task("Stop on --limit")
    assert stop["ansible.builtin.assert"]["that"] == "ansible_limit is not defined" and stop["run_once"] is True
    find = next(play for play in PLAYS if play["name"] == "Find the cluster from the given nodes")
    assert [t["name"] for t in find["tasks"]][:2] == ["Check the options", "Stop on --limit"]
    # -e cassandra_hosts: that group's hosts given (else CASSANDRA_CLUSTER's, else all: test_import_cluster_named);
    # read: the given ones and the ones found, not the inventory's others
    assert "query('community.cassandra.cassandra_nodes', group=cassandra_hosts) if cassandra_hosts" in find["hosts"]
    read = next(play for play in PLAYS if play["name"] == "Read every node")
    hostvars = {"a1": {"import_cluster_given": ["a1"]}, "b1": {}, "10.0.0.2": {}}
    groups = {"all": ["a1", "b1", "10.0.0.2"], "import_cluster_found": ["10.0.0.2"]}
    templar = Templar(loader=DataLoader(), variables={"groups": groups, "hostvars": hostvars})
    assert templar.template(trust_as_template(read["hosts"])) == ["a1", "10.0.0.2"]


def test_found_nodes_matched_by_their_address():
    # a node found in the ring, added under its name: matched to its ring address without facts too
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    match = next(t for t in next(t for t in write["tasks"] if t.get("name") == "Build the inventory")["block"] if t["name"] == "Match the ring with the hosts")
    hostvars = {"node1": {"ansible_facts": {"all_ipv4_addresses": ["10.0.0.1"], "hostname": "node1"}},
                "node2": {"import_cluster_address": "10.0.0.2"}}
    variables = {"_given": ["node1"], "groups": {"import_cluster_found": ["node2"]}, "hostvars": hostvars,
                 "item": {"address": "10.0.0.2"}, "import_cluster_host_names": "hostname"}
    templar = Templar(loader=DataLoader(), variables=variables)
    host = templar.template(trust_as_template(match["vars"]["_host"]))
    assert host == "node2"
    # not reached (no facts): named by its address, as the report says it is not read
    variables.update(_host=host, _hv=hostvars["node2"], _host_names="hostname")
    assert Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(match["vars"]["_name"])) == "10.0.0.2"
    # reached: its hostname
    variables["_hv"] = {"ansible_facts": {"hostname": "node2"}}
    assert Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(match["vars"]["_name"])) == "node2"


def test_found_node_not_taken_for_another_machine():
    # reached by name, the machine has not the ring's address: not this node (named by its address, not read)
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    match = next(t for t in next(t for t in write["tasks"] if t.get("name") == "Build the inventory")["block"] if t["name"] == "Match the ring with the hosts")
    hostvars = {"node1": {"ansible_facts": {"all_ipv4_addresses": ["10.0.0.1"]}},
                "node2": {"import_cluster_address": "10.0.0.2", "ansible_facts": {"all_ipv4_addresses": ["10.9.9.9"]}}}
    variables = {"_given": ["node1"], "groups": {"import_cluster_found": ["node2"]}, "hostvars": hostvars,
                 "item": {"address": "10.0.0.2"}}
    assert Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(match["vars"]["_host"])) == "10.0.0.2"


def _added(given_vars, getent, given="node2", known=(), usable=None):
    """What 'Add the nodes the inventory does not have yet' gives each found node: {address: {var: value}}.
    usable: the names the controller can reach (default: every one)."""
    add = task("Add the nodes the inventory does not have yet")
    out = {}
    for address in ["10.0.0.1", "10.0.0.3"]:
        variables = {"hostvars": {given: given_vars}, "import_cluster_from": given, "item": address,
                     "import_cluster_new": ["10.0.0.1", "10.0.0.3"], "import_cluster_peer_names": {"stdout": getent},
                     "import_cluster_peer_usable": {"stdout": " ".join(
                         usable if usable is not None else ["node1", "node3", "node1.example.org"])},
                     "groups": {"all": [given] + list(known)}, "omit": "__omit__"}
        variables.update((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in add["vars"].items())
        templar = Templar(loader=DataLoader(), variables=variables)
        args = dict((k, templar.template(trust_as_template(v))) for k, v in add["ansible.builtin.add_host"].items()
                    if k != "groups")
        out[address] = dict((k, v) for k, v in args.items() if v != "__omit__")
    return out


def test_found_name_of_an_inventory_host_not_taken():
    # a name the inventory has already: the address
    assert _added({}, "10.0.0.1  web1\n", known=["web1"], usable=["web1"])["10.0.0.1"]["name"] == "10.0.0.1"
    assert _added({}, "10.0.0.1  web1\n", usable=["web1"])["10.0.0.1"]["name"] == "web1"


def test_found_nodes_reached_as_the_given_one():
    # -i node2, with an ssh config for the names only: the found nodes get the name the given node knows them by,
    # its connection variables, and no address to connect to
    given = {"ansible_user": "ops", "ansible_port": 2222, "ansible_ssh_private_key_file": "~/.ssh/ops",
             "ansible_ssh_common_args": "-o ProxyJump=bastion", "ansible_become_method": "su",
             "ansible_become_password": "pw"}
    added = _added(given, "10.0.0.1  node1.example.org node1\n10.0.0.3  node3\n")
    assert dict((k, str(v)) for k, v in added["10.0.0.1"].items()) == dict(
        (k, str(v)) for k, v in dict(given, name="node1", import_cluster_address="10.0.0.1").items())
    assert added["10.0.0.3"]["name"] == "node3" and "ansible_host" not in added["10.0.0.3"]
    # the given node reached at an address of its own: the found ones at theirs
    added = _added(dict(given, ansible_host="10.0.0.2"), "10.0.0.1  node1\n")
    assert added["10.0.0.1"]["name"] == "node1" and added["10.0.0.1"]["ansible_host"] == "10.0.0.1"
    # no name known on the given node: the address, reached there
    assert added["10.0.0.3"]["name"] == "10.0.0.3" and added["10.0.0.3"]["ansible_host"] == "10.0.0.3"
    # a name the controller cannot reach (no address, no ssh config entry): the address, as before
    added = _added(given, "10.0.0.1  node1\n10.0.0.3  node3\n", usable=["node3"])
    assert added["10.0.0.1"]["name"] == "10.0.0.1" and added["10.0.0.1"]["ansible_host"] == "10.0.0.1"
    assert added["10.0.0.3"]["name"] == "node3" and "ansible_host" not in added["10.0.0.3"]


def test_found_nodes_by_name_with_a_container_connection():
    # docker reaches a container by its name: the names, even unknown on the controller, never an address
    given = {"ansible_connection": "community.docker.docker", "ansible_host": "cassandra-node2"}
    added = _added(given, "10.0.0.1  node1.net node1\n", given="node2", usable=[])
    assert added["10.0.0.1"] == {"name": "node1", "import_cluster_address": "10.0.0.1",
                                 "ansible_connection": "community.docker.docker"}
    # a local connection (the import run on a node) is not the found nodes': theirs by default
    added = _added({"ansible_connection": "local", "ansible_host": "10.0.0.2"}, "10.0.0.1  node1\n")
    assert "ansible_connection" not in added["10.0.0.1"] and added["10.0.0.1"]["ansible_host"] == "10.0.0.1"


@pytest.mark.parametrize("ssh_g, resolves, usable", [
    ("hostname node1\n", False, False),                         # nothing knows it
    ("hostname node1\n", True, True),                           # the controller resolves it
    ("hostname 10.0.0.1\n", False, True),                       # an ssh config HostName
    ("hostname node1\nproxyjump bastion\n", False, True),       # a jump host, no HostName
    ("hostname node1\nproxycommand ssh -W %h:%p b\n", False, True),
    ("hostname nODE1\n", False, False),                         # ssh -G lower-cases: the same name
])
def test_controller_check_of_the_names(tmp_path, ssh_g, resolves, usable):
    check = task("Check the controller can reach them by these names (an address, or an ssh config entry)")
    assert check["delegate_to"] == "localhost" and check["vars"]["ansible_connection"] == "local"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text("#!/bin/sh\nprintf '%s' \"$SSH_G\"\n")
    (bin_dir / "getent").write_text("#!/bin/sh\n[ \"$RESOLVES\" = 1 ]\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"PATH": "%s:/usr/bin:/bin" % bin_dir, "SSH_G": ssh_g, "RESOLVES": "1" if resolves else "0",
           "IMPORT_CLUSTER_NAMES": "nODE1"}
    out = subprocess.run(["/bin/sh", "-c", check["ansible.builtin.shell"]], env=env, capture_output=True, text=True,
                         check=True).stdout.split()
    assert out == (["nODE1"] if usable else [])


def test_found_node_matched_at_its_public_address():
    # the ring address a NAT or broadcast address on no interface: the node reached at it (ansible_host) is it
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    match = next(t for t in next(t for t in write["tasks"] if t.get("name") == "Build the inventory")["block"]
                 if t["name"] == "Match the ring with the hosts")
    hostvars = {"node1": {"ansible_host": "54.0.0.1", "ansible_facts": {"all_ipv4_addresses": ["10.0.0.1"]}},
                "node2": {"ansible_host": "node2.example.org", "import_cluster_address": "54.0.0.2",
                          "ansible_facts": {"all_ipv4_addresses": ["10.9.9.9"]}}}
    variables = {"_given": ["node1"], "groups": {"import_cluster_found": ["node2"]}, "hostvars": hostvars,
                 "item": {"address": "54.0.0.1"}}
    assert Templar(loader=DataLoader(), variables=variables).template(trust_as_template(match["vars"]["_host"])) == "node1"
    # a found node reached by name whose machine does not have the address: not it
    variables["item"] = {"address": "54.0.0.2"}
    assert Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(match["vars"]["_host"])) == "54.0.0.2"
