"""The rule of plugin_utils/cassandra_secrets.py, by its lists: each word does what its comment says, in a variable's
name and in a line (the masking itself: test_cassandra_output.py). The words are written out here, one case each, so
that a word taken out of a list fails its case."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_secrets as secrets


@pytest.mark.parametrize("name", ["password", "passwd", "secret", "private_key", "sse_c_key", "access_key",
                                  "key_material", "auth_token", "api_token", "apitoken", "api_key", "apikey",
                                  "access_token", "bearer_token", "refresh_token", "session_token", "private_token",
                                  "passphrase", "encryption_key", "ca_key"])
def test_a_secret_word_anywhere_in_a_name(name):
    for key in (name, "cassandra_%s" % name, "my_%s_old" % name, name.upper()):
        assert secrets.hidden(key, "v1"), key
        assert secrets.mask("%s: v1" % key) == "%s: ****" % key
        assert secrets.mask("-Dx.%s=v1 -Xmx1G" % key) == "-Dx.%s=****" % key


@pytest.mark.parametrize("word", ["_kspw", "_tspw", "_pw", "_pass"])
def test_a_short_word_at_the_end_of_a_variable_anywhere_in_a_line(word):
    assert secrets.hidden("cassandra%s" % word, "v1") and not secrets.hidden("cassandra%s_old" % word, "v1")
    assert secrets.mask("x%s_old=v1" % word) == "x%s_old=****" % word


@pytest.mark.parametrize("name", ["authorization", "headers", "token"])
def test_an_own_name_alone_only(name):
    assert secrets.hidden(name, "v1") and not secrets.hidden("num_%ss" % name, "v1")
    assert secrets.mask("%s: v1" % name) == "%s: ****" % name
    assert secrets.mask("%sProxy: v1" % name) == "%sProxy: v1" % name


@pytest.mark.parametrize("name", ["pw", "pass"])
def test_pw_and_pass_alone_as_a_variable(name):
    assert secrets.hidden(name, "v1") and not secrets.hidden("x%s" % name, "v1")


@pytest.mark.parametrize("ending", ["credential", "credentials", "token", "pwd"])
def test_an_ending_of_a_variable(ending):
    name = "x_" + ending
    assert secrets.hidden(name, "v1") and not secrets.hidden(name + "_validity", "v1")


@pytest.mark.parametrize("ending", ["token", "pwd", "pass", "credential", "credentials"])
def test_an_ending_in_a_line(ending):
    for sep in "._":
        name = "x" + sep + ending
        assert secrets.mask("%s=v1" % name) == "%s=****" % name
        if sep == ".":  # (after a _, _pw and _pass count anywhere: SHORT_WORDS)
            assert secrets.mask("%s_validity=v1" % name) == "%s_validity=v1" % name


def test_not_initial_token():
    assert not secrets.hidden("initial_token", "5") and secrets.mask("initial_token: 5") == "initial_token: 5"
