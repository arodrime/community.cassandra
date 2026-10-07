# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_repository_import: the Cassandra repository files of a node
(import_cluster) -> whether cassandra_repository can take them over without
changing them, and the variables that make it write them as they are.

Input: {path: text} of the node's /etc/yum.repos.d/cassandra-*.repo,
/etc/apt/sources.list.d/cassandra-*.{sources,list} and
/etc/apt/auth.conf.d/cassandra.conf; {path: {mode, checksum}} of those files
and of the signing keys they name; the series; the OS family; the sha1 of the
keys the role ships; the installed packages; whether the Cassandra package
came from a file (cassandra_install_method packages, where the role removes
these files); the packages the role installs with the apt repository.

Output: {"manage": bool, "vars": {...}, "why": text}. manage false: the
files are left as they are (cassandra_repository_manage: false), why says
why; else vars holds the URL, the credentials and the key path the files
have (the ones that are not the role's defaults).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import configparser
import re
from urllib.parse import urlsplit

MARK = "Managed by Ansible (community.cassandra.cassandra_repository)"
YUM_KEYS = {"name", "baseurl", "gpgcheck", "gpgkey", "username", "password"}
DEB_FILE = "Components: main\nX-Repolib-Name: cassandra-%s\nSigned-By: %s\nSuites: %s\nTypes: deb\nURIs: %s\n"
RPM_URL = "https://redhat.cassandra.apache.org/%s/"
DEB_URL = "https://debian.cassandra.apache.org"
RPM_KEY = "/etc/pki/rpm-gpg/apache-cassandra.asc"
DEB_KEY = "/etc/apt/keyrings/apache-cassandra.asc"


def _key(path, stats, shipped):
    """Why the role would replace the signing keys at path ('' if it would not)."""
    if stats.get(path, {}).get("checksum") != shipped:
        return "%s: other signing keys than the role's" % path
    if stats[path].get("mode") != "0644":
        return "%s: mode %s, the role writes 0644" % (path, stats[path].get("mode"))
    return ""


def _keep(why):
    return {"manage": False, "vars": {}, "why": why}


def _yum(files, stats, series, shipped):
    path = "/etc/yum.repos.d/cassandra-%s.repo" % series
    others = sorted(p for p in files if p != path)
    if path not in files or others:
        return None, "%s: the role keeps only %s" % (", ".join(others) or "no " + path, path)
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(files[path])
    except configparser.Error:
        return None, "%s: not read as a yum repository file" % path
    section = "cassandra-%s" % series
    if parser.sections() != [section]:
        return None, "%s: sections %s, the role writes [%s] only" % (path, ", ".join(parser.sections()), section)
    repo = dict(parser.items(section))
    extra = sorted(set(repo) - YUM_KEYS)
    if extra:
        return None, "%s: %s, which the role does not write" % (path, ", ".join(extra))
    if repo.get("name") != "Official Cassandra %s yum repo" % series or repo.get("gpgcheck") != "1" \
            or not repo.get("gpgkey", "").startswith("file://") or not repo.get("baseurl"):
        return None, "%s: not written as the role writes it (name, gpgcheck, gpgkey or baseurl)" % path
    if ("password" in repo) != ("username" in repo):
        return None, "%s: a password without a user, or the other way round" % path
    mode = stats.get(path, {}).get("mode")
    if mode != ("0600" if "username" in repo else "0644"):
        return None, "%s: mode %s, the role writes %s" % (path, mode, "0600" if "username" in repo else "0644")
    key = repo["gpgkey"][len("file://"):]
    why = _key(key, stats, shipped)
    if why:
        return None, why
    out = {}
    if repo["baseurl"] != RPM_URL % series:
        out["cassandra_install_url"] = repo["baseurl"]
    if "username" in repo:
        out["cassandra_install_username"] = repo["username"]
        out["cassandra_install_password"] = repo["password"]
    if key != RPM_KEY:
        out["cassandra_rpm_key_path"] = key
    return out, ""


def _deb822(text):
    out = {}
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z-]+):\s*(.*?)\s*$", line)
        if not m:
            return None
        out[m.group(1)] = m.group(2)
    return out


def _apt(files, stats, series, shipped, packages, debian_packages):
    path = "/etc/apt/sources.list.d/cassandra-%s.sources" % series
    auth = "/etc/apt/auth.conf.d/cassandra.conf"
    others = sorted(p for p in files if p not in (path, auth))
    if path not in files or others:
        return None, "%s: the role keeps only %s" % (", ".join(others) or "no " + path, path)
    src = _deb822(files[path])
    # deb822_repository rewrites the file unless it is its own output exactly (its fields sorted by option name)
    if src is None or not src.get("URIs") or not src.get("Signed-By") or files[path] != DEB_FILE % (
            series, src["Signed-By"], series, src["URIs"]):
        return None, "%s: not written as the role writes it" % path
    if stats.get(path, {}).get("mode") != "0644":
        return None, "%s: mode %s, the role writes 0644" % (path, stats.get(path, {}).get("mode"))
    key = src["Signed-By"]
    why = _key(key, stats, shipped)
    if why:
        return None, why
    missing = [p for p in debian_packages if p not in packages]
    if missing:
        return None, "the role would install %s with the repository" % ", ".join(missing)
    out = {}
    if src["URIs"].rstrip("/") != DEB_URL:
        out["cassandra_install_url"] = src["URIs"]
    if auth in files:
        lines = [s for s in files[auth].split("\n") if s.strip()]
        m = re.match(r"^machine\s+(\S+)\s+login\s+(\S+)\s+password\s+(\S+)\s*$", lines[-1]) if lines else None
        if MARK not in files[auth] or len(lines) != 2 or not m or stats.get(auth, {}).get("mode") != "0600" \
                or (stats.get(auth, {}).get("uid"), stats.get(auth, {}).get("gid")) != (0, 0):
            return None, "%s: not written by the role (it would rewrite or remove it)" % auth
        if m.group(1) != urlsplit(src["URIs"]).netloc:
            return None, "%s: for %s, not the repository's host (the role would rewrite it)" % (auth, m.group(1))
        if files[auth] != "# %s\nmachine %s login %s password %s\n" % (MARK, m.group(1), m.group(2), m.group(3)):
            return None, "%s: not written as the role writes it" % auth
        out["cassandra_install_username"] = m.group(2)
        out["cassandra_install_password"] = m.group(3)
    if key != DEB_KEY:
        out["cassandra_apt_keyring_path"] = key
    return out, ""


def cassandra_repository_import(files, stats, series, os_family, shipped, packages=None, from_file=False,
                                debian_packages=None):
    """See the module."""
    files = dict(files or {})
    if not files:
        return {"manage": True, "vars": {}, "why": ""}
    if from_file:
        return _keep("%s: the package came from a file, the role would remove them" % ", ".join(sorted(files)))
    if os_family == "RedHat":
        found, why = _yum(files, stats or {}, series, shipped)
    elif os_family == "Debian":
        found, why = _apt(files, stats or {}, series, shipped, packages or {}, debian_packages or [])
    else:
        found, why = None, "unknown OS family %s" % os_family
    if found is None:
        return _keep(why)
    return {"manage": True, "vars": found, "why": ""}


class FilterModule(object):
    def filters(self):
        return {"cassandra_repository_import": cassandra_repository_import}
