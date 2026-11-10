from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# cassandra_schema on real nodetool describecluster outputs of 2-node clusters
# (fixtures/nodetool_describecluster_<series>_*.txt): both nodes up and agreed;
# node2 stopped (UNREACHABLE); node2 paused while a keyspace was created on
# node1, then back before it pulled the new schema (two versions).

import os

import pytest

from ansible_collections.community.cassandra.plugins.modules import cassandra_schema

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
SERIES = ["40", "41", "50"]


def describecluster(series, case):
    with open(os.path.join(FIXTURES, "nodetool_describecluster_%s_%s.txt" % (series, case))) as f:
        return f.read()


class ExitJson(Exception):
    pass


class FailJson(Exception):
    pass


class FakeModule(object):

    def __init__(self, argument_spec=None, supports_check_mode=False):
        self.params = dict(FakeModule.params)

    def exit_json(self, **kwargs):
        raise ExitJson(kwargs)

    def fail_json(self, **kwargs):
        raise FailJson(kwargs)


def run_main(answers, poll=1, uuid=None):
    """main() with nodetool describecluster answering each (rc, stdout, stderr) of answers in turn
    (the last one again after them): (exception class, result, number of calls, sleeps)."""
    FakeModule.params = {"uuid": uuid, "poll": poll, "interval": 10, "debug": False}
    calls, sleeps = [], []

    class FakeNodeTool(object):

        def __init__(self, module):
            pass

        def status_command(self):
            calls.append(1)
            return answers[min(len(calls), len(answers)) - 1]

    with patch.object(cassandra_schema, "AnsibleModule", FakeModule), \
            patch.object(cassandra_schema, "NodeToolStatusCommand", FakeNodeTool), \
            patch.object(cassandra_schema.time, "sleep", sleeps.append):
        with pytest.raises((ExitJson, FailJson)) as exc:
            cassandra_schema.main()
    return type(exc.value), exc.value.args[0], len(calls), sleeps


def ok(series, case):
    return (0, describecluster(series, case), "")


@pytest.mark.parametrize("series", SERIES)
def test_agreement(series):
    kind, result, calls, sleeps = run_main([ok(series, "agreement")], poll=6)
    assert kind is ExitJson, result
    assert result["msg"] == "The cluster has reached schema consensus"
    assert len(result["schema_status"]) == 1 and len(list(result["schema_status"].values())[0]) == 2
    assert calls == 1 and sleeps == [] and "unreachable_nodes" not in result and "unreachable" not in result


@pytest.mark.parametrize("series", SERIES)
def test_disagreement_fails_after_every_poll(series):
    kind, result, calls, sleeps = run_main([ok(series, "disagreement")], poll=3)
    assert kind is FailJson, result
    versions = sorted(result["schema_status"].items())
    assert len(versions) == 2
    assert result["msg"] == "The cluster has not reached consensus on the schema: 2 versions, %s on %s; %s on %s" % (
        versions[0][0], versions[0][1][0], versions[1][0], versions[1][1][0])
    assert calls == 3 and sleeps == [10, 10] and result["iterations"] == 3


@pytest.mark.parametrize("series", SERIES)
def test_disagreement_then_agreement_goes_on(series):
    kind, result, calls, sleeps = run_main([ok(series, "disagreement"), ok(series, "agreement")], poll=6)
    assert kind is ExitJson, result
    assert calls == 2 and sleeps == [10] and result["iterations"] == 2


@pytest.mark.parametrize("series", SERIES)
def test_unreachable_is_not_a_version(series):
    # node2 down: its schema is not known; the live nodes agree (the ring check says the node is down)
    kind, result, calls, sleeps = run_main([ok(series, "unreachable")], poll=6)
    assert kind is ExitJson, result
    assert list(result["schema_status"].values()) == [["172.20.0.2"]]
    assert result["unreachable_nodes"] == ["172.20.0.3"] and "unreachable" not in result
    assert result["msg"] == "The cluster has reached schema consensus (unreachable, not counted: 172.20.0.3)"
    assert calls == 1


@pytest.mark.parametrize("series", SERIES)
def test_expected_version(series):
    out = describecluster(series, "agreement")
    version = list(cassandra_schema.cluster_schema(out))[0]
    kind, result, calls, sleeps = run_main([ok(series, "agreement")], uuid=version)
    assert kind is ExitJson and result["msg"] == "The cluster has reached consensus with the expected version"
    other = "00000000-0000-3000-8000-000000000000"
    kind, result, calls, sleeps = run_main([ok(series, "agreement")], poll=2, uuid=other)
    assert kind is FailJson, result
    assert result["msg"] == "The cluster agrees on schema version %s, not the expected %s" % (version, other)
    assert calls == 2


def test_every_poll_failing_is_nodetool_s_error():
    refused = (1, "", "nodetool: Failed to connect to '127.0.0.1:7199' - ConnectException: 'Connection refused'.")
    kind, result, calls, sleeps = run_main([refused], poll=3)
    assert kind is FailJson, result
    assert result["msg"] == "nodetool error: " + refused[2]
    assert result["rc"] == 1 and result["schema_status"] == {} and calls == 3


def test_a_failure_then_agreement_goes_on():
    refused = (1, "", "nodetool: Failed to connect")
    kind, result, calls, sleeps = run_main([refused, ok("50", "agreement")], poll=3)
    assert kind is ExitJson, result
    assert calls == 2


def test_no_version_listed_fails():
    kind, result, calls, sleeps = run_main([(0, "Cluster Information:\n\tName: my_cluster\n", "")])
    assert kind is FailJson and result["msg"] == "nodetool describecluster listed no schema version"
