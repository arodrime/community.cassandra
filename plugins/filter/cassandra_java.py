# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The Java versions each Cassandra series runs on, shared by the roles and
playbooks, and the Java tarball of a version (cassandra_java_tarballs).

cassandra_java_supported: the Java majors a series supports.
cassandra_java_check: why a series does not run on a Java ('' when it does).
cassandra_java_tarball: the cassandra_java_tarballs entry of a Java major.
cassandra_java_release_major: the Java major a JDK's release file names.
cassandra_java_major: the major of a Java version ("1.8" -> "8", "17.0.2" -> "17").
cassandra_java_alternative: the alternatives entry that is the Java package's own java."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.errors import AnsibleFilterError

# The Java majors each series supports (Cassandra's own docs and start
# script). 5.0's script starts on a newer Java too, as on 17, but only 11 and
# 17 are supported: Java 21 comes with 6.0 (CASSANDRA-18831).
SUPPORTED = {
    "40x": ["8", "11"],
    "41x": ["8", "11"],
    "50x": ["11", "17"],
}


def cassandra_java_major(version):
    """'17', 17, '17.0.2', '1.8.0_402' and '8' -> '17' or '8'."""
    version = str(version).strip()
    if version.startswith("1."):
        version = version[2:]
    return re.split(r"[^0-9]", version, maxsplit=1)[0]


def cassandra_java_supported(series):
    """The Java majors series (40x, 41x, 50x) supports, [] when unknown. "50x" -> ["11", "17"]."""
    return list(SUPPORTED.get(str(series), []))


def cassandra_java_check(version, series, allow_unsupported=False):
    """'' when Cassandra series runs on Java version, else why not. "17", "50x" -> ''; "8", "50x" -> "Cassandra 50x runs on
    Java 11 or 17, not 8 (cassandra_java_version). ..."."""
    supported = cassandra_java_supported(series)
    if not supported:
        return "Unknown Cassandra series %s (one of %s)." % (series, ", ".join(sorted(SUPPORTED)))
    if cassandra_java_major(version) in supported or allow_unsupported:
        return ""
    return ("Cassandra %s runs on Java %s, not %s (cassandra_java_version). "
            "To run it anyway, at your own risk, set cassandra_java_allow_unsupported: true."
            % (series, " or ".join(supported), version))


def cassandra_java_tarball(tarballs, version):
    """The entry of cassandra_java_tarballs for Java version ({url, checksum,
    and optionally username, password}), {} when there is none. Keys may be
    strings or numbers ("17" or 17).
    {"17": {"url": u, "checksum": c}}, "17.0.2" -> {"url": u, "checksum": c}."""
    if not tarballs:
        return {}
    if not isinstance(tarballs, dict):
        raise AnsibleFilterError("cassandra_java_tarballs must be a dict keyed by Java major version, "
                                 "e.g. {\"17\": {\"url\": ..., \"checksum\": \"sha256:...\"}}")
    major = cassandra_java_major(version)
    found = [entry for key, entry in tarballs.items() if cassandra_java_major(key) == major] if major else []
    if not found:
        return {}
    if len(found) > 1:
        raise AnsibleFilterError("cassandra_java_tarballs has several entries for Java %s." % major)
    entry = found[0]
    if not isinstance(entry, dict) or not isinstance(entry.get("url"), str) or not entry["url"]:
        raise AnsibleFilterError("cassandra_java_tarballs[\"%s\"] must be a dict with a url "
                                 "(and a checksum, e.g. \"sha256:...\")." % major)
    for key in ("checksum", "username", "password"):
        if key in entry and not isinstance(entry[key], str):
            raise AnsibleFilterError("cassandra_java_tarballs[\"%s\"].%s must be a string." % (major, key))
    unknown = sorted(set(entry) - set(["url", "checksum", "username", "password"]))
    if unknown:
        raise AnsibleFilterError("cassandra_java_tarballs[\"%s\"]: unknown key(s) %s (url, checksum, username, password)."
                                 % (major, ", ".join(unknown)))
    return dict(entry, checksum=entry.get("checksum") or "")


def cassandra_java_release_major(content):
    """The Java major of a JDK or JRE release file (JAVA_VERSION="17.0.12"),
    '' when it names none.
    'JAVA_VERSION="17.0.12"' -> "17"."""
    match = re.search(r'^JAVA_VERSION="?([^"\s]+)"?\s*$', content or "", re.MULTILINE)
    return cassandra_java_major(match.group(1)) if match else ""


def cassandra_java_alternative(alternatives, files, version):
    """The java to make the default (alternatives --set): among the entries of alternatives --display java (its
    lines, an entry starts with its path), the Java package's own java (files: the package's file list, its
    .../bin/java), else a path naming Java version (java-17-..., java-1.8.0-...: the OpenJDK packages), else ''.
    Temurin and Zulu packages (temurin-17-jdk-amd64, zulu17) name no version: their java is found by its files.
    ["/usr/lib/jvm/temurin-17-jdk-amd64/bin/java - priority 1", ...], ["/usr/lib/jvm/temurin-17-jdk-amd64/bin/java",
    ...], "17" -> "/usr/lib/jvm/temurin-17-jdk-amd64/bin/java"."""
    entries = [line.split()[0] for line in alternatives or [] if str(line).startswith("/")]
    own = [path for path in files or [] if str(path).endswith("/bin/java") and path in entries]
    if own:
        return own[0]
    named = re.compile(r"^/\S*java-(1\.)?%s[.-]" % re.escape(cassandra_java_major(version)))
    return next((path for path in entries if named.match(path)), "")


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_java_supported": cassandra_java_supported,
            "cassandra_java_check": cassandra_java_check,
            "cassandra_java_tarball": cassandra_java_tarball,
            "cassandra_java_release_major": cassandra_java_release_major,
            "cassandra_java_major": cassandra_java_major,
            "cassandra_java_alternative": cassandra_java_alternative,
        }
