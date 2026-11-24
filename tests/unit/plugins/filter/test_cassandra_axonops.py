from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest
import yaml

from ansible.errors import AnsibleFilterError

from ansible_collections.community.cassandra.plugins.filter.cassandra_axonops import (
    cassandra_apply_config_axonops_steps,
    cassandra_axonops_changes,
    cassandra_axonops_config,
    cassandra_axonops_loader_refusals,
    cassandra_axonops_java_agent,
    cassandra_axonops_problems,
)
from ansible_collections.community.cassandra.tests.unit.helpers import TOP

SETTINGS = {"server_host": "axon.example.com", "server_port": 1888, "org": "acme", "key": "", "tls_mode": "disabled",
            "tls_skip_verify": False, "tls_ca_file": "", "tls_cert_file": "", "tls_key_file": "", "ntp_hosts": [],
            "extra_settings": {}, "java_agent_package": "", "series": "50x", "java": "17"}


@pytest.mark.parametrize("series, java, package", [
    ("40x", "8", "axon-cassandra4.0-agent-jdk8"),
    ("40x", "11", "axon-cassandra4.0-agent"),
    ("41x", "8", "axon-cassandra4.1-agent-jdk8"),
    ("41x", 11, "axon-cassandra4.1-agent"),
    ("50x", "11", "axon-cassandra5.0-agent-jdk11"),
    ("50x", "17", "axon-cassandra5.0-agent-jdk17"),
    ("50x", "8", ""),
    ("41x", "17", ""),
    ("30x", "8", ""),
])
def test_java_agent_package(series, java, package):
    assert cassandra_axonops_java_agent(series, java) == package


def test_settings_fine():
    assert cassandra_axonops_problems(SETTINGS) == []


@pytest.mark.parametrize("change, said", [
    ({"server_host": ""}, "cassandra_axonops_server_host is empty"),
    ({"org": " "}, "cassandra_axonops_org is empty"),
    ({"tls_mode": "tls"}, "cassandra_axonops_tls_mode is tls: it must be one of disabled, TLS, mTLS"),
    ({"tls_mode": "mTLS", "tls_cert_file": "/c.pem"}, "cassandra_axonops_tls_mode mTLS needs"),
    ({"extra_settings": ["x"]}, "cassandra_axonops_extra_settings must be a dict"),
    ({"java": "8"}, "AxonOps has no Java agent package for Cassandra 5.0 on Java 8 (it has: Java 11, Java 17)"),
    ({"series": "30x", "java": "8"}, "AxonOps has no Java agent package for Cassandra 30x on Java 8 (it has: none)"),
])
def test_settings_refused(change, said):
    problems = cassandra_axonops_problems(dict(SETTINGS, **change))
    assert len(problems) == 1 and problems[0].startswith(said), problems


def test_a_package_given_is_not_checked_against_the_java():
    assert cassandra_axonops_problems(dict(SETTINGS, java="8", java_agent_package="axon-cassandra5.0-agent-jdk17")) == []


def test_config_minimal():
    text = cassandra_axonops_config(SETTINGS)
    assert text.startswith("# Managed by Ansible (community.cassandra.cassandra_axonops)\n")
    assert yaml.safe_load(text) == {"axon-server": {"hosts": "axon.example.com", "port": 1888},
                                    "axon-agent": {"org": "acme", "tls": {"mode": "disabled"}}}
    # the agent's own order: server first
    assert text.index("axon-server:") < text.index("axon-agent:")


def test_config_full():
    config = yaml.safe_load(cassandra_axonops_config(dict(
        SETTINGS, server_port="443", key="s3cr3t", tls_mode="mTLS", tls_skip_verify=True, tls_ca_file="/ca.pem",
        tls_cert_file="/c.pem", tls_key_file="/k.pem", ntp_hosts=["ntp1.example.com:123"],
        extra_settings={"axon-agent": {"disable_command_exec": True, "tls": {"skipVerify": False}},
                        "backup": {"restore_parallelism": 4}})))
    assert config == {
        "axon-server": {"hosts": "axon.example.com", "port": 443},
        "axon-agent": {"org": "acme", "key": "s3cr3t", "disable_command_exec": True,
                       "tls": {"mode": "mTLS", "skipVerify": False, "caFile": "/ca.pem", "certFile": "/c.pem",
                               "keyFile": "/k.pem"}},
        "NTP": {"hosts": ["ntp1.example.com:123"]},
        "backup": {"restore_parallelism": 4}}


def test_config_tls_without_client_certificate():
    config = yaml.safe_load(cassandra_axonops_config(dict(SETTINGS, tls_mode="TLS")))
    assert config["axon-agent"]["tls"] == {"mode": "TLS", "skipVerify": False}


def test_config_refused_settings_raise():
    with pytest.raises(AnsibleFilterError, match="cassandra_axonops_org is empty"):
        cassandra_axonops_config(dict(SETTINGS, org=""))


def test_extra_settings_left_unchanged():
    extra = {"axon-agent": {"tls": {"mode": "TLS"}}}
    cassandra_axonops_config(dict(SETTINGS, extra_settings=extra))
    assert extra == {"axon-agent": {"tls": {"mode": "TLS"}}}


WANTED = [["axon-agent", "2.0.36"], ["axon-cassandra5.0-agent-jdk17", ""]]
SET_UP = ["package axon-agent installed 2.0.36", "package axon-cassandra5.0-agent-jdk17 installed 1.1.1-1",
          "user_groups cassandra axonops", "axonops_groups axonops cassandra", "enabled enabled", "active active",
          "config abc", "config_stat root axonops 640"]


def changes(lines, wanted=None):
    return cassandra_axonops_changes(lines, wanted or WANTED, "abc", "cassandra", "cassandra", "h:1888, org o")


def test_changes_none_when_set_up():
    assert changes(SET_UP) == {"installed": {"axon-agent": "2.0.36", "axon-cassandra5.0-agent-jdk17": "1.1.1-1"},
                               "removed": [], "items": []}
    # a pinned version matches its version-revision
    assert changes(SET_UP, [["axon-agent", "2.0.36"], ["axon-cassandra5.0-agent-jdk17", "1.1.1"]])["items"] == []


def test_changes_on_a_blank_node():
    # dpkg lists a package removed with its config left: not installed
    lines = ["package axon-agent deinstall 2.0.30", "user_groups cassandra", "axonops_groups ", "enabled ", "active "]
    assert changes(lines)["items"] == [
        {"item": "package axon-agent", "before": "absent", "after": "2.0.36"},
        {"item": "package axon-cassandra5.0-agent-jdk17", "before": "absent", "after": "installed (newest)"},
        {"item": "cassandra in group axonops", "before": "no", "after": "yes"},
        {"item": "axonops in group cassandra", "before": "no", "after": "yes"},
        {"item": "/etc/axonops/axon-agent.yml", "before": "absent", "after": "server h:1888, org o"},
        {"item": "service axon-agent", "before": "absent, absent", "after": "active, enabled"}]


def test_changes_version_config_mode_and_service():
    lines = [line for line in SET_UP if not line.startswith(("config", "active"))] + [
        "config other", "config_stat root root 644", "active inactive"]
    lines[0] = "package axon-agent installed 2.0.35"
    assert changes(lines)["items"] == [
        {"item": "package axon-agent", "before": "2.0.35", "after": "2.0.36"},
        {"item": "/etc/axonops/axon-agent.yml", "before": "other settings", "after": "server h:1888, org o"},
        {"item": "service axon-agent", "before": "inactive, enabled", "after": "active, enabled"}]
    lines[lines.index("config other")] = "config abc"
    assert changes(lines)["items"][1] == {"item": "/etc/axonops/axon-agent.yml owner, group, mode",
                                          "before": "root root 644", "after": "root axonops 640"}


def test_changes_another_java_agent_replaced():
    lines = SET_UP[:1] + ["package axon-cassandra5.0-agent-jdk11 installed 1.1.1"] + SET_UP[2:]
    out = changes(lines)
    assert out["removed"] == ["axon-cassandra5.0-agent-jdk11"]
    assert out["items"] == [
        {"item": "package axon-cassandra5.0-agent-jdk17", "before": "absent", "after": "installed (newest)"},
        {"item": "package axon-cassandra5.0-agent-jdk11", "before": "1.1.1", "after": "removed"}]


@pytest.mark.parametrize("options, keep, said", [
    ("-Xss256k", [], []),
    ("-Xss256k . /usr/share/axonops/axonops-jvm.options", [], ["the AxonOps Java agent is already loaded"]),
    ('JVM_OPTS="$JVM_OPTS -javaagent:/usr/share/axonops/axon-cassandra4.1-agent.jar=/etc/axonops/axon-agent.yml"', [],
     ["the AxonOps Java agent is already loaded"]),
    ("", ["cassandra-env.sh"], ["cassandra-env.sh is in cassandra_config_keep_files"]),
])
def test_loader_refusals(options, keep, said):
    got = cassandra_axonops_loader_refusals(options, keep)
    assert [g[:len(s)] for g, s in zip(got, said)] == said and len(got) == len(said)


RESTART = ["write the config", "drain and restart Cassandra"]


@pytest.mark.parametrize("steps, sections, expected", [
    # none: as they are
    (RESTART, ["Packages"], RESTART),
    ([], ["AxonOps"], []),
    # the agent alone: set up before the config, no "apply the ..." step for it
    (RESTART, ["AxonOps"], ["set up the AxonOps agent"] + RESTART),
    (["write the config", "apply the repository, packages, firewall", "drain and restart Cassandra"], ["AxonOps"],
     ["set up the AxonOps agent", "write the config", "drain and restart Cassandra"]),
    # with packages: their step kept
    (["write the config", "apply the repository, packages, firewall", "drain and restart Cassandra"],
     ["AxonOps", "Packages"],
     ["set up the AxonOps agent", "write the config", "apply the repository, packages, firewall",
      "drain and restart Cassandra"]),
    # after the release's repository and the Java
    (["apply the repository, look for Cassandra 5.0.9 in it", "install Java 17", "write the config",
      "apply the packages, firewall", "drain and stop Cassandra", "start Cassandra"], ["Repository", "AxonOps"],
     ["apply the repository, look for Cassandra 5.0.9 in it", "install Java 17", "set up the AxonOps agent",
      "write the config", "drain and stop Cassandra", "start Cassandra"]),
])
def test_apply_config_steps(steps, sections, expected):
    extras = [{"section": s, "item": "x"} for s in sections]
    assert cassandra_apply_config_axonops_steps(steps, extras) == expected


def test_the_node_turn_sets_up_the_agent_before_the_config():
    with open(os.path.join(TOP, "roles", "cassandra_operation", "tasks", "action_apply_config.yml")) as f:
        tasks = yaml.safe_load(f)
    names = [t["name"] for t in tasks]
    setup = tasks[names.index("Set up the AxonOps agent")]
    assert names.index("Set up the AxonOps agent") < names.index("Write the config")
    assert setup["ansible.builtin.include_role"]["name"] == "community.cassandra.cassandra_axonops"
    # only where the plan found its changes (section AxonOps), never under check mode
    assert setup["when"] == ["not ansible_check_mode",
                             "'AxonOps' in cassandra_apply_config_extras | default([]) | map(attribute='section')"]
