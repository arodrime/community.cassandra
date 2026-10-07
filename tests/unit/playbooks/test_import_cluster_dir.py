from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: the output dir is made absolute on the controller, from the
# dir the playbook was run from; the modules writing the files run elsewhere.

import base64
import os

import pytest
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

PICK = [t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == "Pick the output dirs"][0]


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def pick(**variables):
    return picked(**variables)["_dir"]


def picked(**variables):
    variables["_layout"] = {"cluster_group": "prod"}
    variables["_shared"] = variables.get("import_cluster_shared_dir", False)
    variables["_given_dir"] = render(PICK["vars"]["_given_dir"], **variables)
    variables["_given_report_dir"] = render(PICK["vars"]["_given_report_dir"], **variables)
    return dict((k, render(v, **variables)) for k, v in PICK["ansible.builtin.set_fact"].items())


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


def test_report_dir(tmp_path, monkeypatch):
    """With the inventory in its own dir by default; for a shared dir, ./reports/<cluster group>."""
    monkeypatch.chdir(tmp_path)
    assert picked(import_cluster_dir="inv/prod")["_report_dir"] == str(tmp_path.resolve() / "inv" / "prod")
    assert picked()["_report_dir"] == str(tmp_path.resolve() / "prod")
    assert picked(import_cluster_dir="inv", import_cluster_shared_dir=True) == {
        "_dir": str(tmp_path.resolve() / "inv"), "_report_dir": str(tmp_path.resolve() / "reports" / "prod")}
    assert picked(import_cluster_dir="inv", import_cluster_report_dir="~/r")["_report_dir"] == os.path.expanduser("~/r")
    assert picked(import_cluster_dir="inv", import_cluster_shared_dir=True,
                  import_cluster_report_dir="out/r")["_report_dir"] == str(tmp_path.resolve() / "out" / "r")


def test_absolute_kept(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert pick(import_cluster_dir="/srv/inv/../X") == "/srv/inv/../X"


def test_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir("/")
    assert pick(import_cluster_dir="~/inventories/X") == str(tmp_path / "inventories" / "X")


TASKS = dict((t.get("name"), t) for play in PLAYS for t in play.get("tasks", []) + [
    sub for task in play.get("tasks", []) for sub in task.get("block", [])])
WRITE_VARS = next(p for p in PLAYS if p.get("name") == "Write the inventory")["vars"]


def secret(tmp_path, monkeypatch, **variables):
    """The vault password as the play keeps it, and the secrets.yml content written with it."""
    monkeypatch.chdir(tmp_path)
    loader = DataLoader()
    loader.set_basedir(os.path.dirname(PLAYBOOK))
    variables = dict({"import_cluster_vault_stat": {"stat": {"executable": False}}}, **variables)
    variables["_vault_given"] = render(WRITE_VARS["_vault_given"], **variables)
    variables["_vault_file"] = render(WRITE_VARS["_vault_file"], **variables)
    variables["_vault"] = variables["_vault_file"] != ""
    keep = TASKS["Keep the vault password"]["ansible.builtin.set_fact"]["_vault_secret"]
    if variables["_vault"]:
        variables["_vault_secret"] = Templar(loader=loader, variables=variables).template(trust_as_template(keep))
    variables["item"] = {"content": "a: b\n", "secret": True}
    variables["_layout"] = {"cluster_group": "prod"}
    write = TASKS["Write group_vars and host_vars"]["ansible.builtin.copy"]["content"]
    return variables, str(Templar(loader=loader, variables=variables).template(trust_as_template(write)))


def test_vault_password_file_from_the_current_dir(tmp_path, monkeypatch):
    """Not from the playbook's dir, where lookup('file') looks for a relative path."""
    (tmp_path / "vp.txt").write_text("secret\n")
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", None)
    variables, content = secret(tmp_path, monkeypatch, import_cluster_vault_password_file="./vp.txt")
    assert variables["_vault_secret"] == "secret"
    assert variables["_vault_file"] == str(tmp_path.resolve() / "vp.txt")  # the modules get an absolute path
    assert content.startswith("$ANSIBLE_VAULT;")


def test_vault_password_file_of_ansible(tmp_path, monkeypatch):
    """No option: Ansible's own vault password file (ansible.cfg, ANSIBLE_VAULT_PASSWORD_FILE)."""
    (tmp_path / "vp.txt").write_text("secret\n")
    # ansible.cfg or ANSIBLE_VAULT_PASSWORD_FILE, as Ansible read them when it started
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", str(tmp_path / "vp.txt"))
    # (lookup('config') reads the config manager from ansible-core 2.19: the variable, as a run would have it)
    monkeypatch.setenv("ANSIBLE_VAULT_PASSWORD_FILE", str(tmp_path / "vp.txt"))
    variables, content = secret(tmp_path, monkeypatch)
    assert variables["_vault_file"] == str(tmp_path / "vp.txt")
    assert content.startswith("$ANSIBLE_VAULT;")


def test_no_vault_password_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", None)
    variables, content = secret(tmp_path, monkeypatch)
    assert variables["_vault"] is False
    assert content.startswith(GENERATED % "prod" + "\na: b")


def test_vault_password_script(tmp_path, monkeypatch):
    """An executable password file is run, its output is the password."""
    monkeypatch.setattr(C, "DEFAULT_VAULT_PASSWORD_FILE", None)
    variables, content = secret(tmp_path, monkeypatch, import_cluster_vault_password_file="/bin/script",
                                import_cluster_vault_stat={"stat": {"executable": True}},
                                import_cluster_vault_script={"stdout": "secret"})
    assert variables["_vault_secret"] == "secret"
    assert content.startswith("$ANSIBLE_VAULT;")


def test_every_file_written_is_marked():
    """hosts.yml, the vars files and report.txt start with the line that tells a re-import they are its own."""
    for name in ("Write the hosts file", "Write group_vars and host_vars", "Write report.txt"):
        assert "community.cassandra.cassandra_inventory_generated(_layout.cluster_group)" in str(TASKS[name]), name


@pytest.mark.parametrize("path, client", [("/k/vault-client", True), ("/k/vault-client.py", True),
                                          ("/k/vault.sh", False), ("/k/client-vault", False)])
def test_client_script_gets_the_vault_id(path, client):
    argv = render(TASKS["Run the vault password script"]["ansible.builtin.command"]["argv"], _vault_file=path,
                  import_cluster_vault_id="prod")
    assert argv == ([path, "--vault-id", "prod"] if client else [path])


def test_files_in_dot_dirs_are_not_looked_at():
    found = render(TASKS["Sort out the files an earlier import wrote"]["vars"]["_found"], _dir="/inv",
                   import_cluster_existing={"files": [{"path": "/inv/.git/HEAD"}, {"path": "/inv/host_vars/.x/main.yml"},
                                                      {"path": "/inv/notes"}, {"path": "/inv/host_vars/n1/main.yml"}]})
    assert found == ["notes", "host_vars/n1/main.yml"]


def test_no_clear_secrets_over_a_vaulted_file():
    task = TASKS["Stop rather than write in clear over a vaulted file"]
    vaulted = base64.b64encode(b"$ANSIBLE_VAULT;1.1;AES256\n00").decode()
    clear = base64.b64encode(b"a: 1\n").decode()
    variables = {"_files": [{"path": "group_vars/p/secrets.yml", "secret": True},
                            {"path": "group_vars/p/main.yml", "secret": False}],
                 "import_cluster_existing_vars": {"results": [
                     {"item": "group_vars/p/secrets.yml", "content": vaulted},
                     {"item": "group_vars/p/main.yml", "content": vaulted},     # not a secret path: not guarded
                     {"item": "host_vars/x/secrets.yml", "content": vaulted}]}}  # not written: not guarded
    assert render(task["vars"]["_vaulted"], **variables) == ["group_vars/p/secrets.yml"]
    variables["import_cluster_existing_vars"]["results"][0]["content"] = clear
    assert render(task["vars"]["_vaulted"], **variables) == []


def write_vars(shared, **given):
    variables = {"_layout": {"cluster_group": "cluster_a"}, "import_cluster_shared_dir": shared, "_dir": "/inv",
                 "_report_dir": "/inv"}
    variables.update(given)
    for name in ("_shared", "_hosts_file", "_inventory_args"):
        variables[name] = render(WRITE_VARS[name], **variables)
    return variables


def test_shared_dir_names_the_hosts_file_after_the_cluster():
    assert write_vars(False)["_hosts_file"] == "hosts.yml"
    assert write_vars(True)["_hosts_file"] == "cluster_a.yml"
    assert write_vars(False)["_inventory_args"] == "-i /inv/hosts.yml"
    assert write_vars(True)["_inventory_args"] == "-i /inv -e cassandra_hosts=cluster_a"


@pytest.mark.parametrize("shared, force, dir_exists, hosts_exists, passed", [
    (False, False, False, None, True),
    (False, False, True, None, False),
    (False, True, True, None, True),
    (True, False, True, False, True),    # another cluster's dir: this cluster not there yet
    (True, False, True, True, False),    # this cluster there already
    (True, True, True, True, True),
])
def test_stop_rather_than_overwrite(shared, force, dir_exists, hosts_exists, passed):
    task = TASKS["Stop rather than overwrite an inventory"]["ansible.builtin.assert"]
    variables = write_vars(shared, import_cluster_force=force, import_cluster_dir_stat={"stat": {"exists": dir_exists}})
    if hosts_exists is not None:
        variables["import_cluster_hosts_stat"] = {"stat": {"exists": hosts_exists}}
    else:
        variables["import_cluster_hosts_stat"] = {"skipped": True}
    assert render("{{ %s }}" % task["that"], **variables) is passed
    msg = render(task["fail_msg"], **variables)
    assert msg.startswith("/inv/cluster_a.yml exists: this cluster is imported there already" if shared else "/inv exists: pick")


def test_files_read_and_written():
    found = ["cluster_a.yml", "cluster_b.yml", "hosts.yml", "report.txt", "notes.yml", "group_vars/all/main.yml",
             "group_vars/cluster_b/main.yml", "host_vars/n1/secrets.yml", "sub/x.yml"]
    files = [{"path": "group_vars/cluster_a/main.yml"}]
    task = TASKS["Read the group_vars and host_vars files already there"]
    found += ["hosts", "mine.yaml", "README.md", "cluster_a.yml.2026-10-07@10:00:00~", "x.retry"]
    for shared, read in ((False, ["cluster_a.yml", "cluster_b.yml", "hosts.yml", "report.txt", "notes.yml",
                                  "group_vars/all/main.yml", "group_vars/cluster_b/main.yml", "host_vars/n1/secrets.yml"]),
                         (True, ["cluster_a.yml", "cluster_b.yml", "hosts.yml", "report.txt", "notes.yml",
                                 "group_vars/all/main.yml", "group_vars/cluster_b/main.yml", "host_vars/n1/secrets.yml",
                                 "sub/x.yml", "hosts", "mine.yaml"])):
        variables = write_vars(shared, _found=found)
        variables["_top"] = render(task["vars"]["_top"], **variables)
        assert render(task["loop"], **variables) == read
    written = TASKS["Sort out the files an earlier import wrote"]["vars"]["_written"]
    assert render(written, _files=files, **write_vars(False)) == ["hosts.yml", "report.txt", "group_vars/cluster_a/main.yml"]
    variables = write_vars(True, _report_dir="/reports/cluster_a")
    assert render(written, _files=files, **variables) == ["cluster_a.yml", "group_vars/cluster_a/main.yml"]


def test_shared_dir_needs_the_dir():
    task = [t for play in PLAYS for t in play.get("tasks", []) if t.get("name") == "Check the shared dir is given"][0]
    that = "{{ %s }}" % task["ansible.builtin.assert"]["that"]
    assert render(that, import_cluster_shared_dir=True) is False
    assert render(that, import_cluster_shared_dir=True, import_cluster_dir="") is False
    assert render(that, import_cluster_shared_dir=True, import_cluster_dir="inventories") is True
    assert render(that) is True


def test_stop_on_another_clusters_files():
    task = TASKS["Stop rather than write over another cluster's files"]["ansible.builtin.assert"]
    conflicts = {"_leftovers": {"conflicts": ["node2: in cluster_b.yml too"]}, "_dir": "/inv"}
    assert render("{{ %s }}" % task["that"], **conflicts) is False
    assert render(task["fail_msg"], **conflicts).startswith("node2: in cluster_b.yml too. Nothing written: in /inv,")
    assert render("{{ %s }}" % task["that"], _leftovers={"conflicts": []}, _dir="/inv") is True
    # before any file is removed or written
    names = [t.get("name") for play in PLAYS for t in play.get("tasks", [])]
    assert names.index("Stop rather than write over another cluster's files") < names.index(
        "Remove the files an earlier import wrote and this one does not")


@pytest.mark.parametrize("report_dir, line", [("/inv", "# NOT VALID: the import self-check failed, see report.txt"),
                                              ("/reports/cluster_a", "# NOT VALID: the import self-check failed,"
                                                                     " see ../reports/cluster_a/report.txt")])
def test_invalid_hosts_file_points_to_the_report(report_dir, line):
    variables = write_vars(True, _report_dir=report_dir, _self_check_ok=False)
    variables["_layout"]["hosts"] = {"all": {}}
    content = render(TASKS["Write the hosts file"]["ansible.builtin.copy"]["content"], **variables)
    assert content.split("\n")[1] == line


def test_report_of_another_kept(tmp_path):
    task = TASKS["Write report.txt"]
    report = tmp_path / "report.txt"
    variables = write_vars(True, _report_dir=str(tmp_path))
    assert render(task["ansible.builtin.copy"]["backup"], **variables) is False  # none there yet
    for text, backup in (("# Written by community.cassandra.import_cluster for cluster_a: x\n", False),
                         ("# Written by community.cassandra.import_cluster for cluster_ab: x\n", True),
                         ("my notes\n", True)):
        report.write_text(text)
        assert render(task["ansible.builtin.copy"]["backup"], **variables) is backup, text
