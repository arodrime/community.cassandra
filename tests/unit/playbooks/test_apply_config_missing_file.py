from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A config file the role writes, deleted by hand on a joined, running node:
# apply_config --check shows it (written whole) and says the node would apply,
# then restart; the real run writes it again (the role's template task) and
# restarts the node. A JMX users' file (cassandra_jmx_users) deleted: shown,
# written back, no restart (the password file read at each login, the rights
# read at start kept by the running JVM). Run through the role's node-side compare and its tasks'
# own templates, then apply_config's plan and view.

import json
import os
import subprocess
import sys
import warnings

import pytest
import yaml

from ansible.errors import AnsibleUndefinedVariable
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()

from ansible_collections.community.cassandra.plugins.filter.cassandra_apply_config import (  # noqa: E402 pylint: disable=wrong-import-position
    cassandra_apply_config_diff, cassandra_apply_config_outcomes, cassandra_apply_config_warnings)
from ansible_collections.community.cassandra.tests.unit.roles.config_reader import SNITCHES, compare  # noqa: E402 pylint: disable=wrong-import-position
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks  # noqa: E402 pylint: disable=wrong-import-position

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def walk(tasks):
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def find(tasks, name):
    return next(t for t in walk(tasks) if t.get("name") == name)


def trust(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return dict((k, trust(v)) for k, v in value.items())
    if isinstance(value, list):
        return [trust(v) for v in value]
    return value


def render(template, variables):
    return Templar(loader=DataLoader(), variables=trust(variables)).template(trust_as_template(template))


def with_task_vars(task, variables):
    """The variables plus the task's own vars, rendered in the order they use each other."""
    variables = dict(variables)
    todo = dict(task.get("vars") or {})
    while todo:
        done = []
        for name, value in todo.items():
            try:
                variables[name] = render(value, variables) if isinstance(value, str) else value
            except AnsibleUndefinedVariable:
                continue
            done.append(name)
        if not done:  # a variable the test does not set: its error
            name = next(iter(todo))
            render(todo[name], variables)
        for name in done:
            del todo[name]
    return variables


def set_facts(task, variables):
    """The facts a set_fact task sets, rendered with its own vars."""
    variables = with_task_vars(task, variables)

    def value(v):
        if isinstance(v, str):
            return render(v, variables)
        if isinstance(v, dict):
            return dict((k, value(w)) for k, w in v.items())
        return v
    return dict((k, value(v)) for k, v in task["ansible.builtin.set_fact"].items())


ROLE = load_tasks(os.path.join(TOP, "roles", "cassandra_config", "tasks", "main.yml"))
ROLE_VARS = load("roles", "cassandra_config", "vars", "main.yml")
FIND = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Find what each node needs"][0]
HEADER = "# Managed by Ansible (community.cassandra.cassandra_config): change the role variables, not this file.\n"
# what the role renders (the compare does not read the content: a few lines of each kind)
TEXTS = {
    "cassandra.yaml": HEADER + (
        "cluster_name: 'Prod'\nnum_tokens: 16\npartitioner: org.apache.cassandra.dht.Murmur3Partitioner\n"
        "endpoint_snitch: GossipingPropertyFileSnitch\nseed_provider:\n  - class_name: x\n    parameters:\n"
        "      - seeds: \"10.0.0.1\"\n"),
    "cassandra-env.sh": HEADER + "MAX_HEAP_SIZE=\"8G\"\nJMX_PORT=\"7199\"\n",
    "jvm-server.options": HEADER + "-ea\n-Xss256k\n",
    "jvm8-server.options": HEADER + "-XX:+UseParNewGC\n",
    "jvm11-server.options": HEADER + "-XX:+UseG1GC\n",
    "jvm17-server.options": HEADER + "-XX:+UseG1GC\n-XX:MaxGCPauseMillis=300\n",
    "cassandra-rackdc.properties": HEADER + "dc=dc1\nrack=rack1\n",
    "logback.xml": "<configuration>\n  <root level=\"INFO\"/>\n</configuration>\n",
}
CASES = sorted(set((series, name) for series, names in ROLE_VARS["_cassandra_config_files"].items() for name in names))


def stat(path, exists=True):
    return {"exists": False} if not exists else {
        "exists": True, "path": path, "isreg": True, "islnk": False, "pw_name": "root", "gr_name": "cassandra",
        "mode": "0644", "uid": 0, "gid": 990}


def role_compare(tmp_path, series, deleted, keep=False):
    """What cassandra_config finds on a joined, running node whose file `deleted` is gone, the others as the
    role writes them: the facts its tasks set, as apply_config reads them. keep: the file is in
    cassandra_config_keep_files (an import with keep_hand_edits)."""
    names = ROLE_VARS["_cassandra_config_files"][series]
    conf = tmp_path / "conf"
    conf.mkdir()
    for name in names:
        if name != deleted:
            (conf / name).write_text(TEXTS[name])
    new = dict((n, TEXTS[n]) for n in names)
    # the request of "Diff them against the live files" on an initialized node (Cassandra running)
    identity = {"live_yaml": str(conf / "cassandra.yaml"), "new_yaml": {"text": new["cassandra.yaml"]},
                "live_rackdc": str(conf / "cassandra-rackdc.properties"), "new_rackdc": {"text": new["cassandra-rackdc.properties"]},
                "snitches": SNITCHES}
    answer = compare(conf, new, compare_live=True, identity=identity)
    assert answer["yaml_error"] is None and "error" not in answer
    # nothing to keep from a file that is not there: no identity change, and no refusal of the compare
    assert answer["identity"] == {"rc": 0, "changes": []}

    variables = {"cassandra_config_compare": {"stdout": json.dumps(answer)}, "_cassandra_config_new": new,
                 "_cassandra_config_initialized": True, "cassandra_config_confirm": "auto", "cassandra_conf_dir": str(conf),
                 "cassandra_config_keep_files": [deleted] if keep else [],
                 "cassandra_config_keep_stat": {"results": [{"item": deleted, "stat": {"exists": False}}] if keep else []},
                 "cassandra_config_storage_dir": "", "cassandra_jvm": {}}
    variables.update(set_facts(find(ROLE, "Note the comparison"), variables))
    variables.update(set_facts(find(ROLE, "List the files to change and whether to ask first"), variables))
    live_stat = [{"cassandra_config_file": n, "stat": stat(str(conf / n), n != deleted)} for n in names]
    settings = dict((k, render(v, {"cassandra_config_user": "root", "cassandra_config_group": "cassandra",
                                   "cassandra_config_mode": "0644", "cassandra_config_public_mode": "0644",
                                   "cassandra_config_file_permissions": {}, "cassandra_user": "cassandra",
                                   "cassandra_group": "cassandra"}))
                    for k, v in ROLE_VARS["_cassandra_config_perm_settings"].items())
    variables.update({"cassandra_config_live_stat": {"results": live_stat}, "_cassandra_config_perm_settings": settings,
                      "_cassandra_config_links": [], "cassandra_rpm_conf_alternative": "",
                      "cassandra_config_conf_target": {"skipped": True}})
    variables.update(set_facts(find(ROLE, "List the owner, group and mode changes"), variables))
    variables.update({"_cassandra_config_dir_changes": []})
    variables.update(set_facts(find(ROLE, "Record the changes for the report"), variables))
    return variables


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("series, deleted", CASES)
def test_a_deleted_config_file_is_shown_and_written_back(tmp_path, series, deleted, keep):
    # kept as the node has it (cassandra_config_keep_files): not there, written from the template, never left missing
    role = role_compare(tmp_path, series, deleted, keep)
    preview = dict((r["cassandra_config_file"], r) for r in role["cassandra_config_preview"]["results"])
    # the diff writes it whole; the files still there are the same
    assert preview[deleted]["rc"] == 1
    added = [line for line in preview[deleted]["stdout_lines"] if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert added == ["+" + line for line in TEXTS[deleted].splitlines()]
    assert [n for n, r in preview.items() if r["rc"] != 0] == [deleted]
    # a missing file is never "the same settings written another way"
    assert role["_cassandra_config_compared"] == {deleted: None}
    assert role["_cassandra_config_kept"] == [] and role["_cassandra_config_changes"] == [deleted]
    assert role["_cassandra_config_ask"] in (True, "True")  # a joined node: asked first (apply_config sets confirm false itself)

    # the real run's template task writes it, and only it
    template = find(ROLE, "Template Cassandra config files")
    written = [n for n in ROLE_VARS["_cassandra_config_files"][series]
               if all(render("{{ %s }}" % w, dict(role, cassandra_config_file=n)) for w in template["when"])]
    assert written == [deleted]

    # apply_config: a node to do, restarted (a running Cassandra keeps its config until its turn)
    note = dict(role, _cassandra_preflight_running=True, cassandra_config_restart_pending=False,
                cassandra_config_newer_files=[], cassandra_apply_config_unit={"skipped": True})
    facts = set_facts(find(FIND["tasks"], "Note what this node needs"), note)
    assert facts["cassandra_apply_config_todo"] in (True, "True")
    # kept by hand but gone: written from the template, said (its hand edits are lost)
    warning = ("WARNING - %s kept by hand (cassandra_config_keep_files) is missing: written back from the template, the hand"
               " edits are lost. Restore your copy first to keep them." % deleted)
    assert role["_cassandra_config_keep_missing"] == ([deleted] if keep else [])
    assert facts["cassandra_apply_config_then"] == "restart"
    assert facts["cassandra_apply_config_notes"] == ([warning] if keep else [])
    nodes = [{"name": "n1", "todo": False, "then": "none", "result": "nothing to apply", "notes": []},
             {"name": "n2", "todo": True, "then": facts["cassandra_apply_config_then"], "result": "nothing to apply",
              "notes": facts["cassandra_apply_config_notes"]}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == (
        ["n2  will apply, then restart", "n1  nothing to apply"])
    # (its WARNING in the plan's warnings, not under the node)
    assert cassandra_apply_config_warnings(nodes) == ([{"label": "n2", "text": warning[len("WARNING - "):]}] if keep else [])
    if keep:  # the role run alone says it too
        said = find(ROLE, "Warn that a file kept by hand is missing")
        assert " ".join(render(said["ansible.builtin.debug"]["msg"], dict(role, inventory_hostname="n2")).split()) == (
            "WARNING - n2: " + warning[len("WARNING - "):])

    # the view of what differs: the file, absent on n2, written whole
    view = cassandra_apply_config_diff([
        {"name": "n1", "items": [], "live": [], "stats": [], "dir_stats": []},
        {"name": "n2", "items": role["_cassandra_config_items"],
         "live": [{"cassandra_apply_config_file": deleted}],  # read by apply_config: not there, no content
         "stats": role["cassandra_config_live_stat"]["results"], "dir_stats": []}])
    assert view == [deleted, "  file: absent   n2   %s differs (written whole)" % "\u2190"]


def test_the_role_reads_a_missing_live_cassandra_yaml_as_none():
    # the node step's answer for a cassandra.yaml that is not there: no content, no path, no token read from it
    facts = set_facts(find(ROLE, "Note what was read"), {"cassandra_config_read": {"stdout": json.dumps({
        "dirs": [], "unmounted": [], "yaml_stat": {"exists": False}, "yaml": None, "conf_target": None, "keep": [],
        "files": [], "dir_stats": [], "package": "5.0.9", "running": "running", "links": []})}})
    assert facts["cassandra_config_live"] == {"failed": True}
    live_paths = find(ROLE, "List the absolute paths of the live cassandra.yaml")
    assert set_facts(live_paths, {"cassandra_config_live": facts["cassandra_config_live"]})["_cassandra_config_live_paths"] == []
    token = find(ROLE, "Keep the token a joined node was started with")
    variables = {"cassandra_initial_token": "", "_cassandra_config_initialized": True, "cassandra_config_live": facts["cassandra_config_live"]}
    assert not all(render("{{ %s }}" % w, variables) in (True, "True") for w in token["when"][:3])


ACCESS_TASKS = load("roles", "cassandra_config", "tasks", "access.yml")
JMX_USERS = [{"name": "ops", "password": "s3cret", "access": "readwrite"}, {"name": "mon", "password": "m0n", "access": "readonly"}]
JMX_FILES = {"password": "ops s3cret\nmon m0n\n",
             "access": "ops readwrite \\\n    create javax.management.monitor.*,javax.management.timer.* \\\n    unregister\nmon readonly\n"}


def jmx_compare(tmp_path, live, initialized=True, extra=None):
    """What cassandra_config finds of the JMX files on a joined, running node with these live ones ({password,
    access}: text, none when deleted), the users of JMX_USERS: the facts its tasks set, as apply_config reads them."""
    root = tmp_path / "jmx"
    root.mkdir()
    paths = dict((name, str(root / ("jmxremote." + name))) for name in ("password", "access"))
    for name, text in live.items():
        if text is not None:
            (root / ("jmxremote." + name)).write_text(text)
    # the role's own templates and compare script
    templates = os.path.join(TOP, "roles", "cassandra_config", "templates")
    wanted = {"paths": paths, "keep": [], "normalize": False}
    for name in ("password", "access"):
        with open(os.path.join(templates, "jmxremote.%s.j2" % name), encoding="utf-8") as f:
            wanted[name] = render(f.read(), {"cassandra_jmx_users": JMX_USERS})
    assert wanted["password"] == JMX_FILES["password"] and wanted["access"] == JMX_FILES["access"]
    compare_task = find(ACCESS_TASKS, "Compare the JMX files with the users")
    variables = dict({"_cassandra_config_initialized": initialized, "cassandra_config_normalize": False}, **(extra or {}))
    if all(render("{{ %s }}" % w, variables) in (True, "True") for w in [compare_task["when"]]):
        same = subprocess.run([sys.executable, "-c", compare_task["ansible.builtin.command"]["argv"][2]],
                              input=json.dumps(wanted).encode(), stdout=subprocess.PIPE, check=True).stdout.decode()
        variables["cassandra_config_jmx_same"] = {"stdout": same}
    else:
        variables["cassandra_config_jmx_same"] = {"skipped": True}
    live_stat = [{"cassandra_config_file": "jmxremote." + n, "stat": stat(paths[n], live.get(n) is not None)} for n in paths]
    variables.update({"cassandra_config_live_stat": {"results": live_stat}, "_cassandra_config_items": [],
                      "cassandra_jmx_remote_password_file": paths["password"], "cassandra_jmx_remote_access_file": paths["access"]})
    variables.update(set_facts(find(ACCESS_TASKS, "List the JMX files to write"), variables))
    # the copies that write them (the others' owner and mode only)
    variables["written"] = [n for n in ("password", "access")
                            if render("{{ %s }}" % find(ACCESS_TASKS, "Write jmxremote." + n)["when"], variables) in (True, "True")]
    return variables


def jmx_apply(role):
    """apply_config's plan for a running node with these facts (nothing else to change): (facts, plan lines, view)."""
    note = dict(role, _cassandra_preflight_running=True, cassandra_config_restart_pending=False,
                cassandra_config_newer_files=[], cassandra_apply_config_unit={"skipped": True}, _cassandra_config_changes=[],
                _cassandra_config_perm_changes=[], _cassandra_config_dir_changes=[])
    facts = set_facts(find(FIND["tasks"], "Note what this node needs"), note)
    nodes = [{"name": "n1", "todo": False, "then": "none", "result": "nothing to apply", "notes": []},
             {"name": "n2", "todo": facts["cassandra_apply_config_todo"] in (True, "True"), "then": facts["cassandra_apply_config_then"],
              "result": "nothing to apply", "notes": facts["cassandra_apply_config_notes"], "kept_running": facts["cassandra_apply_config_kept_running"],
              "why": facts["cassandra_apply_config_restart_why"]}]
    view = cassandra_apply_config_diff([
        {"name": "n1", "items": [], "live": [], "stats": [], "dir_stats": []},
        {"name": "n2", "items": role["_cassandra_config_items"], "live": [], "stats": role["cassandra_config_live_stat"]["results"],
         "dir_stats": []}])
    return facts, cassandra_apply_config_outcomes(nodes, plan=True), view


@pytest.mark.parametrize("initialized", [True, False])
@pytest.mark.parametrize("deleted", ["password", "access"])
def test_a_deleted_jmx_file_is_shown_and_written_back(tmp_path, deleted, initialized):
    # (a node not compared, e.g. its system keyspace not found: its missing files still seen)
    role = jmx_compare(tmp_path, dict((n, None if n == deleted else t) for n, t in JMX_FILES.items()), initialized)
    path = role["cassandra_jmx_remote_%s_file" % deleted]
    assert role["_cassandra_config_jmx_changes"] == [{"file": "jmxremote." + deleted, "missing": True, "path": path}]
    assert role["_cassandra_config_items"] == [{"item": path, "before": "absent", "after": "written whole", "whole": True}]
    # the real run's copy writes it, and only it (compared: the other kept)
    assert role["written"] == ([deleted] if initialized else ["password", "access"])

    facts, plan, view = jmx_apply(role)
    assert facts["cassandra_apply_config_todo"] in (True, "True") and facts["cassandra_apply_config_notes"] == []
    # the password file is read at each login: no restart; the access file at start: the rights the running
    # Cassandra has are not known to be these
    assert facts["cassandra_apply_config_then"] == {"password": "none", "access": "restart"}[deleted]
    said = {"password": "no restart (jmxremote.password: read at each JMX login)",
            "access": "then restart (jmxremote.access: read at start)"}[deleted]
    assert plan == ["n2  will apply, %s" % said, "n1  nothing to apply"]
    # the view: the file, absent on n2, written whole (never its content)
    assert view == ["jmxremote." + deleted, "  file: absent   n2   %s differs (written whole)" % "←"]
    if deleted == "access":  # local JMX logs in no one: its rights change nothing
        facts, plan, dummy = jmx_apply(dict(role, cassandra_local_jmx=True))
        assert facts["cassandra_apply_config_then"] == "none"
        assert plan[0] == "n2  will apply, no restart (jmxremote.access: local JMX checks no login)"


def test_the_password_file_is_written_first_once_the_run_is_confirmed():
    # the checks before a node's turn log in with it (nodetool on the node itself); its failure stops the run
    names = [t.get("name") for t in FIND["tasks"]]
    first = FIND["tasks"][names.index("Write the JMX password file where it is missing or other")]
    assert names.index("Show the plan and confirm the run") + 1 == names.index(first["name"]) == len(names) - 1
    assert first["any_errors_fatal"] is True
    write, noted = first["block"]
    # its error hidden (passwords): said, the run stopped
    stop = first["rescue"][0]["ansible.builtin.fail"]["msg"]
    assert " ".join(render(stop, with_task_vars(first, {"inventory_hostname": "n2", "_cassandra_config_jmx_changes": [
        {"file": "jmxremote.password", "path": "/p", "missing": True}]})).split()) == (
        "n2: its JMX password file /p could not be written (its directory, disk space?), nothing else was changed on it."
        " The other nodes' password files may be written already (read at each login: no restart needed). Fix it, then"
        " run again.")
    assert write["ansible.builtin.include_role"] == {"name": "community.cassandra.cassandra_config", "tasks_from": "jmx_password.yml"}
    for changes, check, runs in (([{"file": "jmxremote.password", "path": "/p", "missing": True}], False, True),
                                 ([{"file": "jmxremote.password", "path": "/p", "missing": True}], True, False),
                                 ([{"file": "jmxremote.access", "path": "/a", "missing": True}], False, False), (None, False, False)):
        variables = {"ansible_check_mode": check}
        if changes is not None:
            variables["_cassandra_config_jmx_changes"] = changes
        variables = with_task_vars(first, variables)
        assert all(render("{{ %s }}" % w, variables) in (True, "True") for w in first["when"]) is runs
    # reported at the node's turn (by then the same, not listed by the compare)
    variables = with_task_vars(first, {"_cassandra_config_jmx_changes": [{"file": "jmxremote.password", "path": "/p", "missing": True}]})
    written = set_facts(noted, variables)["_cassandra_config_jmx_written"]
    assert written == [{"item": "/p", "before": "absent", "after": "written whole", "whole": True}]
    # the same task as the role's own (access.yml)
    included = find(ACCESS_TASKS, "Write jmxremote.password")
    assert included["ansible.builtin.include_tasks"] == "jmx_password.yml"
    own = load("roles", "cassandra_config", "tasks", "jmx_password.yml")
    copy = find(own, "Write jmxremote.password")
    assert copy["no_log"] is True and copy["diff"] is False
    assert copy["ansible.builtin.copy"]["dest"] == "{{ cassandra_jmx_remote_password_file }}"


def test_the_password_file_written_first_is_in_the_report_of_the_node_turn(tmp_path):
    early = [{"item": "/p", "before": "absent", "after": "written whole", "whole": True}]
    role = jmx_compare(tmp_path, dict(JMX_FILES), extra={"_cassandra_config_jmx_written": early})
    assert role["_cassandra_config_jmx_changes"] == [] and role["_cassandra_config_items"] == early


def test_under_check_a_node_refusing_the_jmx_login_is_checked_from_the_others():
    # a real run writes its password file first; --check writes nothing: no check run on it, nor read from it
    play = [p for p in load("playbooks", "apply_config.yml") if p.get("name") == "Apply the config where nothing is restarted, on these nodes at once"][0]
    apply = find(play["tasks"], "Apply the config on these nodes")
    hostvars = {"n1": {"_cassandra_config_jmx_changes": [{"file": "jmxremote.password"}]},
                "n2": {"_cassandra_config_jmx_changes": [{"file": "jmxremote.access"}]}, "n3": {}}
    for check, host, before, not_peers in ((True, "n1", False, "n1"), (True, "n2", True, "n1"), (False, "n1", True, "")):
        variables = with_task_vars({"vars": dict((k, apply["vars"][k]) for k in (
            "_no_login", "cassandra_service_node_checks_before", "_cassandra_health_not_peers"))},
            {"ansible_check_mode": check, "inventory_hostname": host, "hostvars": hostvars,
             "ansible_play_hosts_all": ["n1", "n2", "n3"]})
        assert variables["cassandra_service_node_checks_before"] in ((True, "True") if before else (False, "False"))
        assert variables["_cassandra_health_not_peers"] == not_peers
    # what node_operation.yml makes of it: no check of the node's own before its action
    operation = load("roles", "cassandra_operation", "tasks", "node_operation.yml")[1]
    before = find(operation["block"], "Check the cluster before touching this node")
    checks = before["vars"]["cassandra_service_health_node_checks"]
    assert render(checks, {"_cassandra_down_node": False, "cassandra_service_node_checks_before": False}) is False
    assert render(checks, {"_cassandra_down_node": False, "cassandra_service_node_checks_before": True}) is True
    assert render(checks, {"_cassandra_down_node": False}) is True
    assert render(checks, {"_cassandra_down_node": True, "cassandra_service_node_checks_before": True}) is False


def test_other_jmx_rights_restart_the_node(tmp_path):
    # the access file is read at start: other rights apply once restarted; other passwords at the next login
    role = jmx_compare(tmp_path, {"password": "ops other\nmon m0n\n", "access": "ops readonly\nmon readonly\n"})
    assert [(c["file"], c["missing"]) for c in role["_cassandra_config_jmx_changes"]] == [
        ("jmxremote.password", False), ("jmxremote.access", False)]
    assert role["written"] == ["password", "access"]
    facts, plan, view = jmx_apply(role)
    assert facts["cassandra_apply_config_then"] == "restart"
    assert plan == ["n2  will apply, then restart (jmxremote.access: read at start)", "n1  nothing to apply"]
    assert view == ["jmxremote.password", "  file: other users or rights   n2   %s differs (written whole)" % "←",
                    "jmxremote.access", "  file: other users or rights   n2   %s differs (written whole)" % "←"]
    assert not [line for line in view if "s3cret" in line or "ops " in line]  # no content shown


def test_other_jmx_passwords_alone_need_no_restart(tmp_path):
    role = jmx_compare(tmp_path, {"password": "ops other\nmon m0n\n", "access": JMX_FILES["access"]})
    facts, plan, dummy = jmx_apply(role)
    assert facts["cassandra_apply_config_then"] == "none"
    assert plan == ["n2  will apply, no restart (jmxremote.password: read at each JMX login)", "n1  nothing to apply"]


def test_jmx_files_as_the_role_writes_them_change_nothing(tmp_path):
    role = jmx_compare(tmp_path, dict(JMX_FILES))
    assert role["_cassandra_config_jmx_changes"] == [] and role["_cassandra_config_items"] == [] and role["written"] == []
    facts, plan, view = jmx_apply(role)
    assert facts["cassandra_apply_config_todo"] in (False, "False") and view == []
