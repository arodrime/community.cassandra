from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A config file the role writes, deleted by hand on a joined, running node:
# apply_config --check shows it (written whole) and says the node would apply,
# then restart; the real run writes it again (the role's template task) and
# restarts the node. Run through the role's node-side compare and its tasks'
# own templates, then apply_config's plan and view.

import json
import os
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
    cassandra_apply_config_diff, cassandra_apply_config_outcomes)
from ansible_collections.community.cassandra.tests.unit.roles.config_reader import SNITCHES, compare  # noqa: E402 pylint: disable=wrong-import-position

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


ROLE = load("roles", "cassandra_config", "tasks", "main.yml")
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
    assert facts["cassandra_apply_config_then"] == "restart" and facts["cassandra_apply_config_notes"] == []
    nodes = [{"name": "n1", "todo": False, "then": "none", "result": "nothing to apply", "notes": []},
             {"name": "n2", "todo": True, "then": facts["cassandra_apply_config_then"], "result": "nothing to apply",
              "notes": facts["cassandra_apply_config_notes"]}]
    assert cassandra_apply_config_outcomes(nodes, plan=True) == ["n2  will apply, then restart", "n1  nothing to apply"]

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
