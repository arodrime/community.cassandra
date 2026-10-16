from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# A stream of whole SSTables (zero-copy) counts a file in netstats once sent: on a node that receives
# (bootstrap, replace, rebuild) each check also reads the space used where it writes (stream_check.yml), its
# growth is progress; a decommission or a move goes by netstats only.

import os
import subprocess

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
TASKS = os.path.join(ROOT, "roles", "cassandra_service", "tasks")


def load(*path):
    with open(os.path.join(ROOT, *path)) as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def disk_task():
    block = load("roles", "cassandra_service", "tasks", "stream_check.yml")[0]["block"]
    return next(t for t in block if t["name"] == "Read the space used where this node writes")


def test_read_only_on_a_receiving_node_while_bytes_are_left():
    task = disk_task()
    assert task["check_mode"] is False and task["changed_when"] is False and task["failed_when"] is False
    assert task["ignore_unreachable"] is True  # one SSH glitch is a check without it, not the end of the wait
    assert "delegate_to" not in task  # the node itself
    when = task["when"]
    assert "_cassandra_stream_disk | default(false) | bool" in when
    assert "_cassandra_stream_state.transferring | default(false) | bool" in when
    # its reading is this node's, and goes to the progress
    block = load("roles", "cassandra_service", "tasks", "stream_check.yml")[0]["block"]
    work = next(t for t in block if t["name"] == "Work out the progress")
    assert "disk=_disk" in work["ansible.builtin.set_fact"]["_cassandra_stream_state"]
    assert render(work["vars"]["_disk"], inventory_hostname="n4", cassandra_stream_disk={"stdout": "123"}) == {"n4": "123"}
    assert render(work["vars"]["_disk"], inventory_hostname="n4", cassandra_stream_disk={"skipped": True}) == {}


def test_the_operations_that_receive_ask_for_it_the_others_not():
    def wait_vars(tasks):
        for t in tasks:
            for inner in t.get("block", []) + [t]:
                inc = inner.get("ansible.builtin.include_tasks") or (inner.get("ansible.builtin.include_role") or {}).get(
                    "tasks_from")
                if inc == "stream_wait.yml":
                    yield inner.get("vars", {})
    joins = list(wait_vars(load("roles", "cassandra_service", "tasks", "action_add.yml")))
    rebuild = [v for p in load("playbooks", "add_datacenter.yml") for v in wait_vars(p.get("tasks", []))]
    assert joins and all(v.get("_cassandra_stream_disk") is True for v in joins)
    assert rebuild and all(v.get("_cassandra_stream_disk") is True for v in rebuild)
    others = list(wait_vars(load("roles", "cassandra_service", "tasks", "action_decommission.yml")))
    others += list(wait_vars(load("roles", "cassandra_service", "tasks", "action_move.yml")))
    for name in ("move_node.yml", "remove_dead_node.yml"):
        others += [v for p in load("playbooks", name) for v in wait_vars(p.get("tasks", []))]
    assert len(others) >= 4 and not any("_cassandra_stream_disk" in v for v in others)


def run(command, path=None):
    env = dict(os.environ, PATH=path + os.pathsep + os.environ["PATH"]) if path else None
    return subprocess.run(["sh", "-c", command], stdout=subprocess.PIPE, universal_newlines=True, env=env,
                          check=False).stdout.strip()


def test_the_command_sums_each_filesystem_once(tmp_path):
    task = disk_task()
    data, commitlog = tmp_path / "data", tmp_path / "commitlog"
    data.mkdir()
    commitlog.mkdir()
    variables = {"_cassandra_preflight": {"cassandra_data_file_directories": [str(data)]},
                 "cassandra_commitlog_dir": str(commitlog)}
    dirs = render(task["vars"]["_dirs"], **variables)
    assert dirs == [str(data), str(commitlog)]
    command = render(task["ansible.builtin.shell"], _dirs=dirs)
    assert run(command).isdigit()  # the real df
    # two directories on one filesystem, a third on another: each filesystem once
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "df").write_text("#!/bin/sh\ncat <<'OUT'\n"
                             "Filesystem 1-blocks Used Available Capacity Mounted on\n"
                             "/dev/sdb1 1000 300 700 30% /data\n"
                             "/dev/sdb1 1000 300 700 30% /data\n"
                             "/dev/sdc1 1000 50 950 5% /commitlog\n"
                             "OUT\n")
    (fake / "df").chmod(0o755)
    assert run(command, str(fake)) == "350"
    # nothing read (a directory missing, df failing): nothing printed, no reading at this check
    assert run(render(task["ansible.builtin.shell"], _dirs=[str(tmp_path / "gone")])) == ""
    # the defaults when the preflight did not record them
    assert render(task["vars"]["_dirs"]) == ["/var/lib/cassandra/data", "/var/lib/cassandra/commitlog"]
