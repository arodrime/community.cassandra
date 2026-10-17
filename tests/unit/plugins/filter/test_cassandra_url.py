from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import pytest

from ansible_collections.community.cassandra.plugins.filter.cassandra_url import cassandra_same_origin

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
