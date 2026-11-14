from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The check-mode audit of the roles import_cluster's self-check is to trust (cassandra_linux, cassandra_service,
# cassandra_firewall, cassandra_repository, cassandra_install's cqlsh): every task forced to run under --check
# (check_mode: false), skipped under it (a when on check mode), or running a command (skipped under it, so it
# reports nothing) is listed here with what --check makes of it. A task added to one of those, or changed into one,
# fails here until it is audited.
#   read: only reads (forced: it runs under --check)
#   report: its change is in the role's change report, worked out from a read (--check lists it)
#   model: import_cluster's model of cassandra_linux (cassandra_os.cassandra_linux_changes) covers it
#   no change: changes nothing on the node (a wait, a check, the role's own bookkeeping, a file's date)
#   side effect: forced, writes outside the node's settings (a package cache, a temporary file removed after)
#   gap: hidden under --check, not covered yet (said in the audit, for a later slice)

import os
import re

import pytest
import yaml

ROLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles")
SCOPE = {"cassandra_linux": None, "cassandra_service": None, "cassandra_firewall": None, "cassandra_repository": None,
         "cassandra_install": ["cqlsh_python.yml", "cqlsh_links.yml"]}
CHECK = re.compile(r"ansible_check_mode|_cassandra_linux_check|_cassandra_service_act")

L, S, F, R, I = "cassandra_linux", "cassandra_service", "cassandra_firewall", "cassandra_repository", "cassandra_install"
AUDITED = {
    (L, "disk_state.yml", "Read the devices' read-ahead, scheduler, type and ids", "forced"): ("read",),
    (L, "disks_apply.yml", "Read the devices' udev ids (stable across reboots, unlike their names)", "forced"): ("read",),
    (L, "disks_apply.yml", "Add it to the profiles tuned runs", "writer"): ("report", "model"),
    (L, "disks_apply.yml", "Date the profile as loaded", "silent"): ("no change",),
    (L, "disks_apply.yml", "Apply the kernel settings again", "skipped,silent"): ("report", "model"),
    (L, "disks_apply.yml", "Disable THP again", "skipped"): ("report", "model"),
    (L, "disks_apply.yml", "Read the devices again, as tuned left them", "skipped"): ("no change",),
    (L, "disks_apply.yml", "Set the read-ahead of the LVM volumes (kept in their metadata)", "writer"): ("report", "model"),
    (L, "disks_apply.yml", "Set the read-ahead", "writer"): ("report", "model"),
    (L, "disks_apply.yml", "Set the IO scheduler to none (SSD/NVMe)", "writer"): ("report", "model"),
    (L, "disks_find.yml", "Read what an earlier restart window could not do", "forced"): ("read",),
    (L, "disks_find.yml", "Check the stand-ins are the tools run (tests)", "forced"): ("read",),
    (L, "disks_find.yml", "Find the filesystem of each Cassandra directory", "forced"): ("read",),
    (L, "disks_find.yml", "Resolve the devices set explicitly (links such as /dev/vg/lv, /dev/disk/by-id/...)",
     "forced"): ("read",),
    (L, "disks_find.yml", "Read the block devices", "forced"): ("read",),
    (L, "disks_find.yml", "Read the LVM volumes", "forced"): ("read",),
    (L, "disks_find.yml", "List the filesystems of the data directories", "forced"): ("read",),
    (L, "disks_find.yml", "Look for a running Cassandra", "forced"): ("read",),
    (L, "disks_find.yml", "Read the active tuned profile", "forced"): ("read",),
    (L, "disks_find.yml", "Read the page size", "forced"): ("read",),
    (L, "os.yml", "Disable Swap", "writer"): ("report", "model"),
    (L, "os.yml", "Ensure service to disable THP is enabled at boot", "skipped"): ("report", "model"),
    (L, "os.yml", "Ensure service to disable THP is started", "skipped"): ("report", "model"),
    (L, "report.yml", "Add the time sync service to the change list", "skipped"): ("report",),
    (L, "snapshot.yml", "Read the current OS settings", "forced"): ("read",),
    (L, "timesync.yml", "Read the installed packages (time sync)", "forced"): ("read",),
    (L, "timesync.yml", "Look for the time sync service (offline)", "forced"): ("read",),
    (L, "timesync.yml", "Refuse a time sync that is not installed (offline)", "skipped"): ("no change",),
    (L, "timesync.yml", "Ensure timesync service starts", "skipped"): ("report", "model"),
    (S, "account_check.yml", "Read the account Cassandra runs as", "forced"): ("read",),
    (S, "config_snapshot.yml", "Record the config Cassandra started with", "writer"): ("no change",),
    (S, "main.yml", "Read the current service state (change report)", "forced"): ("read",),
    (S, "main.yml", "Look for a running Cassandra JVM", "forced"): ("read",),
    (S, "main.yml", "Stop cassandra", "skipped"): ("report",),
    (S, "main.yml", "Enable cassandra at boot", "skipped"): ("report",),
    (S, "main.yml", "Start or restart this node", "skipped"): ("report",),
    (S, "main.yml", "Wait for the node to be joined (Mode NORMAL)", "skipped"): ("no change",),
    (S, "main.yml", "Record the config Cassandra started with", "skipped"): ("no change",),
    (S, "unit.yml", "Tell whether the unit changes", "skipped"): ("no change",),
    (S, "wait_normal_check.yml", "Read the end of its journal", "silent"): ("no change",),
    (F, "firewall.yml", "Read the current firewall state (change report)", "forced"): ("read",),
    (F, "firewall.yml", "Read the installed packages", "forced"): ("read",),
    (F, "firewall.yml", "Ensure firewall service is started", "skipped"): ("report",),
    (F, "firewall.yml", "Ensure ports for cassandra open (RedHat)", "skipped"): ("report",),
    (F, "firewall.yml", "Ensure ports for cassandra open (Debian)", "skipped"): ("report",),
    (F, "firewall.yml", "Ports open to some sources only", "skipped"): ("gap",),
    (R, "no_repository.yml", "Under --check, the repositories dnf leaves out", "skipped"): ("no change",),
    (R, "no_repository.yml", "Update the apt cache (Debian & Ubuntu)", "forced"): ("side effect",),
    (R, "remove_apt_credentials.yml", "Look for the mark of this role in the apt credentials file", "forced"): ("read",),
    (R, "repository.yml", "Check the URL is a repository", "forced"): ("read",),
    (R, "repository.yml", "Refresh the apt cache (Debian & Ubuntu)", "forced"): ("side effect",),
    (R, "repository.yml", "Download the signing keys", "forced"): ("side effect",),
    (R, "repository.yml", "Look for gpg (--check)", "forced,skipped"): ("read",),
    (R, "repository.yml", "Read the fingerprints of the downloaded keys", "forced"): ("read",),
    (R, "repository.yml", "Remove the downloaded file", "forced"): ("side effect",),
    (R, "repository.yml", "Look for python3-debian (--check)", "forced,skipped"): ("read",),
    (I, "cqlsh_links.yml", "Find the site-packages of the Pythons cqlsh may use (RedHat)", "forced"): ("read",),
    (I, "cqlsh_links.yml", "Link cqlshlib into them (RedHat)", "skipped"): ("report",),
    (I, "cqlsh_links.yml", "Find the cqlsh Python pointers written by this role", "forced"): ("read",),
    (I, "cqlsh_python.yml", "Get the system python3 version", "forced"): ("read",),
    (I, "cqlsh_python.yml", "Read the installed packages", "forced"): ("read",),
    (I, "cqlsh_python.yml", "Check whether the distro repos have python3.11 (Ubuntu)", "forced"): ("read",),
    (I, "cqlsh_python.yml", "Add the deadsnakes PPA (Ubuntu without python3.11)", "skipped"): ("gap",),
    (I, "cqlsh_python.yml", "Look up python3.11 in the enabled repos (RedHat)", "forced"): ("read",),
    (I, "cqlsh_python.yml", "Install python3.11", "skipped"): ("report",),
}


def _walk(tasks):
    for t in tasks or []:
        if isinstance(t, dict):
            yield t
            for key in ("block", "rescue", "always"):
                yield from _walk(t.get(key))


def _kinds(task):
    out = []
    if task.get("check_mode") is False:
        out.append("forced")
    when = task.get("when")
    when = when if isinstance(when, list) else [when] if when else []
    if any(CHECK.search(str(c)) for c in when):
        out.append("skipped")
    if any(k.split(".")[-1] in ("command", "shell", "raw") for k in task) and "forced" not in out:
        out.append("silent" if task.get("changed_when") is False else "writer")
    return ",".join(out)


def found():
    out = []
    for role, files in SCOPE.items():
        tasks = os.path.join(ROLES, role, "tasks")
        for name in sorted(files or os.listdir(tasks)):
            with open(os.path.join(tasks, name), encoding="utf-8") as f:
                for task in _walk(yaml.safe_load(f)):
                    kinds = _kinds(task)
                    if kinds:
                        out.append((role, name, task.get("name"), kinds))
    return out


def test_every_task_check_mode_hides_or_forces_is_audited():
    seen = found()
    assert sorted(set(seen) - set(AUDITED)) == []  # new: audit it (and the model, for cassandra_linux)
    assert sorted(set(AUDITED) - set(seen)) == []  # gone or renamed: update the list


@pytest.mark.parametrize("key", sorted(k for k, v in AUDITED.items() if k[0] == "cassandra_linux" and "report" in v
                                       and k[2] != "Add the time sync service to the change list"))
def test_cassandra_linux_changes_hidden_by_check_mode_are_in_the_model(key):
    # the import's self-check judges cassandra_linux by its model: each change --check only shows in the report
    assert "model" in AUDITED[key]


def test_forced_writes_are_known():
    # a task run under --check that writes: only the package caches and the temporary key file
    assert sorted(k[2] for k, v in AUDITED.items() if "side effect" in v) == [
        "Download the signing keys", "Refresh the apt cache (Debian & Ubuntu)", "Remove the downloaded file",
        "Update the apt cache (Debian & Ubuntu)"]
