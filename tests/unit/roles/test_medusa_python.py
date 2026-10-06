from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_medusa's Python on a host whose python3 Medusa does not support:
# python3.11 on the RedHat family, and for a virtualenv on Debian/Ubuntu when a
# repository offers it (Ubuntu 26.04: the deadsnakes PPA). Under --check on a
# blank host the PPA is not there yet: python3.11 is assumed, not installed.

import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_medusa", "tasks", "main.yml")
CANDIDATE = "python3.11:\n  Installed: (none)\n  Candidate: 3.11.17-1+resolute1\n"
SKIPPED = {"skipped": True, "changed": False}


def block():
    with open(TASKS, encoding="utf-8") as f:
        return next(t for t in yaml.safe_load(f) if t.get("name") == "Prepare the Python")["block"]


def task(name):
    return next(t for t in block() if t.get("name") == name)


def pick(policy, check_mode=False, os_family="Debian", distribution="Ubuntu", fits=False, offline=False, venv="/opt/m",
         **overrides):
    variables = {
        "cassandra_medusa_python": "",
        "cassandra_medusa_venv": venv,
        "cassandra_offline": offline,
        "_cassandra_medusa_python3_fits": fits,
        "_cassandra_medusa_system_install": False,
        "ansible_facts": {"os_family": os_family, "distribution": distribution},
        "ansible_check_mode": check_mode,
        "cassandra_medusa_python311_policy": policy,
    }
    variables.update(overrides)
    out = {}
    for name in ["Tell whether python3.11 comes later (--check)", "Pick the Python", "Packages the Python needs"]:
        templar = Templar(loader=DataLoader(), variables=variables)
        for k, v in task(name)["ansible.builtin.set_fact"].items():
            variables[k] = out[k] = templar.template(trust_as_template(v))
    templar = Templar(loader=DataLoader(), variables=variables)
    out["install"] = all(templar.template(trust_as_template("{{ %s }}" % w))
                         for w in task("Install them")["when"])
    return out


def test_python3_fits():
    assert pick(SKIPPED, fits=True)["_cassandra_medusa_python"].strip() == "/usr/bin/python3"


def test_ubuntu_2604_python311_from_the_ppa():
    out = pick({"stdout": CANDIDATE})
    assert out["_cassandra_medusa_python"].strip() == "/usr/bin/python3.11"
    assert out["_cassandra_medusa_python_pending"] in (False, "False")


def test_ubuntu_2604_no_python311_stops():
    assert pick({"stdout": ""})["_cassandra_medusa_python"].strip() == ""


def test_ubuntu_2604_check_mode_before_the_ppa_assumes_python311():
    out = pick({"stdout": ""}, check_mode=True)
    assert out["_cassandra_medusa_python"].strip() == "/usr/bin/python3.11"
    assert out["_cassandra_medusa_python_pending"] in (True, "True")
    assert out["install"] is False  # apt can't resolve python3.11-venv yet


def test_ubuntu_2604_virtualenv_packages():
    out = pick({"stdout": CANDIDATE})
    assert list(out["_cassandra_medusa_packages"]) == ["python3.11-venv"]
    assert out["install"] is True


def test_debian_check_mode_no_ppa_stops_as_the_real_run():
    out = pick({"stdout": ""}, check_mode=True, distribution="Debian")
    assert out["_cassandra_medusa_python"].strip() == ""


def test_check_mode_no_ppa_coming_stops_as_the_real_run():
    # cassandra_install adds the PPA only to install python3.11 for cqlsh
    for overrides in ({"cassandra_cqlsh_python_repo_uri": ""}, {"cassandra_cqlsh_python_manage": False},
                      {"cassandra_cqlsh_python": "/usr/local/bin/python3.12"}):
        assert pick({"stdout": ""}, check_mode=True, **overrides)["_cassandra_medusa_python"].strip() == "", overrides


def test_offline_check_mode_no_ppa_stops_as_the_real_run():
    out = pick({"stdout": ""}, check_mode=True, offline=True)
    assert out["_cassandra_medusa_python"].strip() == ""


def test_no_virtualenv_never_pending():
    out = pick(SKIPPED, check_mode=True)  # the lookup runs for a virtualenv only
    assert out["_cassandra_medusa_python"].strip() == ""
    assert out["_cassandra_medusa_python_pending"] in (False, "False")


def test_redhat_python311():
    assert pick(SKIPPED, os_family="RedHat")["_cassandra_medusa_python"].strip() == "/usr/bin/python3.11"
