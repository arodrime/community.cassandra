from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The settings that weaken the cluster, said by health_check (SETTINGS TO REVIEW) and the import report (a table: one row per
# setting), two lines per finding, each silenced on a node by cassandra_settings_kept_on_purpose.

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_suspicious import (
    KEPT, SUSPICIOUS, cassandra_suspicious_settings, suspicious, cassandra_restart_blockers)

MEMBERS = ["node4", "node5"]
AB_WHY = "a node that loses its data would restart empty, without streaming it back"
AB_FIX = "remove cassandra_auto_bootstrap (the default is true)"
BOOTSTRAPPED = "the node is bootstrapped already: Cassandra refuses its next start with this flag"
WIPED = "the node is a member of the ring: ignored now, it would replace a node again once its data is wiped"
SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_operation", "meta",
                    "argument_specs.yml")


_MEMBERS = object()  # (None is a value: the ring not read)


def rows(values, members=_MEMBERS):
    """[(row, the finding, what to do)] for node4 with these variables (members: MEMBERS by default)."""
    members = MEMBERS if members is _MEMBERS else members
    return [(row, text.format(nodes="node4"), fix) for row, names, text, fix in suspicious({"node4": values}, members)]


@pytest.mark.parametrize("value, warned", [(False, True), ("false", True), ("no", True), (0, True), (True, False),
                                           (None, False), ("true", False), ("", False), ("maybe", False),
                                           ("{{ undefined_x }}", False)])  # a template not resolved: not a false
def test_auto_bootstrap(value, warned):
    # two lines: the setting, its value, the nodes and why; what to do, then how to keep it
    lines = cassandra_suspicious_settings({"node4": {"cassandra_auto_bootstrap": value}, "node5": {}})
    assert lines == (["auto_bootstrap false on node4: " + AB_WHY,
                      "  %s; kept on purpose: list auto_bootstrap_false in %s" % (AB_FIX, KEPT)] if warned else [])


def test_one_finding_per_setting_with_its_nodes():
    nodes = dict(("node%d" % i, {"cassandra_auto_bootstrap": False}) for i in (1, 2, 3))
    lines = cassandra_suspicious_settings(nodes)
    assert len(lines) == 2 and lines[0].startswith("auto_bootstrap false on node1..node3: ")
    assert cassandra_suspicious_settings({}) == [] and cassandra_suspicious_settings(None) == []


def test_kept_on_purpose_on_a_node():
    nodes = {"node4": {"cassandra_auto_bootstrap": False, KEPT: ["auto_bootstrap_false"]},
             "node5": {"cassandra_auto_bootstrap": False, KEPT: "heap_size, auto_bootstrap_false"},
             "node6": {"cassandra_auto_bootstrap": False, KEPT: ["heap_size"]}}
    assert [names for row, names, t, f in suspicious(nodes)] == [["node6"]]


def test_kept_given_another_way():
    for kept in (5, True, None, {"x": 1}):  # not a list nor a string: silences nothing, no error
        assert len(suspicious({"node4": {"cassandra_auto_bootstrap": False, KEPT: kept}})) == 1


def test_an_unknown_id_is_said():
    lines = cassandra_suspicious_settings({"node4": {KEPT: ["auto_bootstrap", "heap_size"]}, "node5": {KEPT: "auto_bootstrap"}})
    assert lines == ["%s: no check is called auto_bootstrap (node4, node5); the ids are auto_bootstrap_false, replace_address,"
                     " heap_size, allow_all_authenticator, failure_policy_ignore, large_sizes, jmx_passwords_readable"
                     % KEPT]
    assert KEPT == "cassandra_settings_kept_on_purpose"


@pytest.mark.parametrize("values, found", [
    ({"cassandra_jvm_extra_options": ["-Dcassandra.replace_address=10.0.0.9"]},
     [("replace flag -Dcassandra.replace_address=10.0.0.9 in cassandra_jvm_extra_options on node4: " + BOOTSTRAPPED,
       "remove -Dcassandra.replace_address=10.0.0.9 from cassandra_jvm_extra_options")]),
    ({"cassandra_jvm11_extra_options": ["-Xss512k", "-Dcassandra.replace_address_first_boot=10.0.0.9"]},
     [("replace flag -Dcassandra.replace_address_first_boot=10.0.0.9 in cassandra_jvm11_extra_options on node4: " + WIPED,
       "remove -Dcassandra.replace_address_first_boot=10.0.0.9 from cassandra_jvm11_extra_options")]),
    # in a value with other options: the flag only
    ({"cassandra_service_environment": {"JVM_EXTRA_OPTS": "-Xss1m -Dcassandra.replace_address_first_boot=10.0.0.9"}},
     [("replace flag -Dcassandra.replace_address_first_boot=10.0.0.9 in cassandra_service_environment.JVM_EXTRA_OPTS on"
       " node4: " + WIPED,
       "remove -Dcassandra.replace_address_first_boot=10.0.0.9 from cassandra_service_environment.JVM_EXTRA_OPTS")]),
    # a line of cassandra-env.sh no variable covers (the import's hand edits): the line, its quote out of the flag
    ({"_cassandra_suspicious_lines": ['JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"']},
     [("replace flag -Dcassandra.replace_address=10.0.0.9 in the node's config on node4: " + BOOTSTRAPPED,
       'remove the line JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"')]),
    # commented out: sets nothing
    ({"_cassandra_suspicious_lines": ['#JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"',
                                      "#-Dcassandra.replace_address=listen_address or broadcast_address of dead node"],
      "cassandra_jvm_extra_options": ["# -Dcassandra.replace_address_first_boot=10.0.0.9"]}, []),
    ({"cassandra_jvm_extra_options": ["-Dcassandra.ring_delay_ms=1000"]}, []),
    # quoted: shown without its quotes
    ({"cassandra_jvm_extra_options": ['-Dcassandra.replace_address="10.0.0.9"']},
     [("replace flag -Dcassandra.replace_address=10.0.0.9 in cassandra_jvm_extra_options on node4: " + BOOTSTRAPPED,
       "remove -Dcassandra.replace_address=10.0.0.9 from cassandra_jvm_extra_options")]),
])
def test_replace_flag_left_on_a_member(values, found):
    assert [(t, f) for r, t, f in rows(values)] == found
    assert rows(values, members=["node5"]) == []  # not a member yet: replace_node's own flag
    assert rows(values, members=None) == []  # the ring not read


def test_a_replace_flag_is_a_restart_blocker_in_every_plan():
    # the posture findings are health_check's; a flag that fails the next start is said in the operations' plans
    nodes = {"node4": {"cassandra_jvm_extra_options": ["-Dcassandra.replace_address=10.0.0.9"],
                       "cassandra_heap_size": "1G"}}
    assert cassandra_restart_blockers(nodes, members=["node4"]) == [
        "WARNING  replace flag -Dcassandra.replace_address=10.0.0.9 in cassandra_jvm_extra_options on node4: " + BOOTSTRAPPED,
        "  remove -Dcassandra.replace_address=10.0.0.9 from cassandra_jvm_extra_options before any restart"]
    assert cassandra_restart_blockers(nodes, members=["node5"]) == []  # not a member: replace_node's own flag
    assert cassandra_restart_blockers(nodes, members=None) == []  # the ring not read
    assert cassandra_restart_blockers({"node4": {"cassandra_heap_size": "1G"}}, members=["node4"]) == []  # posture only


HS = "set cassandra_heap_size"
ROOM = "Cassandra needs 2G to %sG (%sunder 32G)"


@pytest.mark.parametrize("heap, ram_mb, version, why, fix", [
    ("", 64200, "41x", "heap not set on node4: Cassandra 4.x sizes it to 8G at most, with 63G of RAM; "
                       + ROOM % (31, "at most half the RAM, "), HS),
    ("", 61440, "40x", "heap not set on node4: Cassandra 4.x sizes it to 8G at most, with 60G of RAM; "
                       + ROOM % (30, "at most half the RAM, "), HS),
    ("", 59000, "41x", None, None),  # under 60G
    ("", 64200, "50x", None, None),  # 5.0's auto-sizing: half the RAM, 31G at most
    ("", 64200, None, None, None),  # (the role's default series: 5.0)
    ("", 32768, "40x", None, None),  # 8G on 32G: fine
    ("", None, "41x", None, None),
    ("1G", None, "41x", "heap 1G (cassandra_heap_size) on node4: too small, " + ROOM % (31, ""), HS),
    ("1536M", 15884, "50x", "heap 1536M (cassandra_heap_size) on node4: too small, " + ROOM % (8, "at most half the RAM, "), HS),
    ("32G", 262144, "50x", "heap 32G (cassandra_heap_size) on node4: 32G or more loses the JVM's compressed pointers, 31G holds"
                           " more; " + ROOM % (31, "at most half the RAM, "), HS),
    ("12G", 15884, "50x", "heap 12G (cassandra_heap_size) on node4: more than half of the 16G of RAM, which the page cache and"
                          " off-heap memory need; " + ROOM % (8, "at most half the RAM, "), HS),
    ("8G", 15884, "50x", None, None),  # a 16G host: memtotal_mb is a bit under it
    ("16G", 31950, "50x", None, None),
    ("2G", 3000, "50x", "heap 2G (cassandra_heap_size) on node4: the host has 3G of RAM, too little for Cassandra (8G or more)",
     "give the host more RAM"),  # no range to give
    ("16g", None, "50x", None, None),
    ("1t", None, "50x", "heap 1t (cassandra_heap_size) on node4: 32G or more loses the JVM's compressed pointers, 31G holds"
                        " more; " + ROOM % (31, ""), HS),
    ("{{ x }}", 16384, "50x", None, None),  # not a size: not judged
])
def test_heap(heap, ram_mb, version, why, fix):
    values = {"cassandra_heap_size": heap, "cassandra_version": version}
    if ram_mb:
        values["ansible_facts"] = {"memtotal_mb": ram_mb}
    assert [(t, f) for r, t, f in rows(values)] == ([(why, fix)] if why else [])


def test_heap_from_the_unit_environment_or_an_xmx_option():
    found = rows({"cassandra_service_environment": {"MAX_HEAP_SIZE": "40G"}})
    assert [t.split(":")[0] for r, t, f in found] == ["heap 40G (cassandra_service_environment.MAX_HEAP_SIZE) on node4"]
    assert rows({"cassandra_heap_size": "8G", "cassandra_service_environment": {"MAX_HEAP_SIZE": "40G"}}) == []
    # cassandra-env.sh leaves MAX_HEAP_SIZE out when the options give -Xmx: what to change is the option
    values = {"cassandra_heap_size": "8G", "cassandra_jvm_extra_options": ["-Xmx40G"], "cassandra_version": "41x",
              "ansible_facts": {"memtotal_mb": 131072}}
    assert [(t.split(":")[0], f) for r, t, f in rows(values)] == [
        ("heap -Xmx40G in cassandra_jvm_extra_options on node4", "change -Xmx in cassandra_jvm_extra_options")]
    values = {"cassandra_jvm_extra_options": ["-Xmx16G"], "cassandra_version": "41x", "ansible_facts": {"memtotal_mb": 131072}}
    assert rows(values) == []  # set by the option: not "heap not set"
    assert rows({"cassandra_jvm_extra_options": ["#-Xmx40G"], "cassandra_heap_size": "8G"}) == []  # commented
    # the unit's JVM_EXTRA_OPTS comes last
    values = {"cassandra_jvm_extra_options": ["-Xmx8G"], "cassandra_service_environment": {"JVM_EXTRA_OPTS": "'-Xmx40G'"}}
    assert [t.split(":")[0] for r, t, f in rows(values)] == ["heap -Xmx40G in cassandra_service_environment.JVM_EXTRA_OPTS on node4"]


@pytest.mark.parametrize("values, warned", [
    ({}, False),  # the defaults: localhost
    ({"cassandra_listen_address": "10.0.0.4"}, False),  # internode only: clients come on the rpc address
    ({"cassandra_rpc_address": "10.0.0.4"}, True),
    ({"cassandra_rpc_address": "0.0.0.0"}, True),
    ({"cassandra_rpc_address": ""}, True),  # the hostname's
    ({"cassandra_extra_settings": {"rpc_interface": "eth0"}}, True),  # (no variable: the import's extra settings)
    ({"cassandra_rpc_address": " ::ffff:127.0.0.1 "}, False),
    ({"cassandra_rpc_address": "{{ ansible_facts['default_ipv4']['address'] }}"}, True),  # the import's
    ({"cassandra_rpc_address": "127.0.0.1"}, False),
    ({"cassandra_rpc_address": "localhost.localdomain"}, False),
    ({"cassandra_rpc_address": "[::1]"}, False),
    ({"cassandra_rpc_address": "10.0.0.4", "cassandra_authenticator": "PasswordAuthenticator"}, False),
    ({"cassandra_rpc_address": "10.0.0.4", "cassandra_authenticator": "org.apache.cassandra.auth.AllowAllAuthenticator"},
     True),
])
def test_allow_all_authenticator_on_an_address(values, warned):
    assert [r for r, t, f in rows(values)] == (["allow_all_authenticator"] if warned else [])
    if warned:
        assert rows(values)[0][1:] == (
            "no authentication (AllowAllAuthenticator) on node4, and their RPC address is reachable from other hosts: any"
            " client that reaches it gets in", "set an authenticator (cassandra_authenticator: PasswordAuthenticator)")


def test_failure_policy_ignore():
    found = rows({"cassandra_disk_failure_policy": "ignore", "cassandra_commit_failure_policy": "stop"})
    assert [t.split(":")[0] for r, t, f in found] == ["disk_failure_policy ignore on node4"]
    found = rows({"cassandra_disk_failure_policy": " ignore", "cassandra_commit_failure_policy": "ignore"})
    assert [t.split(":")[0] for r, t, f in found] == ["commit_failure_policy ignore on node4", "disk_failure_policy ignore on node4"]
    found = rows({"cassandra_commit_failure_policy": "IGNORE"})
    assert [(t, f) for r, t, f in found] == [
        ("commit_failure_policy ignore on node4: the node acknowledges writes its commit log could not keep",
         "set cassandra_commit_failure_policy: stop (the default) or die")]


def test_every_row_has_its_id_documented():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import _role_variables
    assert KEPT in _role_variables()
    with open(SPEC) as f:
        option = yaml.safe_load(f)["argument_specs"]["main"]["options"][KEPT]
    assert "choices" not in option  # an unknown id: a line of its own, not a refusal
    assert [row for row, dummy in SUSPICIOUS if "V(%s)" % row not in " ".join(option["description"])] == []


@pytest.mark.parametrize("values, found", [
    ({"cassandra_batch_size_fail_threshold": "640000KiB"}, ["batch_size_fail_threshold 640000KiB on node4"]),
    ({"cassandra_batch_size_fail_threshold": "1MiB"}, []),  # (at the limit: not said)
    ({"cassandra_native_transport_max_frame_size": "1024MiB"}, ["native_transport_max_frame_size 1024MiB on node4"]),
    ({"cassandra_native_transport_max_frame_size": "16MiB"}, []),
    ({"cassandra_config_file_permissions": {"jmxremote.password": {"mode": "0644"}}},
     ["jmxremote.password is mode 0644 on node4"]),
    ({"cassandra_config_file_permissions": {"jmxremote.password": "0640"}}, ["jmxremote.password is mode 0640 on node4"]),
    ({"cassandra_config_file_permissions": {"jmxremote.password": {"mode": "0600"}}}, []),
    ({"cassandra_config_file_permissions": {"cassandra.yaml": "0644"}}, []),
])
def test_large_sizes_and_readable_jmx_passwords(values, found):
    lines = cassandra_suspicious_settings({"node4": values})
    assert [line for line in lines if not line.startswith("  ")] == [
        line for line in lines if any(line.startswith(f) for f in found)] and len(lines) == 2 * len(found)
