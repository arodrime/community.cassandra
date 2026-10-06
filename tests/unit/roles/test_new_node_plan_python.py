from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The new node checks (add_node, replace_node...) plan the Pythons cqlsh and
# Medusa need as the roles pick them: on Ubuntu 26.04 (python3 3.14), python3.11
# for cqlsh and for Medusa's virtualenv, from the deadsnakes PPA.

import json
import os

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

PLAN = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "templates",
                    "new_node_checks_plan.j2")


def plan(py3, distribution="Ubuntu", os_family="Debian", **overrides):
    variables = {
        "ansible_facts": {"distribution": distribution, "os_family": os_family, "python": {}, "python_version": "3"},
        "cassandra_new_node_python3": {"rc": 0, "stdout": py3},
        "cassandra_new_node_paths": {"results": []},
        "cassandra_new_node_realpath": {"stdout_lines": []},
        "cassandra_new_node_uid": {"stdout": "0"},
        "_dirs": [],
        "cassandra_offline": False,
        "cassandra_install_java": False,
        "_cassandra_java_home": "/opt/java",
        "cassandra_java_home": "/opt/java",
        "cassandra_java_package": "",
        "cassandra_java_tarball": "",
        "cassandra_install_method": "repository",
        "cassandra_install_url": "https://debian.cassandra.apache.org",
        "cassandra_install_package_file": "{name}_{version}_all.deb",
        "cassandra_packages": ["cassandra"],
        "cassandra_package_version": "",
        "cassandra_repository_manage": False,
        "cassandra_repository_key_url": "",
        "cassandra_version": "50x",
        "cassandra_cqlsh_python": "",
        "cassandra_cqlsh_python_manage": True,
        "cassandra_cqlsh_python_supported": {"50x": ["3.8", "3.13"]},
        "cassandra_cqlsh_python_repo_uri": "https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu",
        "cassandra_medusa_enabled": True,
        "cassandra_medusa_venv": "/opt/cassandra-medusa",
        "cassandra_medusa_python": "",
        "cassandra_medusa_python_supported": ["3.10", "3.12"],
        "cassandra_medusa_version": "0.30.1",
        "cassandra_medusa_pip_index_url": "",
        "cassandra_medusa_pip_extra_args": [],
        "cassandra_medusa_pip_cert": "",
        "cassandra_data_file_directories": ["/var/lib/cassandra/data"],
        "cassandra_commitlog_dir": "/var/lib/cassandra/commitlog",
        "cassandra_hints_dir": "/var/lib/cassandra/hints",
        "cassandra_saved_caches_dir": "/var/lib/cassandra/saved_caches",
        "cassandra_seeds": "",
        "cassandra_legacy_ssl_storage_port_enabled": False,
        "cassandra_internode_encryption": "none",
        "cassandra_start_native_transport": True,
        "cassandra_storage_port": 7000,
        "cassandra_ssl_storage_port": 7001,
        "cassandra_native_transport_port": 9042,
        "cassandra_jmx_port": 7199,
    }
    variables.update(overrides)
    with open(PLAN, encoding="utf-8") as f:
        text = f.read()
    return json.loads(Templar(loader=DataLoader(), variables=variables).template(trust_as_template(text)))


def needs(out):
    return {p["name"]: p for p in out["packages"]}


def test_ubuntu_2604_medusa_virtualenv_on_python311_from_deadsnakes():
    out = plan("3.14")
    assert out["problems"] == []
    assert needs(out)["python3.11"]["fallback"] == "the deadsnakes PPA"  # cqlsh
    venv = needs(out)["python3.11-venv"]
    assert venv["fallback"] == "the deadsnakes PPA" and venv["mode"] == "either"
    assert "Medusa virtualenv" in venv["why"]


def test_ubuntu_2604_medusa_virtualenv_needs_python311_from_the_repositories_without_the_ppa():
    venv = needs(plan("3.14", cassandra_cqlsh_python_repo_uri=""))["python3.11-venv"]
    assert venv["fallback"] == ""  # then a problem unless a repository has it


def test_ubuntu_2604_medusa_virtualenv_no_ppa_when_cqlsh_does_not_need_it():
    # cassandra_install adds the PPA only to install python3.11 for cqlsh
    for overrides in ({"cassandra_cqlsh_python": "/usr/local/bin/python3.12"}, {"cassandra_cqlsh_python_manage": False}):
        venv = needs(plan("3.14", cassandra_new_node_paths={"results": [
            {"item": "/usr/local/bin/python3.12", "stat": {"exists": True}}]}, **overrides))["python3.11-venv"]
        assert venv["fallback"] == "", overrides


def test_debian_medusa_virtualenv_python311_from_the_repositories():
    venv = needs(plan("3.13", distribution="Debian"))["python3.11-venv"]
    assert venv["fallback"] == ""  # no PPA on Debian: a repository must have it


def test_ubuntu_2604_python311_there_no_ppa():
    out = plan("3.14", cassandra_new_node_paths={"results": [{"item": "/usr/bin/python3.11", "stat": {"exists": True}}]})
    assert "python3.11" not in needs(out) and needs(out)["python3.11-venv"]["fallback"] == ""


def test_ubuntu_2604_medusa_without_virtualenv_is_a_problem():
    out = plan("3.14", cassandra_medusa_venv="")
    assert any(p.startswith("Medusa 0.30.1 runs on Python 3.10 to 3.12, python3 here is 3.14") for p in out["problems"])


def test_ubuntu_2404_medusa_virtualenv_on_python3():
    out = plan("3.12")
    assert out["problems"] == []
    assert "python3-venv" in needs(out) and "python3.11-venv" not in needs(out)
