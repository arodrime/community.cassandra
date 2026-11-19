from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The filters of the cassandra_service role: its change report, the refusal of the account Cassandra runs as, the
# other seeds a new seed looks for.

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_service import (
    cassandra_service_account_refusal, cassandra_service_changes, cassandra_service_other_seeds)

DROPIN = "/etc/systemd/system/cassandra.service.d/zz-ansible-environment.conf"
RUNNING = {"active": "active", "enabled": "enabled", "unit": "/etc/systemd/system/cassandra.service"}


def changes(before=None, state="started", check=False, unit_written=False, dropin_existed=False, dropin_written=False,
            dropin_removed=False, jvm_rc=0, enabled=True):
    return cassandra_service_changes(before or RUNNING, state, check, unit_written, DROPIN, dropin_existed,
                                     dropin_written, dropin_removed, jvm_rc, enabled)


def test_nothing_to_report():
    assert changes() == []


@pytest.mark.parametrize("unit, before", [
    ("", "absent"), ("SysV /etc/init.d/cassandra", "SysV /etc/init.d/cassandra"),
    ("/etc/systemd/system/cassandra.service", "different")])
def test_the_unit_written(unit, before):
    assert changes(dict(RUNNING, unit=unit), unit_written=True) == [
        {"item": "/etc/systemd/system/cassandra.service", "before": before, "after": "native unit"}]
    assert changes(dict(RUNNING, unit=unit), unit_written=True, check=True)[0]["after"] == "would be a native unit"


def test_the_drop_in():
    assert changes(dropin_written=True, dropin_existed=True) == [{"item": DROPIN, "before": "different", "after": "written"}]
    assert changes(dropin_written=True, check=True) == [{"item": DROPIN, "before": "absent", "after": "would be written"}]
    assert changes(dropin_removed=True) == [{"item": DROPIN, "before": "present", "after": "removed"}]


@pytest.mark.parametrize("state, active, check, after", [
    ("restarted", "active", False, "restarted"), ("restarted", "active", True, "would restart"),
    ("started", "inactive", False, "active"), ("started", "", True, "would be active"),
    ("stopped", "active", False, "inactive")])
def test_the_service(state, active, check, after):
    assert changes(dict(RUNNING, active=active), state, check) == [
        {"item": "cassandra", "before": active or "unknown", "after": after}]


def test_a_unit_left_active_with_no_cassandra_started_again():
    started = {"item": "cassandra", "before": "active, no Cassandra running", "after": "would be started again"}
    assert changes(jvm_rc=1, check=True) == [started]
    assert changes(jvm_rc=1, state="stopped") == [{"item": "cassandra", "before": "active", "after": "inactive"}]
    assert changes(jvm_rc=1, state="restarted") == [{"item": "cassandra", "before": "active", "after": "restarted"}]


def test_the_start_at_boot():
    assert changes(enabled=False) == [{"item": "cassandra at boot", "before": "enabled", "after": "disabled"}]
    assert changes(dict(RUNNING, enabled="generated"), check=True) == [
        {"item": "cassandra at boot", "before": "generated", "after": "would be enabled"}]
    assert changes(dict(RUNNING, enabled="")) == [{"item": "cassandra at boot", "before": "unknown", "after": "enabled"}]


def test_items_in_order():
    items = changes(dict(RUNNING, active="inactive", enabled="disabled", unit=""), unit_written=True, dropin_removed=True)
    assert [i["item"] for i in items] == ["/etc/systemd/system/cassandra.service", DROPIN, "cassandra", "cassandra at boot"]


@pytest.mark.parametrize("role_unit, user_text, group_text", [
    (True, "the user dbsvc does not exist on n1: create it (the Cassandra package creates cassandra), or set cassandra_user",
     "the group dbgrp does not exist on n1: create it, or set cassandra_group"),
    (False, "the user dbsvc does not exist on n1 (the User= of the unit it starts with, not the one of the role):"
            " create it, or fix the unit",
     "the group dbgrp does not exist on n1 (the Group= of the unit it starts with, not the one of the role):"
     " create it, or fix the unit")])
def test_account_missing(role_unit, user_text, group_text):
    assert cassandra_service_account_refusal({"user": "dbsvc", "group": "dbsvc", "nouser": "yes"}, "n1", [], [],
                                             role_unit) == user_text
    assert cassandra_service_account_refusal({"user": "cassandra", "group": "dbgrp", "nogroup": "yes"}, "n1", [], [],
                                             role_unit) == group_text


def test_account_that_cannot_read():
    account = {"user": "cassandra", "group": "cassandra"}
    unreadable = ["cassandra.yaml (root:dbgrp 0640)", "cassandra-env.sh (root:dbgrp 0640)"]
    assert cassandra_service_account_refusal(account, "n1", ["cassandra", "990"], unreadable, True) == (
        "Cassandra would run as cassandra:cassandra (groups: cassandra, 990), which could not read cassandra.yaml"
        " (root:dbgrp 0640), cassandra-env.sh (root:dbgrp 0640). Set cassandra_user and cassandra_group (one setting"
        " for the unit and the config files), or make the group of the unit and cassandra_config_group agree.")
    assert cassandra_service_account_refusal(account, "n1", [], unreadable, False).startswith(
        "Cassandra runs (the unit it starts with, not the one of the role) as cassandra:cassandra (groups: ), ")


@pytest.mark.parametrize("seeds, others", [
    (["10.0.0.1:7001", "[2001:db8::4]:7000", "2001:db8::2", "10.0.0.4", "node4:7000"], ["10.0.0.1:7001", "2001:db8::2"]),
    (" 10.0.0.1 ,, node4:7000, 10.0.0.2:7001", ["10.0.0.1", "10.0.0.2:7001"]),
    ([" ", ""], []),
    ([], []),
    ("", []),
])
def test_other_seeds(seeds, others):
    assert cassandra_service_other_seeds(seeds, ["node4", "10.0.0.4", "2001:db8::4"]) == others
