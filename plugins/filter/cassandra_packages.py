# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The installed packages of a few names, read with one command (rpm -q, dpkg-query -W) instead of
package_facts: package_facts puts every package of the node in its facts, which Ansible copies again for each
task (seconds per task with a real server's packages). cassandra_package_query: the command (argv) for the
names; cassandra_installed_packages: its output as package_facts gives these names (name -> its installed versions)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible.errors import AnsibleFilterError

# one line per installed package, the same fields for both: state, name, version, release, epoch, arch
_RPM = ["rpm", "-q", "--qf", "installed\t%{NAME}\t%{VERSION}\t%{RELEASE}\t%{EPOCH}\t%{ARCH}\n"]
_DPKG = ["dpkg-query", "-W", "-f=${db:Status-Status}\t${Package}\t${Version}\t\t\t${Architecture}\n"]
# dpkg's states of a package that is not installed (removed, its config files left)
_NOT_INSTALLED = ("not-installed", "config-files")
# a name not installed, as rpm -q (its output) and dpkg-query -W (its errors) say it
_RPM_ABSENT = re.compile(r"package \S+ is not installed$")
_DPKG_ABSENT = re.compile(r"dpkg-query: no packages found matching ")


def cassandra_package_query(names, os_family):
    """names (a name or a list, lists flattened; empty ones dropped), the node's os_family -> the argv that
    reads them, e.g. (["cassandra"], "RedHat") -> ["rpm", "-q", "--qf", "...", "cassandra"]. A name
    with a version (java-17-openjdk-headless-17.0.9) reads that package as rpm -q does."""
    flat = []
    for name in names if isinstance(names, (list, tuple)) else [names]:
        for n in name if isinstance(name, (list, tuple)) else [name]:
            if n and str(n) not in flat:
                flat.append(str(n))
    return (_DPKG if os_family == "Debian" else _RPM) + flat


def cassandra_installed_packages(read):
    """The registered result of cassandra_package_query's command (or its output) -> {name: [{name, version,
    release, epoch, arch}, ...]} for the names installed (Debian: name, version, arch), as package_facts gives
    them; the names not installed left out ("package x is not installed", a dpkg state of a removed package,
    "no packages found matching x"). A result skipped (not read) -> {}. Fails, as package_facts would, when the
    command gave nothing of that (no rpm or dpkg-query, a database that can't be read)."""
    if not isinstance(read, dict):
        read = {"stdout": read, "rc": 0}
    if "stdout" not in read and "rc" not in read:
        return {}
    packages = {}
    understood = 0
    for line in str(read.get("stdout") or "").splitlines():
        fields = line.split("\t")
        if len(fields) != 6 or not fields[1]:
            understood += bool(_RPM_ABSENT.match(line))
            continue
        understood += 1
        state, name, version, release, epoch, arch = fields
        if state in _NOT_INSTALLED:
            continue
        package = {"name": name, "version": version, "arch": arch}
        if release:  # rpm (a dpkg line has none)
            package.update(release=release, epoch=None if epoch == "(none)" else epoch)
        packages.setdefault(name, []).append(package)
    understood += len([line for line in str(read.get("stderr") or "").splitlines() if _DPKG_ABSENT.match(line)])
    if read.get("rc", 0) != 0 and not understood:
        raise AnsibleFilterError("the installed packages could not be read: %s"
                                 % (str(read.get("stderr") or read.get("msg") or "").strip() or "rc %s" % read.get("rc")))
    return packages


class FilterModule(object):
    def filters(self):
        return {"cassandra_package_query": cassandra_package_query,
                "cassandra_installed_packages": cassandra_installed_packages}
