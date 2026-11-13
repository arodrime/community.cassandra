from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_url import cassandra_same_origin, cassandra_url_masked

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
    # a password with any character: the login runs to the last @ (a path with an @ hides the host: too much, not
    # too little)
    ("https://u:pa/ss@m.example.com/x", "https://****@m.example.com/x"),
    ("https://u:pa'ss@m.example.com/x", "https://****@m.example.com/x"),
    ("https://u:pa#ss@m.example.com/x", "https://****@m.example.com/x"),
    ("https://u:pa?ss@m.example.com/x", "https://****@m.example.com/x"),
    ("https://m.example.com/a@b/c", "https://****@b/c"),
    ("https://m.example.com/jdk.tgz", "https://m.example.com/jdk.tgz"),
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
