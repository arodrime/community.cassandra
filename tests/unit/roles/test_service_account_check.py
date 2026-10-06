from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_service (and preflight) refuse an account Cassandra runs as that
# could not read the config files cassandra_config writes: Cassandra would
# stop at once on "cassandra-env.sh: Permission denied".

import os

import jinja2
import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_permissions import cassandra_unreadable_config

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service")

with open(os.path.join(ROLE, "tasks", "account_check.yml")) as f:
    TASKS = yaml.safe_load(f)
with open(os.path.join(ROLE, "defaults", "main.yml")) as f:
    DEFAULTS = yaml.safe_load(f)

CHECK = next(t for t in TASKS if t["name"] == "Refuse an account that could not read the config")["ansible.builtin.assert"]
VARS = next(t for t in TASKS if t["name"] == "Refuse an account that could not read the config")["vars"]
ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, extensions=["jinja2.ext.do"])
ENV.filters["community.cassandra.cassandra_unreadable_config"] = cassandra_unreadable_config
ENV.filters["split"] = lambda text, sep=None: text.split(sep)
ENV.filters["bool"] = lambda value: value if isinstance(value, bool) else str(value).lower() in ("true", "yes", "1")
ENV.tests["defined"] = lambda value: not isinstance(value, jinja2.Undefined)


def value(template, variables):
    """A variable's template, its value back (rendered to JSON)."""
    body = template.strip()
    assert body.startswith("{{") and body.endswith("}}")
    return yaml.safe_load(ENV.from_string("{{ (%s) | tojson }}" % body[2:-2]).render(**variables))


def condition(text, variables):
    """A when/that condition, as ansible-core before 2.19 evaluates it (inside {% if %})."""
    return ENV.from_string("{%% if %s %%}True{%% else %%}False{%% endif %%}" % text).render(**variables) == "True"


def check(account, check_mode=False, must_exist=True, **inventory):
    """(passes, message) of the check, for what the node said (account: its key=value lines)."""
    v = {"cassandra_user": "cassandra", "cassandra_group": "cassandra", "cassandra_version": "50x",
         "cassandra_service_unit_manage": True, "inventory_hostname": "n1", "ansible_check_mode": check_mode,
         "_cassandra_account_must_exist": must_exist}
    v.update(inventory)
    v.setdefault("cassandra_service_user", v["cassandra_user"])
    v.setdefault("cassandra_service_group", v["cassandra_group"])
    perms = {}
    for key, template in DEFAULTS["_cassandra_service_config_perms"].items():
        rendered = ENV.from_string(template).render(**v)
        perms[key] = yaml.safe_load(rendered) if key == "files" else rendered
    v["_cassandra_service_config_perms"] = perms
    v["_account"] = dict(line.split("=", 1) for line in account)
    for name in ("_groups", "_unreadable"):
        v[name] = value(VARS[name], v)
    passes = all(condition(c.split("  #")[0], v) for c in CHECK["that"])
    return passes, ENV.from_string(CHECK["fail_msg"]).render(**v).strip()


DEFAULT = ["user=cassandra", "group=cassandra", "uid=990", "gid=990", "gname=cassandra", "others="]


def test_defaults_pass():
    assert check(DEFAULT)[0]


def test_one_setting_for_the_unit_and_the_files():
    # cassandra_group: the unit's Group= and the config files' group
    assert check(["user=cassandra", "group=dbgrp", "uid=990", "gid=991", "gname=dbgrp", "others="],
                 cassandra_group="dbgrp")[0]


def test_unit_group_apart_from_the_files_refused():
    passes, msg = check(DEFAULT, cassandra_group="dbgrp", cassandra_service_group="cassandra")
    assert not passes
    assert "Cassandra would run as cassandra:cassandra (groups: cassandra, 990), which could not read cassandra.yaml" \
        " (root:dbgrp 0640)" in msg
    assert "cassandra-env.sh" not in msg  # 0644: readable by all


def test_user_in_the_files_group_passes():
    assert check(DEFAULT[:-1] + ["others=dbgrp 991 "], cassandra_config_group="dbgrp")[0]


def test_numeric_group_matches():
    assert check(DEFAULT[:-1] + ["others=dbgrp 991 "], cassandra_config_group="991")[0]


def test_files_readable_by_all_pass():
    assert check(DEFAULT, cassandra_config_group="dbgrp", cassandra_config_mode="0644")[0]


def test_former_name_of_the_owner_counts():
    assert check(DEFAULT, cassandra_config_group="dbgrp", cassandra_config_owner="cassandra")[0]


@pytest.mark.parametrize("check_mode, must_exist, passes", [(False, True, False), (True, True, True), (False, False, True)])
def test_missing_user(check_mode, must_exist, passes):
    # --check on a blank host, or preflight before a new node gets the package: no user yet
    result = check(["user=dbsvc", "group=dbsvc", "nouser=yes"], check_mode, must_exist)
    assert result[0] is passes
    if not passes:
        assert result[1].startswith("the user dbsvc does not exist on n1")


def test_missing_group():
    passes, msg = check(["user=cassandra", "group=nogrp", "nogroup=yes"])
    assert not passes and msg.startswith("the group nogrp does not exist on n1")


def test_kept_unit_said_so():
    passes, msg = check(DEFAULT, cassandra_group="dbgrp", cassandra_service_unit_manage=False)
    assert not passes and msg.startswith("Cassandra runs (its own unit, kept) as cassandra:cassandra")


SETTINGS = {"owner": "root", "group": "dbgrp", "mode": "0640", "public_mode": "0644", "files": {},
            "user": "cassandra", "user_group": "cassandra"}


def test_unreadable_config_filter():
    assert cassandra_unreadable_config("41x", SETTINGS, ["cassandra", "990"], ["cassandra"]) == [
        "cassandra.yaml (root:dbgrp 0640)", "jvm-server.options (root:dbgrp 0640)", "jvm8-server.options (root:dbgrp 0640)",
        "jvm11-server.options (root:dbgrp 0640)"]
    assert cassandra_unreadable_config("41x", SETTINGS, ["cassandra"], ["dbgrp"]) == []
    # the owner reads it, root reads everything, another series is not checked
    assert cassandra_unreadable_config("41x", dict(SETTINGS, owner="cassandra"), ["cassandra"], ["x"]) == []
    assert cassandra_unreadable_config("41x", SETTINGS, ["root", "0"], []) == []
    assert cassandra_unreadable_config("311x", SETTINGS, ["cassandra"], []) == []


@pytest.mark.parametrize("files, unreadable", [
    # the owner gets the owner's bits only, a member the group's only (as the kernel does)
    ({"cassandra.yaml": {"owner": "cassandra", "mode": "0040"}}, ["cassandra.yaml (cassandra:dbgrp 0040)"]),
    ({"cassandra.yaml": {"mode": "0604"}}, ["cassandra.yaml (root:dbgrp 0604)"]),
])
def test_permission_classes_in_kernel_order(files, unreadable):
    assert cassandra_unreadable_config("50x", dict(SETTINGS, files=files), ["cassandra"], ["dbgrp"])[:1] == unreadable


def test_preflight_stops_the_whole_run_on_a_refused_node():
    """Failing one host would let the operation go on without it (apply_config, stop_rack...)."""
    with open(os.path.join(ROLE, "..", "..", "playbooks", "preflight.yml")) as f:
        plays = yaml.safe_load(f)
    tasks = [t for play in plays for t in play.get("tasks", [])]
    block = next(t for t in tasks if any(sub.get("ansible.builtin.include_role", {}).get("tasks_from") == "account_check.yml"
                                         for sub in t.get("block", [])))
    assert block["any_errors_fatal"] is True


def test_checked_before_the_unit_is_written_and_before_a_drain():
    """A restart writes the unit before draining: a refused account must stop the run before both."""
    def tasks(name):
        with open(os.path.join(ROLE, "tasks", name)) as f:
            return yaml.safe_load(f)

    unit = tasks("unit.yml")
    assert unit[0].get("ansible.builtin.include_tasks") == "account_check.yml"
    assert "ansible.builtin.template" in unit[1]
    restart = tasks("action_restart.yml")
    assert restart[0].get("ansible.builtin.include_tasks") == "unit.yml"
    assert any("community.cassandra.cassandra_drain" in t for t in restart[1:])
    main = tasks("main.yml")
    assert not any(t.get("ansible.builtin.include_tasks") == "account_check.yml" for t in main)
    assert any(t.get("ansible.builtin.include_tasks") == "unit.yml" for t in main)
