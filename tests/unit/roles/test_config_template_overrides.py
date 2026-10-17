from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config: your own template for a config file (cassandra_config_template_overrides), rendered
# instead of the role's and handled as its own; the lines added at the end of cassandra-env.sh and of
# logback.xml's <configuration> (cassandra_env_extra_lines, cassandra_logback_extra_lines).

import os
import xml.etree.ElementTree as ET

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

from ansible_collections.community.cassandra.plugins.filter.cassandra_settings import same_settings
from ansible_collections.community.cassandra.tests.unit.roles.config_reader import preview
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config")
with open(os.path.join(ROLE, "vars", "main.yml"), encoding="utf-8") as f:
    ROLE_VARS = yaml.safe_load(f)
with open(os.path.join(ROLE, "defaults", "main.yml"), encoding="utf-8") as f:
    DEFAULTS = yaml.safe_load(f)


def task(name):
    todo = load_tasks(os.path.join(ROLE, "tasks", "main.yml"))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


def render(template, escape_backslashes=True, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template),
                                                                      escape_backslashes=escape_backslashes)


def role_vars(series="5.0", **variables):
    out = {"_cassandra_config_files": ROLE_VARS["_cassandra_config_files"], "_cassandra_config_series": series,
           "_cassandra_config_templates": trust_as_template(ROLE_VARS["_cassandra_config_templates"]),
           "cassandra_config_template_overrides": {}, "cassandra_config_keep_files": []}
    out.update(variables)
    return out


REFUSE = task("Refuse template overrides the role cannot use")


def refused(overrides, keep=()):
    variables = role_vars(cassandra_config_template_overrides=overrides, cassandra_config_keep_files=list(keep))
    variables["_files"] = trust_as_template(REFUSE["vars"]["_files"])
    return render(REFUSE["vars"]["_wrong"], **variables)


def test_a_valid_override_passes(tmp_path):
    (tmp_path / "env.j2").write_text("X=1\n")
    assert refused({"cassandra-env.sh": str(tmp_path / "env.j2")}) == []
    assert refused({}) == []


@pytest.mark.parametrize("overrides, keep, said", [
    ({"jvm8-server.options": "/x.j2"}, [], "jvm8-server.options is not a file the role writes (cassandra.yaml, "),
    ({"jmxremote.password": "/x.j2"}, [], "jmxremote.password is not a file the role writes"),
    ({"cassandra-env.sh": "templates/env.j2"}, [], "cassandra-env.sh: an absolute path on the controller is expected"),
    ({"cassandra-env.sh": ["/x.j2"]}, [], "cassandra-env.sh: an absolute path on the controller is expected"),
    ({"cassandra-env.sh": "/nowhere/env.j2"}, [], "cassandra-env.sh: /nowhere/env.j2 not found on the controller"),
    ({"logback.xml": "{tmp}"}, ["logback.xml"], "logback.xml is also in cassandra_config_keep_files"),
])
def test_refusals(tmp_path, overrides, keep, said):
    (tmp_path / "t.j2").write_text("x\n")
    overrides = dict((k, str(tmp_path / "t.j2") if v == "{tmp}" else v) for k, v in overrides.items())
    wrong = refused(overrides, keep)
    assert len(wrong) == 1 and wrong[0].startswith(said), wrong


def test_refuses_a_value_that_is_not_a_dict():
    assert refused(["cassandra-env.sh"]) == ["a dict of {file name: template path} is expected"]


def test_runs_before_anything_is_read_or_changed():
    names = [t.get("name") for t in load_tasks(os.path.join(ROLE, "tasks", "main.yml"))]
    assert names.index("Refuse template overrides the role cannot use") < names.index("Configure")
    assert "when" not in REFUSE  # always, under --check too


def test_the_template_of_each_file(tmp_path):
    templates = render("{{ _cassandra_config_templates }}",
                       **role_vars("4.1", cassandra_config_template_overrides={"logback.xml": "/i/logback.xml.j2"}))
    assert templates == dict((f, "/i/logback.xml.j2" if f == "logback.xml" else "4.1/%s.j2" % f)
                             for f in ROLE_VARS["_cassandra_config_files"]["4.1"])


def test_the_role_renders_and_writes_the_override():
    # the preview, the compare and apply_config's hashes all use _cassandra_config_new: rendered from the same templates
    assert "map('extract', _cassandra_config_templates)" in task("Render the config files")["ansible.builtin.set_fact"][
        "_cassandra_config_new"]
    assert task("Template Cassandra config files")["ansible.builtin.template"]["src"] == \
        "{{ _cassandra_config_templates[cassandra_config_file] }}"


def test_the_override_is_rendered_with_the_role_variables(tmp_path):
    (tmp_path / "env.j2").write_text('JMX_PORT="{{ cassandra_jmx_port }}"\nexport KEYSTORE_PASSWORD={{ pw }}\n')
    variables = role_vars(cassandra_config_template_overrides={"cassandra-env.sh": str(tmp_path / "env.j2")},
                          cassandra_jmx_port=7299, pw="hunter2")
    paths = render("{{ ['cassandra-env.sh'] | map('extract', _cassandra_config_templates) | list }}", **variables)
    new = render("{{ query('template', *paths) | first }}", paths=paths, **variables)  # (short name outside a play)
    assert new == 'JMX_PORT="7299"\nexport KEYSTORE_PASSWORD=hunter2\n'
    # compared as the role's own file: the same, nothing to do; another value: shown, the secret masked
    live = 'JMX_PORT="7299"\nexport KEYSTORE_PASSWORD=hunter2\n'
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert preview(tmp_path / "a", "cassandra-env.sh", live, new) == ""
    assert same_settings("cassandra-env.sh", "# by hand\n" + live, new)
    diff = preview(tmp_path / "b", "cassandra-env.sh", live.replace("hunter2", "old"), new)
    assert "+export KEYSTORE_PASSWORD=****" in diff and "hunter2" not in diff and "old" not in diff


def test_extra_settings_checked_against_the_override(tmp_path):
    (tmp_path / "y.j2").write_text("cluster_name: x\nmy_own_key: 1\n")
    t = task("Refuse extra settings the template already has")
    variables = role_vars(cassandra_config_template_overrides={"cassandra.yaml": str(tmp_path / "y.j2")},
                          role_path=ROLE, cassandra_extra_settings={"my_own_key": 2, "num_tokens": 8},
                          _cassandra_config_extra_sections={}, cassandra_version="50x",
                          _cassandra_config_old_names=ROLE_VARS["_cassandra_config_old_names"],
                          _cassandra_config_old_names_of=ROLE_VARS["_cassandra_config_old_names_of"])
    # (outside a play only the lookup's short name resolves)
    for name in ("_template", "_keys", "_old"):  # the task's own vars (the template read: the override)
        variables[name] = trust_as_template(t["vars"][name].replace("lookup('ansible.builtin.file'", "lookup('file'"))
    assert render(t["vars"]["_taken"], **variables) == ["my_own_key"]  # num_tokens: not in that template


def render_file(series, name, **variables):
    with open(os.path.join(ROLE, "templates", series, name + ".j2"), encoding="utf-8") as f:
        text = f.read().split("\n", 1)[1]  # (without the header line)
    ctx = dict(DEFAULTS, cassandra_version=series[0] + series[2] + "x", **variables)
    return render(text, escape_backslashes=False, **ctx)


@pytest.mark.parametrize("series", ["4.0", "4.1", "5.0"])
def test_env_extra_lines_after_the_last_line(series):
    lines = ["export X=1", 'JVM_OPTS="$JVM_OPTS -Dcom.example=1"']
    plain, more = render_file(series, "cassandra-env.sh"), render_file(series, "cassandra-env.sh", cassandra_env_extra_lines=lines)
    last = 'JVM_OPTS="$JVM_OPTS $JVM_EXTRA_OPTS"'
    assert plain.rstrip("\n").endswith(last)
    assert more == plain.replace(last + "\n", "\n".join([last] + lines) + "\n")


@pytest.mark.parametrize("series", ["4.0", "4.1", "5.0"])
def test_logback_extra_lines_inside_configuration(series):
    lines = ['  <logger name="com.example" level="WARN"/>']
    more = render_file(series, "logback.xml", cassandra_logback_extra_lines=lines, cassandra_log_level_cassandra="DEBUG")
    root = ET.fromstring(more)
    assert [(e.get("name"), e.get("level")) for e in root.findall("logger")][-2:] == [
        ("org.apache.cassandra", "DEBUG"), ("com.example", "WARN")]
    assert more.rstrip().endswith(lines[0] + "\n</configuration>")


@pytest.mark.parametrize("installed, overrides, said", [
    ("4.1.5", {"cassandra-env.sh": "/i/env.j2"}, True),  # the upgrade's preview of 5.0: check yours suits it
    ("5.0.2", {"cassandra-env.sh": "/i/env.j2"}, False),
    ("4.1.5", {}, False),
    ("", {"cassandra-env.sh": "/i/env.j2"}, False),
])
def test_said_when_your_template_renders_another_series(installed, overrides, said):
    t = task("Say which files your own templates render for another series than the one installed")
    variables = role_vars("5.0", cassandra_config_template_overrides=overrides, _cassandra_config_installed=installed)
    assert all(render("{{ %s }}" % w, **variables) for w in t["when"]) is said
