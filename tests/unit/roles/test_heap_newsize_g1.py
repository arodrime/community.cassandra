from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# 5.0 with G1 and a cassandra_heap_newsize (one of a 4.x on CMS, or found by import_cluster): written as
# HEAP_NEWSIZE, which Cassandra ignores with G1; apply_config/site.yml and the upgrade say so (a warning, not a
# refusal).

import os

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _task(path, name):
    with open(os.path.join(TOP, path), encoding="utf-8") as f:
        docs = yaml.safe_load(f)
    tasks = [t for p in docs for t in p.get("tasks", [])] if any("tasks" in d for d in docs) else docs
    return next(t for t in tasks if t.get("name") == name)


def _when(task, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(templar.template(trust_as_template("{{ %s }}" % w)) for w in task["when"])


def test_the_config_role_warns():
    warn = _task("roles/cassandra_config/tasks/checks.yml", "Warn about a young generation size G1 ignores")
    assert warn["vars"]["cassandra_output"] is True and "ansible.builtin.debug" in warn
    assert _when(warn, cassandra_version="50x", cassandra_heap_newsize="128M", _cassandra_config_gcs=["G1", "G1"]) is True
    assert _when(warn, cassandra_version="50x", cassandra_heap_newsize="", _cassandra_config_gcs=["G1", "G1"]) is False
    assert _when(warn, cassandra_version="50x", cassandra_heap_newsize="1G", _cassandra_config_gcs=["CMS", "CMS"]) is False
    assert _when(warn, cassandra_version="41x", cassandra_heap_newsize="1G", _cassandra_config_gcs=["G1", "G1"]) is False


def test_the_upgrade_preflight_lists_it():
    # its preflight runs cassandra_config on the target's settings (the config preview): the warning above, once
    with open(os.path.join(TOP, "playbooks", "upgrade.yml"), encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    tasks = [t for p in plays for t in p.get("tasks", [])]
    preview = [t for t in tasks if t.get("vars", {}).get("_cassandra_config_target_preview")]
    assert preview and preview[0]["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_config"
    assert "_phase == 'preflight'" in str(preview[0]["when"])
    assert not [t for t in tasks if "cassandra_heap_newsize" in str(t.get("when", ""))]  # not said twice


def test_said_once_a_run():
    # apply_config and upgrade say it in their preview (Compare the config, the preflight's config preview): not
    # again at each node's write
    warn = _task("roles/cassandra_config/tasks/checks.yml", "Warn about a young generation size G1 ignores")
    on = dict(cassandra_version="50x", cassandra_heap_newsize="128M", _cassandra_config_gcs=["G1"])
    assert _when(warn, _cassandra_config_no_newsize_warning=True, **on) is False
    for path, name in (("roles/cassandra_operation/tasks/action_apply_config.yml", "Write the config"),
                       ("roles/cassandra_operation/tasks/action_upgrade.yml", "Write the target series' config")):
        write = _task(path, name)
        assert write["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_config"
        assert write["vars"]["_cassandra_config_no_newsize_warning"] is True
    preview = _task("playbooks/apply_config.yml", "Compare the config")
    assert "_cassandra_config_no_newsize_warning" not in preview["vars"] and "when" not in preview


def _role_vars(**variables):
    found = {}
    for path in ("roles/cassandra_config/defaults/main.yml", "roles/cassandra_config/vars/main.yml"):
        with open(os.path.join(TOP, path), encoding="utf-8") as f:
            found.update(yaml.safe_load(f))
    return dict(found, **variables)


def _assert(task, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in _role_vars(**variables).items())
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template("{{ %s }}" % task["ansible.builtin.assert"]["that"]))


def test_a_custom_gc_on_g1_takes_the_heap_alone():
    # (import_cluster: G1's stock block not the node's, its lines carried) G1 all the same: MAX_HEAP_SIZE alone
    heap = _task("roles/cassandra_config/tasks/checks.yml", "Assert heap settings are consistent")
    custom = dict(cassandra_version="50x", cassandra_heap_size="31G", cassandra_jvm17_gc="custom")
    assert _assert(heap, cassandra_jvm17_extra_options=["-XX:+UseG1GC", "-XX:MaxGCPauseMillis=500"], **custom) is True
    # another GC, or G1 turned off after: the pair needed, as before
    assert _assert(heap, cassandra_jvm17_extra_options=["-XX:+UseZGC"], **custom) is False
    assert _assert(heap, cassandra_jvm17_extra_options=["-XX:+UseG1GC", "-XX:-UseG1GC"], **custom) is False
    assert _assert(heap, cassandra_jvm_extra_options=["-XX:+UseG1GC"], **custom) is True  # (jvm-server.options')
    # (re-audit, 2026-11-14) 4.x: the file its Java reads only (Java 11, the series' default: jvm11; jvm8's default
    # CMS does not count); on Java 8, jvm8's
    for jvm11 in (dict(cassandra_jvm11_gc="custom", cassandra_jvm11_extra_options=["-XX:+UseG1GC"]),
                  dict(cassandra_jvm11_gc="G1")):
        assert _assert(heap, cassandra_version="41x", cassandra_heap_size="8G", **jvm11) is True
        assert _assert(heap, cassandra_version="41x", cassandra_heap_size="8G", cassandra_java_version="1.8",
                       **jvm11) is False
    # extra options not a list: no type error (the argument checks say so)
    assert _assert(heap, cassandra_jvm17_extra_options="-XX:+UseG1GC", **custom) is False


def test_no_young_generation_with_g1_when_the_heap_is_in_the_jvm_options():
    check = _task("roles/cassandra_config/tasks/checks.yml",
                  "Assert no young generation size with G1 when the heap is in the JVM options")
    on = dict(cassandra_version="50x", cassandra_heap_size="31G")
    assert _assert(check, cassandra_heap_newsize="", cassandra_heap_in_jvm_options=True, **on) is True
    # (cassandra-env.sh stops on -Xmn with G1)
    assert _assert(check, cassandra_heap_newsize="3G", cassandra_heap_in_jvm_options=True, **on) is False
    assert _assert(check, cassandra_heap_newsize="3G", cassandra_heap_in_jvm_options=False, **on) is True
