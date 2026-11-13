# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_same_origin: whether two http(s) URLs are on the same server
(same scheme, host and port), to send a mirror's credentials only to it.
cassandra_url_masked: a URL, or a text holding some, as the output shows it:
no login, no query (a signature, a token), no fragment."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re
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


# scheme://, a login (user:password@, up to the last @ of the URL: a password
# may hold any character, better too much hidden than too little), the host
# and path, a query, a fragment
_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^\s<>\"]*@)?([^\s?#'\"<>]*)(\?[^\s#'\"<>]*)?(#[^\s'\"<>]*)?")
# what ends a sentence after a URL, not masked with its query or fragment
_PUNCTUATION = ".,;)"


def _hidden(part, mark):
    """A query or a fragment masked, the punctuation after it kept."""
    if not part:
        return ""
    end = part.rstrip(_PUNCTUATION)
    return mark + "****" + part[max(len(end), 1):]


def cassandra_url_masked(text):
    """text with the login, the query and the fragment of every URL in it
    masked, for the output: "https://user:pass@m.example.com/jdk.tgz?X-Amz-Signature=abc" ->
    "https://****@m.example.com/jdk.tgz?****"; a path or a URL without them as is."""
    return _URL.sub(lambda m: m.group(1) + ("****@" if m.group(2) else "") + m.group(3) + _hidden(m.group(4), "?")
                    + _hidden(m.group(5), "#"), str(text if text is not None else ""))


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_same_origin": cassandra_same_origin,
            "cassandra_url_masked": cassandra_url_masked,
        }
