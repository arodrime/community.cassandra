from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Every playbook starts by refusing a -e variable under a prefix of the
# collection that no role or playbook knows (a typo would otherwise be silently
# ignored), and a variable under a former name. The message is rendered by
# Ansible; the names are found by cassandra_unknown_extra_vars.

import os
import re

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.filter.cassandra_names import cassandra_unknown_extra_vars

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


TASKS = load("roles", "cassandra_service", "tasks", "variables_check.yml")
CHECK = TASKS[0]
PLAYBOOK_VARIABLES = load("roles", "cassandra_service", "defaults", "main.yml")["_cassandra_playbook_variables"]
PLAYBOOKS = sorted(name[:-4] for name in os.listdir(os.path.join(TOP, "playbooks")) if name.endswith(".yml"))


def check(renamed=None, unknown=None):
    variables = dict(CHECK["vars"], _renamed=renamed or {}, _unknown=unknown or {})
    assert_ = CHECK["ansible.builtin.assert"]
    return render("{{ %s }}" % assert_["that"], **variables), render(assert_["fail_msg"], **variables)


def test_one_task_on_the_controller_marked_for_the_ops_callback():
    assert len(TASKS) == 1
    assert CHECK["run_once"] is True
    assert CHECK["tags"] == ["always"]
    assert CHECK["vars"]["cassandra_output"] is True  # its message as is, REFUSED in the error colour


def test_nothing_unknown_passes():
    assert check()[0] is True


def test_typo_refused_with_its_close_name_and_the_bypass():
    passed, msg = check(unknown={"import_cluster_keep_hand_edit": "import_cluster_keep_hand_edits"})
    assert passed is False
    assert msg.split("\n") == [
        "REFUSED  variable names  nothing was changed",
        "  -e import_cluster_keep_hand_edit: not a variable of this collection, did you mean"
        " import_cluster_keep_hand_edits?",
        "  Fix the name; for a variable of your own: -e cassandra_allow_unknown_variables=<name>[,<name>...] (or true)."]


def test_former_name_and_unknown_together():
    passed, msg = check(renamed={"cassandra_new_nodes": "cassandra_target_nodes"}, unknown={"cassandra_frobnicate": ""})
    assert passed is False
    assert msg.split("\n") == [
        "REFUSED  variable names  nothing was changed",
        "  cassandra_new_nodes is renamed cassandra_target_nodes: set cassandra_target_nodes instead.",
        "  -e cassandra_frobnicate: not a variable of this collection.",
        "  Fix the name; for a variable of your own: -e cassandra_allow_unknown_variables=<name>[,<name>...] (or true)."]


def test_former_name_alone_has_no_bypass_line():
    passed, msg = check(renamed={"cassandra_new_nodes": "cassandra_target_nodes"})
    assert passed is False and "cassandra_allow_unknown_variables" not in msg


@pytest.mark.parametrize("allow, left", [
    ("true", []), (True, []), ("yes", []),  # every name
    ("false", ["cassandra_frob", "cassandra_frobnicate"]), (False, ["cassandra_frob", "cassandra_frobnicate"]),
    ("", ["cassandra_frob", "cassandra_frobnicate"]), (None, ["cassandra_frob", "cassandra_frobnicate"]),
    ("cassandra_frobnicate", ["cassandra_frob"]),  # those named only
    ("cassandra_frobnicate, cassandra_frob", []), (["cassandra_frob"], ["cassandra_frobnicate"]),
    ("[cassandra_frobnicate,cassandra_frob]", []), ("cassandra_frobnicate cassandra_frob", []),
])
def test_bypass(allow, left):
    out = cassandra_unknown_extra_vars(["cassandra_frob", "cassandra_frobnicate"], PLAYBOOK_VARIABLES, allow=allow)
    assert sorted(out) == left


@pytest.mark.parametrize("extra, given, allow", [
    (["cassandra_allow_unknown_variables"], "true", "true"),
    ([], "true", False),  # set in the inventory: not a bypass
])
def test_bypass_given_with_e_only(extra, given, allow):
    expr = CHECK["vars"]["_unknown"]
    allow_expr = expr[expr.index("allow=") + len("allow="):expr.rindex(")")]
    assert render("{{ %s }}" % allow_expr, _extra=extra, cassandra_allow_unknown_variables=given) == allow


def test_the_bypass_is_a_known_variable():
    assert "cassandra_allow_unknown_variables" in PLAYBOOK_VARIABLES
    assert cassandra_unknown_extra_vars(["cassandra_allow_unknown_variables"], PLAYBOOK_VARIABLES) == {}


def first_check(playbook):
    """The tasks file the playbook runs first, followed to variables_check.yml (None when it does not get there)."""
    plays = load("playbooks", playbook + ".yml")
    if plays[0].get("ansible.builtin.import_playbook") == "community.cassandra.preflight":
        plays = load("playbooks", "preflight.yml")
    first = plays[0]["tasks"][0]
    assert plays[0]["hosts"] == "localhost", playbook
    name = first["ansible.builtin.include_role"]["tasks_from"]
    while name != "variables_check.yml":
        tasks = load("roles", "cassandra_service", "tasks", name)
        step = tasks[0]
        name = step.get("ansible.builtin.include_tasks") or step.get("ansible.builtin.import_tasks")
        if name is None:
            return None
    return first


@pytest.mark.parametrize("playbook", PLAYBOOKS)
def test_every_playbook_checks_first(playbook):
    first = first_check(playbook)
    assert first is not None, playbook
    assert first["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_service"
    # help and import_cluster read neither the former names nor cassandra_target_nodes: not theirs to refuse
    renamed = (first.get("vars") or {}).get("_cassandra_variables_check_renamed", True)
    assert renamed is (playbook not in ("help", "import_cluster")), playbook
    # under --tags too
    assert "always" in first["tags"], playbook


@pytest.mark.parametrize("playbook", ["status", "health_check", "start_rack"])
def test_checked_again_where_limit_skips_localhost(playbook):
    # their first play is localhost only: the first node play checks too, after the nodes' own facts
    nodes_play = load("playbooks", playbook + ".yml")[1]
    names = [t.get("ansible.builtin.include_role", {}).get("tasks_from") for t in nodes_play["tasks"]]
    assert names.index("variables_check.yml") == 2, playbook
    assert "always" in nodes_play["tasks"][2]["tags"]


def test_preflight_checks_again_where_limit_skips_localhost():
    # --limit skips the first play (localhost): the nodes' play runs the check too, run_once
    nodes_play = load("playbooks", "preflight.yml")[1]
    names = [t.get("ansible.builtin.include_role", {}).get("tasks_from") for t in nodes_play["tasks"]]
    assert "target_nodes_check.yml" in names
    assert load("roles", "cassandra_service", "tasks", "target_nodes_check.yml")[0] == {
        "name": "No unknown variable, none under a former name", "ansible.builtin.import_tasks": "variables_check.yml"}


# -e name=..., --extra-vars name=..., "-e name" in the help output and the TO DO lines (Python strings too)
EXTRA_VAR = re.compile(r"""(?:-e|--extra-vars)[ =]+['"]?@?([A-Za-z_]\w*)""")
# the typos shown on purpose: hosts_check.yml, the guide (and the unit tests', which are not read)
TYPOS = {"cassandra_host", "import_cluster_keep_hand_edit"}


def test_every_documented_extra_var_is_known():
    found = {}
    for top in ("playbooks", "roles", "plugins", "docs", "tests", "changelogs", "README.md"):
        top = os.path.join(TOP, top)
        walked = os.walk(top) if os.path.isdir(top) else [(os.path.dirname(top), [], [os.path.basename(top)])]
        for root, dummy, files in walked:
            if os.path.join("tests", "unit") in root or os.path.join("tests", "output") in root:
                continue
            for name in files:
                if not name.endswith((".yml", ".yaml", ".j2", ".py", ".rst", ".md", ".sh", ".cfg", ".ini")):
                    continue
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                    for var in EXTRA_VAR.findall(f.read()):
                        found.setdefault(var, set()).add(name)
    assert "import_cluster_keep_hand_edits" in found and "help_topic" in found  # the scan reads them
    unknown = cassandra_unknown_extra_vars(sorted(found), PLAYBOOK_VARIABLES)
    assert dict((k, sorted(found[k])) for k in unknown if k not in TYPOS) == {}


def test_import_cluster_options_are_suggested():
    # each import_cluster_* name in import_cluster's comments (its options) is a playbook variable, so the one
    # suggested for a typo (not an internal fact or register of the import)
    with open(os.path.join(TOP, "playbooks", "import_cluster.yml"), encoding="utf-8") as f:
        comments = "\n".join(line for line in f if line.lstrip().startswith("#"))
    options = set(re.findall(r"\b(import_cluster_\w+)", comments))
    assert "import_cluster_vault_id" in options and "import_cluster_keep_hand_edits" in options
    assert options - set(PLAYBOOK_VARIABLES) == set()
    # never an internal one (import_cluster_vault_stat, import_cluster_repo_files are registers of the import)
    out = cassandra_unknown_extra_vars(["import_cluster_vault_pass", "import_cluster_repo_file", "import_cluster_vault_passwd_file"],
                                       PLAYBOOK_VARIABLES)
    assert set(out.values()) <= options | {""}
    assert out["import_cluster_vault_passwd_file"] == "import_cluster_vault_password_file"
