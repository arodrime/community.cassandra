from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.module_utils.nodetool_cmd_objects import NodeToolCmd


class Failed(Exception):
    pass


class FakeModule(object):
    def __init__(self, **params):
        self.params = dict(host="127.0.0.1", port=7199, password=None, password_file=None, username=None,
                           nodetool_path=None, nodetool_flags="", debug=False, cassandra_version="5.0")
        self.params.update(params)
        self.commands = []

    def fail_json(self, **kwargs):
        raise Failed(kwargs["msg"])

    def run_command(self, cmd):
        self.commands.append(cmd)
        return 0, "", ""


@pytest.mark.parametrize("secret", [{"password_file": "/etc/cassandra/jmxremote.password"}, {"password": "p"}])
def test_password_without_username_fails(secret):
    # nodetool would run without credentials: "Credentials required"
    with pytest.raises(Failed, match="without username"):
        NodeToolCmd(FakeModule(**secret))


def test_credentials_passed():
    module = FakeModule(username="ops", password_file="/etc/cassandra/jmxremote.password")
    NodeToolCmd(module).nodetool_cmd("status")
    assert "--username ops --password-file /etc/cassandra/jmxremote.password status" in module.commands[0]


def test_no_credentials():
    module = FakeModule()
    NodeToolCmd(module).nodetool_cmd("status")
    assert "--username" not in module.commands[0] and "--password" not in module.commands[0]
