from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster's read of the OS: every kind of record the OS filters read (cassandra_os: the report, and the
# model of what cassandra_linux would change, which the self-check runs) is printed by its script; the facts tell
# a container (cassandra_linux applies nothing live there).

import os
import re
import subprocess

import pytest
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


def firewall_records(tmp_path, enabled="disabled", active="active", permanent="9042/tcp 7000/tcp", rich=(),
                     rich_permanent=None, ufw=None):
    """What the read's firewall lines print on a node whose firewalld runs (stand-ins for systemctl, firewall-cmd),
    rich: its rich rules (as firewall-cmd lists them), the same after a reload unless rich_permanent says; or whose
    ufw runs, ufw: the lines of ufw status."""
    text = script()
    snippet = text[text.index("# firewall: firewalld, ufw"):text.index("exit 0", text.index("# firewall: firewalld, ufw"))]
    assert "{{" not in snippet
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    svc = "ufw" if ufw is not None else "firewalld"
    rich_permanent = rich if rich_permanent is None else rich_permanent
    stubs = {
        "systemctl": """#!/bin/sh
case "$1 $2" in
  "is-enabled %s") echo %s; [ %s = enabled ] ;;
  "is-active %s") echo %s; [ %s = active ] ;;
  *) exit 4 ;;
esac
""" % (svc, enabled, enabled, svc, active, active),
        "firewall-cmd": """#!/bin/sh
case "$*" in
  --get-default-zone) echo public ;;
  --get-active-zones) printf 'public\\n  interfaces: eth0\\n' ;;
  "--zone=public --list-ports") echo "9042/tcp 7000/tcp" ;;
  "--permanent --zone=public --list-ports") echo "%s" ;;
  "--zone=public --list-rich-rules") cat <<'END'
%s
END
  ;;
  "--permanent --zone=public --list-rich-rules") cat <<'END'
%s
END
  ;;
esac
""" % (permanent, "\n".join(rich), "\n".join(rich_permanent))}
    if ufw is not None:
        del stubs["firewall-cmd"]
        stubs["ufw"] = """#!/bin/sh
cat <<'END'
Status: active

To                         Action      From
--                         ------      ----
%s
END
""" % "\n".join(ufw)
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


PORTS = ["7000/tcp", "9042/tcp", "7199/tcp"]
JMX_FROM = ['rule family="ipv4" source address="10.0.9.0/24" port port="7199" protocol="tcp" accept',
            'rule family="ipv6" source address="2001:db8::/64" port port="7199" protocol="tcp" accept']


def imported_and_checked(out, family="RedHat"):
    """The import's firewall values from the records, and what cassandra_firewall would change with them."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import (
        cassandra_firewall_changes, cassandra_os_import)
    r = cassandra_os_import(out, {"ports": PORTS})
    fw = {"manage": True, "enabled": True, "open_ports": r["vars"].get("open_ports", []),
          "port_sources": r["vars"].get("cassandra_firewall_port_sources", {})}
    return r, cassandra_firewall_changes(out, fw, family, PORTS)


def test_firewalld_ports_open_to_sources_carried_as_the_role_writes_them(tmp_path):
    out = firewall_records(tmp_path, enabled="enabled", rich=JMX_FROM)
    r, changes = imported_and_checked(out)
    assert r["vars"]["cassandra_firewall_port_sources"] == {"7199/tcp": ["10.0.9.0/24", "2001:db8::/64"]}
    assert changes == []
    # a source the inventory adds: opened by the role, a change
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_firewall_changes
    fw = {"manage": True, "enabled": True, "open_ports": ["9042/tcp", "7000/tcp"],
          "port_sources": {"7199/tcp": ["10.0.9.0/24", "2001:db8::/64", "10.0.8.1"]}}
    assert cassandra_firewall_changes(out, fw, "RedHat", PORTS) == [
        {"what": "port 7199/tcp from 10.0.8.1", "now": "closed", "would": "open", "effect": "change"}]
    # one the inventory leaves out: the role removes none, new nodes would not get it
    fw["port_sources"] = {"7199/tcp": ["10.0.9.0/24"]}
    assert cassandra_firewall_changes(out, fw, "RedHat", PORTS) == [
        {"what": "firewall rule (not carried)", "now": "port 7199/tcp open to 2001:db8::/64",
         "would": "not written by the roles", "effect": "change"}]


def test_port_open_to_everyone_and_to_sources_the_role_closes_it_to_everyone(tmp_path):
    out = firewall_records(tmp_path, enabled="enabled", rich=[
        'rule family="ipv4" source address="10.0.9.0/24" port port="7000" protocol="tcp" accept'])
    r, changes = imported_and_checked(out)
    assert r["vars"]["open_ports"] == ["9042/tcp", "7000/tcp"]
    assert r["vars"]["cassandra_firewall_port_sources"] == {"7000/tcp": ["10.0.9.0/24"]}
    assert changes == [{"what": "port 7000/tcp", "now": "open to everyone",
                        "would": "closed to everyone (open to 10.0.9.0/24 only)", "effect": "change"}]


@pytest.mark.parametrize("rich, rich_permanent, said", [
    # not after a reload, or only after one
    (JMX_FROM[:1], [], "port 7199/tcp open to 10.0.9.0/24 now only"),
    ([], JMX_FROM[:1], "port 7199/tcp open to 10.0.9.0/24 from the next reload only"),
    # what the role does not write: logging, another action, other elements, a family that is not the address's
    (['rule family="ipv4" source address="10.0.9.0/24" port port="7199" protocol="tcp" log prefix="jmx" accept'], None,
     "rich rule rule family="),
    (['rule family="ipv4" source address="10.0.9.0/24" port port="7199" protocol="tcp" reject'], None,
     "rich rule rule family="),
    (['rule family="ipv4" source address="10.0.9.0/24" destination address="10.0.0.1" port port="7199"'
      ' protocol="tcp" accept'], None, "rich rule rule family="),
    (['rule family="ipv4" source address="2001:db8::/64" port port="7199" protocol="tcp" accept'], None,
     "rich rule rule family=")])
def test_source_rules_the_role_does_not_write_stop(tmp_path, rich, rich_permanent, said):
    out = firewall_records(tmp_path, enabled="enabled", rich=rich, rich_permanent=rich_permanent)
    r, changes = imported_and_checked(out)
    assert "cassandra_firewall_port_sources" not in r["vars"]
    assert [c["what"] for c in changes] == ["firewall rule (not carried)"] and said in changes[0]["now"]


def test_ufw_ports_open_to_sources_carried(tmp_path):
    rules = ["9042/tcp                   ALLOW       Anywhere",
             "7199/tcp                   ALLOW       10.0.9.0/24",
             "9042/tcp (v6)              ALLOW       Anywhere (v6)",
             "7199/tcp (v6)              ALLOW       2001:db8::/64"]
    out = firewall_records(tmp_path, enabled="enabled", ufw=rules)
    assert "fw_rule|ufw|7199/tcp                   ALLOW       10.0.9.0/24" in out
    r, changes = imported_and_checked(out + "fw_state|ufw-rules|active|yes\n", "Debian")
    assert r["vars"]["open_ports"] == ["9042/tcp"]
    assert r["vars"]["cassandra_firewall_port_sources"] == {"7199/tcp": ["10.0.9.0/24", "2001:db8::/64"]}
    assert changes == []
    # on an interface, with a comment: not as the role writes it
    (tmp_path / "x").mkdir()
    out = firewall_records(tmp_path / "x", enabled="enabled", ufw=rules[:1] + rules[2:3] + [
        "7199/tcp on eth1           ALLOW       10.0.9.0/24"])
    r, changes = imported_and_checked(out + "fw_state|ufw-rules|active|yes\n", "Debian")
    assert "cassandra_firewall_port_sources" not in r["vars"]
    assert [c["what"] for c in changes] == ["firewall rule (not carried)"]
