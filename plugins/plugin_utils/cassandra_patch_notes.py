# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""What a move to a newer release of the same series needs to know (apply_config updates the package in the node's
restart): read in NEWS.txt of each release, at its tag (not a branch head):
https://raw.githubusercontent.com/apache/cassandra/cassandra-<release>/NEWS.txt (4.0.0 to 4.0.21, 4.1.0 to 4.1.12,
5.0.0 to 5.0.9). Within 4.0.x, 4.1.x and 5.0.x the sstable format and the messaging version do not change: a release
of the series is a package and a restart; what matters is a setting the new release refuses to start with, and a
few defaults that change.

KNOWN: the last release of each series these notes cover. A newer one stops apply_config (its NEWS.txt not read
here): -e cassandra_allow_unknown_patch=true goes on.
REFUSALS: (series, the first release that refuses, the check, why): a node that would move to that release or a
later one with that setting stops before any change. check: a function of the node's settings
({cassandra_jvm_options: [every -D/-XX option the inventory gives], cassandra_memtable_allocation_type,
cassandra_memtable_offheap_space}) -> True when it refuses.
NOTES: (series, release, text): said in the plan when a node crosses that release (runs an older one, moves to it
or a later one).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

KNOWN = {"40x": "4.0.21", "41x": "4.1.12", "50x": "5.0.9"}


def _bcrypt_rounds(settings):
    """-Dcassandra.auth_bcrypt_gensalt_log2_rounds above 30 (the release checks it at start)."""
    for option in settings.get("cassandra_jvm_options") or []:
        m = re.match(r"^-Dcassandra\.auth_bcrypt_gensalt_log2_rounds=(\d+)\s*$", str(option).strip())
        if m and int(m.group(1)) > 30:
            return True
    return False


def _offheap_zero(settings):
    """memtable_offheap_space 0 with an off-heap memtable allocation (the release refuses it at start)."""
    space = re.sub(r"[^0-9]", "", str(settings.get("cassandra_memtable_offheap_space") or "").strip())
    return (str(settings.get("cassandra_memtable_allocation_type") or "").startswith("offheap")
            and space != "" and int(space) == 0)


_BCRYPT = ("-Dcassandra.auth_bcrypt_gensalt_log2_rounds above 30 (cassandra_jvm_*extra_options): Cassandra"
           " %s refuses to start with it. Set it to 30 or below, or remove it")
_OFFHEAP = ("memtable_offheap_space 0 with memtable_allocation_type %s: Cassandra %s refuses to start with it. Give"
            " cassandra_memtable_offheap_space a size, or leave it empty")

REFUSALS = [
    ("40x", "4.0.2", _bcrypt_rounds, _BCRYPT),
    ("41x", "4.1.0", _bcrypt_rounds, _BCRYPT),
    ("50x", "5.0.0", _bcrypt_rounds, _BCRYPT),
    # (from these releases on, not out yet when the notes were read: kept for when they are)
    ("40x", "4.0.22", _offheap_zero, _OFFHEAP),
    ("41x", "4.1.13", _offheap_zero, _OFFHEAP),
    ("50x", "5.0.10", _offheap_zero, _OFFHEAP),
]

NOTES = [
    ("40x", "4.0.6", "compaction throughput: its default goes from 16 to 64 MiB/s where it is not set"
                     " (cassandra_compaction_throughput)"),
    ("40x", "4.0.14", "reject_out_of_token_range_requests is on by default: a request for a token range the node does"
                      " not own is refused"),
    ("41x", "4.1.6", "a custom QueryHandler must be compiled again for this release"),
    ("41x", "4.1.7", "reject_out_of_token_range_requests is on by default: a request for a token range the node does"
                     " not own is refused"),
    ("50x", "5.0.1", "reject_out_of_token_range_requests is on by default: a request for a token range the node does"
                     " not own is refused"),
    ("50x", "5.0.4", "the heap, direct memory and GC flags worked out by the stock cassandra-env.sh / jvm options change:"
                     " a node relying on the stock values gets other ones"),
    ("50x", "5.0.8", "automatic repair (opt-in) must be set the same way on every node before any schema change"),
]


def numbers(version):
    """'4.1.5', '4.1.5-1' -> (4, 1, 5); () when it is not a version."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(version or "").strip())
    return tuple(int(x) for x in m.groups()) if m else ()


def known(series, target):
    """Whether these notes cover the release target of series."""
    last = KNOWN.get(series)
    return bool(last) and numbers(target) <= numbers(last)


def refusals(series, running, target, settings):
    """The reasons the target release would refuse to start with these settings (it, or a release crossed on the way
    from running: the node starts the target only)."""
    out = []
    for s, since, check, text in REFUSALS:
        if s == series and numbers(target) >= numbers(since) and check(settings or {}):
            out.append(text % ((target,) if check is _bcrypt_rounds
                               else (settings.get("cassandra_memtable_allocation_type"), target)))
    return out


def notes(series, running, target):
    """The notes of the releases a node crosses from running to target: ["<release>: <text>"]."""
    return ["%s: %s" % (release, text) for s, release, text in NOTES
            if s == series and numbers(running) < numbers(release) <= numbers(target)]
