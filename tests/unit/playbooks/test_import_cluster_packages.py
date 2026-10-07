from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: what cassandra_install adds next to Cassandra (cassandra-tools,
# jemalloc, the Debian hold) is left out where the node has none, and the
# inventory's cassandra_packages is what the role installs.

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

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(ROOT, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name):
    todo = [t for play in PLAYS for t in play.get("tasks", [])]
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", [])
    raise KeyError(name)


MATCH = task("Match the ring with the hosts")["vars"]


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def installed(packages, os_family="RedHat", held=None, read=True, package="5.0.7", dsbulk="/usr/share/dsbulk-1.11.2/bin/dsbulk"):
    hv = {"ansible_facts": {"packages": {p: [{"version": "1"}] for p in packages}, "os_family": os_family},
          "import_cluster_package": package, "import_cluster_held": held or [], "import_cluster_dsbulk": dsbulk}
    out = render(MATCH["_installed"], _hv=hv, _read=read)
    if dsbulk == "/usr/share/dsbulk-1.11.2/bin/dsbulk":
        assert out.pop("cassandra_dsbulk_version", None) == ("1.11.2" if read else None)
    return out


@pytest.mark.parametrize("dsbulk, expected", [
    # where cassandra_install puts it: that version is kept
    ("/usr/share/dsbulk-1.11.1/bin/dsbulk", {"cassandra_dsbulk_version": "1.11.1"}),
    # none, or installed another way (the role would refuse to replace it): left as it is
    ("", {"cassandra_dsbulk_install": False}),
    ("/opt/dsbulk-1.11.2/bin/dsbulk", {"cassandra_dsbulk_install": False}),
])
def test_dsbulk_kept_as_the_node_has_it(dsbulk, expected):
    assert installed(["cassandra", "cassandra-tools", "jemalloc"], dsbulk=dsbulk) == expected
    # whatever the install method
    assert installed([], package="", dsbulk=dsbulk) == expected


def test_everything_there():
    assert installed(["cassandra", "cassandra-tools", "jemalloc"]) == {}
    assert installed(["cassandra", "cassandra-tools", "libjemalloc2"], "Debian", ["cassandra", "cassandra-tools"]) == {}


def test_rpm_files_without_tools_or_jemalloc():
    assert installed(["cassandra"]) == {"cassandra_install_tools": False, "cassandra_install_jemalloc": False}


@pytest.mark.parametrize("held, expected", [
    ([], {"cassandra_package_hold": False}),
    (["cassandra"], {"cassandra_package_hold": False}),  # tools would get a hold
    (["cassandra", "cassandra-tools"], {}),
])
def test_debian_hold(held, expected):
    assert installed(["cassandra", "cassandra-tools", "libjemalloc2"], "Debian", held) == expected


def test_jemalloc_the_role_installs():
    # libjemalloc1 is not the libjemalloc2 the role would install
    assert installed(["cassandra", "cassandra-tools", "libjemalloc1"], "Debian", ["cassandra", "cassandra-tools"]) == {
        "cassandra_install_jemalloc": False}


def test_held_without_tools():
    assert installed(["cassandra", "libjemalloc2"], "Debian", ["cassandra"]) == {"cassandra_install_tools": False}


def test_not_read_or_not_a_package():
    assert installed(["cassandra"], read=False) == {}
    assert installed([], package="") == {}  # a tarball install: no package to keep company


def test_written_in_the_host_vars():
    # this node's: nodes added later get cassandra-tools, jemalloc and the hold
    hv = {"import_cluster_keep": {}, "import_cluster_medusa": {}}
    keep = render(MATCH["_node"]["keep"], _hv=hv, _read=True, _env_log_dir={}, _java_link={}, _hand_kept={},
                  _repo={"manage": True}, _installed={"cassandra_install_tools": False})
    assert keep == {"cassandra_firewall_manage": False, "cassandra_install_tools": False}
    assert "_installed" not in MATCH["_pkg"]


def test_role_installs_the_inventory_packages():
    # no OS vars file overrides cassandra_packages any more
    role = os.path.join(ROOT, "roles", "cassandra_install")
    assert not os.path.exists(os.path.join(role, "vars"))
    with open(os.path.join(role, "tasks", "install.yml")) as f:
        assert "include_vars" not in f.read()
    with open(os.path.join(role, "defaults", "main.yml")) as f:
        defaults = yaml.safe_load(f)
    for packages, tools, expected in [("cassandra", True, ["cassandra", "cassandra-tools"]),
                                      (["cassandra"], False, ["cassandra"]),
                                      (["cassandra", "cassandra-tools"], True, ["cassandra", "cassandra-tools"])]:
        assert render(defaults["_cassandra_packages"], cassandra_packages=packages, cassandra_install_tools=tools) == expected


def test_role_switches_gate_the_tasks():
    with open(os.path.join(ROOT, "roles", "cassandra_install", "tasks", "install.yml")) as f:
        tasks = yaml.safe_load(f)
    todo, found = list(tasks), {}
    while todo:
        t = todo.pop(0)
        found[t.get("name")] = t
        todo += t.get("block", []) + t.get("always", [])
    assert "cassandra_package_hold | bool" in found["Hold the pinned packages (Debian)"]["when"]
    for name in ["Install jemalloc", "Look up jemalloc in the enabled repos (RedHat)"]:
        assert "cassandra_install_jemalloc | bool" in found[name]["when"]
    assert found["Look up jemalloc in the enabled repos (RedHat)"]["failed_when"] is False


def test_dsbulk_read_only_as_the_role_installs_it():
    # its marker and both links: a dsbulk unpacked by hand under /usr/share is not the role's (it would relink it)
    script = task("Read the running Cassandra (conf dir, Cassandra and Java versions)")["ansible.builtin.shell"]
    line = next(i for i, x in enumerate(script.split("\n")) if "dsbulk=$dsb" in x)
    test = " ".join(script.split("\n")[line - 1:line + 1])
    assert "-L /usr/share/dsbulk" in test and ".cassandra_install" in test
