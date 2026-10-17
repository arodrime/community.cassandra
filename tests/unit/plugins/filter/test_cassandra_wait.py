from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The texts themselves, as the tasks render them: tests/unit/roles/test_wait_failure_text.py

from ansible_collections.community.cassandra.plugins.filter.cassandra_stream import (
    cassandra_cleanup_failure, cassandra_stream_failure)


def test_a_failed_job_says_its_messages_and_its_stderr():
    assert cassandra_stream_failure("job_failed", "n1", "rebuild", job={"msg": ["a", None, ["b"]], "stderr": "e"},
                                    last="r") == "n1, rebuild: failed: a b e Last progress: r"
    assert cassandra_stream_failure("job_failed", "n1", "rebuild", last="r") == "n1, rebuild: failed:   Last progress: r"


def test_the_advice_replaces_the_generic_one_when_given():
    stalled = cassandra_stream_failure("stalled", "n1", "move", idle=90, stall_time=60, last="r")
    assert stalled == ("n1, move: no streaming progress for 1m (cassandra_stream_stall_time 60s). Nothing was stopped:"
                       " check nodetool netstats and system.log on the nodes. Last progress: r")
    assert "stopped: look at n2. Last" in cassandra_stream_failure("stalled", "n1", "move", advice="look at n2.", last="r")


def test_a_cleanup_job_without_a_result_is_named_with_the_failed_ones():
    jobs = [{"item": {"item": "n2"}}, {"item": {"item": "n1"}, "failed": True, "msg": "x"},
            {"item": {"item": "n3"}, "finished": 1}, {"item": {"item": "n4"}, "unreachable": True}]
    assert cassandra_cleanup_failure("failed", jobs, last="r") == (
        "Cleanup failed or can't be followed on n1, n2: x (see system.log there). Last progress: r")
