from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.modules import (
    cassandra_batchlogreplaythrottle,
    cassandra_compactionthroughput,
    cassandra_interdcstreamthroughput,
    cassandra_maxhintwindow,
    cassandra_streamthroughput,
    cassandra_timeout,
    cassandra_traceprobability,
)

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch


class ExitJson(Exception):
    pass


class FailJson(Exception):
    pass


class FakeModule(object):
    """AnsibleModule stand-in: run_command answers the get command with
    FakeModule.get_result and records every nodetool sub-command."""

    def __init__(self, argument_spec=None, supports_check_mode=False):
        self.params = dict(FakeModule.params)
        self.check_mode = self.params.pop('_check_mode', False)
        self.commands = FakeModule.commands

    def run_command(self, cmd):
        sub_command = cmd.split("--port 7199 ", 1)[1]
        self.commands.append(sub_command)
        if sub_command.startswith("get"):
            return FakeModule.get_result
        return FakeModule.set_result

    def debug(self, msg):
        pass

    def exit_json(self, **kwargs):
        raise ExitJson(kwargs)

    def fail_json(self, **kwargs):
        raise FailJson(kwargs)


def run_main(module, version, get_out, value=None, param='value', extra=None,
             check_mode=False, get_rc=0, set_rc=0):
    """Run module.main() with nodetool mocked; return (exception, sub-commands)."""
    FakeModule.params = {
        'host': '127.0.0.1', 'port': 7199, 'password': None, 'password_file': None,
        'username': None, 'nodetool_path': None, 'nodetool_flags': '', 'debug': False,
        'cassandra_version': version, param: value, '_check_mode': check_mode,
    }
    FakeModule.params.update(extra or {})
    FakeModule.commands = []
    FakeModule.get_result = (get_rc, get_out, 'get error' if get_rc else '')
    FakeModule.set_result = (set_rc, '', 'set error' if set_rc else '')
    with patch.object(module, 'AnsibleModule', FakeModule):
        with pytest.raises((ExitJson, FailJson)) as exc:
            module.main()
    return exc.value, FakeModule.commands


# module, extra params, version, get sub-command, nodetool output,
# parsed (current, unit), a value different from current and its set command
CASES = [
    (cassandra_streamthroughput, None, "4.0", "getstreamthroughput",
     "Current stream throughput: 200 Mb/s\n", (200.0, "Mb/s"), 500, "setstreamthroughput 500"),
    (cassandra_streamthroughput, None, "4.1", "getstreamthroughput -d",
     "Current stream throughput: 201.326592 Mb/s\n", (201.326592, "Mb/s"), 200, "setstreamthroughput 200"),
    (cassandra_streamthroughput, None, "5.0", "getstreamthroughput -d",
     "Current stream throughput: 200.0 Mb/s\n", (200.0, "Mb/s"), 500, "setstreamthroughput 500"),
    (cassandra_interdcstreamthroughput, None, "4.0", "getinterdcstreamthroughput",
     "Current inter-datacenter stream throughput: 200 Mb/s\n", (200.0, "Mb/s"), 500,
     "setinterdcstreamthroughput 500"),
    (cassandra_interdcstreamthroughput, None, "4.1", "getinterdcstreamthroughput -d",
     "Current stream throughput: 200.0 Mb/s\n", (200.0, "Mb/s"), 500, "setinterdcstreamthroughput 500"),
    (cassandra_interdcstreamthroughput, None, "5.0", "getinterdcstreamthroughput -d",
     "Current stream throughput: 200.0 Mb/s\n", (200.0, "Mb/s"), 500, "setinterdcstreamthroughput 500"),
    (cassandra_compactionthroughput, None, "4.0", "getcompactionthroughput",
     "Current compaction throughput: 64 MB/s\n", (64.0, "MB/s"), 32, "setcompactionthroughput 32"),
    (cassandra_compactionthroughput, None, "4.1", "getcompactionthroughput -d",
     "Current compaction throughput: 64.0 MiB/s\n", (64.0, "MiB/s"), 32, "setcompactionthroughput 32"),
    (cassandra_compactionthroughput, None, "5.0", "getcompactionthroughput -d",
     "Current compaction throughput: 64.0 MiB/s\n", (64.0, "MiB/s"), 32, "setcompactionthroughput 32"),
    (cassandra_batchlogreplaythrottle, None, "5.0", "getbatchlogreplaythrottle",
     "Batchlog replay throttle: 1024 KB/s\n", (1024, "KB/s"), 2048, "setbatchlogreplaythrottle  2048"),
    (cassandra_maxhintwindow, None, "5.0", "getmaxhintwindow",
     "Current max hint window: 10800000 ms\n", (10800000, "ms"), 3600000, "setmaxhintwindow  -- 3600000"),
    (cassandra_traceprobability, None, "5.0", "gettraceprobability",
     "Current trace probability: 0.0\n", (0.0, None), 0.5, "settraceprobability 0.5"),
    (cassandra_timeout, {'timeout_type': 'read'}, "5.0", "gettimeout read",
     "Current timeout for type read: 5000 ms\n", (5000, "ms"), 10000, "settimeout read 10000"),
]
IDS = ["{0}-{1}".format(c[0].__name__.split('.')[-1], c[2]) for c in CASES]


def param_of(module):
    return 'timeout' if module is cassandra_timeout else 'value'


def assert_current(res, out, parsed):
    assert res['current'] == parsed[0]
    assert type(res['current']) is type(parsed[0])
    if 'unit' in res or parsed[1] is not None:
        assert res['unit'] == parsed[1]
    assert res['current_raw'] == out.strip()


@pytest.mark.parametrize("check_mode", [False, True])
@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_read_mode(module, extra, version, get_cmd, out, parsed, other, set_cmd, check_mode):
    res, commands = run_main(module, version, out, param=param_of(module), extra=extra,
                             check_mode=check_mode)
    assert isinstance(res, ExitJson)
    assert res.args[0]['changed'] is False
    assert_current(res.args[0], out, parsed)
    assert commands == [get_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_read_mode_get_failure(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    res, commands = run_main(module, version, "", param=param_of(module), extra=extra, get_rc=1)
    assert isinstance(res, FailJson)
    assert res.args[0]['msg'] == "get command failed"
    assert res.args[0]['name'] == get_cmd
    assert 'current' not in res.args[0]
    assert commands == [get_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_read_mode_unparsable_output(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    res, commands = run_main(module, version, "something else\n", param=param_of(module), extra=extra)
    assert isinstance(res, FailJson)
    assert res.args[0]['msg'] == "unable to parse the get command output: something else"
    assert res.args[0]['name'] == get_cmd
    assert 'current' not in res.args[0]
    assert commands == [get_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_get_failure_with_parsable_output(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    # a failed get is not trusted, even if its output parses
    res, commands = run_main(module, version, out, param=param_of(module), extra=extra, get_rc=1)
    assert isinstance(res, FailJson)
    assert res.args[0]['msg'] == "get command failed"
    assert 'current' not in res.args[0]
    if parsed[0] != int(parsed[0]) and module is not cassandra_traceprobability:
        return
    value = parsed[0] if module is cassandra_traceprobability else int(parsed[0])
    # same value: unchanged behaviour, the failed get makes the module fail
    res, commands = run_main(module, version, out, value=value, param=param_of(module), extra=extra,
                             get_rc=1)
    assert isinstance(res, FailJson)
    assert 'current' not in res.args[0]
    assert commands == [get_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_set_same_value(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    if parsed[0] != int(parsed[0]) and module is not cassandra_traceprobability:
        pytest.skip("not settable with an integer value")
    value = parsed[0] if module is cassandra_traceprobability else int(parsed[0])
    res, commands = run_main(module, version, out, value=value, param=param_of(module), extra=extra)
    assert isinstance(res, ExitJson)
    assert not res.args[0].get('changed')
    assert_current(res.args[0], out, parsed)
    assert commands == [get_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_set_other_value(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    res, commands = run_main(module, version, out, value=other, param=param_of(module), extra=extra)
    assert isinstance(res, ExitJson)
    assert res.args[0]['changed'] is True
    # current is the value before the change
    assert_current(res.args[0], out, parsed)
    assert commands == [get_cmd, set_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_set_other_value_check_mode(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    res, commands = run_main(module, version, out, value=other, param=param_of(module), extra=extra,
                             check_mode=True)
    assert isinstance(res, ExitJson)
    assert res.args[0]['changed'] is True
    assert_current(res.args[0], out, parsed)
    assert commands == [get_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_set_failure(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    res, commands = run_main(module, version, out, value=other, param=param_of(module), extra=extra,
                             set_rc=1)
    assert isinstance(res, FailJson)
    assert res.args[0]['changed'] is False
    assert commands == [get_cmd, set_cmd]


@pytest.mark.parametrize("module, extra, version, get_cmd, out, parsed, other, set_cmd", CASES, ids=IDS)
def test_set_when_get_fails(module, extra, version, get_cmd, out, parsed, other, set_cmd):
    # unchanged behaviour: with a value, a failed get still tries the set
    res, commands = run_main(module, version, "", value=other, param=param_of(module), extra=extra,
                             get_rc=1)
    assert isinstance(res, ExitJson)
    assert res.args[0]['changed'] is True
    assert 'current' not in res.args[0]
    assert commands == [get_cmd, set_cmd]


@pytest.mark.parametrize("module, get_cmd", [
    (cassandra_streamthroughput, "getstreamthroughput -d"),
    (cassandra_interdcstreamthroughput, "getinterdcstreamthroughput -d"),
])
@pytest.mark.parametrize("version", ["4.1", "5.0"])
def test_unlimited(module, get_cmd, version):
    out = "Current stream throughput: unlimited\n"
    res, commands = run_main(module, version, out)
    assert res.args[0]['changed'] is False
    assert res.args[0]['current'] == 0.0
    assert res.args[0]['unit'] is None
    # 0 (no throttling) is printed as unlimited since 4.1: no change
    res, commands = run_main(module, version, out, value=0)
    assert isinstance(res, ExitJson)
    assert not res.args[0].get('changed')
    assert commands == [get_cmd]


def test_traceprobability_exponent():
    out = "Current trace probability: 1.0E-4\n"
    res, commands = run_main(cassandra_traceprobability, "5.0", out, value=0.0001)
    assert isinstance(res, ExitJson)
    assert not res.args[0].get('changed')
    assert res.args[0]['current'] == 0.0001
    assert commands == ["gettraceprobability"]


@pytest.mark.parametrize("module, extra, out, msg", [
    (cassandra_batchlogreplaythrottle, None, "Batchlog replay throttle: 1024 KB/s\n",
     "Batch log replay throttle is 1024 KB/s"),
    (cassandra_maxhintwindow, None, "Current max hint window: 1024 ms\n", "Max Hint Window is 1024 ms"),
    (cassandra_timeout, {'timeout_type': 'write'}, "Current timeout for type write: 2000 ms\n",
     "write timeout is 2000 ms"),
])
def test_read_mode_msg(module, extra, out, msg):
    res, commands = run_main(module, "4.0", out, param=param_of(module), extra=extra)
    assert res.args[0]['msg'] == msg


def test_maxhintwindow_unchanged_msg():
    res, commands = run_main(cassandra_maxhintwindow, "4.0", "Current max hint window: 1024 ms\n", value=1024)
    assert res.args[0]['msg'] == "Max Hint Window is already 1024 ms"
