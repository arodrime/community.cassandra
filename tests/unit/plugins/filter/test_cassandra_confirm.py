from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_confirm import cassandra_confirm_answer


def reply(user_input, start="2026-09-29 17:09:44.464659", stop="2026-09-29 17:09:46.520091"):
    return {"user_input": user_input, "start": start, "stop": stop}


@pytest.mark.parametrize("answer", ["yes", "y", "YES", "Yes", "Y", "  yes ", "y\t"])
def test_yes(answer):
    assert cassandra_confirm_answer(reply(answer)) == "go"


@pytest.mark.parametrize("answer", ["no", "n", "NO", "No", "N", " n  "])
def test_no(answer):
    assert cassandra_confirm_answer(reply(answer)) == "stop"


@pytest.mark.parametrize("answer", ["", " ", "yse", "ye", "yes please", "nope", "o", "true", "1", "y es", None])
def test_anything_else_asks_again(answer):
    assert cassandra_confirm_answer(reply(answer)) == "again"


def test_empty_answer_at_once_is_no_terminal():
    # what pause returns without a terminal: nothing, in a few ms
    assert cassandra_confirm_answer(reply("", stop="2026-09-29 17:09:44.468515")) == "no_terminal"
    assert cassandra_confirm_answer(reply(" ", stop="2026-09-29 17:09:44.464659")) == "no_terminal"
    assert cassandra_confirm_answer(reply("", start="2026-09-29 17:09:44", stop="2026-09-29 17:09:44.050000")) == "no_terminal"


def test_empty_answer_after_a_while_asks_again():
    # Enter pressed: at least a tenth of a second after the prompt
    assert cassandra_confirm_answer(reply("", stop="2026-09-29 17:09:44.570000")) == "again"
    assert cassandra_confirm_answer(reply("", start="2026-09-29 17:09:59.900000", stop="2026-09-29 17:10:00")) == "again"


def test_a_real_answer_is_never_no_terminal():
    assert cassandra_confirm_answer(reply("yes", stop="2026-09-29 17:09:44.464700")) == "go"
    assert cassandra_confirm_answer(reply("x", stop="2026-09-29 17:09:44.464700")) == "again"


@pytest.mark.parametrize("result", [None, {}, {"user_input": ""}, reply("", start="?", stop=None)])
def test_no_times_asks_again(result):
    assert cassandra_confirm_answer(result) == "again"
