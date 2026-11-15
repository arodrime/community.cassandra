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
                     rich_permanent=None, ufw=None, answers=None, rules_enabled="yes", ipv6="yes", apps=None):
    """What the read's firewall lines print on a node whose firewalld runs (stand-ins for systemctl, firewall-cmd),
    rich: its rich rules (as firewall-cmd lists them), the same after a reload unless rich_permanent says; answers:
    {firewall-cmd arguments: output, False: it fails} more (another zone...); or whose ufw runs, ufw: the lines of
    ufw status (False: it fails), rules_enabled: ufw.conf ENABLED, ipv6: /etc/default/ufw IPV6."""
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
    cases = "".join('  "%s") exit 1 ;;\n' % k if v is False else '  "%s") cat <<\'END\'\n%s\nEND\n  ;;\n' % (k, v)
                    for k, v in said.items() if v or v is False)
    stubs = {
        "systemctl": """#!/bin/sh
case "$1 $2" in
  "is-enabled %s") echo %s; [ %s = enabled ] ;;
  "is-active %s") echo %s; [ %s = active ] ;;
  *) exit 4 ;;
esac
""" % (svc, enabled, enabled, svc, active, active),
        "firewall-cmd": '#!/bin/sh\ncase "$*" in\n%sesac\n' % cases}
    if ufw is False:
        del stubs["firewall-cmd"]
        stubs["ufw"] = "#!/bin/sh\nexit 1\n"
    elif ufw is not None:
        del stubs["firewall-cmd"]
        infos = "".join('  "app info %s") cat <<\'END\'\nProfile: %s\n\n%s\nEND\n  ;;\n' % (a, a, v)
                        for a, v in (apps or {}).items())
        listed = "".join("  %s\n" % a for a in apps or {})
        stubs["ufw"] = """#!/bin/sh
case "$*" in
  "app list") printf 'Available applications:\\n%s' ;;
%s  *) cat <<'END'
Status: active

To                         Action      From
--                         ------      ----
%s
END
  ;;
esac
""" % (listed.replace("\n", "\\n"), infos, "\n".join(ufw))
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = dict(os.environ, PATH="%s:/usr/bin:/bin" % bin_dir)
    # (ufw.conf: read from a copy here)
    conf = tmp_path / "ufw.conf"
    conf.write_text("ENABLED=%s\n" % rules_enabled)
    default = tmp_path / "default-ufw"
    default.write_text("IPV6=%s\n" % ipv6)
    snippet = snippet.replace("/etc/ufw/ufw.conf", str(conf)).replace("/etc/default/ufw", str(default))
    return subprocess.run(["/bin/sh", "-c", "ports='7000|9042|7199'\n" + snippet],
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
    fw = {"manage": True, "enabled": r["vars"].get("cassandra_firewall_enabled", True),
          "open_ports": r["vars"].get("open_ports", []),
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
    ({"--zone=public --list-services": "cassandra", "--info-service=cassandra": "cassandra\n  ports: 7199/tcp"},
     "zone public service cassandra: 7199/tcp")])
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


FIXTURES = os.path.join(os.path.dirname(__file__), "..", "plugins", "filter", "fixtures")


@pytest.mark.parametrize("platform, family", [("rockylinux9", "RedHat"), ("ubuntu2404", "Debian")])
def test_a_node_as_the_roles_molecule_converge_leaves_it_imports_without_a_stop(platform, family):
    """The firewall read (this script) of the containers cassandra_firewall's molecule scenario converged (with
    7199/tcp open to 10.0.9.0/24): imported, the self-check has nothing to say (RHEL's stock ssh service on 22/tcp,
    open to everyone by the role too, changes nothing). On Ubuntu the role starts ufw.service but enables no rule
    (ufw status inactive): no firewall, as the node is."""
    with open(os.path.join(FIXTURES, "firewall_read_role_built_%s.txt" % platform), encoding="utf-8") as f:
        out = f.read()
    r, changes = imported_and_checked(out, family)
    assert changes == []
    assert not [c for c in r["carried"] if c.startswith(("NOT carried", "NOT read"))]
    if family == "RedHat":
        assert r["vars"]["open_ports"] == ["22/tcp", "7000/tcp", "7001/tcp", "9042/tcp"]
        assert r["vars"]["cassandra_firewall_port_sources"] == {"7199/tcp": ["10.0.9.0/24"]}
        assert "zone public: service ssh: 22/tcp opens only ports open to everyone, changes nothing (not carried)" \
            in r["carried"]
    else:
        assert r["vars"]["cassandra_firewall_enabled"] is False


@pytest.mark.parametrize("failing", [
    "--permanent --zone=public --list-ports", "--zone=public --list-rich-rules", "--zone=public --list-services"])
def test_a_list_firewall_cmd_cannot_give_is_never_no_rule(tmp_path, failing):
    """firewall-cmd failing (D-Bus down, firewalld starting): said, the self-check stops; never read as no rule."""
    out = firewall_records(tmp_path, enabled="enabled", answers={failing: False})
    what = failing.replace("--zone=public --list-", "").replace("--permanent ", "permanent ").replace("-", " ")
    assert "fw_unread|zone public|%s\n" % what in out
    r, changes = imported_and_checked(out)
    assert ("NOT read: zone public %s could not be listed: its rules are not known, not carried; import again once"
            " firewall-cmd answers, or keep the firewall as the nodes have it (-e import_keep_hand_edits=firewall)"
            % what) in r["carried"]
    assert changes == [{"what": "firewall rules (not read)", "now": "zone public: %s could not be listed" % what,
                        "would": "not known: the roles may change them; import again once firewall-cmd answers, or"
                                 " keep the firewall as the nodes have it (-e import_keep_hand_edits=firewall)",
                        "effect": "change"}]


def test_ufw_status_failing_is_never_no_firewall(tmp_path):
    out = firewall_records(tmp_path, enabled="enabled", ufw=False)
    assert "fw|ufw|status unknown|\nfw_unread|ufw|ufw status\n" in out
    r, changes = imported_and_checked(out, "Debian")
    assert "cassandra_firewall_enabled" not in r["vars"]
    assert [c["what"] for c in changes] == ["firewall rules (not read)"]


def test_ufw_without_ipv6_writes_ipv4_rules_only(tmp_path):
    rules = ["9042/tcp                   ALLOW       Anywhere"]
    out = firewall_records(tmp_path, enabled="enabled", ufw=rules, ipv6="no")
    assert "fw|ufw|active|IPV6=no\n" in out
    r, changes = imported_and_checked(out, "Debian")
    assert r["vars"]["open_ports"] == ["9042/tcp"] and changes == []
    # with IPv6 on: IPv4 only is a rule in one set
    r, changes = imported_and_checked(firewall_records(tmp_path / "v6", enabled="enabled", ufw=rules), "Debian")
    assert r["vars"]["open_ports"] == [] and changes


def test_a_range_in_one_set_only_is_read(tmp_path):
    out = firewall_records(tmp_path, enabled="enabled", permanent="9042/tcp 7000/tcp 6990-7010/tcp")
    assert [c for c in imported_and_checked(out)[0]["carried"] if "port 6990-7010/tcp open to everyone from the next"
            " reload only" in c]


def test_the_import_and_its_self_check_touch_the_same_ports():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import firewall_touched
    assert firewall_touched(PORTS, "9404") == PORTS + ["9404/tcp"]
    assert firewall_touched(PORTS, "", ["22/tcp", "9042/tcp"], {"9404/tcp": ["10.0.20.5"]}) == PORTS + [
        "22/tcp", "9404/tcp"]


def test_direct_rules_on_a_cassandra_port_are_said(tmp_path):
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_os_import
    out = firewall_records(tmp_path, enabled="enabled", answers={
        "--direct --get-all-rules": "ipv4 filter INPUT 0 -p tcp --dport 9042 -j DROP"})
    r = cassandra_os_import(out, {"ports": PORTS})
    assert ("firewalld direct rule naming a port cassandra_firewall opens, not compared (it writes none): ipv4 filter"
            " INPUT 0 -p tcp --dport 9042 -j DROP") in r["carried"]


def test_a_port_range_open_in_both_sets_with_the_ssh_service(tmp_path):
    """A range open to everyone now and after a reload: carried as such, the import and the self-check compare by
    bounds (no error), and RHEL's ssh service on 22/tcp changes nothing."""
    answers = {"--zone=public --list-ports": "22/tcp 7000-7001/tcp 9042/tcp",
               "--permanent --zone=public --list-ports": "22/tcp 7000-7001/tcp 9042/tcp",
               "--zone=public --list-services": "ssh", "--permanent --zone=public --list-services": "ssh",
               "--info-service=ssh": "ssh\n  ports: 22/tcp", "--permanent --info-service=ssh": "ssh\n  ports: 22/tcp"}
    out = firewall_records(tmp_path, enabled="enabled", answers=answers)
    r, changes = imported_and_checked(out)
    assert r["vars"]["open_ports"] == ["22/tcp", "7000-7001/tcp", "9042/tcp"]
    assert changes == [] and not [c for c in r["carried"] if c.startswith("NOT")]
    # a range in open_ports, compared by the self-check: open, nothing to do; a port of it closed: opened
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_firewall_changes
    fw = {"manage": True, "enabled": True, "open_ports": ["7000-7001/tcp", "9042/tcp", "7199/tcp"], "port_sources": {}}
    assert cassandra_firewall_changes(out, fw, "RedHat", PORTS) == [
        {"what": "port 7199/tcp", "now": "closed", "would": "open", "effect": "change"}]


@pytest.mark.parametrize("answers, said", [
    # another protocol than the one open: a rule of its own
    ({"--zone=public --list-services": "x", "--permanent --zone=public --list-services": "x",
      "--info-service=x": "x\n  ports: 7000/udp", "--permanent --info-service=x": "x\n  ports: 7000/udp"},
     "zone public: service x: 7000/udp"),
    # a range wider than the ports open
    ({"--zone=public --list-services": "x", "--permanent --zone=public --list-services": "x",
      "--info-service=x": "x\n  ports: 7000-7001/tcp", "--permanent --info-service=x": "x\n  ports: 7000-7001/tcp"},
     "zone public: service x: 7000-7001/tcp"),
    # a service of another zone
    ({"--get-active-zones": "public\n  interfaces: eth0\ninternal\n  interfaces: eth1",
      "--zone=internal --list-services": "x", "--permanent --zone=internal --list-services": "x",
      "--info-service=x": "x\n  ports: 7000/tcp", "--permanent --info-service=x": "x\n  ports: 7000/tcp"},
     "zone internal: service x: 7000/tcp")])
def test_a_service_opening_more_than_the_ports_open_to_everyone_stops(tmp_path, answers, said):
    r, changes = imported_and_checked(firewall_records(tmp_path, enabled="enabled", answers=answers))
    assert [c for c in changes if said in c["now"]], changes


@pytest.mark.parametrize("rule", [
    "22                         ALLOW       Anywhere",  # (no protocol: udp too)
    "9042/tcp on eth1           ALLOW       Anywhere",  # (one interface)
    "9042/tcp                   ALLOW OUT   Anywhere"])  # (outgoing)
def test_ufw_rules_not_opening_only_the_ports_open_stop(tmp_path, rule):
    rules = ["9042/tcp                   ALLOW       Anywhere", "9042/tcp (v6)              ALLOW       Anywhere (v6)",
             "22/tcp                     ALLOW       Anywhere", "22/tcp (v6)                ALLOW       Anywhere (v6)"]
    r, changes = imported_and_checked(firewall_records(tmp_path, enabled="enabled", ufw=rules + [rule]), "Debian")
    assert [c for c in changes if c["what"] == "firewall rule (not carried)"], changes
    # the same port with its protocol, allowed again: changes nothing
    r, changes = imported_and_checked(firewall_records(tmp_path / "same", enabled="enabled", ufw=rules + [
        "9042/tcp                   ALLOW IN    Anywhere"]), "Debian")
    assert changes == []


@pytest.mark.parametrize("failing, what", [
    ("--get-default-zone", "firewalld the default zone"), ("--get-active-zones", "firewalld the active zones"),
    ("--info-service=x", "zone public service x")])
def test_the_zones_and_services_firewall_cmd_cannot_give_stop(tmp_path, failing, what):
    answers = {"--zone=public --list-services": "x", failing: False}
    out = firewall_records(tmp_path, enabled="enabled", answers=answers)
    r, changes = imported_and_checked(out)
    assert [c for c in r["carried"] if c.startswith("NOT read: %s could not be listed" % what)
            and c.endswith("import again once firewall-cmd answers, or keep the firewall as the nodes have it"
                           " (-e import_keep_hand_edits=firewall)")], r["carried"]
    assert [c for c in changes if c["what"] == "firewall rules (not read)"]


def test_the_exporter_port_in_one_set_only_stops_the_self_check(tmp_path):
    """The self-check touches the exporter's port as the import does (firewall_touched): its rule now only stops."""
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_firewall_changes
    rule = 'rule family="ipv4" source address="10.0.20.5" port port="9404" protocol="tcp" accept'
    out = firewall_records(tmp_path, enabled="enabled", rich=[rule], rich_permanent=[])
    fw = {"manage": True, "enabled": True, "open_ports": ["9042/tcp", "7000/tcp"], "port_sources": {},
          "exporter_port": "9404"}
    assert cassandra_firewall_changes(out, fw, "RedHat", PORTS) == [
        {"what": "firewall rule (not carried)", "now": "zone public: port 9404/tcp open to 10.0.20.5 now only",
         "would": "not written by the roles", "effect": "change"}]
    assert cassandra_firewall_changes(out, dict(fw, exporter_port=""), "RedHat", PORTS) == []


def test_sources_let_in_on_every_port_are_said(tmp_path):
    out = firewall_records(tmp_path, enabled="enabled", answers={
        "--get-active-zones": "public\n  interfaces: eth0\ntrusted\n  sources: 10.0.5.0/24",
        "--zone=public --list-rich-rules": 'rule family="ipv4" source address="10.0.6.0/24" accept',
        "--permanent --zone=public --list-rich-rules": 'rule family="ipv4" source address="10.0.6.0/24" accept'})
    r, changes = imported_and_checked(out)
    said = [c for c in r["carried"] if "not compared (cassandra_firewall writes none)" in c]
    assert said == ["firewalld zone trusted: sources 10.0.5.0/24: that source is handled by the zone's own rules, not"
                    " compared (cassandra_firewall writes none)",
                    'firewalld zone public: rich rule rule family="ipv4" source address="10.0.6.0/24" accept, for that'
                    " source on every port, Cassandra's too; not compared (cassandra_firewall writes none)"]
    assert changes == []


ODD_UFW = [
    "Anywhere                   ALLOW       10.0.0.1 7000/tcp",  # a source port
    "Anywhere (v6)              ALLOW       2001:db8::1 7000/tcp",
    "Cassandra                  ALLOW       Anywhere",  # an app profile
    "Anywhere                   ALLOW       Cassandra",
    "7000,9042/tcp              ALLOW       Anywhere",  # multiport: tcp for both
    "7001:7000/tcp              ALLOW       Anywhere",  # a reversed range
    "7000/tcp                   DENY        Anywhere",
    "7000/tcp                   ALLOW       Anywhere # a comment",
    "7000/tcp ALLOW",  # fields missing
    "/tcp                       ALLOW       Anywhere",
    "ssh/tcp                    ALLOW       Anywhere",
    "99999/tcp                  ALLOW       Anywhere"]
ODD_FIREWALLD = [
    "ports 7000/tcp ssh/tcp", "ports /tcp", "ports 7001-7000/tcp", "service x: ", "service x: cassandra/tcp",
    'rich rule rule family="ipv4" source address="10.0.9.0/24" service name="cassandra" accept',
    'rich rule rule family="ipv4" source address="10.0.9.0/24" source-port port="7000" protocol="tcp" accept',
    'rich rule rule family="ipv4" forward-port port="7000" protocol="tcp" to-port="7001"', "rich rule ", "ports"]


@pytest.mark.parametrize("line", ODD_UFW)
def test_odd_ufw_lines_never_raise_nor_slip_through(line):
    """A ufw line of any form: carried only when it is one the role writes; else said (a note, or a stop), never an
    error and never dropped when it may concern a Cassandra port."""
    base = ("fw_state|ufw|active|enabled\nfw_state|ufw-rules|active|yes\nfw|ufw|active|\n"
            "fw_rule|ufw|9042/tcp                   ALLOW       Anywhere\n"
            "fw_rule|ufw|9042/tcp (v6)              ALLOW       Anywhere (v6)\n")
    out = base + "fw_rule|ufw|%s\n" % line
    r, changes = imported_and_checked(out, "Debian")
    said = [c for c in r["carried"] if " ".join(line.split()) in c]
    assert said or line.startswith("7000,9042/tcp"), r["carried"]
    if line.startswith("7000,9042/tcp"):  # (opens 7000/tcp too, not open to everyone in both families: a stop)
        assert [c for c in changes if "7000,9042/tcp" in c["now"]]


@pytest.mark.parametrize("line", ODD_FIREWALLD)
def test_odd_firewalld_lines_never_raise_nor_slip_through(line):
    base = ("fw_state|firewalld|active|enabled\nfw|firewalld|active|default zone public\n"
            "fw_rule|zone public|ports 9042/tcp\nfw_rule|zone public|permanent ports 9042/tcp\n")
    out = base + "fw_rule|zone public|%s\n" % line
    r, changes = imported_and_checked(out)
    flat = " ".join(line.split())
    named = any(n in line for n in ("7000", "7001", "cassandra", "ssh"))
    shown = flat.replace("ports 7001-7000/tcp", "port 7000-7001/tcp")  # (a reversed range: said in order)
    assert not named or [c for c in r["carried"] if shown in c] or [c for c in changes if shown in c["now"]], r["carried"]


def test_ufw_source_port_rule_stops_both_paths():
    from ansible_collections.community.cassandra.plugins.filter.cassandra_os import cassandra_firewall_changes
    out = ("fw_state|ufw|active|enabled\nfw_state|ufw-rules|active|yes\nfw|ufw|active|\n"
           "fw_rule|ufw|Anywhere                   ALLOW       10.0.0.1 7000/tcp\n")
    r, changes = imported_and_checked(out, "Debian")
    assert "NOT carried: ufw Anywhere ALLOW 10.0.0.1 7000/tcp (cassandra_firewall does not write it; new nodes would" \
        " not get it)" in r["carried"]
    fw = {"manage": True, "enabled": True, "open_ports": [], "port_sources": {}}
    assert [c["now"] for c in cassandra_firewall_changes(out, fw, "Debian", PORTS)] == [
        "ufw: Anywhere ALLOW 10.0.0.1 7000/tcp"]
    # an allow-all from a source (no port): said, not a stop
    out = out.replace("10.0.0.1 7000/tcp", "10.0.9.0/24")
    r, changes = imported_and_checked(out, "Debian")
    assert changes == [] and "ufw ufw: Anywhere ALLOW 10.0.9.0/24, for that source on every port, Cassandra's too; not" \
        " compared (cassandra_firewall writes none)" in r["carried"]


def test_ufw_app_profiles_read_by_their_ports(tmp_path):
    """Ubuntu's OpenSSH profile on 22/tcp, open to everyone: changes nothing; a profile on a Cassandra port: a stop;
    a profile of no ports read: a stop."""
    rules = ["9042/tcp                   ALLOW       Anywhere", "9042/tcp (v6)              ALLOW       Anywhere (v6)",
             "OpenSSH                    ALLOW       Anywhere", "OpenSSH (v6)               ALLOW       Anywhere (v6)",
             "Apache Full                ALLOW       Anywhere"]
    apps = {"OpenSSH": "Port:\n  22/tcp", "Apache Full": "Ports:\n  80,443/tcp"}
    out = firewall_records(tmp_path, enabled="enabled", ufw=rules, apps=apps)
    assert "fw_svc|ufw app OpenSSH|22/tcp \n" in out and "fw_svc|ufw app Apache Full|80,443/tcp \n" in out
    r, changes = imported_and_checked(out, "Debian")
    assert changes == []  # (22/tcp not touched, 80 and 443 neither)
    apps["OpenSSH"] = "Port:\n  7000/tcp"
    r, changes = imported_and_checked(firewall_records(tmp_path / "c", enabled="enabled", ufw=rules, apps=apps), "Debian")
    assert [c["now"] for c in changes] == ["ufw: OpenSSH ALLOW Anywhere", "ufw: OpenSSH (v6) ALLOW Anywhere (v6)"]
    r, changes = imported_and_checked(firewall_records(tmp_path / "u", enabled="enabled", ufw=rules), "Debian")
    assert [c["now"] for c in changes] == ["ufw: OpenSSH ALLOW Anywhere", "ufw: OpenSSH (v6) ALLOW Anywhere (v6)",
                                           "ufw: Apache Full ALLOW Anywhere"]
