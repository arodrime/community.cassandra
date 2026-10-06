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
MATCH = TASKS["Match the ring with the hosts"]["vars"]


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_key_paths_read_from_the_files():
    # the loop as Ansible renders it (a backslash doubled in the YAML would reach the regex as two)
    loop = TASKS["Read their mode and the signing keys they name"]["loop"]
    text = "[x]\ngpgkey = file:///etc/pki/rpm-gpg/k.asc\nSigned-By: /etc/apt/keyrings/k.asc\n"
    slurp = {"results": [{"item": "/etc/yum.repos.d/cassandra-50x.repo", "content": base64.b64encode(text.encode()).decode()}]}
    assert render(loop, import_cluster_repo_slurp=slurp) == [
        "/etc/yum.repos.d/cassandra-50x.repo", "/etc/pki/rpm-gpg/k.asc", "/etc/apt/keyrings/k.asc"]


def test_verdict_written():
    keep = MATCH["_node"]["keep"]
    left = {"manage": False, "vars": {}, "why": "x"}
    taken = {"manage": True, "vars": {"cassandra_install_url": "https://m/"}, "why": ""}
    hv = {"import_cluster_keep": {}, "import_cluster_medusa": {}}
    assert render(keep, _hv=hv, _read=True, _env_log_dir={}, _java_link={}, _hand_kept={}, _repo=left)["cassandra_repository_manage"] is False
    assert "cassandra_repository_manage" not in render(keep, _hv=hv, _read=True, _env_log_dir={}, _java_link={}, _hand_kept={}, _repo=taken)
    pkg = render(MATCH["_pkg"], _hv={"import_cluster_package": ""}, _read=True, _installed={}, _repo=taken)
    assert pkg == {"cassandra_install_url": "https://m/"}


def test_apt_credentials_removed_only_with_the_mark():
    with open(os.path.join(ROOT, "roles", "cassandra_repository", "tasks", "remove_apt_credentials.yml")) as f:
        steps = yaml.safe_load(f)
    assert "Managed by Ansible (community.cassandra.cassandra_repository)" in steps[0]["ansible.builtin.command"]["argv"]
    assert steps[1]["when"] == "_cassandra_repository_auth_mark.rc == 0"
    for name in ("repository.yml", "no_repository.yml"):
        with open(os.path.join(ROOT, "roles", "cassandra_repository", "tasks", name)) as f:
            text = f.read()
        assert "auth.conf.d/cassandra.conf\n    state: absent" not in text and "'/etc/apt/auth.conf.d/cassandra.conf'" not in text
