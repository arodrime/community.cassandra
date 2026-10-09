# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""cassandra_failure_cause: a failed task's cause in one short line (a
repository that can't be reached named as such); cassandra_failure_report:
the failures of several nodes at the same step, identical causes grouped,
then what to check (add_node's prepare step)."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import re

from ansible_collections.community.cassandra.plugins.module_utils import cassandra_output as out

# the longest cause shown (the whole error with -v)
_WIDTH = 100
# dnf, yum and apt saying a repository's metadata can't be downloaded
_REPO = (
    re.compile(r"(?:metadata for repo(?:sitory)?|for repo(?:sitory)?|repo(?:sitory)?)\s+'([^']+)'", re.IGNORECASE),
    re.compile(r"Failed to fetch (\S+)"),
)
_REPO_WORDS = re.compile(r"metadata|repomd|mirror|Failed to fetch|Cannot download|Curl error|Could not resolve|"
                         r"Temporary failure resolving|timed out|Connection refused", re.IGNORECASE)


# a login in a URL (a mirror's): https://user:****@host
_URL_LOGIN = re.compile(r"(://[^/\s:@]+:)[^@\s/]+@")


def _text(result):
    """The error's text: msg, a package module's failures, stderr, a loop's
    failed items (their msg, or the messages themselves); secrets masked."""
    if isinstance(result, dict):
        parts = [result.get("msg")] + list(result.get("failures") or []) + [result.get("stderr")]
        for item in result.get("results") or []:  # a loop: its failed items
            parts.append(item.get("msg") if isinstance(item, dict) and item.get("failed") else
                         item if not isinstance(item, dict) else None)
        text = "\n".join(str(p) for p in parts if p)
    else:
        text = str(result or "")
    return _URL_LOGIN.sub(r"\1****@", out.mask(text))


def cassandra_failure_repos(result):
    """The repositories a package task's error says it could not read
    (dnf/yum repo ids, apt URLs), [] when it is no repository problem."""
    text = _text(result)
    if not _REPO_WORDS.search(text):
        return []
    repos = []
    for pattern in _REPO:
        for repo in pattern.findall(text):
            if repo not in repos:
                repos.append(repo)
    return repos


def cassandra_failure_cause(result, task=""):
    """The cause of a failure in one short line: "Install python3.11:
    repository 'epel' unreachable (a mirror problem on the node)", else
    the task and the first line of its error, cut at _WIDTH."""
    repos = cassandra_failure_repos(result)
    if repos:  # our own words: never cut
        cause = "%s %s unreachable (a repository or mirror problem on the node)" % (
            "repository" if len(repos) == 1 else "repositories", ", ".join("'%s'" % r for r in repos))
        return "%s: %s" % (task, cause) if task else cause
    lines = [line.strip() for line in _text(result).splitlines() if line.strip()]
    line = "%s: %s" % (task, lines[0] if lines else "failed") if task else (lines[0] if lines else "failed")
    return line if len(line) <= _WIDTH else line[:_WIDTH - 3].rstrip() + "..."


def cassandra_failure_report(failures, operation="", cluster="", what="could not be prepared",
                             nothing="nothing was started", rerun="", ad_hoc="", python311=False):
    """failures: [{host, task, result, family}] (family: the OS family, for
    the repository command). The verdict, a line per cause with the nodes
    that share it, then the TO DO: the repository check (a repository
    problem), the cqlsh Python way out (python311: a python3.11 install
    failed), and the run again (rerun: its command). ad_hoc: the ansible
    command that starts the check (by default the one of rerun: its -i)."""
    ad_hoc = ad_hoc or (re.sub(r"^ansible-playbook", "ansible", rerun.split(" community.cassandra.")[0]) if rerun
                        else "ansible")
    groups = []
    for failure in failures or []:
        cause = cassandra_failure_cause(failure.get("result"), failure.get("task", ""))
        for group in groups:
            if group["cause"] == cause:
                group["hosts"].append(failure["host"])
                break
        else:
            groups.append({"cause": cause, "hosts": [failure["host"]], "repos": cassandra_failure_repos(failure.get("result")),
                           "family": failure.get("family", "")})
    hosts = [f["host"] for f in failures or []]
    # no node with a failure of its own (an include that failed...): said all the same
    lines = ["  ".join(x for x in ["FAILED", operation, cluster, "%s %s: %s" % (
        out.nodes(hosts, keep_order=True) if hosts else "the new nodes", what, nothing)] if x)]
    lines += ["  %s  %s" % (out.nodes(g["hosts"], keep_order=True), g["cause"]) for g in groups]
    items = []
    # the repository check, one command per OS family
    by_family = {}
    for group in groups:
        if group["repos"]:
            by_family.setdefault(group["family"] == "Debian", []).extend(group["hosts"])
    for debian, repo_hosts in sorted(by_family.items()):
        items.append({"text": "check the repositories of %s (a mirror down or unreachable from %s)" % (
            out.nodes(repo_hosts, keep_order=True), "it" if len(repo_hosts) == 1 else "them"),
            "command": '%s %s -b -m ansible.builtin.command -a "%s"' % (
                ad_hoc, ",".join(repo_hosts), "apt-get update" if debian else "dnf -q makecache")})
    if by_family:
        items.append("fix the repository or mirror on the node (or its proxy)")
    if python311:
        items.append("or, when cqlsh gets its Python another way, set cassandra_cqlsh_python_manage: false in the"
                     " inventory: python3.11 only runs cqlsh on the new nodes (the reset check reads the keyspaces with"
                     " cqlsh on a node of the cluster)")
    if rerun:
        items.append({"text": "run it again once fixed (%s)" % nothing, "command": rerun})
    return lines + [""] + out.todo(items) if items else lines


class FilterModule(object):
    def filters(self):
        return {"cassandra_failure_cause": cassandra_failure_cause,
                "cassandra_failure_repos": cassandra_failure_repos,
                "cassandra_failure_report": cassandra_failure_report}
