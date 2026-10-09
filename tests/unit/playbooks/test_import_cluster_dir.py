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
    variables["_given_dir"] = render(PICK["vars"]["_given_dir"], **variables)
    variables["_abs_dir"] = render(PICK["vars"]["_abs_dir"], **variables)
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
    assert pick() == str(tmp_path.resolve() / "inventories")
    assert pick(import_cluster_dir="") == str(tmp_path.resolve() / "inventories")


def test_reached_through_a_symlink(tmp_path, monkeypatch):
    (tmp_path / "data" / "project").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "project").symlink_to(tmp_path / "data" / "project")
    monkeypatch.chdir(tmp_path / "home" / "project")
    monkeypatch.setenv("PWD", str(tmp_path / "home" / "project"))
    assert pick(import_cluster_dir="inventories/X") == str(tmp_path.resolve() / "data" / "project" / "inventories" / "X")


def test_report_dir(tmp_path, monkeypatch):
    """reports/<cluster group> next to the inventory dir, out of it."""
    monkeypatch.chdir(tmp_path)
    assert picked() == {"_dir": str(tmp_path.resolve() / "inventories"),
                        "_report_dir": str(tmp_path.resolve() / "reports" / "prod")}
    assert picked(import_cluster_dir="a/inv/")["_report_dir"] == str(tmp_path.resolve() / "a" / "reports" / "prod")
    assert picked(import_cluster_dir="/x/y/inventories/") == {"_dir": "/x/y/inventories", "_report_dir": "/x/y/reports/prod"}
    assert picked(import_cluster_dir="/inv")["_report_dir"] == "/reports/prod"
    assert picked(import_cluster_dir="/x/inv/.") == {"_dir": "/x/inv", "_report_dir": "/x/reports/prod"}
    assert picked(import_cluster_dir="/x/y/../inv")["_report_dir"] == "/x/y/../reports/prod"  # kept as given, like _dir
    (tmp_path / "data" / "inv").mkdir(parents=True)
    (tmp_path / "inventories").symlink_to(tmp_path / "data" / "inv")  # next to the dir as given, not its target
    assert picked()["_report_dir"] == str(tmp_path.resolve() / "reports" / "prod")
    assert picked(import_cluster_dir="/")["_report_dir"] == "/reports/prod"
    assert picked(import_cluster_dir="inv", import_cluster_report_dir="~/r")["_report_dir"] == os.path.expanduser("~/r")
    assert picked(import_cluster_dir="inv", import_cluster_report_dir="out/r")["_report_dir"] == str(tmp_path.resolve() / "out" / "r")


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
                   _report_files=["../reports/p/report.txt"],
                   import_cluster_existing={"files": [{"path": "/inv/.git/HEAD"}, {"path": "/inv/host_vars/.x/main.yml"},
                                                      {"path": "/inv/notes"}, {"path": "/inv/host_vars/n1/main.yml"}]})
    assert found == ["notes", "host_vars/n1/main.yml"]


def test_report_in_the_inventory_dir_not_a_leftover():
    """import_cluster_report_dir in the inventory dir: its report is not listed as kept, it is written again."""
    task = TASKS["Sort out the files an earlier import wrote"]["vars"]
    files = [{"path": "/inv/notes"}, {"path": "/inv/report.txt"}, {"path": "/inv/reports/p/RUNBOOK.md"}]
    for report_dir, left in (("/inv", ["notes", "reports/p/RUNBOOK.md"]),
                             ("/inv/reports/p", ["notes", "report.txt", "reports/p/RUNBOOK.md"]),
                             ("/reports/p", ["notes", "report.txt", "reports/p/RUNBOOK.md"])):
        variables = {"_dir": "/inv", "_report_dir": report_dir, "import_cluster_existing": {"files": files}}
        variables["_report_files"] = render(task["_report_files"], **variables)
        assert render(task["_found"], **variables) == left, report_dir


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


def write_vars(**given):
    variables = {"_layout": {"cluster_group": "cluster_a"}, "_dir": "/inv", "_report_dir": "/reports/cluster_a"}
    variables.update(given)
    for name in ("_hosts_file", "_next_hosts"):
        variables[name] = render(WRITE_VARS[name], **variables)
    return variables


def test_hosts_file_named_after_the_cluster():
    assert write_vars()["_hosts_file"] == "cluster_a.yml"


@pytest.mark.parametrize("files, hosts", [
    ([], ""),                                                                   # a first import: the cluster alone
    (["cluster_a.yml", "group_vars/all/x.yml", ".git/x", "notes.md", "a.yml~"], ""),  # no other inventory file
    (["cluster_a.yml", "cluster_b.yml"], "cluster_a"),                          # another cluster: -e cassandra_hosts
    (["web.ini"], "cluster_a"),
    (["cluster_a.yml", "lab/hosts.ini"], "cluster_a"),                      # Ansible reads subdirs too
    (["cluster_a.yml", "group_vars/cluster_b/main.yml", "host_vars/x/main.yml"], ""),
])
def test_next_commands_name_the_cluster_only_with_others(files, hosts):
    existing = {"files": [{"path": "/inv/" + f} for f in files]}
    assert write_vars(import_cluster_existing=existing)["_next_hosts"] == hosts


def test_next_commands_without_i_on_the_default_inventory():
    template = WRITE_VARS["_next_inventory"].replace(
        "lookup('ansible.builtin.config', 'DEFAULT_HOST_LIST')", "default_list")
    assert render(template, _dir="/p/inventories", default_list=["/p/inventories"]) == ""
    assert render(template, _dir="/p/inventories", default_list=["/etc/ansible/hosts"]) == "/p/inventories"
    # one of several sources: -i kept (the others may hold other clusters)
    assert render(template, _dir="/p/inventories", default_list=["/p/inventories", "/p/lab.ini"]) == "/p/inventories"


def test_user_files_counted_are_the_layouts():
    # the user's files at a path the import writes are replaced: left out of the self-check too
    keep = TASKS["Keep your files the import does not write over"]["ansible.builtin.set_fact"]["_user_files"]
    assert render(keep, _user_files=[{"path": "group_vars/c/main.yml"}, {"path": "group_vars/all/x.yml"}],
                  _layout={"user_paths": ["group_vars/all/x.yml"]}) == [{"path": "group_vars/all/x.yml"}]


def test_files_known_by_their_layout_backed_up_before_removal():
    names = [t.get("name") for play in PLAYS for t in play.get("tasks", [])]
    backup = TASKS["Keep a backup of the files it removes that do not say they are its"]
    assert names.index("Keep a backup of the files it removes that do not say they are its") < names.index(
        "Remove the files an earlier import wrote and this one does not")
    assert backup["loop"] == "{{ _leftovers.backup | default([]) }}" and backup["ansible.builtin.copy"]["remote_src"]


@pytest.mark.parametrize("force, hosts_exists, passed", [
    (False, False, True),    # a dir of other clusters, or none yet: this cluster not there yet
    (False, True, False),    # this cluster there already
    (True, True, True),
])
def test_stop_rather_than_overwrite(force, hosts_exists, passed):
    task = TASKS["Stop rather than overwrite an inventory"]["ansible.builtin.assert"]
    variables = write_vars(import_cluster_force=force, import_cluster_hosts_stat={"stat": {"exists": hosts_exists}})
    assert render("{{ %s }}" % task["that"], **variables) is passed
    assert render(task["fail_msg"], **variables).startswith(
        "/inv/cluster_a.yml exists: this cluster is imported there already, set import_cluster_force=true")


def test_files_read_and_written():
    """Read: every file Ansible reads as inventory (whose hosts, which groups), and the vars files (the user's
    own too: they apply to the hosts)."""
    found = ["cluster_a.yml", "cluster_b.yml", "hosts.yml", "report.txt", "notes.yml", "group_vars/all/main.yml",
             "group_vars/cluster_b/main.yml", "host_vars/n1/secrets.yml", "sub/x.yml", "hosts", "mine.yaml", "README.md",
             "cluster_a.yml.2026-10-07@10:00:00~", "x.retry", "group_vars/all/local.yml"]
    task = TASKS["Read the group_vars and host_vars files already there"]
    variables = write_vars(_found=found)
    variables["_top"] = render(task["vars"]["_top"], **variables)
    assert render(task["loop"], **variables) == [
        "cluster_a.yml", "cluster_b.yml", "hosts.yml", "notes.yml", "group_vars/all/main.yml",
        "group_vars/cluster_b/main.yml", "host_vars/n1/secrets.yml", "sub/x.yml", "hosts", "mine.yaml",
        "group_vars/all/local.yml"]
    written = TASKS["Sort out the files an earlier import wrote"]["vars"]["_written"]
    files = [{"path": "group_vars/cluster_a/main.yml"}]
    assert render(written, _files=files, **write_vars()) == ["cluster_a.yml", "group_vars/cluster_a/main.yml"]


def test_stop_on_another_clusters_files():
    task = TASKS["Stop rather than write over another cluster's files"]["ansible.builtin.assert"]
    conflicts = {"_leftovers": {"conflicts": ["node2: in cluster_b.yml too"]}, "_dir": "/inv"}
    assert render("{{ %s }}" % task["that"], **conflicts) is False
    assert render(task["fail_msg"], **conflicts).startswith("node2: in cluster_b.yml too. Nothing written: in /inv,")
    assert render("{{ %s }}" % task["that"], _leftovers={"conflicts": []}, _dir="/inv") is True
    assert "import_cluster_adopt" not in render(task["fail_msg"], **conflicts)
    # files of an earlier import not known to be this cluster's: the switch that takes them over
    unknown = "group_vars/cluster_a/main.yml: written by an earlier import, not known to be cluster_a's"
    msg = render(task["fail_msg"], _dir="/inv", _leftovers={"conflicts": [unknown], "adoptable": [unknown]})
    assert msg.endswith("If an earlier import of this cluster wrote them: -e import_cluster_adopt=true takes them"
                        " over, each file replaced kept as <file>.<date>~")
    msg = render(task["fail_msg"], _dir="/inv", _leftovers={"conflicts": [unknown, "x"], "adoptable": [unknown]})
    assert msg.endswith("-e import_cluster_adopt=true would take over 1 of them only")
    # before any file is removed or written
    names = [t.get("name") for play in PLAYS for t in play.get("tasks", [])]
    assert names.index("Stop rather than write over another cluster's files") < names.index(
        "Remove the files an earlier import wrote and this one does not")


@pytest.mark.parametrize("report_dir, line", [("/inv", "# NOT VALID: the import self-check failed, see report.txt"),
                                              ("/reports/cluster_a", "# NOT VALID: the import self-check failed,"
                                                                     " see ../reports/cluster_a/report.txt")])
def test_invalid_hosts_file_points_to_the_report(report_dir, line):
    variables = write_vars(_report_dir=report_dir, _self_check_ok=False)
    variables["_layout"]["hosts"] = {"all": {}}
    content = render(TASKS["Write the hosts file"]["ansible.builtin.copy"]["content"], **variables)
    assert content.split("\n")[1] == line


def test_report_of_another_kept(tmp_path):
    task = TASKS["Write report.txt"]
    report = tmp_path / "report.txt"
    variables = write_vars(_report_dir=str(tmp_path))
    assert render(task["ansible.builtin.copy"]["backup"], **variables) is False  # none there yet
    for text, backup in (("# Written by community.cassandra.import_cluster for cluster_a: x\n", False),
                         ("# Written by community.cassandra.import_cluster for cluster_ab: x\n", True),
                         ("my notes\n", True)):
        report.write_text(text)
        assert render(task["ansible.builtin.copy"]["backup"], **variables) is backup, text


def test_check_diff_writes_nothing():
    """--check --diff reviews a re-import: the tasks that write or remove run in check mode (their diff shown, the
    secrets hidden), the report is not written."""
    write = next(p for p in PLAYS if p.get("name") == "Write the inventory")["tasks"]
    for task in write:
        if any(k in task for k in ("ansible.builtin.copy", "ansible.builtin.file")):
            assert "check_mode" not in task and "diff" not in task, task["name"]
    assert TASKS["Write report.txt"]["when"] == "not ansible_check_mode"
    assert TASKS["Write group_vars and host_vars"]["no_log"] == "{{ item.secret }}"
    assert "check=_report_args.check | bool" in TASKS["Show the summary"]["ansible.builtin.debug"]["msg"]
    assert "screen=true" in TASKS["Show the summary"]["ansible.builtin.debug"]["msg"]
    # the ops callback prints them as they are
    assert TASKS["Show the summary"]["vars"]["cassandra_output"] is True
    assert TASKS["Stop on a failed self-check"]["vars"]["cassandra_output"] is True
    assert render(WRITE_VARS["_report_args"]["check"], ansible_check_mode=True) is True


def test_short_screen_at_the_end():
    """The run ends with the summary (the report goes to report.txt), then the self-check failure if any."""
    write = next(p for p in PLAYS if p.get("name") == "Write the inventory")["tasks"]
    debug = [t["name"] for t in write if "ansible.builtin.debug" in t]
    assert debug == ["Show the summary"]
    assert [t["name"] for t in write][-2:] == ["Show the summary", "Stop on a failed self-check"]


def test_report_lines_joined_by_a_real_newline():
    # in a folded block '\n' stays a backslash and an n (report.txt was one line): a variable holds the newline
    assert WRITE_VARS["_newline"] == "\n" and "join(_newline)" in WRITE_VARS["_report"]
    with open(PLAYBOOK, encoding="utf-8") as f:
        assert "join('\\n')" not in f.read()


def test_the_files_that_change():
    # what the screen says: the files written that change (would, under --check), removed, there before
    write = next(play for play in PLAYS if play["name"] == "Write the inventory")
    changes = write["vars"]["_changes"]
    variables = {
        "_dir": "/p/inventories", "_hosts_file": "prod.yml",
        "import_cluster_wrote_hosts": {"changed": False},
        "import_cluster_wrote_vars": {"results": [
            {"item": {"path": "group_vars/prod/main.yml"}, "changed": True},
            {"item": {"path": "group_vars/prod/secrets.yml"}, "skipped": True, "changed": False},  # same secret
            {"item": {"path": "host_vars/node1/main.yml"}, "changed": False}]},
        "import_cluster_removed": {"results": [{"item": "host_vars/gone/main.yml", "changed": True}]},
        "import_cluster_existing": {"files": [{"path": "/p/inventories/prod.yml"}]}}
    assert render(changes["changed"], **variables) == ["group_vars/prod/main.yml"]
    assert render(changes["removed"], **variables) == ["host_vars/gone/main.yml"]
    assert render(changes["existed"], **variables) == ["prod.yml"]
    variables.update(import_cluster_wrote_hosts={"changed": True}, import_cluster_removed={"skipped": True})
    assert render(changes["changed"], **variables) == ["prod.yml", "group_vars/prod/main.yml"]
    assert render(changes["removed"], **variables) == []
    # both the report and the screen get them
    show = next(t for t in write["tasks"] if t.get("name") == "Show the summary")
    assert "changes=_changes" in show["ansible.builtin.debug"]["msg"] and "changes=_changes" in write["vars"]["_report"]
