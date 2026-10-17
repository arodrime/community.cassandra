# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Reads files on a node in one go, each as a module would (import_cluster runs it with the Python Ansible runs
there: a module run per file is a round trip each). Python 2.7 and 3.

stdin: [[what, how, [[item, path]...]]...] (JSON); prints {what: [result]}, each result as a loop of the module
gives it (with its item), by how:
  content: slurp (its content, else its error)
  stat: stat following links (its stat, else its error)
  checksum: stat not following links, with the sha1 checksum
  find: find of the files (lstat) in the dir item whose name matches one of the patterns path, not hidden
    ({files: [{path, ctime}]})"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import base64
import errno
import fnmatch
import grp
import hashlib
import json
import os
import pwd
import stat
import sys

PY3 = sys.version_info[0] >= 3


def to_bytes(text):
    """A path for the OS calls, as the modules give it (UTF-8, whatever the locale)."""
    if isinstance(text, bytes):
        return text
    return text.encode("utf-8", "surrogateescape") if PY3 else text.encode("utf-8")


def to_text(data):
    if not isinstance(data, bytes):
        return data
    return data.decode("utf-8", "surrogateescape") if PY3 else data.decode("utf-8", "replace")


def expand(path):
    return os.path.expanduser(os.path.expandvars(path))


def content(item, path):
    path = expand(path)
    try:
        with open(to_bytes(path), "rb") as f:
            data = f.read()
    except (IOError, OSError) as e:
        if e.errno == errno.ENOENT:
            msg = "file not found: %s" % path
        elif e.errno == errno.EACCES:
            msg = "file is not readable: %s" % path
        elif e.errno == errno.EISDIR:
            msg = "source is a directory and must be a file: %s" % path
        else:
            msg = "unable to slurp file: %s" % e
        return {"item": item, "failed": True, "msg": msg}
    return {"item": item, "content": base64.b64encode(data).decode("ascii"), "source": path, "encoding": "base64"}


def status(item, path, follow, checksum):
    path = expand(path)
    b_path = to_bytes(path)
    try:
        st = os.stat(b_path) if follow else os.lstat(b_path)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return {"item": item, "stat": {"exists": False}}
        return {"item": item, "failed": True, "msg": e.strerror}
    m = st.st_mode
    out = {"exists": True, "path": path, "mode": "%04o" % stat.S_IMODE(m), "isdir": stat.S_ISDIR(m),
           "isreg": stat.S_ISREG(m), "islnk": stat.S_ISLNK(m), "uid": st.st_uid, "gid": st.st_gid,
           "size": st.st_size, "mtime": st.st_mtime, "ctime": st.st_ctime, "readable": os.access(b_path, os.R_OK)}
    try:
        out["pw_name"] = to_text(pwd.getpwuid(st.st_uid).pw_name)
    except (TypeError, KeyError):
        pass
    try:
        out["gr_name"] = to_text(grp.getgrgid(st.st_gid).gr_name)
    except (KeyError, ValueError, OverflowError):
        pass
    if checksum and out["isreg"] and out["readable"]:
        digest = hashlib.sha1()
        with open(b_path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                digest.update(block)
        out["checksum"] = digest.hexdigest()
    return {"item": item, "stat": out}


def find(top, patterns):
    top = expand(top)
    files = []
    try:
        names = os.listdir(to_bytes(top))
    except OSError:
        names = []
    for b_name in names:
        name = to_text(b_name)
        if name.startswith("."):
            continue
        path = os.path.normpath(os.path.join(top, name))
        try:
            st = os.lstat(to_bytes(path))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and any(fnmatch.fnmatch(name, p) for p in patterns):
            files.append({"path": path, "ctime": st.st_ctime})
    return {"item": top, "files": files}


HOW = {
    "content": content,
    "stat": lambda item, path: status(item, path, True, False),
    "checksum": lambda item, path: status(item, path, False, True),
    "find": find,
}


def read(how, item, path):
    """One file: what its module gives; an unexpected error, that file alone not read (its error, no content)."""
    try:
        return HOW[how](item, path)
    except Exception as e:  # pylint: disable=broad-except
        return {"item": item, "failed": True, "msg": "unable to read %s: %s" % (path, type(e).__name__)}


def main():
    out = {}
    for what, how, items in json.loads(sys.stdin.read()):
        out[what] = [read(how, item, path) for item, path in items]
    print(json.dumps(out))


if __name__ == "__main__":
    main()
