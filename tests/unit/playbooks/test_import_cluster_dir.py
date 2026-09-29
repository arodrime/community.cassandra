from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the output dir is made absolute on the controller, from the
# dir the playbook was run from; the modules writing the files run elsewhere.

import os

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

PICK = [t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == "Pick the output dir"][0]


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def pick(**variables):
    variables["_layout"] = {"cluster_group": "prod"}
    variables["_given_dir"] = render(PICK["vars"]["_given_dir"], **variables)
    return render(PICK["ansible.builtin.set_fact"]["_dir"], **variables)


def test_relative_from_the_current_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", "/elsewhere")
    assert pick(import_cluster_dir="./inventories/X") == str(tmp_path.resolve() / "inventories" / "X")
    assert pick(import_cluster_dir="inventories/X/") == str(tmp_path.resolve() / "inventories" / "X")
    assert pick(import_cluster_dir="../X") == str(tmp_path.resolve().parent / "X")


def test_default_in_the_current_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pick() == str(tmp_path.resolve() / "prod")
    assert pick(import_cluster_dir="") == str(tmp_path.resolve() / "prod")


def test_reached_through_a_symlink(tmp_path, monkeypatch):
    (tmp_path / "data" / "project").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "project").symlink_to(tmp_path / "data" / "project")
    monkeypatch.chdir(tmp_path / "home" / "project")
    monkeypatch.setenv("PWD", str(tmp_path / "home" / "project"))
    assert pick(import_cluster_dir="inventories/X") == str(tmp_path.resolve() / "data" / "project" / "inventories" / "X")


def test_absolute_kept(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pick(import_cluster_dir="/srv/inv/../X") == "/srv/inv/../X"


def test_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir("/")
    assert pick(import_cluster_dir="~/inventories/X") == str(tmp_path / "inventories" / "X")


def test_vault_password_file_from_the_current_dir(tmp_path, monkeypatch):
    """Not from the playbook's dir, where lookup('file') looks for a relative path."""
    write = [t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == "Write group_vars and host_vars"][0]
    (tmp_path / "vp.txt").write_text("secret\n")
    monkeypatch.chdir(tmp_path)
    loader = DataLoader()
    loader.set_basedir(os.path.dirname(PLAYBOOK))
    content = Templar(loader=loader, variables={
        "item": {"content": "a: b\n", "secret": True}, "_vault": True,
        "import_cluster_vault_password_file": "./vp.txt",
    }).template(trust_as_template(write["ansible.builtin.copy"]["content"]))
    assert str(content).startswith("$ANSIBLE_VAULT;")
