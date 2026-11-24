from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCmd


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


def test_version_failure_says_the_login_was_refused():
    stderr = "error: Invalid username or password\n-- StackTrace --\nFailedLoginException\n"
    module = FakeModule(result=(1, "", stderr), cassandra_version=None)
    with pytest.raises(Failed, match=r"^JMX login refused \(Invalid username or password\): check the JMX user"):
        NodeToolCmd(module)


def test_fail_nodetool_says_the_command_its_error_and_the_advice():
    from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import fail_nodetool

    class Module(object):
        def fail_json(self, **kwargs):
            self.failed = kwargs

    module, result = Module(), {"stdout": "x"}
    fail_nodetool(module, result, "flush", 2, "", "error: Disk full", "Check it, then run it again")
    assert module.failed == {"error": "error: Disk full", "rc": 2, "changed": False, "stdout": "", "stderr": "error: Disk full",
                             "msg": "nodetool flush did not execute successfully (rc 2): error: Disk full. Check it, then"
                                    " run it again"}
    fail_nodetool(module, {}, "drain", 1, "", "", "Restart it")
    assert module.failed["msg"] == "nodetool drain did not execute successfully (rc 1). Restart it"


def test_release_series_of_nodetool_version():
    from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import release_series
    assert release_series("ReleaseVersion: 5.0.7\n") == "5.0"
    assert release_series("Picked up X\nReleaseVersion: 4.1.12") == "4.1"
    assert release_series("ReleaseVersion: 4.0.21-SNAPSHOT") == "4.0"
    # pre-releases: the series only, which cassandra_version_at_least compares
    from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import cassandra_version_at_least
    for printed in ("5.0-rc1", "5.0-beta2", "5.0-SNAPSHOT", "5.0.0-alpha1"):
        assert release_series("ReleaseVersion: %s\n" % printed) == "5.0"
        assert cassandra_version_at_least(release_series("ReleaseVersion: %s" % printed), "4.1")


@pytest.mark.parametrize("params, ssl", [({}, False), ({"ssl": False}, False), ({"ssl": True}, True)])
def test_jmx_over_tls(params, ssl):
    """--ssl: bin/nodetool adds the options of ~/.cassandra/nodetool-ssl.properties; a module without the option
    (its params): none."""
    module = FakeModule(**params)
    NodeToolCmd(module).nodetool_cmd("status")
    assert module.commands[0].endswith(" --ssl status") is ssl
