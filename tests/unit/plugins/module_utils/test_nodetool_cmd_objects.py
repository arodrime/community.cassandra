from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCmd, version_error


class Failed(Exception):
    pass


class FakeModule(object):
    def __init__(self, result=(0, "", ""), **params):
        self.params = dict(host="127.0.0.1", port=7199, password=None, password_file=None, username=None,
                           nodetool_path=None, nodetool_flags="", debug=False, cassandra_version="5.0")
        self.params.update(params)
        self.result = result
        self.commands = []

    def fail_json(self, **kwargs):
        raise Failed(kwargs["msg"])

    def run_command(self, cmd):
        self.commands.append(cmd)
        return self.result


@pytest.mark.parametrize("secret", [{"password_file": "/etc/cassandra/jmxremote.password"}, {"password": "p"}])
def test_password_without_username_fails(secret):
    # nodetool would run without credentials: "Credentials required"
    module = FakeModule(**secret)
    with pytest.raises(Failed, match="without username"):
        NodeToolCmd(module)
    assert module.commands == []


def test_credentials_passed():
    module = FakeModule(username="ops", password_file="/etc/cassandra/jmxremote.password")
    NodeToolCmd(module).nodetool_cmd("status")
    assert "--username ops --password-file /etc/cassandra/jmxremote.password status" in module.commands[0]


def test_no_credentials():
    module = FakeModule()
    NodeToolCmd(module).nodetool_cmd("status")
    assert "--username" not in module.commands[0] and "--password" not in module.commands[0]


def test_version_detected():
    module = FakeModule(result=(0, "ReleaseVersion: 4.1.10\n", ""), cassandra_version=None)
    NodeToolCmd(module)
    assert module.params["cassandra_version"] == "4.1"


STACK = "-- StackTrace --\njavax.security.auth.login.FailedLoginException: Invalid username or password\n" \
        "\tat java.base/java.lang.Thread.run(Thread.java:840)\n"


@pytest.mark.parametrize("out, err, msg", [
    ("", "error: Invalid username or password\n" + STACK,
     "JMX login refused (Invalid username or password): check username and password or password_file"),
    ("", "error: Authentication failed! Credentials required\n-- StackTrace --\njava.lang.SecurityException: x\n",
     "JMX login refused (Authentication failed! Credentials required): check username and password or password_file"),
    ("", "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'.\n",
     "Unable to determine Cassandra version: nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException:"
     " 'Connection refused'."),
    ("", "nodetool: Failed to connect to '127.0.0.1:7199' - FailedLoginException: 'Provided username x are incorrect'.\n",
     "JMX login refused (authentication failed): check username and password or password_file"),
    ("", "error: Access denied! Invalid access level for requested MBeanServer operation.\n",
     "Unable to determine Cassandra version: Access denied! Invalid access level for requested MBeanServer operation."),
    ("", "", "Unable to determine Cassandra version: ")])
def test_version_error(out, err, msg):
    assert version_error(out, err) == msg


def test_version_failure_message():
    module = FakeModule(result=(1, "", "error: Invalid username or password\n" + STACK), cassandra_version=None)
    with pytest.raises(Failed, match=r"^JMX login refused \(Invalid username or password\)"):
        NodeToolCmd(module)
