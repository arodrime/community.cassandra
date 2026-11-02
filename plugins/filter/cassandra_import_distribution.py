# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What the nodes of an import run: Apache Cassandra, or a distribution the
collection does not drive (import_cluster refuses it before anything is written).

cassandra_import_unsupported: {host: what the distribution read printed on it
    (playbooks/files/import_distribution.sh, with the release= or version=
    line of the play)} -> the lines of the REFUSED block for the hosts that run
    DataStax Enterprise or ScyllaDB, [] when none does.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.filter.cassandra_import import SERIES
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_output import nodes

NAMES = {"dse": "DataStax Enterprise", "scylla": "ScyllaDB"}
VERSION = re.compile(r"\d+(?:\.\d+)+")  # the version in "6.8.42" or "5.4.3-0.20240305.c0c3ad4ac5a1"
# a DataStax Enterprise build answers nodetool version with four numbers (4.0.0.6816, 3.11.3.5115)
DSE_RELEASE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def _pairs(text):
    """The key=value lines of a read -> {key: value}."""
    out = {}
    for line in str(text or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _version(text):
    """The version a command printed ("5.4.3-0.20240305.c0c3ad4ac5a1" -> "5.4.3"), "" when none."""
    found = VERSION.search(text or "")
    return found.group(0) if found else ""


def runs(text):
    """What a node runs when it is not Apache Cassandra, from its read: "DataStax Enterprise 6.8.42",
    "ScyllaDB 5.4.3", "a DataStax Enterprise build (ReleaseVersion 4.0.0.6816)"; "" for Apache Cassandra
    (or nothing found)."""
    read = _pairs(text)
    name = NAMES.get(read.get("distribution", ""))
    if name:
        version = _version(read.get("distribution_version", ""))
        return "%s %s" % (name, version) if version else name
    release = read.get("release", "") or read.get("version", "")
    if DSE_RELEASE.match(release):
        return "a DataStax Enterprise build (ReleaseVersion %s)" % release
    return ""


def series_list():
    """The Cassandra series the collection drives: "4.0, 4.1 and 5.0"."""
    versions = sorted(SERIES.values(), key=lambda v: [int(x) for x in v.split(".")])
    return " and ".join(x for x in (", ".join(versions[:-1]), versions[-1]) if x)


def cassandra_import_unsupported(read):
    """read: {host: what the distribution read printed on it} -> the REFUSED block for the hosts that run
    DataStax Enterprise or ScyllaDB (one line per distribution and version, the hosts that run it), [] when
    every host runs Apache Cassandra."""
    by_text = {}
    for host, text in (read or {}).items():
        said = runs(text)
        if said:
            by_text.setdefault(said, []).append(str(host))
    if not by_text:
        return []
    lines = ["REFUSED  import_cluster  nothing was written"]
    for said in sorted(by_text, key=lambda s: sorted(by_text[s])):
        hosts = by_text[said]
        lines.append("  %s run%s %s: not supported." % (nodes(hosts), "s" if len(hosts) == 1 else "", said))
    lines.append("  This collection drives Apache Cassandra %s only (DSE and ScyllaDB have their own packages, services"
                 " and configuration files)." % series_list())
    return lines


class FilterModule(object):
    def filters(self):
        return {
            "cassandra_import_unsupported": cassandra_import_unsupported,
        }
