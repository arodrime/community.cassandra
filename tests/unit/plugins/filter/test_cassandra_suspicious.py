from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The sensitive or suspicious settings preflight and the import report warn about (a table: one row per setting),
# each silenced on a node by cassandra_suspicious_ack.

import os

import pytest
import yaml

from ansible_collections.community.cassandra.plugins.filter.cassandra_suspicious import (
    SUSPICIOUS, cassandra_suspicious_settings, suspicious)

MEMBERS = ["node4", "node5"]
AB_WHY = (u"a node that loses its data restarts empty without streaming it back; remove the setting (default true)"
          u" unless you know why it is there")
BOOTSTRAPPED = "this node is already bootstrapped: Cassandra refuses its next start with it"
WIPED = "a member of the ring: ignored now, it would replace a node again after its data is wiped"
SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_service", "meta",
                    "argument_specs.yml")


_MEMBERS = object()  # (None is a value: the ring not read)


def rows(values, members=_MEMBERS):
    """[(row, the setting, why)] for node4 with these variables (members: MEMBERS by default)."""
    members = MEMBERS if members is _MEMBERS else members
    return [(row, setting, why) for row, names, setting, why in suspicious({"node4": values}, members)]


@pytest.mark.parametrize("value, warned", [(False, True), ("false", True), ("no", True), (0, True), (True, False),
                                           (None, False), ("true", False), ("", False), ("maybe", False),
                                           ("{{ undefined_x }}", False)])  # a template not resolved: not a false
def test_auto_bootstrap(value, warned):
    lines = cassandra_suspicious_settings({"node4": {"cassandra_auto_bootstrap": value}, "node5": {}})
    assert lines == ([u"WARNING - node4: auto_bootstrap: false — %s (kept on purpose? add auto_bootstrap_false to"
                      u" cassandra_suspicious_ack)" % AB_WHY] if warned else [])


def test_one_line_per_setting_with_its_nodes():
    nodes = dict(("node%d" % i, {"cassandra_auto_bootstrap": False}) for i in (1, 2, 3))
    assert len(cassandra_suspicious_settings(nodes)) == 1 and cassandra_suspicious_settings(nodes)[0].startswith(
        "WARNING - node1..node3: ")
    assert cassandra_suspicious_settings({}) == [] and cassandra_suspicious_settings(None) == []


def test_acknowledged_on_a_node():
    nodes = {"node4": {"cassandra_auto_bootstrap": False, "cassandra_suspicious_ack": ["auto_bootstrap_false"]},
             "node5": {"cassandra_auto_bootstrap": False, "cassandra_suspicious_ack": "heap_size, auto_bootstrap_false"},
             "node6": {"cassandra_auto_bootstrap": False, "cassandra_suspicious_ack": ["heap_size"]}}
    assert [names for row, names, s, w in suspicious(nodes)] == [["node6"]]


def test_ack_given_another_way():
    for ack in (5, True, None, {"x": 1}):  # not a list nor a string: silences nothing, no error
        assert len(suspicious({"node4": {"cassandra_auto_bootstrap": False, "cassandra_suspicious_ack": ack}})) == 1


def test_an_unknown_ack_id_is_said():
    lines = cassandra_suspicious_settings({"node4": {"cassandra_suspicious_ack": ["auto_bootstrap", "heap_size"]},
                                           "node5": {"cassandra_suspicious_ack": "auto_bootstrap"}})
    assert lines == ["WARNING - node4, node5: cassandra_suspicious_ack has auto_bootstrap, which no check has"
                     " (auto_bootstrap_false, replace_address, heap_size, allow_all_authenticator, failure_policy_ignore):"
                     " it silences nothing"]


@pytest.mark.parametrize("values, found", [
    ({"cassandra_jvm_extra_options": ["-Dcassandra.replace_address=10.0.0.9"]},
     [("-Dcassandra.replace_address=10.0.0.9 in cassandra_jvm_extra_options",
       BOOTSTRAPPED + "; remove -Dcassandra.replace_address=10.0.0.9 from cassandra_jvm_extra_options")]),
    ({"cassandra_jvm11_extra_options": ["-Xss512k", "-Dcassandra.replace_address_first_boot=10.0.0.9"]},
     [("-Dcassandra.replace_address_first_boot=10.0.0.9 in cassandra_jvm11_extra_options",
       WIPED + "; remove -Dcassandra.replace_address_first_boot=10.0.0.9 from cassandra_jvm11_extra_options")]),
    # in a value with other options: the flag only
    ({"cassandra_service_environment": {"JVM_EXTRA_OPTS": "-Xss1m -Dcassandra.replace_address_first_boot=10.0.0.9"}},
     [("-Dcassandra.replace_address_first_boot=10.0.0.9 in cassandra_service_environment.JVM_EXTRA_OPTS",
       WIPED + "; remove -Dcassandra.replace_address_first_boot=10.0.0.9 from"
               " cassandra_service_environment.JVM_EXTRA_OPTS")]),
    # a line of cassandra-env.sh no variable covers (the import's hand edits): the line, its quote out of the flag
    ({"_cassandra_suspicious_lines": ['JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"']},
     [("-Dcassandra.replace_address=10.0.0.9 in the node's config",
       BOOTSTRAPPED + '; remove the line JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"')]),
    # commented out: sets nothing
    ({"_cassandra_suspicious_lines": ['#JVM_OPTS="$JVM_OPTS -Dcassandra.replace_address=10.0.0.9"',
                                      "#-Dcassandra.replace_address=listen_address or broadcast_address of dead node"],
      "cassandra_jvm_extra_options": ["# -Dcassandra.replace_address_first_boot=10.0.0.9"]}, []),
    ({"cassandra_jvm_extra_options": ["-Dcassandra.ring_delay_ms=1000"]}, []),
    # quoted: shown without its quotes
    ({"cassandra_jvm_extra_options": ['-Dcassandra.replace_address="10.0.0.9"']},
     [("-Dcassandra.replace_address=10.0.0.9 in cassandra_jvm_extra_options",
       BOOTSTRAPPED + "; remove -Dcassandra.replace_address=10.0.0.9 from cassandra_jvm_extra_options")]),
])
def test_replace_flag_left_on_a_member(values, found):
    assert [(s, w) for r, s, w in rows(values)] == found
    assert rows(values, members=["node5"]) == []  # not a member yet: replace_node's own flag
    assert rows(values, members=None) == []  # the ring not read


HS = "set cassandra_heap_size"


@pytest.mark.parametrize("heap, ram_mb, version, why", [
    ("", 64200, "41x", "Cassandra 4.x sizes it to 8G at most, with 63G of RAM; %s between 2G and 31G (at most half the"
                       " RAM, never 32G or more)" % HS),
    ("", 61440, "40x", "Cassandra 4.x sizes it to 8G at most, with 60G of RAM; %s between 2G and 30G (at most half the"
                       " RAM, never 32G or more)" % HS),
    ("", 59000, "41x", None),  # under 60G
    ("", 64200, "50x", None),  # 5.0's auto-sizing: half the RAM, 31G at most
    ("", 64200, None, None),  # (the role's default series: 5.0)
    ("", 32768, "40x", None),  # 8G on 32G: fine
    ("", None, "41x", None),
    ("1G", None, "41x", "too small for Cassandra; %s between 2G and 31G (never 32G or more)" % HS),
    ("1536M", 15884, "50x", "too small for Cassandra; %s between 2G and 8G (at most half the RAM, never 32G or more)" % HS),
    ("32G", 262144, "50x", "32G or more loses the JVM's compressed pointers (31G holds more); %s between 2G and 31G (at"
                           " most half the RAM, never 32G or more)" % HS),
    ("12G", 15884, "50x", "more than half of the 16G of RAM, which the page cache and off-heap memory need; %s between"
                          " 2G and 8G (at most half the RAM, never 32G or more)" % HS),
    ("8G", 15884, "50x", None),  # a 16G host: memtotal_mb is a bit under it
    ("16G", 31950, "50x", None),
    ("2G", 3000, "50x", "the host has 3G of RAM, too little for Cassandra (8G or more)"),  # no range to give
    ("16g", None, "50x", None),
    ("1t", None, "50x", "32G or more loses the JVM's compressed pointers (31G holds more); %s between 2G and 31G (never"
                        " 32G or more)" % HS),
    ("{{ x }}", 16384, "50x", None),  # not a size: not judged
])
def test_heap(heap, ram_mb, version, why):
    values = {"cassandra_heap_size": heap, "cassandra_version": version}
    if ram_mb:
        values["ansible_facts"] = {"memtotal_mb": ram_mb}
    assert [w for r, s, w in rows(values)] == ([why] if why else [])


def test_heap_from_the_unit_environment_or_an_xmx_option():
    found = rows({"cassandra_service_environment": {"MAX_HEAP_SIZE": "40G"}})
    assert [s for r, s, w in found] == ["cassandra_service_environment.MAX_HEAP_SIZE: 40G"]
    assert rows({"cassandra_heap_size": "8G", "cassandra_service_environment": {"MAX_HEAP_SIZE": "40G"}}) == []
    # cassandra-env.sh leaves MAX_HEAP_SIZE out when the options give -Xmx: what to change is the option
    values = {"cassandra_heap_size": "8G", "cassandra_jvm_extra_options": ["-Xmx40G"], "cassandra_version": "41x",
              "ansible_facts": {"memtotal_mb": 131072}}
    assert [(s, w.split("; ")[1]) for r, s, w in rows(values)] == [
        ("-Xmx40G in cassandra_jvm_extra_options", "change -Xmx in cassandra_jvm_extra_options between 2G and 31G (at most"
                                                   " half the RAM, never 32G or more)")]
    values = {"cassandra_jvm_extra_options": ["-Xmx16G"], "cassandra_version": "41x", "ansible_facts": {"memtotal_mb": 131072}}
    assert rows(values) == []  # set by the option: not "heap not set"
    assert rows({"cassandra_jvm_extra_options": ["#-Xmx40G"], "cassandra_heap_size": "8G"}) == []  # commented
    # the unit's JVM_EXTRA_OPTS comes last
    values = {"cassandra_jvm_extra_options": ["-Xmx8G"], "cassandra_service_environment": {"JVM_EXTRA_OPTS": "'-Xmx40G'"}}
    assert [s for r, s, w in rows(values)] == ["-Xmx40G in cassandra_service_environment.JVM_EXTRA_OPTS"]


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
    assert [r for r, s, w in rows(values)] == (["allow_all_authenticator"] if warned else [])


def test_failure_policy_ignore():
    found = rows({"cassandra_disk_failure_policy": "ignore", "cassandra_commit_failure_policy": "stop"})
    assert [s for r, s, w in found] == ["disk_failure_policy: ignore"]
    found = rows({"cassandra_disk_failure_policy": " ignore", "cassandra_commit_failure_policy": "ignore"})
    assert [s for r, s, w in found] == ["commit_failure_policy: ignore", "disk_failure_policy: ignore"]
    found = rows({"cassandra_commit_failure_policy": "IGNORE"})
    assert [(s, w) for r, s, w in found] == [("commit_failure_policy: ignore", "the node acknowledges writes its commit"
                                              " log could not keep; use stop (the default) or die")]


def test_every_row_has_its_id_documented():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_names import _role_variables
    assert "cassandra_suspicious_ack" in _role_variables()
    with open(SPEC) as f:
        option = yaml.safe_load(f)["argument_specs"]["main"]["options"]["cassandra_suspicious_ack"]
    assert "choices" not in option  # an unknown id: a WARNING, not a refusal
    assert [row for row, dummy in SUSPICIOUS if "V(%s)" % row not in " ".join(option["description"])] == []
