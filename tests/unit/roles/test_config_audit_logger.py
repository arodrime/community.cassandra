from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_config: the audit logger's parameters (cassandra_audit_logger_parameters) written under its entry in
# every series' cassandra.yaml, as Ansible renders the template (the one item Cassandra reads, its values as text);
# the section's sub-keys that have a variable written at their stock line; a logger, or a sub-key that has a
# variable, given in cassandra_extra_settings refused, naming the variables.

import os

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters
from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

load_collection_filters()

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config")
with open(os.path.join(ROLE, "defaults", "main.yml"), encoding="utf-8") as f:
    DEFAULTS = yaml.safe_load(f)
with open(os.path.join(ROLE, "vars", "main.yml"), encoding="utf-8") as f:
    ROLE_VARS = yaml.safe_load(f)
SERIES = ["4.0", "4.1", "5.0"]
# made-up parameters, in the order the inventory gives them
PARAMETERS = {"log_dir": "/srv/audit/node", "max_files": "7", "tag": "yes", "note": "caf\u00e9 \"x\""}


def render(text, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(text),
                                                                      escape_backslashes=False)


def audit_section(series, **overrides):
    """The template's audit_logging_options lines, down to its logger's, rendered with the role defaults."""
    with open(os.path.join(ROLE, "templates", series, "cassandra.yaml.j2"), encoding="utf-8") as f:
        lines = f.read().split("\n")
    start = lines.index("audit_logging_options:")
    end = next(i for i in range(start, len(lines)) if "- class_name: {{ cassandra_audit_logger_class_name }}" in lines[i])
    variables = dict(DEFAULTS, cassandra_version=series.replace(".", "") + "x",
                     _cassandra_config_extra_sections=ROLE_VARS["_cassandra_config_extra_sections"], **overrides)
    return "\n".join(render(line, **variables) for line in lines[start:end + 1]) + "\n"


@pytest.mark.parametrize("series", SERIES)
def test_no_parameters_is_the_stock_logger(series):
    text = audit_section(series)
    assert text.endswith("- class_name: BinAuditLogger\n") and "parameters" not in text
    assert yaml.safe_load(text)["audit_logging_options"]["logger"] == [{"class_name": "BinAuditLogger"}]


@pytest.mark.parametrize("series, entry", [("4.0", 6), ("4.1", 4), ("5.0", 4)])
def test_parameters_written_under_the_logger_entry(series, entry):
    text = audit_section(series, cassandra_audit_logger_class_name="FileAuditLogger",
                         cassandra_audit_logger_parameters=PARAMETERS)
    pad = " " * entry
    # as stock writes seed_provider's: "parameters:" under class_name, its one item 2 spaces in, the keys in their
    # order, each value quoted (text for Cassandra)
    assert text.endswith(pad + "- class_name: FileAuditLogger\n" + pad + "  parameters:\n"
                         + pad + '    - log_dir: "/srv/audit/node"\n' + pad + '      max_files: "7"\n'
                         + pad + '      tag: "yes"\n' + pad + '      note: "caf\u00e9 \\"x\\""\n')
    assert yaml.safe_load(text)["audit_logging_options"]["logger"] == [
        {"class_name": "FileAuditLogger", "parameters": [PARAMETERS]}]


@pytest.mark.parametrize("series", SERIES)
def test_parameters_are_written_as_text(series):
    """A number given unquoted is written as text, as Cassandra reads it."""
    text = audit_section(series, cassandra_audit_logger_parameters={"max_files": 7, "ratio": 0.5})
    assert yaml.safe_load(text)["audit_logging_options"]["logger"][0]["parameters"] == [
        {"max_files": "7", "ratio": "0.5"}]


@pytest.mark.parametrize("series", SERIES)
def test_parameters_and_extra_sub_keys(series):
    """The section's other sub-keys (cassandra_extra_settings) still at its own level, after the logger."""
    text = audit_section(series, cassandra_audit_logger_parameters={"a": "1", "b": "x"},
                         cassandra_extra_settings={"audit_logging_options": {"roll_cycle": "DAILY"}})
    assert yaml.safe_load(text)["audit_logging_options"] == {
        "enabled": False, "logger": [{"class_name": "BinAuditLogger", "parameters": [{"a": "1", "b": "x"}]}],
        "roll_cycle": "DAILY"}


@pytest.mark.parametrize("series", SERIES)
def test_a_boolean_is_written_as_python_says_it(series):
    """True is written "True": quote it in the inventory to write it otherwise (the docs say so)."""
    text = audit_section(series, cassandra_audit_logger_parameters={"flag": True, "other": "true"})
    assert '- flag: "True"\n' in text and ' other: "true"\n' in text


def task(name):
    todo = load_tasks(os.path.join(ROLE, "tasks", "main.yml"))
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", []) + t.get("always", [])
    raise KeyError(name)


@pytest.mark.parametrize("version", ["40x", "41x", "50x"])
def test_a_logger_in_the_extra_settings_is_refused(version):
    """Never two logger lists: the logger goes through its variables."""
    t = task("Refuse extra settings the template already has")
    variables = dict(DEFAULTS, **dict(ROLE_VARS, cassandra_version=version, role_path=ROLE, inventory_hostname="node1"))
    variables["cassandra_extra_settings"] = {"audit_logging_options": {"logger": [
        {"class_name": "FileAuditLogger", "parameters": [{"a": 1}]}], "roll_cycle": "DAILY"}}
    variables.update(t["vars"])
    assert render("{{ %s }}" % t["ansible.builtin.assert"]["that"], **variables) is False
    msg = " ".join(render(t["ansible.builtin.assert"]["fail_msg"], **variables).split())
    assert msg.startswith("cassandra_extra_settings: audit_logging_options.logger (cassandra_audit_logger_class_name,"
                          " cassandra_audit_logger_parameters) already in cassandra.yaml")
    assert msg.endswith("set them with their variable")
    # the section's other sub-keys pass
    variables["cassandra_extra_settings"] = {"audit_logging_options": {"roll_cycle": "DAILY"}}
    assert render("{{ %s }}" % t["ansible.builtin.assert"]["that"], **variables) is True


# the sub-keys that have a variable: (variable, value, the sub-key)
SUB_KEYS = [("cassandra_audit_logs_dir", "/srv/audit", "audit_logs_dir"),
            ("cassandra_audit_included_categories", "DDL,DCL", "included_categories"),
            ("cassandra_audit_excluded_keyspaces", "system,app_cache", "excluded_keyspaces"),
            ("cassandra_audit_excluded_users", "monitor,backup", "excluded_users"),
            ("cassandra_audit_max_queue_weight", 134217728, "max_queue_weight"),
            ("cassandra_audit_max_log_size", 8589934592, "max_log_size")]


@pytest.mark.parametrize("version", ["40x", "41x", "50x"])
@pytest.mark.parametrize("var, value, key", SUB_KEYS, ids=[c[2] for c in SUB_KEYS])
def test_a_sub_key_with_a_variable_in_the_extra_settings_is_refused(version, var, value, key):
    """Never the same sub-key twice in the section: refused whether its variable is set or not, the variable named."""
    t = task("Refuse extra settings the template already has")
    for changes in ({}, {var: value}):
        variables = dict(DEFAULTS, **dict(ROLE_VARS, cassandra_version=version, role_path=ROLE,
                                          inventory_hostname="node1", **changes))
        variables["cassandra_extra_settings"] = {"audit_logging_options": {key: value, "roll_cycle": "DAILY"}}
        variables.update(t["vars"])
        assert render("{{ %s }}" % t["ansible.builtin.assert"]["that"], **variables) is False
        msg = " ".join(render(t["ansible.builtin.assert"]["fail_msg"], **variables).split())
        assert msg.startswith("cassandra_extra_settings: audit_logging_options.%s (%s) already in cassandra.yaml"
                              % (key, var))


def whole_section(series, **overrides):
    """The template's audit_logging_options section, down to its blank line, rendered with the role defaults."""
    with open(os.path.join(ROLE, "templates", series, "cassandra.yaml.j2"), encoding="utf-8") as f:
        lines = f.read().split("\n")
    start = lines.index("audit_logging_options:")
    end = lines.index("", start)
    variables = dict(DEFAULTS, cassandra_version=series.replace(".", "") + "x",
                     _cassandra_config_extra_sections=ROLE_VARS["_cassandra_config_extra_sections"], **overrides)
    return "\n".join(render(line, **variables) for line in lines[start:end]) + "\n"


@pytest.mark.parametrize("series", SERIES)
def test_sub_keys_written_in_the_section(series):
    """Commented out at their defaults; set, at their stock line, next to the logger's parameters and the extra
    sub-keys."""
    stock = yaml.safe_load(whole_section(series))["audit_logging_options"]
    assert stock == {"enabled": False, "logger": [{"class_name": "BinAuditLogger"}]}
    text = whole_section(series, cassandra_audit_logger_parameters={"a": "1"},
                         cassandra_extra_settings={"audit_logging_options": {"roll_cycle": "DAILY"}},
                         **dict((var, value) for var, value, dummy in SUB_KEYS))
    assert yaml.safe_load(text)["audit_logging_options"] == dict(
        {"enabled": False, "logger": [{"class_name": "BinAuditLogger", "parameters": [{"a": "1"}]}],
         "roll_cycle": "DAILY"}, **dict((key, value) for dummy, value, key in SUB_KEYS))
    indent = "    " if series == "4.0" else "  "
    assert "\n%smax_log_size: 8589934592\n" % indent in text and "\n%s# included_users:\n" % indent in text


def refused(parameters):
    """The parameters the role's check refuses, as its message says them ('' when it passes)."""
    t = task("Refuse audit logger parameters the template cannot write")
    variables = dict(DEFAULTS, **dict(ROLE_VARS, cassandra_audit_logger_parameters=parameters))
    variables.update(t["vars"])
    if render("{{ %s }}" % t["ansible.builtin.assert"]["that"], **variables):
        return ""
    return " ".join(render(t["ansible.builtin.assert"]["fail_msg"], **variables).split())


@pytest.mark.parametrize("key", ["on", "yes", "Null", "n", 7, "7a", "a:b", "a#b", "*x", "a b", ""])
def test_keys_the_template_cannot_write_plain_are_refused(key):
    msg = refused({key: "x", "good_key": "y"})
    assert msg.startswith('cassandra_audit_logger_parameters: key "%s". A key is written plain' % key)


@pytest.mark.parametrize("value", [None, [1], {"a": 1}, "a\nb", "a\rb", "a\x85b", "a\x7fb", "a\u2028b", "a\x00b"])
def test_values_the_template_cannot_write_are_refused(value):
    assert refused({"k1": "x", "k2": value}).startswith("cassandra_audit_logger_parameters: the value of k2. A key")


def test_writable_parameters_pass():
    assert refused({"log_dir": "/srv/audit", "max_files": 7, "ratio": 0.5, "flag": True, "tab": "a\tb",
                    "note": "caf\u00e9", "Key_2.x-y": ""}) == ""
    assert refused(["a"]).startswith("cassandra_audit_logger_parameters: a mapping is expected.")


@pytest.mark.parametrize("series", SERIES)
def test_comma_lists_given_as_lists(series):
    """Cassandra reads them as text: a list is written joined with commas, never as a YAML list it cannot start on."""
    text = whole_section(series, cassandra_audit_included_categories=["DDL", "DCL"],
                         cassandra_audit_excluded_keyspaces=["system", "app_cache"],
                         cassandra_audit_excluded_users=["monitor"])
    section = yaml.safe_load(text)["audit_logging_options"]
    assert (section["included_categories"], section["excluded_keyspaces"], section["excluded_users"]) \
        == ("DDL,DCL", "system,app_cache", "monitor")
    assert "excluded_keyspaces" not in yaml.safe_load(whole_section(series, cassandra_audit_excluded_keyspaces=[]))[
        "audit_logging_options"]  # empty: Cassandra's default


@pytest.mark.parametrize("series", SERIES)
def test_no_excluded_keyspace(series):
    """"''" (the docs say so) writes an empty text: no keyspace excluded, not Cassandra's default."""
    text = whole_section(series, cassandra_audit_excluded_keyspaces="''")
    assert "excluded_keyspaces: ''\n" in text
    assert yaml.safe_load(text)["audit_logging_options"]["excluded_keyspaces"] == ""
