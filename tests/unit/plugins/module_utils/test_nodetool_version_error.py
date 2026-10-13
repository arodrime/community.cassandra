from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The first nodetool call of a module (its version) failing on a JMX login
# refused says so, naming the options to check, not "Unable to determine
# Cassandra version:" with an empty reason.

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import version_error
from ansible_collections.community.cassandra.plugins.filter.cassandra_health import (
    _error, cassandra_health_findings, cassandra_health_report)

STACK = "-- StackTrace --\njavax.security.auth.login.FailedLoginException: Invalid username or password\n" \
        "\tat java.base/java.lang.Thread.run(Thread.java:840)\n"


@pytest.mark.parametrize("out, err, msg", [
    ("", "error: Invalid username or password\n" + STACK,
     "JMX login refused (Invalid username or password): check the JMX user and its password or password file"
     " (cassandra_jmx_username, cassandra_jmx_password_file or cassandra_jmx_password)"),
    ("", "error: Authentication failed! Credentials required\n-- StackTrace --\njava.lang.SecurityException: x\n",
     "JMX login refused (Authentication failed! Credentials required): check the JMX user and its password or"
     " password file (cassandra_jmx_username, cassandra_jmx_password_file or cassandra_jmx_password)"),
    # JMX not reached: where, why in a word, what to look at (not "Unable to determine Cassandra version")
    ("", "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'.\n",
     "nodetool gets no answer from JMX on 127.0.0.1:7199 (Connection refused): Cassandra is not running there, or its"
     " JMX listens on another port (cassandra_jmx_port). Check: systemctl status cassandra, and its system.log"),
    ("", "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused (Connection refused)'.\n",
     "nodetool gets no answer from JMX on 127.0.0.1:7199 (Connection refused): Cassandra is not running there, or its"
     " JMX listens on another port (cassandra_jmx_port). Check: systemctl status cassandra, and its system.log"),
    ("", "nodetool: Failed to connect to '10.0.0.9:7199' - SocketTimeoutException: 'connect timed out'.\n",
     "nodetool gets no answer from JMX on 10.0.0.9:7199 (no answer in time): a firewall, or JMX listening on another"
     " address (LOCAL_JMX in cassandra-env.sh)"),
    ("", "nodetool: Failed to connect to '10.0.0.9:7199' - NoRouteToHostException: 'No route to host'.\n",
     "nodetool gets no answer from JMX on 10.0.0.9:7199 (no route to host): the host is down or out of reach, or a"
     " firewall rejects it"),
    ("", "nodetool: Failed to connect to 'nohost:7199' - UnknownHostException: 'nohost: Name or service not known'.\n",
     "nodetool gets no answer from JMX on nohost:7199 (unknown host): that host name does not resolve"),
    ("", "nodetool: Failed to connect to '127.0.0.1:7199'\n",
     "nodetool can't reach JMX on 127.0.0.1:7199: is Cassandra running there (systemctl status cassandra), on that JMX"
     " port (cassandra_jmx_port)?"),
    ("", "nodetool: Failed to connect to '127.0.0.1:7199' - IOException: 'something else'.\n",
     "nodetool can't reach JMX on 127.0.0.1:7199 (something else): is Cassandra running there (systemctl status"
     " cassandra), on that JMX port (cassandra_jmx_port)?"),
    # anything else: nodetool's own line
    ("", "/bin/sh: 1: nodetool: not found\n", "nodetool version failed: is Cassandra installed and running?"
                                              " (/bin/sh: 1: nodetool: not found)"),
    ("", "", "nodetool version failed: is Cassandra installed and running? (no error printed)")])
def test_version_error(out, err, msg):
    assert version_error(out, err) == msg


def test_version_error_names_the_address_asked():
    assert version_error("", "error: boom\n", "127.0.0.1", 7199) == \
        "nodetool version failed on 127.0.0.1:7199: is Cassandra installed and running? (boom)"
    # "Connection refused" stays in the message: what tells a stopped node (cassandra_health, cassandra_stream)
    assert "Connection refused" in version_error("", "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException:"
                                                     " 'Connection refused'.", "127.0.0.1", 7199)


def test_health_says_the_login_once_not_gossip_down():
    failed = {"failed": True, "msg": version_error("", "error: Invalid username or password\n" + STACK),
              "stderr": "error: Invalid username or password\n" + STACK}
    assert _error(failed) == failed["msg"]  # no stack trace line after it
    found = cassandra_health_findings([{"from": "n1", "result": failed}], 3, "n1", gossip=failed, binary=failed,
                                      netstats=failed)
    assert [f["kind"] for f in found] == ["nodetool", "netstats"]
    assert "JMX login refused" in found[0]["text"]
    # nodetool status answered on this node: the failed checks are said as such
    found = cassandra_health_findings([{"from": "n1", "result": {"cluster_status": {"dc1": {"nodes": []}}}}], 0, "n1", gossip=failed,
                                      binary={"is_up": True})
    assert [f["text"] for f in found] == ["the gossip check failed on n1: " + failed["msg"]]
    timeout = {"failed": True, "msg": "statusbinary command failed"}
    found = cassandra_health_findings([{"from": "n1", "result": {"cluster_status": {"dc1": {"nodes": []}}}}], 0, "n1",
                                      gossip=failed, binary=timeout)
    assert [f["text"] for f in found] == ["the gossip check failed on n1: " + failed["msg"],
                                          "the native transport (CQL) check failed on n1: statusbinary command failed"]
    # the report: said as a check that failed, not as nodetool status; not merged with a status failure
    status_failed = {"kind": "nodetool", "on": "n2", "error": "statusbinary command failed",
                     "text": "nodetool status failed on n2: statusbinary command failed"}
    report = cassandra_health_report({"n1": found, "n2": [status_failed]}, "c", ["n1", "n2"])
    assert "  nodetool:  native transport (CQL) check failed on n1: statusbinary command failed" in report["lines"]
    assert "  nodetool:  status failed on n2: statusbinary command failed" in report["lines"]
    # a gossip that answered "not running" is still a problem
    down = cassandra_health_findings([], 3, "n1", gossip={"is_up": False}, binary={"is_up": True})
    assert [f["kind"] for f in down] == ["gossip"]


def test_health_does_not_repeat_nodetool_line_after_the_jmx_message():
    err = "nodetool: Failed to connect to '10.0.0.5:7199' - ConnectException: 'Connection refused'.\n"
    failed = {"failed": True, "msg": version_error("", err, "10.0.0.5", 7199), "stderr": err}
    assert _error(failed) == failed["msg"] and "Failed to connect" not in _error(failed)
