# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_same_origin: whether two http(s) URLs are on the same server
(same scheme, host and port), to send a mirror's credentials only to it.
cassandra_url_login_ok: whether the login of a URL, if any, is written as
RFC 3986 allows (a / # ? " @ or space in it percent-encoded).
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


_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
# a login: letters, digits, -._~, %XX, !$&'()*+,;= and : (RFC 3986 userinfo)
_LOGIN = re.compile(r"[A-Za-z0-9._~%!$&'()*+,;=:-]*\Z")


def cassandra_url_login_ok(url):
    """False when the URL's login holds a character to percent-encode: get_url would read the URL another way,
    and show it whole in its error. The login runs from scheme:// to the last @ before the first / ? or #
    ("https://u:p%2Fw@m/f.tgz" -> True, "https://u:p\"w@m/f.tgz" -> False); an @ after them is in the path
    ("https://m/v@2/f.tgz" -> True), unless what comes before has a port that is not a number: the end of a login
    with a raw / ? or # ("https://u:p/w@m/f.tgz" -> False). Text that is not a URL -> True."""
    url = str(url or "").strip()
    scheme = _SCHEME.match(url)
    if not scheme or "@" not in url:
        return True
    authority = re.split(r"[/?#]", url[scheme.end():], maxsplit=1)[0]
    if "@" in authority:
        return _LOGIN.match(authority.rpartition("@")[0]) is not None
    port = authority.rpartition("]")[2].partition(":")[2]  # [::1]:8443 -> 8443
    return port == "" or port.isdigit()


# scheme://, the authority (a login up to its last @, the host, the port), the path, a query, a fragment
_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^\s/?#]*)([^\s?#]*)(\?[^\s#]*)?(#\S*)?")
# what ends a sentence or a quote after a URL, not masked with its query or fragment
_PUNCTUATION = ".,;:)'\">"


def _hidden(part, mark):
    """A query or a fragment masked, the punctuation after it kept."""
    if not part:
        return ""
    end = part.rstrip(_PUNCTUATION)
    return mark + "****" + part[max(len(end), 1):]


def _masked(match):
    scheme, authority, path, query, fragment = match.groups()
    dummy, at, host = authority.rpartition("@")
    return scheme + ("****@" if at else "") + host + path + _hidden(query, "?") + _hidden(fragment, "#")


def cassandra_url_masked(text):
    """text with the login, the query and the fragment of every URL in it
    masked, for the output: "https://user:pass@m.example.com/jdk.tgz?X-Amz-Signature=abc" ->
    "https://****@m.example.com/jdk.tgz?****"; a path or a URL without them as is."""
    return _URL.sub(_masked, str(text if text is not None else ""))


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_same_origin": cassandra_same_origin,
            "cassandra_url_login_ok": cassandra_url_login_ok,
            "cassandra_url_masked": cassandra_url_masked,
        }
