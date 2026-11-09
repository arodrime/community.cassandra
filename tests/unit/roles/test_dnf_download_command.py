from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_install, RedHat with a Java home: the dnf download command is looked for with rpm first, dnf (which
# reads every enabled repository, an unreachable OS one failing it on an air-gapped site) only when it is missing,
# and its failure names the repositories dnf could not read.

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

PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_install", "tasks", "cassandra.yml")


def _find(tasks, name):
    todo = list(tasks)
    while todo:
        t = todo.pop(0)
        if t.get("name") == name and ("block" in t or "ansible.builtin.fail" in t or "ansible.builtin.command" in t):
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def test_dnf_only_when_the_download_command_is_missing():
    load_collection_filters()
    with open(PATH, encoding="utf-8") as f:
        tasks = yaml.safe_load(f)
    look = _find(tasks, "Look for the dnf download command")
    assert look["ansible.builtin.command"]["argv"] == ["rpm", "-q", "--whatprovides", "dnf-command(download)"]
    assert look["check_mode"] is False and look["failed_when"] is False
    install = _find(tasks, "Install the dnf download command")
    assert install["when"] == "cassandra_install_dnf_download.rc != 0"
    stop = _find(install["rescue"], "Stop where dnf could not install it")
    failed = {"failed": True, "msg": "Failed to download metadata for repo 'epel': Cannot download repomd.xml: Curl error (6):"
                                     " Couldn't resolve host name"}
    msg = Templar(loader=DataLoader(), variables={"ansible_failed_result": failed}).template(
        trust_as_template(stop["ansible.builtin.fail"]["msg"]))
    assert "dnf could not install it: repository 'epel' unreachable" in " ".join(msg.split())
