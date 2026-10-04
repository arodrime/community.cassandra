# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.modules import cassandra_move, cassandra_ring
from ansible_collections.community.cassandra.plugins.modules.cassandra_move import parse_info_tokens

INFO = """ID                     : 2c5f4b7e-6a1d-4c39-9d64-0b7e8a1f2d3c
Gossip active          : true
Native Transport active: true
Load                   : 1.21 MiB
Uptime (seconds)       : 3600
Data Center            : dc1
Rack                   : r1
Exceptions             : 0
Token                  : %s
"""
NETSTATS = "Mode: %s\nNot sending any streams.\nRead Repair Statistics:\nAttempted: 0\n"


def test_parse_info_tokens():
    assert parse_info_tokens(INFO % "-9223372036854775808") == ["-9223372036854775808"]
    assert parse_info_tokens(INFO % "1\nToken                  : 2") == ["1", "2"]
    vnodes = INFO.replace("%s", "(invoke with -T/--tokens to see all 16 tokens)")
    assert parse_info_tokens(vnodes) == []


class Exit(Exception):
    pass


def run(monkeypatch, module, outputs, token="0", check_mode=False):
    """Runs module.main() with nodetool answering outputs[subcommand]; returns (exit kwargs, failed, commands)."""
    ran = []

    class Module(object):
        def __init__(self, **kwargs):
            self.params = {"debug": False, "token": token}
            self.check_mode = check_mode

        def exit_json(self, **kwargs):
            raise Exit(kwargs, False)

        def fail_json(self, **kwargs):
            raise Exit(kwargs, True)

    class Cmd(object):
        def __init__(self, mod, cmd):
            self.cmd = cmd

        def run_command(self):
            ran.append(self.cmd)
            return outputs.get(self.cmd.split()[0], (0, "", ""))

    monkeypatch.setattr(module, "AnsibleModule", Module)
    monkeypatch.setattr(module, "NodeToolCommandSimple", Cmd)
    with pytest.raises(Exit) as e:
        module.main()
    return e.value.args[0], e.value.args[1], ran


def test_move_runs_nodetool_move(monkeypatch):
    out, failed, ran = run(monkeypatch, cassandra_move, {"info": (0, INFO % "-9223372036854775808", ""),
                                                         "netstats": (0, NETSTATS % "NORMAL", "")}, "-3074457345618258603")
    assert not failed and out["changed"]
    assert out["token_before"] == "-9223372036854775808" and out["token"] == "-3074457345618258603"
    assert ran == ["info -T", "netstats", "move -- -3074457345618258603"]


def test_move_already_there(monkeypatch):
    out, failed, ran = run(monkeypatch, cassandra_move, {"info": (0, INFO % "42", ""),
                                                         "netstats": (0, NETSTATS % "NORMAL", "")}, " 042 ")
    assert not failed and not out["changed"] and ran == ["info -T", "netstats"]


def test_move_check_mode(monkeypatch):
    out, failed, ran = run(monkeypatch, cassandra_move, {"info": (0, INFO % "1", ""),
                                                         "netstats": (0, NETSTATS % "NORMAL", "")}, "2", check_mode=True)
    assert not failed and out["changed"] and out["token"] == "2" and "move -- 2" not in ran


@pytest.mark.parametrize("info, mode, token, match", [
    (INFO % "1\nToken                  : 2", "NORMAL", "3", "has 2 tokens"),
    (INFO % "1", "MOVING", "3", "the node is MOVING, not NORMAL"),
    (INFO % "1", "NORMAL", "1,2", "token must be one integer"),
])
def test_move_refused(monkeypatch, info, mode, token, match):
    out, failed, ran = run(monkeypatch, cassandra_move, {"info": (0, info, ""), "netstats": (0, NETSTATS % mode, "")}, token)
    assert failed and match in out["msg"]
    assert not [c for c in ran if c.startswith("move")]


def test_move_failure_keeps_the_old_token(monkeypatch):
    out, failed, ran = run(monkeypatch, cassandra_move, {"info": (0, INFO % "1", ""), "netstats": (0, NETSTATS % "NORMAL", ""),
                                                         "move": (2, "", "error: target token is already owned")}, "5")
    assert failed and out["token"] == "1" and "already owned" in out["stderr"]


def test_ring_module(monkeypatch):
    out, failed, ran = run(monkeypatch, cassandra_ring, {"ring": (0, "Datacenter: dc1\n10.100.100.1 r1 Up Normal 1 KiB ? 7\n", "")})
    assert not failed and not out["changed"] and ran == ["ring"]
    assert out["ring"] == {"dc1": [{"address": "10.100.100.1", "rack": "r1", "status": "Up", "state": "Normal", "token": "7"}]}
    out, failed, ran = run(monkeypatch, cassandra_ring, {"ring": (1, "", "Connection refused")})
    assert failed and out["stderr"] == "Connection refused"
