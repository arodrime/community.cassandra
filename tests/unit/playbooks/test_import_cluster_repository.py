from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the verdict on a node's Cassandra repository files goes to the
# inventory (its URL, credentials and key path, or cassandra_repository_manage:
# false), and cassandra_repository never removes an apt credentials file it
# did not write.

import base64
import os

import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.tests.unit.import_nodes import node

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
with open(os.path.join(ROOT, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def tasks(plays):
    todo = [t for p in plays for t in p.get("tasks", [])]
    while todo:
        t = todo.pop(0)
        yield t
        todo += t.get("block", []) + t.get("rescue", [])


TASKS = dict((t.get("name"), t) for t in tasks(PLAYS))


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_key_paths_read_from_the_files():
    # the loop as Ansible renders it (a backslash doubled in the YAML would reach the regex as two)
    loop = TASKS["Read the JMX users, the repository files' mode and their signing keys"]["vars"]["_repo"]
    text = "[x]\ngpgkey = file:///etc/pki/rpm-gpg/k.asc\nSigned-By: /etc/apt/keyrings/k.asc\n"
    slurp = {"results": [{"item": "/etc/yum.repos.d/cassandra-50x.repo", "content": base64.b64encode(text.encode()).decode()}]}
    assert render(loop, import_cluster_repo_slurp=slurp) == [
        "/etc/yum.repos.d/cassandra-50x.repo", "/etc/pki/rpm-gpg/k.asc", "/etc/apt/keyrings/k.asc"]


def test_verdict_written():
    left = {"manage": False, "vars": {}, "why": "x"}
    taken = {"manage": True, "vars": {"cassandra_install_url": "https://m/"}, "why": ""}
    out = node({"import_cluster_repo": left})
    assert out["keep"]["cassandra_repository_manage"] is False
    assert "Cassandra repository files left as they are: x" in out["notes"]
    out = node({"import_cluster_repo": taken, "import_cluster_package": ""})
    assert "cassandra_repository_manage" not in out["keep"]
    assert out["vars"] == {"cassandra_version": "41x", "cassandra_java_tarballs": {}, "cassandra_install_url": "https://m/"}


def test_apt_credentials_removed_only_with_the_mark():
    with open(os.path.join(ROOT, "roles", "cassandra_repository", "tasks", "remove_apt_credentials.yml")) as f:
        steps = yaml.safe_load(f)
    assert "Managed by Ansible (community.cassandra.cassandra_repository)" in steps[0]["ansible.builtin.command"]["argv"]
    assert steps[1]["when"] == "_cassandra_repository_auth_mark.rc == 0"
    for name in ("repository.yml", "no_repository.yml"):
        with open(os.path.join(ROOT, "roles", "cassandra_repository", "tasks", name)) as f:
            text = f.read()
        assert "auth.conf.d/cassandra.conf\n    state: absent" not in text and "'/etc/apt/auth.conf.d/cassandra.conf'" not in text


def test_a_mirror_url_your_files_give_is_not_written_again():
    # (owner list) the repository taken over gives the mirror's URL; your group_vars/all sets the same one (with
    # {{ cassandra_version }}): not written again in the cluster's main.yml (it would win over yours), and no TODO
    # asking for it
    from ansible_collections.community.cassandra.plugins.filter.cassandra_import import (
        cassandra_inventory_files, cassandra_inventory_layout_over)
    n = {"name": "n1", "address": "10.0.0.1", "hostname": "n1", "dc": "dc1", "rack": "r1", "read": True,
         "vars": {"cassandra_version": "41x", "cassandra_install_method": "packages",
                  "cassandra_install_url": "http://mirror/repo/{cassandra_version}/"},
         "hand_edits": [], "normalized": [], "notes": [], "keep": {}}
    yours = {"path": "group_vars/all/local.yml",
             "content": 'cassandra_install_url: "http://mirror/repo/{{ cassandra_version }}/"\n'}
    layout = cassandra_inventory_layout_over([n], "c", [yours])
    text = "\n".join(f["content"] for f in cassandra_inventory_files(layout))
    assert "cassandra_install_url:" not in text and "TODO: cassandra_install_url" not in text
    # another URL of yours: the node's written (it wins, listed), and still no TODO
    yours["content"] = 'cassandra_install_url: "http://other/repo/{{ cassandra_version }}/"\n'
    text = "\n".join(f["content"] for f in cassandra_inventory_files(cassandra_inventory_layout_over([n], "c", [yours])))
    assert "cassandra_install_url: http://mirror/repo/{{ cassandra_version }}/" in text
    assert "TODO: cassandra_install_url" not in text
