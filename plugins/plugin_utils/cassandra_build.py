# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The collection's version and the git commit it was built from, said at the head of the reports.

The version is galaxy.yml's (a git tree) or MANIFEST.json's (an installed collection). The commit is COMMIT below,
which tools/build.sh writes in the copy it builds (an installed collection has no .git); in a git tree of the
collection, its HEAD. Neither found: said unknown, never guessed."""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import os
import re
import subprocess

COMMIT = ""  # (empty in git: tools/build.sh writes it in the copy it builds)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def version(root=ROOT):
    """The collection's version: MANIFEST.json's (installed), else galaxy.yml's (a git tree); "" when neither."""
    try:
        with open(os.path.join(root, "MANIFEST.json")) as f:
            return str(json.load(f)["collection_info"]["version"])
    except (OSError, IOError, ValueError, KeyError, TypeError):
        pass
    try:
        with open(os.path.join(root, "galaxy.yml")) as f:
            found = re.search(r"^version:\s*['\"]?([^'\"\s#]+)", f.read(), re.M)
        return found.group(1) if found else ""
    except (OSError, IOError):
        return ""


def _git(root, *args):
    return subprocess.check_output(("git", "-C", root) + args, stderr=subprocess.DEVNULL,
                                   timeout=10).decode("utf-8", "replace").strip()


def commit(root=ROOT, built=None):
    """The commit the collection was built from: built (COMMIT by default), else the HEAD of the git tree the
    collection is the top of (not one it is inside of, e.g. a playbook repository holding it), "+" when its tracked
    files differ from it; "" when not known."""
    built = COMMIT if built is None else built
    if built:
        return built
    try:
        if os.path.realpath(_git(root, "rev-parse", "--show-toplevel")) != os.path.realpath(root):
            return ""
        head = _git(root, "rev-parse", "--short=12", "HEAD")
        return head + ("+" if _git(root, "status", "--porcelain", "--untracked-files=no") else "")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def label(root=ROOT, built=None):
    """"community.cassandra 3.0.0-rc1, commit 1a2b3c4d5e6f"; "version unknown", "commit unknown" for what is not
    known."""
    return "community.cassandra %s, commit %s" % (version(root) or "version unknown", commit(root, built) or "unknown")
