from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The modules that run one nodetool command: a non-zero rc fails the task
# explicitly, with nodetool's error line, rc, stdout and stderr, and what to check.

import pytest

from ansible_collections.community.cassandra.plugins.modules import (
    cassandra_cleanup,
    cassandra_drain,
    cassandra_flush,
    cassandra_garbagecollect,
    cassandra_reload,
    cassandra_truncatehints,
    cassandra_upgradesstables,
    cassandra_verify,
)

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch


class ExitJson(Exception):
    pass


class FailJson(Exception):
    pass


class Params(dict):
    """The module's parameters: None for an option the test does not set."""

    def __missing__(self, key):
        return None


class FakeModule(object):
    """AnsibleModule stand-in: run_command answers FakeModule.result and
    records the nodetool sub-commands."""

    def __init__(self, argument_spec=None, supports_check_mode=False):
        FakeModule.supports_check_mode = supports_check_mode
        self.params = Params(FakeModule.params)

    def run_command(self, cmd):
        FakeModule.commands.append(cmd.split("--port 7199 ", 1)[1])
        return FakeModule.result

    def debug(self, msg):
        pass

    def exit_json(self, **kwargs):
        raise ExitJson(kwargs)

    def fail_json(self, **kwargs):
        raise FailJson(kwargs)


# module, extra options, the nodetool sub-command it runs
CASES = [
    (cassandra_drain, {}, "drain"),
    (cassandra_reload, {"reload": "seeds"}, "reloadseeds"),
    (cassandra_verify, {}, "verify"),
    (cassandra_truncatehints, {}, "truncatehints"),
    (cassandra_flush, {}, "flush"),
    (cassandra_garbagecollect, {}, "garbagecollect"),
    (cassandra_cleanup, {}, "cleanup"),
    (cassandra_upgradesstables, {}, "upgradesstables"),
]
IDS = [case[2] for case in CASES]


def run_main(module, extra, rc, out="", err="", debug=False):
    """Run module.main() with nodetool mocked; return (exception, sub-commands)."""
    FakeModule.params = dict({"host": "127.0.0.1", "port": 7199, "nodetool_flags": "", "debug": debug,
                              "cassandra_version": "4.1"}, **extra)
    FakeModule.commands = []
    FakeModule.result = (rc, out, err)
    with patch.object(module, "AnsibleModule", FakeModule):
        with pytest.raises((ExitJson, FailJson)) as exc:
            module.main()
    return exc.value, FakeModule.commands


@pytest.mark.parametrize("module, extra, command", CASES, ids=IDS)
def test_success(module, extra, command):
    exc, commands = run_main(module, extra, 0)
    assert isinstance(exc, ExitJson)
    result = exc.args[0]
    assert result["changed"] is True
    assert result["msg"] == "nodetool {0} executed successfully".format(command)
    assert "rc" not in result and "stderr" not in result
    assert [c.split()[0] for c in commands] == [command]


@pytest.mark.parametrize("module, extra, command", CASES, ids=IDS)
def test_failure_fails(module, extra, command):
    err = "error: Some failure\n-- StackTrace --\njava.lang.RuntimeException: Some failure\n"
    exc, commands = run_main(module, extra, 1, out="", err=err)
    assert isinstance(exc, FailJson)
    result = exc.args[0]
    assert result["changed"] is False
    assert result["rc"] == 1
    assert result["stderr"] == err.strip() and result["error"] == "error: Some failure"
    assert result["msg"].startswith(
        "nodetool {0} did not execute successfully (rc 1): error: Some failure. ".format(command))
    assert [c.split()[0] for c in commands] == [command]


@pytest.mark.parametrize("module, extra, command", CASES, ids=IDS)
def test_failure_without_output(module, extra, command):
    exc = run_main(module, extra, 2)[0]
    assert isinstance(exc, FailJson)
    assert exc.args[0]["msg"].startswith("nodetool {0} did not execute successfully (rc 2). ".format(command))


@pytest.mark.parametrize("module, extra, command", CASES, ids=IDS)
def test_failure_error_line_from_stdout(module, extra, command):
    """No 'error:' line: nodetool's first line, wherever it printed it."""
    exc = run_main(module, extra, 1, out="nodetool: something went wrong\nmore\n")[0]
    assert isinstance(exc, FailJson)
    assert ": nodetool: something went wrong. " in exc.args[0]["msg"]
    assert exc.args[0]["stdout"] == "nodetool: something went wrong\nmore"


@pytest.mark.parametrize("module, extra, command", CASES, ids=IDS)
def test_check_mode_not_supported(module, extra, command):
    """No check mode: Ansible skips them under --check, which the playbooks rely on (no drain, no WARNING)."""
    run_main(module, extra, 0)
    assert FakeModule.supports_check_mode is False


def test_drain_failure_says_what_to_check():
    msg = run_main(cassandra_drain, {}, 1, err="error: null")[0].args[0]["msg"]
    assert msg == ("nodetool drain did not execute successfully (rc 1): error: null. If its Mode (nodetool netstats) is"
                   " DRAINING or DRAINED, the node is out of the cluster: restart Cassandra on it. Else check its"
                   " system.log, then drain it again")


def test_reload_failure_names_the_reload():
    exc = run_main(cassandra_reload, {"reload": "ssl"}, 1, err="error: keystore")[0]
    assert exc.args[0]["msg"].startswith("nodetool reloadssl did not execute successfully (rc 1): error: keystore. ")


def test_reloadseeds_failure_with_rc_0():
    """reloadseeds returns 0 when the seed provider fails: its output says so."""
    exc = run_main(cassandra_reload, {"reload": "seeds"}, 0, out="Failed to reload the seed node list.\n")[0]
    assert isinstance(exc, FailJson)
    assert exc.args[0]["msg"].startswith(
        "nodetool reloadseeds did not execute successfully (rc 0): Failed to reload the seed node list. ")


@pytest.mark.parametrize("out", ["Updated seed node IP list, excluding the current node's IP: 10.0.0.2",
                                 "Seed node list does not contain any remote node IPs"])
def test_reloadseeds_success(out):
    exc = run_main(cassandra_reload, {"reload": "seeds"}, 0, out=out + "\n")[0]
    assert isinstance(exc, ExitJson) and exc.args[0]["changed"] is True


def test_the_message_has_one_full_stop():
    """nodetool's line ending with a full stop: not two."""
    msg = run_main(cassandra_flush, {}, 2, err="error: Disk full.")[0].args[0]["msg"]
    assert "(rc 2): error: Disk full. The memtables" in msg


def test_jvm_banner_is_not_the_error():
    err = "Picked up JAVA_TOOL_OPTIONS: -Xss1m\nnodetool: Failed to connect to '127.0.0.1:7199'\n"
    msg = run_main(cassandra_drain, {}, 1, err=err)[0].args[0]["msg"]
    assert "(rc 1): nodetool: Failed to connect to '127.0.0.1:7199'. " in msg
