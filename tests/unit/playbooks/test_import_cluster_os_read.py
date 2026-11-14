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


def firewall_records(tmp_path, enabled="disabled", active="active", permanent="9042/tcp 7000/tcp"):
    """What the read's firewall lines print on a node whose firewalld runs (stand-ins for systemctl, firewall-cmd)."""
    text = script()
    snippet = text[text.index("# firewall: firewalld, ufw"):text.index("exit 0", text.index("# firewall: firewalld, ufw"))]
    assert "{{" not in snippet
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "systemctl": """#!/bin/sh
case "$1 $2" in
  "is-enabled firewalld") echo %s; [ %s = enabled ] ;;
  "is-active firewalld") echo %s; [ %s = active ] ;;
  *) exit 4 ;;
esac
""" % (enabled, enabled, active, active),
        "firewall-cmd": """#!/bin/sh
case "$*" in
  --get-default-zone) echo public ;;
  --get-active-zones) printf 'public\\n  interfaces: eth0\\n' ;;
  "--zone=public --list-ports") echo "9042/tcp 7000/tcp" ;;
  "--permanent --zone=public --list-ports") echo "%s" ;;
esac
""" % permanent}
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = dict(os.environ, PATH="%s:/usr/bin:/bin" % bin_dir)
    return subprocess.run(["/bin/sh", "-c", "ports='7000|9042|7199'\n" + snippet], env=env, stdout=subprocess.PIPE,
                          universal_newlines=True, check=True).stdout


def test_firewall_running_not_started_at_boot_is_read(tmp_path):
    # is-enabled exits 1 for disabled: the line is printed all the same (a false pass before)
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_firewall_changes
    out = firewall_records(tmp_path)
    assert "fw_state|firewalld|active|disabled\n" in out
    fw = {"manage": True, "enabled": True, "open_ports": ["9042/tcp", "7000/tcp"], "port_sources": {}}
    assert cassandra_firewall_changes(out, fw, "RedHat", ["7000/tcp", "9042/tcp", "7199/tcp"]) == [
        {"what": "firewall firewalld at boot", "now": "disabled", "would": "enabled", "effect": "change"}]


def test_firewall_runtime_only_port_is_read(tmp_path):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_os_import
    out = firewall_records(tmp_path, enabled="enabled", permanent="9042/tcp")
    r = cassandra_os_import(out, {"ports": ["7000/tcp", "9042/tcp", "7199/tcp"]})
    assert r["vars"]["open_ports"] == ["9042/tcp"]
    assert any("port 7000/tcp open now only" in c for c in r["carried"])
