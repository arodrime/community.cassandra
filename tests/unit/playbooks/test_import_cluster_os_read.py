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
                     rich_permanent=None, ufw=None, answers=None, rules_enabled="yes"):
    """What the read's firewall lines print on a node whose firewalld runs (stand-ins for systemctl, firewall-cmd),
    rich: its rich rules (as firewall-cmd lists them), the same after a reload unless rich_permanent says; answers:
    {firewall-cmd arguments: output} more (another zone...); or whose ufw runs, ufw: the lines of ufw status,
    rules_enabled: ufw.conf ENABLED."""
    text = script()
    snippet = text[text.index("# firewall: firewalld, ufw"):text.index("exit 0", text.index("# firewall: firewalld, ufw"))]
    assert "{{" not in snippet
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    svc = "ufw" if ufw is not None else "firewalld"
    rich_permanent = rich if rich_permanent is None else rich_permanent
    said = {"--get-default-zone": "public", "--get-active-zones": "public\n  interfaces: eth0",
            "--zone=public --list-ports": "9042/tcp 7000/tcp", "--permanent --zone=public --list-ports": permanent,
            "--zone=public --list-rich-rules": "\n".join(rich),
            "--permanent --zone=public --list-rich-rules": "\n".join(rich_permanent)}
    said.update(answers or {})
    cases = "".join('  "%s") cat <<\'END\'\n%s\nEND\n  ;;\n' % (k, v) for k, v in said.items() if v)
    stubs = {
        "systemctl": """#!/bin/sh
case "$1 $2" in
  "is-enabled %s") echo %s; [ %s = enabled ] ;;
  "is-active %s") echo %s; [ %s = active ] ;;
  *) exit 4 ;;
esac
""" % (svc, enabled, enabled, svc, active, active),
        "firewall-cmd": '#!/bin/sh\ncase "$*" in\n%sesac\n' % cases}
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
    # (ufw.conf: read from a copy here)
    conf = tmp_path / "ufw.conf"
    conf.write_text("ENABLED=%s\n" % rules_enabled)
    return subprocess.run(["/bin/sh", "-c", "ports='7000|9042|7199'\n" + snippet.replace("/etc/ufw/ufw.conf", str(conf))],
                          env=env, stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout


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
    assert any("port 7000/tcp open to everyone now only" in c for c in r["carried"])


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


def test_source_rule_on_a_port_open_to_everyone_is_redundant(tmp_path):
    """It changes nothing: not carried, one note, no stop (carried, the role would close the port to everyone)."""
    for permanent in (None, []):  # in both sets, or now only
        out = firewall_records(tmp_path / str(permanent), enabled="enabled", rich_permanent=permanent, rich=[
            'rule family="ipv4" source address="10.0.9.0/24" port port="7000" protocol="tcp" accept'])
        r, changes = imported_and_checked(out)
        assert r["vars"]["open_ports"] == ["9042/tcp", "7000/tcp"]
        assert "cassandra_firewall_port_sources" not in r["vars"]
        assert "port 7000/tcp open to everyone: its rule for 10.0.9.0/24 changes nothing, not carried" in r["carried"]
        assert changes == []
    # sources wanted on it (yours): the role closes it to everyone
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_firewall_changes
    fw = {"manage": True, "enabled": True, "open_ports": ["9042/tcp"], "port_sources": {"7000/tcp": ["10.0.9.0/24"]}}
    assert cassandra_firewall_changes(out, fw, "RedHat", PORTS) == [
        {"what": "port 7000/tcp", "now": "open to everyone", "would": "closed to everyone (open to 10.0.9.0/24 only)",
         "effect": "change"}]


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
    r, changes = imported_and_checked(out, "Debian")
    assert r["vars"]["open_ports"] == ["9042/tcp"]
    assert r["vars"]["cassandra_firewall_port_sources"] == {"7199/tcp": ["10.0.9.0/24", "2001:db8::/64"]}
    assert changes == []
    # on an interface, with a comment: not as the role writes it
    (tmp_path / "x").mkdir()
    out = firewall_records(tmp_path / "x", enabled="enabled", ufw=rules[:1] + rules[2:3] + [
        "7199/tcp on eth1           ALLOW       10.0.9.0/24"])
    r, changes = imported_and_checked(out, "Debian")
    assert "cassandra_firewall_port_sources" not in r["vars"]
    assert [c["what"] for c in changes] == ["firewall rule (not carried)"]


def other_rules(out, family="RedHat", exporter=""):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_os_import
    r = cassandra_os_import(out, {"ports": PORTS, "exporter_port": exporter})
    return r, [c for c in r["carried"] if c.startswith("NOT carried")]


@pytest.mark.parametrize("answers, said", [
    # a rule after the next reload only, of a form the role does not write (it was never seen before)
    ({"--permanent --zone=public --list-rich-rules":
      'rule family="ipv4" source address="10.0.9.0/24" port port="7199" protocol="tcp" reject'},
     'zone public permanent rich rule rule family="ipv4"'),
    # another zone (active): now, or after a reload only
    ({"--get-active-zones": "public\n  interfaces: eth0\ninternal\n  sources: 10.0.0.0/8",
      "--permanent --zone=internal --list-ports": "7000-7001/tcp"}, "zone internal permanent ports 7000-7001/tcp"),
    ({"--get-active-zones": "public\n  interfaces: eth0\ninternal\n  sources: 10.0.0.0/8",
      "--zone=internal --list-ports": "7199/tcp"}, "zone internal ports 7199/tcp"),
    # a service on a Cassandra port
    ({"--zone=public --list-services": "cassandra", "--info-service=cassandra": "cassandra\n  ports: 9042/tcp"},
     "zone public service cassandra: 9042/tcp")])
def test_every_rule_on_a_port_the_role_touches_is_read(tmp_path, answers, said):
    out = firewall_records(tmp_path, enabled="enabled", answers=answers)
    r, other = other_rules(out)
    assert [o for o in other if said in o], other
    assert r["vars"]["open_ports"] == ["9042/tcp", "7000/tcp"]


def test_rules_on_ports_the_role_does_not_touch_are_left_alone(tmp_path):
    out = firewall_records(tmp_path, enabled="enabled", answers={
        "--zone=public --list-rich-rules": 'rule family="ipv6" source address="2001:db8:7000::/48" service name="ssh"'
                                           ' accept',
        "--permanent --zone=public --list-rich-rules": 'rule family="ipv6" source address="2001:db8:7000::/48"'
                                                       ' service name="ssh" accept',
        "--zone=public --list-services": "http", "--info-service=http": "http\n  ports: 80/tcp"})
    assert other_rules(out)[1] == []


def test_exporter_port_sources_carried_and_its_rules_read(tmp_path):
    rule = 'rule family="ipv4" source address="10.0.20.5" port port="9404" protocol="tcp" accept'
    r, other = other_rules(firewall_records(tmp_path / "a", enabled="enabled", rich=[rule]), exporter="9404")
    assert r["vars"]["cassandra_prometheus_exporter_sources"] == ["10.0.20.5"] and other == []
    # now only: not carried, a stop
    r, other = other_rules(firewall_records(tmp_path / "b", enabled="enabled", rich=[rule], rich_permanent=[]),
                           exporter="9404")
    assert "cassandra_prometheus_exporter_sources" not in r["vars"]
    assert other == ["NOT carried: zone public port 9404/tcp open to 10.0.20.5 now only (cassandra_firewall does not"
                     " write it; new nodes would not get it)"]


def test_ufw_rules_other_than_allow_on_a_cassandra_port_are_read(tmp_path):
    rules = ["9042/tcp                   ALLOW       Anywhere", "9042/tcp (v6)              ALLOW       Anywhere (v6)",
             "7199/tcp                   DENY        Anywhere", "7000/tcp                   LIMIT       Anywhere",
             "8080/tcp                   DENY        Anywhere"]
    out = firewall_records(tmp_path, enabled="enabled", ufw=rules)
    r, other = other_rules(out, "Debian")
    assert r["vars"]["open_ports"] == ["9042/tcp"]
    assert [o.split(" (")[0] for o in other] == [
        "NOT carried: ufw 7199/tcp DENY Anywhere", "NOT carried: ufw 7000/tcp LIMIT Anywhere"]


def test_ufw_boot_is_the_unit_the_role_enables(tmp_path):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_firewall_changes
    rules = ["9042/tcp                   ALLOW       Anywhere", "9042/tcp (v6)              ALLOW       Anywhere (v6)"]
    fw = {"manage": True, "enabled": True, "open_ports": ["9042/tcp"], "port_sources": {}}
    out = firewall_records(tmp_path / "a", enabled="disabled", ufw=rules)
    assert "fw_state|ufw|active|disabled\n" in out
    assert cassandra_firewall_changes(out, fw, "Debian", PORTS) == [
        {"what": "firewall ufw at boot", "now": "disabled", "would": "enabled", "effect": "change"}]
    assert cassandra_firewall_changes(firewall_records(tmp_path / "b", enabled="enabled", ufw=rules), fw, "Debian",
                                      PORTS) == []
    # no firewall wanted: ufw inactive now, its rules active from the next boot -> the role leaves it, said
    stopped = "fw_state|ufw|inactive|enabled\nfw_state|ufw-rules|inactive|yes\n"
    assert cassandra_firewall_changes(stopped, dict(fw, enabled=False), "Debian", PORTS) == [
        {"what": "firewall ufw", "now": "inactive, active from the next boot (ufw.conf ENABLED=yes)",
         "would": "left as it is: cassandra_firewall disables ufw only where it is active", "effect": "change"}]
    assert cassandra_firewall_changes(stopped.replace("|yes", "|no"), dict(fw, enabled=False), "Debian", PORTS) == []
