from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The Prometheus exporter's Java agent: found in a JVM options file, a JVM_OPTS line of cassandra-env.sh or a block
# of it that only adds it (if ... fi), imported into the cassandra_prometheus_exporter_* variables, the block taken
# out; the role then writes the agent at the end of jvm-server.options and the JVM gets the same arguments. The
# exporter's config compared (a restart for its httpServer part only), and health_check's finding.

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_prometheus import cassandra_prometheus_config_change
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_prometheus import (
    agent, block_as_line, config_change, without_agent)


JAR = "/etc/cassandra-monitoring/lib/jmx_prometheus_javaagent-1.7.0.jar"
CONF = "/etc/cassandra-monitoring/conf/jmx/oss.yml"
OPTION = "-javaagent:%s=9404:%s" % (JAR, CONF)
# the shape of a block written by hand: the agent only when its jar and config are readable and its port free
OWNERS_BLOCK = (
    "if [ -r %s ] && [ -r %s ] && ! { cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; true; } | awk '$4 == \"0A\" &&"
    " substr($2, length($2) - 4) == \":24BC\" { f = 1 } END { exit !f }'; then JVM_OPTS=\"$JVM_OPTS %s\"; else echo"
    " \"... NOT loaded ...\" >&2; fi" % (JAR, CONF, OPTION))
# the same with guards that pass where the tests run (bash runs the files)
PASSING_BLOCK = ('if [ -n "$CASSANDRA_CONF" ]; then\n  JVM_OPTS="$JVM_OPTS %s"\nelse\n  echo "exporter NOT loaded" >&2\nfi'
                 % OPTION)
# --- the agent's option ------------------------------------------------------

@pytest.mark.parametrize("text, wanted", [
    (OPTION, {"jar": JAR, "version": "1.7.0", "dir": "/etc/cassandra-monitoring/lib", "address": "", "port": 9404,
              "config": CONF}),
    ('JVM_OPTS="$JVM_OPTS -javaagent:/opt/e/jmx_prometheus_javaagent-1.6.0.jar=127.0.0.1:9500:/etc/e.yml"',
     {"jar": "/opt/e/jmx_prometheus_javaagent-1.6.0.jar", "version": "1.6.0", "dir": "/opt/e", "address": "127.0.0.1",
      "port": 9500, "config": "/etc/e.yml"}),
    ("-javaagent:/opt/e/jmx_prometheus_javaagent-1.6.0.jar=[::1]:9500:/etc/e.yml",
     {"jar": "/opt/e/jmx_prometheus_javaagent-1.6.0.jar", "version": "1.6.0", "dir": "/opt/e", "address": "::1",
      "port": 9500, "config": "/etc/e.yml"}),
])
def test_the_agent_is_read_from_its_option(text, wanted):
    found = agent(text)
    assert dict((k, found[k]) for k in wanted) == wanted


@pytest.mark.parametrize("text", [
    "-javaagent:/opt/e/other_agent.jar=9404:/etc/e.yml",  # another agent
    "-javaagent:/opt/e/jmx_prometheus_javaagent-1.7.0.jar",  # no port nor config
    "-javaagent:relative/jmx_prometheus_javaagent-1.7.0.jar=9404:/etc/e.yml",  # not a path the role writes
])
def test_other_agents_are_not_the_exporter(text):
    assert agent(text) is None


def test_a_line_of_a_jvm_options_file():
    text, found = without_agent("jvm-server.options", "-ea\n%s\n-Xss256k\n" % OPTION)
    assert text == "-ea\n-Xss256k\n"
    assert (found["kind"], found["line"], found["lines"]) == ("line", 2, 1)
    # commented out: no agent; with other options on its line: left as it is
    assert without_agent("jvm-server.options", "#%s\n" % OPTION) == ("#%s\n" % OPTION, None)
    text, found = without_agent("jvm-server.options", "%s -Dx=1\n" % OPTION)
    assert text == "%s -Dx=1\n" % OPTION and found["kind"] == "kept"


@pytest.mark.parametrize("env, kind, lines", [
    ('A=1\nJVM_OPTS="$JVM_OPTS %s"\nB=2\n' % OPTION, "line", 1),
    ("A=1\n%s\nB=2\n" % OWNERS_BLOCK, "block", 1),
    ("A=1\n%s\nB=2\n" % PASSING_BLOCK, "block", 5),
])
def test_cassandra_env_sh_line_or_block(env, kind, lines):
    text, found = without_agent("cassandra-env.sh", env)
    assert text == "A=1\nB=2\n"
    assert (found["kind"], found["line"], found["lines"], found["option"]) == (kind, 2, lines, OPTION)


@pytest.mark.parametrize("env, why", [
    ('if true; then\n  X=1\n  JVM_OPTS="$JVM_OPTS %s"\nfi\n' % OPTION, "its block in cassandra-env.sh sets more than the agent"),
    ('JVM_OPTS="$JVM_OPTS %s -Dx=1"\n' % OPTION, "its line in cassandra-env.sh sets more than the agent"),
    ('if true; then\n  JVM_OPTS="$JVM_OPTS %s"\n' % OPTION, "its block in cassandra-env.sh is not closed"),
])
def test_a_block_doing_more_is_left_as_it_is(env, why):
    text, found = without_agent("cassandra-env.sh", env)
    assert text == env and (found["kind"], found["why"]) == ("kept", why)


def test_the_block_is_read_as_its_line_when_the_jvm_has_the_agent():
    env = "A=1\n%s\nB=2\n" % PASSING_BLOCK
    assert block_as_line(env, "/usr/bin/java\0%s\0org.apache.cassandra.service.CassandraDaemon" % OPTION) == (
        'A=1\nJVM_OPTS="$JVM_OPTS %s"\nB=2\n' % OPTION)
    assert block_as_line(env, "/usr/bin/java\0org.apache.cassandra.service.CassandraDaemon") == env  # guards failed


# --- the exporter's config ----------------------------------------------------

def test_a_change_of_the_rules_alone_is_reloaded_one_of_the_http_server_restarts():
    rules = "lowercaseOutputName: true\nrules:\n  - pattern: '.*'\n"
    http = "httpServer:\n  authentication:\n    basic: {username: prom, password: x}\n"
    assert config_change(rules, rules.replace("true", "true  # names")) == {"same": True, "restart": False}
    assert config_change(rules, rules.replace("'.*'", "'org.*'")) == {"same": False, "restart": False}
    assert config_change(rules, rules + http) == {"same": False, "restart": True}
    assert config_change(rules + http, rules + http.replace("x}", "y}")) == {"same": False, "restart": True}
    assert config_change(None, rules) == {"same": False, "restart": False}
    assert config_change("", rules + http) == {"same": False, "restart": True}
    assert config_change("{not yaml", rules) == {"same": False, "restart": True}  # what the agent read: not known
    assert cassandra_prometheus_config_change(rules, rules) == {"same": True, "restart": False, "diff": ""}


def test_open_telemetry_is_read_at_start_too():
    assert config_change("rules: []\n", "rules: []\nopenTelemetry: {endpoint: x}\n") == {"same": False, "restart": True}


def test_the_diff_of_the_rules_masks_the_passwords():
    live = "rules: []\nhttpServer:\n  authentication:\n    basic: {username: prom, password: old1}\n"
    new = "rules: [x]\nhttpServer:\n  authentication:\n    basic: {username: prom, password: new2}\n"
    diff = cassandra_prometheus_config_change(live, new, "/etc/cassandra/jmx_prometheus.yml")["diff"]
    assert diff.startswith("--- /etc/cassandra/jmx_prometheus.yml (live)") and "+rules: [x]" in diff
    assert "old1" not in diff and "new2" not in diff
    assert cassandra_prometheus_config_change(live, live)["diff"] == ""


def test_the_diff_masks_the_open_telemetry_headers():
    live = "rules: []\nopenTelemetry:\n  endpoint: http://otel:4317\n  headers:\n    Authorization: Bearer old1\n"
    new = ("rules: [x]\nopenTelemetry:\n  endpoint: http://otel:4317\n  headers:\n    Authorization: Bearer new2\n"
           "    X-Scope: {token: new3}\n")
    diff = cassandra_prometheus_config_change(live, new, "/etc/cassandra/jmx_prometheus.yml")["diff"]
    assert "+rules: [x]" in diff and "endpoint: http://otel:4317" in diff
    assert "old1" not in diff and "new2" not in diff and "new3" not in diff
    flow = cassandra_prometheus_config_change("rules: []\n", "openTelemetry: {headers: {Authorization: Bearer f1}}\n")["diff"]
    assert "f1" not in flow
    # a name inside a longer one is no secret (cassandra.yaml's num_tokens, the JMX AuthorizationProxy)
    from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
    for text in ("num_tokens: 16", "initial_token: 5", "-Dcassandra.jmx.authorizer=org.x.AuthorizationProxy"):
        assert out.mask(text) == text and not out.secret("cassandra_extra_settings", text)
    assert out.mask("token: t1") == "token: ****"


def test_the_config_with_open_telemetry_is_written_with_no_log():
    import os
    from ansible_collections.community.cassandra.tests.unit.task_files import load_tasks
    tasks = load_tasks(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "roles", "cassandra_config", "tasks",
                                    "prometheus_exporter.yml"))
    block = [t for t in tasks if t.get("name") == "The role's exporter config"][0]
    assert "openTelemetry is defined" in block["vars"]["_secrets"] and "httpServer is defined" in block["vars"]["_secrets"]


def test_the_record_of_the_parts_read_at_start():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_prometheus import cassandra_prometheus_at_start
    a = cassandra_prometheus_at_start("rules: []\nhttpServer: {x: 1}\n")
    assert a.startswith("# Managed by Ansible") and "x: 1" not in a  # a digest, no secret
    assert cassandra_prometheus_at_start("rules: [y]\nhttpServer: {x: 1}\n") == a  # the rules: not in it
    assert cassandra_prometheus_at_start("rules: []\nhttpServer: {x: 2}\n") != a
