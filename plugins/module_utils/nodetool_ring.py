# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""nodetool ring's output, read (cassandra_ring)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re


# nodetool ring: per datacenter, a line per token
_RING_STATUS = ("Up", "Down", "?")
_RING_STATE = ("Normal", "Leaving", "Joining", "Moving", "?")


def parse_ring(stdout):
    """nodetool ring output -> {dc: [{'address', 'rack', 'status', 'state',
    'token'}]}, one entry per token line (a vnode has several)."""
    rings = {}
    dc = None
    for line in (stdout or "").splitlines():
        m = re.match(r"^Datacenter:\s*(.*?)\s*$", line)
        if m:
            dc = m.group(1)
            rings.setdefault(dc, [])
            continue
        fields = line.split()
        if dc is None or len(fields) < 6 or fields[0] == "Address":
            continue
        at = next((i for i in range(2, len(fields) - 1)
                   if fields[i] in _RING_STATUS and fields[i + 1] in _RING_STATE), None)
        if at is None or not re.match(r"^-?[0-9]+$", fields[-1]):
            continue
        rings[dc].append({"address": fields[0], "rack": " ".join(fields[1:at]), "status": fields[at],
                          "state": fields[at + 1], "token": fields[-1]})
    return rings
