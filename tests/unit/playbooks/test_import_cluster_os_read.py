from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's read of the OS: every kind of record the OS filters read (cassandra_os: the report, and the
# model of what cassandra_linux would change, which the self-check runs) is printed by its script; the facts tell
# a container (cassandra_linux applies nothing live there).

import os
import re
import subprocess

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
PLAYBOOK = os.path.join(ROOT, "playbooks", "import_cluster.yml")
FILTER = os.path.join(ROOT, "plugins", "filter", "cassandra_os.py")


def tasks():
    with open(PLAYBOOK, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    for play in plays:
        yield from play.get("tasks") or []


def script():
    return next(t for t in tasks() if t.get("name") == "Read the OS tuning")["ansible.builtin.shell"]


def test_every_record_kind_read_is_printed():
    with open(FILTER, encoding="utf-8") as f:
        kinds = set(re.findall(r'kind [!=]= "(\w+)"', f.read()))
    text = script()
    missing = [k for k in sorted(kinds) if not re.search(r"(?<![\w-])%s\||\bf %s " % (k, k), text)]
    assert missing == []


def test_facts_tell_a_container():
    gather = next(t for t in tasks() if t.get("name") == "Gather facts")
    assert "virtual" in gather["ansible.builtin.setup"]["gather_subset"]


def test_limits_block_of_earlier_versions_read(tmp_path):
    # the awk of the script on a limits.conf with the block the role's earlier versions wrote
    awk = re.search(r"(awk '/\^#<!-- BEGIN.*?' )/etc/security/limits.conf", script(), re.S).group(1)
    conf = tmp_path / "limits.conf"
    conf.write_text("* soft core 0\n#<!-- BEGIN ANSIBLE MANAGED BLOCK -->\n# x\ncassandra - nofile 100000\n"
                    "#<!-- END ANSIBLE MANAGED BLOCK -->\n@admin - nproc 10\n")
    out = subprocess.run(["sh", "-c", awk + str(conf)], capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == ["limits_block_found|", "limits_block|cassandra - nofile 100000"]
