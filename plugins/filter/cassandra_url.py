# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_same_origin: whether two http(s) URLs are on the same server
(same scheme, host and port), to send a mirror's credentials only to it."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from urllib.parse import urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url):
    """(scheme, host, port) of an http(s) URL, None for any other URL."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:  # a port that is not a number
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    return scheme, parts.hostname.lower(), port or DEFAULT_PORTS[scheme]


def cassandra_same_origin(url, other):
    """True when url and other are http(s) URLs with the same scheme, host and
    port (80 and 443 by default). "https://m/keys", "https://m:443/repo" -> True;
    "http://m/keys" or "https://m:8443/keys", "https://m/repo" -> False."""
    origin = _origin(url)
    return origin is not None and origin == _origin(other)


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_same_origin": cassandra_same_origin,
        }
