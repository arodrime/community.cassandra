# Copyright: Contributors to the community.cassandra collection
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""The report a changing operation writes on the controller (run_report.yml of cassandra_operation), next to the
import's: reports/<cluster group>/<operation>-<YYYY-MM-DD-HHMMSS>.txt (--check: <operation>-check-...).

cassandra_reports_dir: the dir of the reports: the one given, else reports/<cluster group> next to the
    inventory dir (as import_cluster writes its report.txt by default).
cassandra_run_report_name: the file name of a run's report.
cassandra_run_report: its text: the head (the operation, the cluster, the outcome, the collection's version and
    commit, when it started and ended), then its sections (the plan, what was done, the warnings), in full.
cassandra_run_report_times: when each node's turn started and how it ended, one node per line.
cassandra_run_report_old: the reports of an operation past the last ones kept (the oldest first).
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import datetime
import os
import re

from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_build
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_output as out
from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_secrets import mask

KEEP = 20  # the reports kept per operation and cluster


def cassandra_reports_dir(sources, group, given="", cwd=""):
    """sources: the inventory sources (ansible_inventory_sources); group: the cluster group; given: the dir the
    operator gave (cassandra_report_dir), relative to cwd (the dir the playbook was run from) -> the absolute dir of
    the reports: given, else reports/<group> next to the first source's dir (the source itself when a dir, else the
    dir it is in), else in cwd.
    e.g. (["/x/inventories"], "prod") -> "/x/reports/prod"; (["/x/inventories/prod.yml"], "prod") -> the same."""
    cwd = str(cwd or os.getcwd())
    if given:
        path = os.path.expanduser(str(given))
        return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    sources = [str(s) for s in sources or [] if s]
    if not sources:
        return os.path.normpath(os.path.join(cwd, "reports", str(group)))
    first = os.path.expanduser(sources[0])
    first = os.path.normpath(first if os.path.isabs(first) else os.path.join(cwd, first))
    inventory_dir = first if os.path.isdir(first) else os.path.dirname(first)
    return os.path.join(os.path.dirname(inventory_dir), "reports", str(group))


def _local(epoch):
    return datetime.datetime.fromtimestamp(int(float(epoch)))


def cassandra_run_report_name(operation, check=False, epoch=None):
    """The file name of the report of a run of operation that started at epoch (seconds; default now), the
    controller's local time, to the second (two runs in the same minute: two reports):
    "apply_config-2026-11-19-235841.txt", "apply_config-check-2026-11-19-235841.txt"."""
    when = _local(epoch) if epoch not in (None, "") else datetime.datetime.now()
    return "%s-%s%s.txt" % (operation, "check-" if check else "", when.strftime("%Y-%m-%d-%H%M%S"))


def _stamp(epoch):
    return _local(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch not in (None, "") else "not known"


def cassandra_run_report(sections, operation, cluster="", check=False, status="", started=None, ended=None,
                         build=None, run=""):
    """sections: [{title, lines, mask}] (one without lines left out; mask: true for lines of raw text, a failure's
    message, masked here; the others are built from masked text, as they are: masked again, a line naming
    secrets.yml or a "token:" text would lose its end); operation, cluster, check (--check), status (DONE,
    FAILED, STOPPED...; default CHECK under --check, else DONE), started, ended (seconds since the epoch), build (the
    collection's version and commit; default cassandra_build's), run (what the run is, for an operation that changes
    nothing: "read only, nothing was changed"; default by check) -> the report's text, its lines as they are (not
    fitted to a terminal).
    e.g. ([{title: "WILL DO", lines: ["node1  will apply"]}], "apply_config", "prod", started=..., ended=...) ->
    "apply_config  prod  DONE\\n  made by:  community.cassandra 3.0.0, commit ...\\n  started:  ...\\n...\\nWILL DO\\n
    node1  will apply\\n"."""
    status = status or ("CHECK" if check else "DONE")
    head = ["  ".join(x for x in (str(operation), str(cluster or ""), status) if x),
            "  made by:  %s" % (build or cassandra_build.label()),
            "  started:  %s" % _stamp(started)]
    if ended not in (None, ""):
        took = " (%s)" % out.duration(int(float(ended)) - int(float(started))) if started not in (None, "") else ""
        head.append("  ended:    %s%s" % (_stamp(ended), took))
    head.append("  run:      %s" % (run or ("--check, nothing was changed" if check else "a real run")))
    body = []
    for section in sections or []:
        lines = [mask(line) if section.get("mask") else str(line) for line in section.get("lines") or []]
        if not lines:
            continue
        body += [""] + ([str(section["title"])] if section.get("title") else []) + lines
    return "\n".join(head + body) + "\n"


def cassandra_run_report_times(nodes, starts=None):
    """nodes: [{name, result}] in their order; starts: {name: when its turn started, seconds since the epoch} (none
    or "": it never started) -> ["node1  23:58:02  apply_config done in 42s", "node2  -         not done (the run
    stopped before it)"]."""
    starts = starts or {}
    rows = []
    for node in nodes or []:
        start = starts.get(node.get("name"), node.get("start"))
        rows.append((str(node.get("name", "")), _local(start).strftime("%H:%M:%S") if start not in (None, "") else "-",
                     str(node.get("result") or "")))
    width = max([len(r[0]) for r in rows] or [0])
    return ["%s  %-8s  %s" % (name.ljust(width), start, result) for name, start, result in rows]


def cassandra_run_report_old(files, operation, keep=KEEP):
    """files: the dir's files (ansible.builtin.find's: [{path, mtime}]); operation -> the paths of its reports
    (cassandra_run_report_name's, --check's too) past the keep newest, the oldest first; the same for the .diff files
    next to them (apply_config's)."""
    old = []
    for ext in ("txt", "diff"):
        mine = re.compile(r"^%s-(check-)?\d{4}-\d{2}-\d{2}-\d{4}(\d{2})?\.%s$" % (re.escape(str(operation)), ext))
        reports = sorted((f for f in files or [] if mine.match(os.path.basename(str(f.get("path", ""))))),
                         key=lambda f: (float(f.get("mtime") or 0), os.path.basename(str(f["path"]))))
        old += [str(f["path"]) for f in reports[:max(len(reports) - int(keep), 0)]]
    return old


class FilterModule(object):
    def filters(self):
        return {"cassandra_reports_dir": cassandra_reports_dir,
                "cassandra_run_report_name": cassandra_run_report_name,
                "cassandra_run_report": cassandra_run_report,
                "cassandra_run_report_times": cassandra_run_report_times,
                "cassandra_run_report_old": cassandra_run_report_old}
