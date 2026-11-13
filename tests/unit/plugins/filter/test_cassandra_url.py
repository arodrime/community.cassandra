from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import random
import string

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_url import (
    cassandra_same_origin, cassandra_url_login_ok, cassandra_url_masked)

MIRROR = "https://mirror.example.com/cassandra/"


@pytest.mark.parametrize("url", [
    "https://mirror.example.com/keys/KEYS",
    "https://mirror.example.com:443/keys/KEYS",
    "HTTPS://Mirror.Example.com/keys/KEYS",
    "https://reader@mirror.example.com/keys/KEYS",
])
def test_same_server(url):
    assert cassandra_same_origin(url, MIRROR) is True


@pytest.mark.parametrize("url", [
    "https://downloads.apache.org/cassandra/KEYS",
    "http://mirror.example.com/keys/KEYS",  # the credentials would go in clear
    "https://mirror.example.com:8443/keys/KEYS",  # another service on the mirror's host
    "https://mirror.example.com.evil.org/keys/KEYS",
    "file:///tmp/KEYS",
    "https://mirror.example.com:port/keys/KEYS",
    "",
    None,
])
def test_another_server(url):
    assert cassandra_same_origin(url, MIRROR) is False


def test_default_ports():
    assert cassandra_same_origin("http://m.example.com/a", "http://m.example.com:80/b") is True
    assert cassandra_same_origin("http://m.example.com:443/a", "https://m.example.com/b") is False
    assert cassandra_same_origin("https://m.example.com/a", "") is False


@pytest.mark.parametrize("url, shown", [
    ("https://user:s3cret@m.example.com/jdk.tgz", "https://****@m.example.com/jdk.tgz"),
    ("https://T0KEN@m.example.com/jdk.tgz", "https://****@m.example.com/jdk.tgz"),
    ("https://user:p@ss@m.example.com:8443/jdk.tgz", "https://****@m.example.com:8443/jdk.tgz"),
    ("https://b.s3.amazonaws.com/jdk.tgz?X-Amz-Credential=AKIA&X-Amz-Signature=abc",
     "https://b.s3.amazonaws.com/jdk.tgz?****"),
    ("https://m.example.com/KEYS?token=abc#part", "https://m.example.com/KEYS?****#****"),
    ("https://m.example.com/KEYS#access_token=abc", "https://m.example.com/KEYS#****"),
    # a password percent-encoded, or with a ' (RFC 3986 allows it raw)
    ("https://u:pa%2Fs%22s%20@m.example.com/x", "https://****@m.example.com/x"),
    ("https://u:pa'ss@m.example.com/x", "https://****@m.example.com/x"),
    # all up to the last @ masked, whatever is in it: a / ? # in a login, or an @ in the path (masks more)
    ("https://user:/s3cr3t@m.example.com/x", "https://****@m.example.com/x"),
    ("https://AbC/dEf+tok@m.example.com/x", "https://****@m.example.com/x"),
    ("https://u:12?s3cr3t@m.example.com/x", "https://****@m.example.com/x"),
    ("https://u:12#s3cr3t@m.example.com/x?sig=a", "https://****@m.example.com/x?****"),
    ("https://m.example.com/path@v2/x.tgz", "https://****@v2/x.tgz"),
    ("https://m.example.com/x?sig=a@b", "https://****@b"),
    # a query, a fragment masked up to the next whitespace (a quote or a < in them too)
    ("https://m.example.com/x?sig=a'b\"c<d", "https://m.example.com/x?****"),
    ("https://m.example.com/x#a'b", "https://m.example.com/x#****"),
    ("https://m.example.com/jdk.tgz", "https://m.example.com/jdk.tgz"),
    ("no URL: user@host.example.com?", "no URL: user@host.example.com?"),
    ("/srv/files/jdk.tgz", "/srv/files/jdk.tgz"),
    ("file:///srv/files/jdk.tgz", "file:///srv/files/jdk.tgz"),
    ("", ""),
    (None, ""),
])
def test_url_masked(url, shown):
    assert cassandra_url_masked(url) == shown


def test_urls_masked_in_a_text():
    text = "Request failed for 'https://u:pw@m.example.com/a?sig=x': HTTP 404, see https://m.example.com/b."
    assert cassandra_url_masked(text) == ("Request failed for 'https://****@m.example.com/a?****': HTTP 404,"
                                          " see https://m.example.com/b.")
    # the punctuation after a query or a fragment stays
    text = "from https://m.example.com/a?sig=x, then (https://m.example.com/b#t=y); see https://m.example.com/c?s=z."
    assert cassandra_url_masked(text) == ("from https://m.example.com/a?****, then (https://m.example.com/b#****);"
                                          " see https://m.example.com/c?****.")
    text = "Request failed for 'https://m.example.com/a?sig=x': HTTP 404, <https://m.example.com/b?s=y>"
    assert cassandra_url_masked(text) == ("Request failed for 'https://m.example.com/a?****': HTTP 404,"
                                          " <https://m.example.com/b?****>")


@pytest.mark.parametrize("url", [
    "https://m.example.com/jdk.tgz",
    "https://T0KEN@m.example.com/jdk.tgz",
    "https://u:pa'ss!$&()*+,;=~@m.example.com:8443/jdk.tgz",
    "https://[::1]:8443/jdk.tgz",
    "/srv/files/j@dk.tgz",
    "file:///srv/files/jdk.tgz",
    "",
    None,
])
@pytest.mark.parametrize("version", ["2.16.19", "2.19.0", "2.20.1rc1", None])
def test_login_ok(url, version):
    assert cassandra_url_login_ok(url, version) is True


@pytest.mark.parametrize("url", [
    'https://u:pa"ss@m.example.com/jdk.tgz',
    "https://u:pa ss@m.example.com/jdk.tgz",
    "https://u:pa<ss>@m.example.com/jdk.tgz",
    "https://u:p@ss@m.example.com/jdk.tgz",
    "https://u:pa/ss@m.example.com/jdk.tgz",
    "https://user:/s3cr3t@m.example.com/jdk.tgz",  # a / at the start of the password
    "https://AbC/dEf+tok@m.example.com/jdk.tgz",  # a token alone, a / in it
    "https://u:12#s3cr3t@m.example.com/jdk.tgz",
    "https://u:12?s3cr3t@m.example.com/jdk.tgz",
    "https://u:pa%2@m.example.com/jdk.tgz",  # not a %XX
    "https://m.example.com/path@v2/jdk.tgz",  # an @ in the path: written %40
    "https://m.example.com/jdk.tgz?X-Amz-Signature=a@b",
])
@pytest.mark.parametrize("version", ["2.16.19", "2.19.0", "2.21.4", None])
def test_login_refused(url, version):
    assert cassandra_url_login_ok(url, version) is False


# ansible-core 2.16 sends a percent-encoded login as it is (2.19+ decodes it)
@pytest.mark.parametrize("url", ["https://u:p%2Fw@m.example.com/jdk.tgz", "https://user%40corp.com:pw@m.example.com/x"])
def test_an_encoded_login_needs_2_19(url):
    assert cassandra_url_login_ok(url, "2.16.19") is False
    assert cassandra_url_login_ok(url, "2.18.9") is False
    assert cassandra_url_login_ok(url) is False
    assert cassandra_url_login_ok(url, "2.19.15") is True
    assert cassandra_url_login_ok(url, "2.21.4") is True


LOGIN_CHARS = string.ascii_letters + string.digits + "-._~!$&'()*+,;=:/?#@%[]\\\"<>{}|^`"
REST_CHARS = string.ascii_lowercase + string.digits + "./?#=&-:'\"<>)"


def test_masking_never_leaks_a_login():
    # all from scheme:// to the last @ of a URL is masked, whatever it holds: what is shown does not depend on it
    rnd = random.Random(1)
    for dummy in range(5000):
        login = "".join(rnd.choice(LOGIN_CHARS) for dummy in range(rnd.randint(1, 24)))
        rest = "".join(rnd.choice(REST_CHARS) for dummy in range(rnd.randint(0, 24)))
        for before, after in (("", ""), ("a ", " b"), ("<", ">,"), ("'", "': HTTP 401")):
            shown = cassandra_url_masked(before + "https://" + login + "@" + rest + after)
            assert shown == cassandra_url_masked(before + "https://x@" + rest + after), (login, rest, shown)
            assert shown.startswith(before + "https://****@"), (login, rest, shown)
