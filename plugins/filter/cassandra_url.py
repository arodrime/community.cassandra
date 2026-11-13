# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_same_origin: whether two http(s) URLs are on the same server
(same scheme, host and port), to send a mirror's credentials only to it.
cassandra_url_login_ok: whether all before the last @ of a URL, if any, is a
plain login (user:password), one get_url reads and sends as it is.
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
# a plain login: letters, digits, -._~!$&'()*+,;= and : (RFC 3986 userinfo without %XX)
_LOGIN = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:-]*\Z")
# the same, %XX too: ansible-core 2.19+ decodes them (2.16 sends them as they are)
_LOGIN_ENCODED = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=:-]|%[0-9A-Fa-f]{2})*\Z")


def _decodes_login(ansible_version):
    """Whether this ansible-core decodes a percent-encoded login in a URL (2.19 and later)."""
    numbers = re.findall(r"\d+", str(ansible_version or ""))[:2]
    return len(numbers) == 2 and (int(numbers[0]), int(numbers[1])) >= (2, 19)


def cassandra_url_login_ok(url, ansible_version=None):
    """False when the URL holds an @ and what runs from scheme:// to its last @ is not a plain login: get_url would
    read it another way, or send it as it is ("https://u:pw@m/f.tgz" -> True; "https://u:p/w@m/f.tgz",
    "https://m/v@2/f.tgz" -> False). A %XX in it only on ansible-core 2.19+ (ansible_version), that decodes it.
    Text that is not a URL -> True."""
    url = str(url or "").strip()
    scheme = _SCHEME.match(url)
    if not scheme or "@" not in url[scheme.end():]:
        return True
    login = url[scheme.end():].rpartition("@")[0]
    return (_LOGIN_ENCODED if _decodes_login(ansible_version) else _LOGIN).match(login) is not None


# a URL: scheme:// up to a whitespace
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S*")
# the rest after the login: the host and the path, a query, a fragment
_REST = re.compile(r"([^?#]*)(\?[^#]*)?(#.*)?\Z", re.DOTALL)
# what ends a sentence or a quote after a URL, not masked with its query or fragment
_PUNCTUATION = ".,;:)'\">"


def _hidden(part, mark):
    """A query or a fragment masked, the punctuation after it kept."""
    if not part:
        return ""
    end = part.rstrip(_PUNCTUATION)
    return mark + "****" + part[max(len(end), 1):]


def _masked(match):
    url = match.group(0)
    scheme = _SCHEME.match(url).group(0)
    login, at, rest = url[len(scheme):].rpartition("@")
    path, query, fragment = _REST.match(rest).groups()
    return scheme + ("****@" if at else "") + path + _hidden(query, "?") + _hidden(fragment, "#")


def cassandra_url_masked(text):
    """text with the login, the query and the fragment of every URL in it masked, for the output:
    "https://user:pass@m.example.com/jdk.tgz?X-Amz-Signature=abc" -> "https://****@m.example.com/jdk.tgz?****".
    The login is all from scheme:// to the URL's last @, whatever is in it: an @ in the path masks more
    ("https://m/v@2/f.tgz" -> "https://****@2/f.tgz"), never less. A path or a URL without them as is."""
    return _URL.sub(_masked, str(text if text is not None else ""))


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_same_origin": cassandra_same_origin,
            "cassandra_url_login_ok": cassandra_url_login_ok,
            "cassandra_url_masked": cassandra_url_masked,
        }
