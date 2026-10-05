from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the output dir is made absolute on the controller, from the
# dir the playbook was run from; the modules writing the files run elsewhere.

import os

import yaml

from ansible import constants as C
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import GENERATED

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


TASKS = dict((t.get("name"), t) for play in PLAYS for t in play.get("tasks", []) + [
    sub for task in play.get("tasks", []) for sub in task.get("block", [])])
WRITE_VARS = PLAYS[-1]["vars"]


def vault_file(**variables):
    return render(WRITE_VARS["_vault_file"], **variables)


def secret(tmp_path, monkeypatch, **variables):
    """The vault password as the play keeps it, and the secrets.yml content written with it."""
    monkeypatch.chdir(tmp_path)
    loader = DataLoader()
    loader.set_basedir(os.path.dirname(PLAYBOOK))
    variables = dict({"import_cluster_vault_stat": {"stat": {"executable": False}}}, **variables)
    variables["_vault_file"] = render(WRITE_VARS["_vault_file"], **variables)
    variables["_vault"] = variables["_vault_file"] != ""
    keep = TASKS["Keep the vault password"]["ansible.builtin.set_fact"]["_vault_secret"]
    if variables["_vault"]:
        variables["_vault_secret"] = Templar(loader=loader, variables=variables).template(trust_as_template(keep))
    variables["item"] = {"content": "a: b\n", "secret": True}
    write = TASKS["Write group_vars and host_vars"]["ansible.builtin.copy"]["content"]
    return variables, str(Templar(loader=loader, variables=variables).template(trust_as_template(write)))


def test_vault_password_file_from_the_current_dir(tmp_path, monkeypatch):
    """Not from the playbook's dir, where lookup('file') looks for a relative path."""
    (tmp_path / "vp.txt").write_text("secret\n")
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", None)
    variables, content = secret(tmp_path, monkeypatch, import_cluster_vault_password_file="./vp.txt")
    assert variables["_vault_secret"] == "secret"
    assert content.startswith("$ANSIBLE_VAULT;")


def test_vault_password_file_of_ansible(tmp_path, monkeypatch):
    """No option: Ansible's own vault password file (ansible.cfg, ANSIBLE_VAULT_PASSWORD_FILE)."""
    (tmp_path / "vp.txt").write_text("secret\n")
    # ansible.cfg or ANSIBLE_VAULT_PASSWORD_FILE, as Ansible read them when it started
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", str(tmp_path / "vp.txt"))
    variables, content = secret(tmp_path, monkeypatch)
    assert variables["_vault_file"] == str(tmp_path / "vp.txt")
    assert content.startswith("$ANSIBLE_VAULT;")


def test_no_vault_password_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", None)
    variables, content = secret(tmp_path, monkeypatch)
    assert variables["_vault"] is False
    assert content.startswith(GENERATED + "\na: b")


def test_vault_password_script(tmp_path, monkeypatch):
    """An executable password file is run, its output is the password."""
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", None)
    variables, content = secret(tmp_path, monkeypatch, import_cluster_vault_password_file="/bin/script",
                                import_cluster_vault_stat={"stat": {"executable": True}},
                                import_cluster_vault_script={"stdout": "secret\n"})
    assert variables["_vault_secret"] == "secret"
    assert content.startswith("$ANSIBLE_VAULT;")


def test_every_file_written_is_marked():
    """hosts.yml, the vars files and report.txt start with the line that tells a re-import they are its own."""
    for name in ("Write hosts.yml", "Write group_vars and host_vars", "Write report.txt"):
        assert "community.cassandra.cassandra_inventory_generated" in str(TASKS[name]), name
