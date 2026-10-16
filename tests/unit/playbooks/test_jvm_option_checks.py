from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# preflight warns about the extra JVM options worth a look that each node's
# Java reads, one line per warning with its nodes; a Java change (java_check.yml:
# update_java, upgrade, a new node, cassandra_install) is refused with an
# option that Java does not take. The expressions are read from the playbook
# and the task file, rendered by Ansible.

import os
import warnings

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

from ansible_collections.community.cassandra.tests.unit.collection_filters import load_collection_filters

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()
load_collection_filters()  # the community.cassandra filters, whatever ran before

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")

with open(os.path.join(TOP, "playbooks", "preflight.yml"), encoding="utf-8") as f:
    PLAY = next(p for p in yaml.safe_load(f) if p.get("name") == "Cassandra preflight checks")
FIND = next(t for t in PLAY["tasks"] if t.get("name") == "Find the JVM options worth a look")
WARN = next(t for t in PLAY["tasks"] if t.get("name") == "Warn about the JVM options")
with open(os.path.join(TOP, "roles", "cassandra_install", "tasks", "java_check.yml"), encoding="utf-8") as f:
    REFUSE = next(t for t in yaml.safe_load(f) if t.get("name") == "The JVM options suit this Java")
with open(os.path.join(TOP, "playbooks", "update_java.yml"), encoding="utf-8") as f:
    UPDATE = next(p for p in yaml.safe_load(f) if p.get("name") == "Check the Java change")
UPDATE_KEPT = next(t for t in UPDATE["tasks"] if t.get("name") == "The new Java reads an options file the role writes")
with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as f:
    UPGRADE_KEPT = [t for p in yaml.safe_load(f) for t in p.get("tasks") or []
                    if t.get("name") == "The target reads an options file the role writes"]
with open(os.path.join(TOP, "roles", "cassandra_install", "tasks", "java_options_kept.yml"), encoding="utf-8") as f:
    KEPT = yaml.safe_load(f)[0]


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def found(**variables):
    variables.setdefault("cassandra_version", "50x")
    variables["_java"] = render(FIND["vars"]["_java"], **variables)
    for k, v in FIND["vars"]["_options"].items():
        variables.setdefault("_options", {})[k] = render(v, **variables)
    return render(FIND["ansible.builtin.set_fact"]["_cassandra_preflight_jvm"], **variables)


def test_preflight_finds_what_the_java_reads():
    assert found() == []  # the role defaults: nothing
    assert len(found(cassandra_jvm_extra_options=["-Xss1024M"])) == 1
    # cassandra_java_version not loaded in preflight: the series' default (5.0: 17)
    assert len(found(cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"])) == 1
    assert found(cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"], cassandra_java_version="11") == []
    assert found(cassandra_jvm11_extra_options=["-Xss1024M"]) == []  # Java 17 does not read it
    # cassandra_java_versions overridden (not cassandra_java_version): its Java
    assert found(cassandra_jvm17_extra_options=["-XX:-UseBiasedLocking"],
                 cassandra_java_versions={"50x": "11"}) == []
    # 4.x on Java 17 (unsupported): the role's own jvm-server.options line, unless the node's file is kept
    assert len(found(cassandra_version="41x", cassandra_java_version="17")) == 1
    assert found(cassandra_version="41x", cassandra_java_version="17",
                 cassandra_config_keep_files=["jvm-server.options"]) == []
    # nor the variable's options in a kept file: the role does not write them there
    assert found(cassandra_jvm_extra_options=["-Xss1024M"], cassandra_config_keep_files=["jvm-server.options"]) == []


def test_preflight_one_line_per_warning_with_its_nodes():
    stack = found(cassandra_jvm_extra_options=["-Xss1024M"])
    hostvars = {"node1": {"_cassandra_preflight_jvm": stack}, "node2": {"_cassandra_preflight_jvm": stack},
                "node3": {"_cassandra_preflight_jvm": []}}
    variables = dict(WARN["vars"], ansible_play_hosts=["node1", "node2", "node3"], hostvars=hostvars)
    assert render("{{ %s }}" % WARN["when"], **variables) is True
    assert render(WARN["vars"]["_note"], **variables) == "WARNING - node1, node2: " + stack[0]
    hostvars = dict((h, {"_cassandra_preflight_jvm": []}) for h in hostvars)
    assert render("{{ %s }}" % WARN["when"], **dict(variables, hostvars=hostvars)) is False


def refused(**variables):
    variables.setdefault("cassandra_version", "50x")
    return render(REFUSE["vars"]["_refused"], **variables)


@pytest.mark.parametrize("java", ["18", "21"])
def test_a_java_change_is_refused_with_biased_locking(java):
    out = refused(cassandra_java_version=java, cassandra_jvm_extra_options=["-XX:-UseBiasedLocking"])
    assert len(out) == 1 and "Remove it from cassandra_jvm_extra_options first" in out[0]
    # in the file of that Java too (5.0 on 18+: jvm17)
    assert len(refused(cassandra_java_version=java, cassandra_jvm17_extra_options=["-XX:+UseBiasedLocking"])) == 1


def test_a_java_change_goes_on_without_it():
    assert refused(cassandra_java_version="17", cassandra_jvm_extra_options=["-XX:-UseBiasedLocking"]) == []
    # 5.0's stock jvm11 line: read by Java 11 only
    assert refused(cassandra_java_version="21", cassandra_jvm11_extra_options=["-XX:-UseBiasedLocking"]) == []
    assert refused(cassandra_java_version="21") == []  # other roles' defaults not loaded
    assert refused(cassandra_java_version="21", cassandra_jvm_extra_options=["-Xss1024M"]) == []  # a warning only


def test_a_java_change_with_the_role_s_own_line():
    # 4.1's jvm-server.options writes -XX:-UseBiasedLocking: refused on Java 21 (allow_unsupported), unless the
    # node keeps its own file
    assert len(refused(cassandra_version="41x", cassandra_java_version="21")) == 1
    assert refused(cassandra_version="41x", cassandra_java_version="21",
                   cassandra_config_keep_files=["jvm-server.options"]) == []


def kept_ok(running, target, keep, todo=True, series="50x"):
    variables = dict(cassandra_version=series, cassandra_java_version=target, cassandra_update_java_major=running,
                     cassandra_update_java_todo=todo, cassandra_config_keep_files=keep)
    if not render("{{ %s }}" % UPDATE_KEPT["when"], **variables):
        return True, variables
    variables.update((k, render(v, **variables)) for k, v in UPDATE_KEPT["vars"].items())
    variables.update((k, render(v, **variables)) for k, v in KEPT["vars"].items())
    return render("{{ %s }}" % KEPT["ansible.builtin.assert"]["that"], **variables), variables


def test_a_move_to_a_java_whose_file_is_kept_is_refused():
    # import_cluster kept jvm11-server.options of a 5.0 node on Java 17: back to Java 11 would read it as it is
    ok, variables = kept_ok("17", "11", ["jvm11-server.options"])
    assert ok is False
    msg = render(KEPT["ansible.builtin.assert"]["fail_msg"], inventory_hostname="node1", **variables)
    assert "jvm11-server.options is in cassandra_config_keep_files" in msg
    assert "cassandra_jvm11_extra_options" in msg and "Nothing was changed." in msg


def test_an_upgrade_to_another_series_checks_its_options_file():
    # 4.1 on Java 8 imported with jvm11 kept, upgraded to 5.0 on Java 11: the 4.1 file would be read as it is
    assert len(UPGRADE_KEPT) == 1
    variables = dict(cassandra_version="50x", cassandra_java_version="11", cassandra_upgrade_from_series="41x",
                     cassandra_config_keep_files=["jvm11-server.options"])
    assert render("{{ %s }}" % UPGRADE_KEPT[0]["when"], **variables) is True
    variables.update((k, render(v, **variables)) for k, v in UPGRADE_KEPT[0]["vars"].items())
    variables.update((k, render(v, **variables)) for k, v in KEPT["vars"].items())
    assert render("{{ %s }}" % KEPT["ansible.builtin.assert"]["that"], **variables) is False
    # the same series (a patch release): not checked
    same_series = dict(variables)
    same_series["cassandra_upgrade_from_series"] = "50x"
    assert render("{{ %s }}" % UPGRADE_KEPT[0]["when"], **same_series) is False


@pytest.mark.parametrize("running, target, keep, todo, series", [
    ("17", "11", [], True, "50x"),  # not kept: the role writes it
    ("11", "11", ["jvm11-server.options"], False, "50x"),  # nothing to do on that node
    ("17", "21", ["jvm17-server.options"], True, "50x"),  # the same file before and after: read already
    ("11", "17", ["jvm11-server.options"], True, "41x"),  # 4.x reads jvm11 on both
    ("11", "17", ["cassandra.yaml"], True, "50x"),
])
def test_a_move_goes_on_otherwise(running, target, keep, todo, series):
    assert kept_ok(running, target, keep, todo, series)[0] is True
