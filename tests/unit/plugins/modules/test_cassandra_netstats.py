from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os

import pytest

from ansible_collections.community.cassandra.plugins.modules.cassandra_netstats import parse_netstats

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


def test_normal_not_streaming():
    assert parse_netstats(load_fixture("nodetool_netstats_normal.txt")) == ("NORMAL", [])


def test_joining_streaming():
    mode, streams = parse_netstats(load_fixture("nodetool_netstats_joining.txt"))
    assert mode == "JOINING"
    assert streams[0].startswith("Bootstrap ")
    assert len(streams) == 4


def test_node_down_output():
    assert parse_netstats("") == ("", [])


def test_failure_returns_stderr(monkeypatch):
    from ansible_collections.community.cassandra.plugins.modules import cassandra_netstats

    class Module(object):
        params = {"debug": False}

        def __init__(self, **kwargs):
            pass

        def fail_json(self, **kwargs):
            raise SystemExit(kwargs)

    class Cmd(object):
        def __init__(self, module, cmd):
            pass

        def run_command(self):
            return 1, "", "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'."

    monkeypatch.setattr(cassandra_netstats, "AnsibleModule", Module)
    monkeypatch.setattr(cassandra_netstats, "NodeToolCommandSimple", Cmd)
    with pytest.raises(SystemExit) as e:
        cassandra_netstats.main()
    assert "Connection refused" in e.value.args[0]["stderr"]
