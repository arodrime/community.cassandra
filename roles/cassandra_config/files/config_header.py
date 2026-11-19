# -*- coding: utf-8 -*-
# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# The role's header line ("Managed by Ansible ...: change the role variables, not this file") added to the files
# cassandra_config leaves as they are (the same settings as the role's, written another way): the header alone, the
# rest of the file byte for byte, in place (its owner, mode, links and SELinux context kept). Run on the node with
# its Python (python -c, a JSON request on stdin, a JSON answer on stdout). Python 2.7 and 3.6+.
#   request: [{path, header, mark, sha1}]: header, the line to add (no line end); mark, the text that says a file has
#       one already; sha1, the file's content as the role compared it (another one now: left as it is)
#   answer: {path: {status, before, after}}: status added, present (it has one), changed (not what was compared),
#       link (not followed), missing; before and after, the sha1 of the file before and after
#   or, to find the files that need it (nothing written): {"find": [{path, mark, ...}]} -> [the items of the files
#       without the mark (missing or unreadable ones left out), each with its sha1]

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import hashlib
import json
import os
import sys

BOM = b"\xef\xbb\xbf"
# a first line that must stay first: an interpreter line, an XML declaration
FIRST = (b"#!", b"<?xml")


def with_header(data, header):
    """data, header (bytes) -> data with header on a line of its own, first (after a UTF-8 BOM), or after a first
    line that must stay first; the line end of the file's first line (CRLF or LF); the rest byte for byte."""
    bom = BOM if data.startswith(BOM) else b""
    body = data[len(bom):]
    end = body.find(b"\n")
    newline = b"\r\n" if end > 0 and body[end - 1:end] == b"\r" else b"\n"
    if body.startswith(FIRST):
        if end < 0:  # (that line alone, with no line end)
            return bom + body + newline + header + newline
        return bom + body[:end + 1] + header + newline + body[end + 1:]
    return bom + header + newline + body


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def add(item):
    path = item["path"]
    if os.path.islink(path):
        return {"status": "link"}
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (IOError, OSError):
        return {"status": "missing"}
    before = sha1(data)
    if item["mark"].encode("utf-8") in data:
        return {"status": "present", "before": before, "after": before}
    if before != item["sha1"]:
        return {"status": "changed", "before": before, "after": before}
    new = with_header(data, item["header"].encode("utf-8"))
    with open(path, "r+b") as f:  # in place: the same file, its owner, mode and context
        f.write(new)  # (longer than data: every byte of it written over)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    return {"status": "added", "before": before, "after": sha1(new)}


def find(items):
    out = []
    for item in items:
        try:
            with open(item["path"], "rb") as f:
                data = f.read()
        except (IOError, OSError):
            continue
        if item["mark"].encode("utf-8") not in data:
            out.append(dict(item, sha1=sha1(data)))
    return out


def main():
    request = json.loads(sys.stdin.read())
    if isinstance(request, dict):
        sys.stdout.write(json.dumps(find(request["find"])))
    else:
        sys.stdout.write(json.dumps(dict((item["path"], add(item)) for item in request)))


if __name__ == "__main__":
    main()
