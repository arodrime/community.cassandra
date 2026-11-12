from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Before ansible-core 2.19 a variable set by a template (a task's vars, set_fact, -e) is text: "110", "" for
# none, AnsibleUnsafeText when it comes from a result. The filters take numbers as such too.

import pytest

from ansible.utils.unsafe_proxy import AnsibleUnsafeText

from ansible_collections.community.cassandra.plugins.filter import cassandra_output
from ansible_collections.community.cassandra.plugins.filter.cassandra_summary import cassandra_op_recap
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out


def as_text(value):
    """value as a template hands it over before 2.19: text, plain or unsafe."""
    return [str(value), AnsibleUnsafeText(str(value))]


@pytest.mark.parametrize("given, number", [
    (None, None), ("", None), ("None", None), (AnsibleUnsafeText(""), None), (12, 12), ("12", 12), (" 12 ", 12),
    ("12.0", 12), ("12.5", 12.5), (12.5, 12.5), (AnsibleUnsafeText("110"), 110), ("-3", -3)])
def test_number(given, number):
    assert out.number(given) == number and type(out.number(given)) is type(number)


def test_number_default_and_no_number():
    assert out.number("", 0) == 0 and out.number(None, 7) == 7 and out.number("5", 7) == 5
    with pytest.raises(ValueError):
        out.number("ten")


@pytest.mark.parametrize("seconds", as_text(110) + ["110.4"])
def test_duration(seconds):
    assert out.duration(seconds) == "1m50s" and cassandra_output.cassandra_duration(seconds) == "1m50s"


@pytest.mark.parametrize("none", ["", AnsibleUnsafeText(""), "None", None])
def test_duration_of_none(none):
    assert out.duration(none) == "0s"


@pytest.mark.parametrize("elapsed", as_text(110))
def test_normal_wait_line_elapsed(elapsed):
    # wait_normal.yml: _cassandra_op_to_normal, a task's vars
    assert cassandra_output.cassandra_normal_wait_line("NORMAL", done=True, waited="30", index="1", steps="5",
                                                       node="node1", operation="restart", elapsed=elapsed) == \
        "[1/5] node1  restarted  NORMAL after 30s  1m50s"


@pytest.mark.parametrize("none", ["", AnsibleUnsafeText(""), None])
def test_normal_wait_line_without_elapsed(none):
    assert cassandra_output.cassandra_normal_wait_line("NORMAL", done=True, waited="30", node="node1",
                                                       operation="restart", elapsed=none) == \
        "node1  restarted  NORMAL after 30s"


def test_normal_wait_line_waiting():
    assert cassandra_output.cassandra_normal_wait_line("JOINING", waited=AnsibleUnsafeText("70"), timeout="600",
                                                       index="2", steps="5", node="node2", operation="restart") == \
        "[2/5] node2  restart  waiting  UJ, waiting for UN  1m10s/10m"


def recap(seconds):
    return cassandra_op_recap([{"node": "n1", "result": "restart done in 100s"}], operation="rolling_restart",
                              cluster="c", seconds=seconds)[0]


@pytest.mark.parametrize("seconds", as_text(65))
def test_op_recap_seconds(seconds):
    # summary.yml: _seconds, a task's vars
    assert recap(seconds) == recap(65) and recap(seconds).endswith("  1m05s")


@pytest.mark.parametrize("none", ["", AnsibleUnsafeText(""), None])
def test_op_recap_without_seconds(none):
    assert recap(none) == recap(None) and not recap(none).endswith("s")
