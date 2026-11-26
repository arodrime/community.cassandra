from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The drift playbook's filters: OFF STOCK (each setting off the collection's default: its values, the default
# resolved per node, its cost, the grep that finds where it is set), BETWEEN NODES (import_cluster's convergence over
# the values off the default), FIXES, IN SYNC (apply_config's compare summed up). No file named: Ansible's values
# only. Neutral data: zorglub, 192.0.2.x.

import os
import re

from ansible_collections.community.cassandra.plugins.filter import cassandra_drift
from ansible_collections.community.cassandra.plugins.filter.cassandra_drift import (
    AREAS, BETWEEN_LINES, ELLIPSIS, FIXES_SHOWN, SCREEN_WIDTH, _cost_of, _grep_paths, cassandra_drift_screen,
    cassandra_drift_sections, cassandra_drift_sync, cassandra_drift_values, cassandra_drift_verdict)
from ansible_collections.community.cassandra.plugins.plugin_utils import cassandra_build
from ansible_collections.community.cassandra.tests.unit.helpers import golden_lines

SECRET = "zorglub-s3cr3t"
NODES = [{"name": n, "groups": ["prod", "prod_dc1"], "dc": "dc1", "rack": "rack1"} for n in ("db1", "db2", "db3")]
CMD = {"inventory": ["inventories/prod.yml"], "cwd": "/x"}


def sections(values, nodes=None, **kwargs):
    args = dict(CMD, width=400)
    args.update(kwargs)
    return cassandra_drift_sections(values, nodes or NODES, "prod", **args)


def text(result):
    return "\n".join(result["stock"] + result["between"])


def row(lines, setting):
    """The lines of a setting's row: its first line and the ones under it (indented further)."""
    start = next(i for i, line in enumerate(lines) if line.startswith("  %s " % setting))
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith("      ")), len(lines))
    return lines[start:end]


def every(**values):
    return dict((n, dict(values)) for n in ("db1", "db2", "db3"))


# --- OFF STOCK -----------------------------------------------------------------------------------------------------

GREP = "where: grep -Rnw %s inventories/prod.yml"  # (the -i file; no group_vars next to it here)


def test_a_setting_off_its_default_with_its_cost_and_the_grep():
    # (no file named: where a value is set is Ansible's precedence; the grep finds the files)
    result = sections(every(cassandra_concurrent_reads=64))
    lines = row(result["stock"], "cassandra_concurrent_reads")
    assert lines[0] == "  cassandra_concurrent_reads   64 on all (3 nodes)"
    assert lines[1] == "      default: 32"
    # (every node: no --limit naming them all)
    assert lines[2] == ("      3 nodes change, then restart; check: ansible-playbook -i inventories/prod.yml"
                        " community.cassandra.apply_config --check --diff")
    assert lines[3] == "      " + GREP % "cassandra_concurrent_reads"
    # some nodes only: --limit to them
    one = sections(dict(every(), db2={"cassandra_concurrent_reads": 64}))
    assert row(one["stock"], "cassandra_concurrent_reads")[2].endswith(
        "community.cassandra.apply_config --limit db2 --check --diff")
    assert result["counts"] == {"stock": 1, "between": 0, "kept": 0}
    assert "every node has the same value of every setting" in result["between"][1]
    assert "(not seen: -e, vars plugins, inventory plugins or scripts, vaulted files)" in \
        " ".join(line.strip() for line in result["stock"][:4])


def test_the_grep_quotes_its_dir_and_follows_links():
    result = sections(every(cassandra_concurrent_reads=64), inventory=["/x/my inv/prod.yml"])
    assert row(result["stock"], "cassandra_concurrent_reads")[3] == \
        "      where: grep -Rnw cassandra_concurrent_reads 'my inv/prod.yml'"


def test_a_release_and_a_java_cost_what_apply_config_does():
    # e2e: drift said "an upgrade (upgrade, update_java)" for both; apply_config updates a release of the series and
    # moves the Java in the node's restart, and the default release ("": any of the series) changes no node
    result = sections(every(cassandra_package_version="4.1.12", cassandra_java_version=8, cassandra_version="41x"))
    lines = row(result["stock"], "cassandra_package_version")
    assert lines[2] == ("      no node changes: the default keeps the release each node runs (a node added later gets"
                        " the repository's newest of the series)")
    lines = row(result["stock"], "cassandra_java_version")
    assert lines[2].startswith("      3 nodes change, then restart (apply_config moves the Java in it, as update_java);"
                               " check: ansible-playbook -i inventories/prod.yml community.cassandra.apply_config")
    lines = row(result["stock"], "cassandra_version")
    assert lines[2] == ("      3 nodes change, an upgrade (upgrade, its preflight phase first), never down; check: none,"
                        " apply_config does not show it")
    assert "update_java)," not in text(result)


def test_every_cost_line_ends_with_its_check_the_same_words_in_both_sections():
    # e2e: OFF STOCK said "apply_config: applied in the node's one restart" for a unit limit, BETWEEN NODES
    # "restart unknown (apply_config does not write it); check: none" for the same variable; some lines had no check
    values = {"db1": {"cassandra_service_limit_nofile": 500000, "cassandra_linux_timesync": "chrony"},
              "db2": {"cassandra_service_limit_nofile": 524288, "cassandra_linux_timesync": "chrony"},
              "db3": {"cassandra_service_limit_nofile": 524288, "cassandra_linux_timesync": "chrony"}}
    result = sections(values)
    stock = row(result["stock"], "cassandra_service_limit_nofile")
    between = row(result["between"], "cassandra_service_limit_nofile")
    assert stock[2].startswith("      3 nodes change, apply_config: ") and "; check: ansible-playbook" in stock[2]
    assert between[-1].startswith("      1 node changes, apply_config: ") and "; check: ansible-playbook" in between[-1]
    assert stock[2].split(", apply_config: ")[1].split(";")[0] == between[-1].split(", apply_config: ")[1].split(";")[0]
    assert "restart unknown" not in text(result)
    # (a restart for the time sync: its reason)
    timesync = row(result["stock"], "cassandra_linux_timesync")
    assert "then restart (apply_config starts time sync in the node's restart window, Cassandra stopped" in timesync[2]
    for lines in (result["stock"], result["between"]):
        costs = [line for line in lines if " change, " in line or " changes, " in line]
        assert costs and all("; check: " in line for line in costs), costs


def test_a_value_equal_to_its_default_is_at_stock_whatever_its_type():
    # "32" (a string in the inventory) is the default 32; "true" is True: nothing off stock
    result = sections(every(cassandra_concurrent_reads="32", cassandra_linux_swap_off="true"))
    assert result["counts"]["stock"] == 0
    assert "every setting of every node is at the collection's default" in result["stock"][1]


def test_templated_defaults_resolved_per_node():
    # cassandra_data_file_directories defaults to [cassandra_data_dir]: at stock when it follows the node's own
    # cassandra_data_dir; cassandra_key_cache_save_period's default depends on the node's series
    values = {"db1": {"cassandra_data_dir": "/data/x", "cassandra_data_file_directories": ["/data/x"],
                      "cassandra_version": "40x", "cassandra_key_cache_save_period": "14400"},
              "db2": {"cassandra_version": "40x", "cassandra_key_cache_save_period": "14400",
                      "cassandra_data_file_directories": ["/var/lib/cassandra/data"]},
              "db3": {"cassandra_version": "41x", "cassandra_key_cache_save_period": "14400"}}
    stock = sections(values)["stock"]
    assert not any(line.startswith("  cassandra_data_file_directories ") for line in stock)
    assert row(stock, "cassandra_data_dir")[1] == "      default: /var/lib/cassandra/data"
    period = row(stock, "cassandra_key_cache_save_period")
    assert period[0] == "  cassandra_key_cache_save_period   14400 on db3 (1 node)"
    assert period[1] == "      default: 4h"


def test_a_default_made_of_facts_is_known_once_the_nodes_are_read():
    values = every(cassandra_conf_dir="/etc/cassandra")
    unknown = sections(values)
    assert row(unknown["stock"], "cassandra_conf_dir")[1] == \
        "      default: not known here (made of the node's facts, or of a filter)"
    facts = dict((n, {"os_family": "Debian", "hostname": n}) for n in ("db1", "db2", "db3"))
    assert sections(values, facts=facts)["counts"]["stock"] == 0
    redhat = dict((n, {"os_family": "RedHat", "hostname": n}) for n in ("db1", "db2", "db3"))
    assert row(sections(values, facts=redhat)["stock"], "cassandra_conf_dir")[1] == "      default: /etc/cassandra/conf"


def test_no_file_is_ever_named():
    # (audit rc3 r2: drift named files from its own model of Ansible's inventory: never again, on any section)
    values = every(cassandra_concurrent_reads=64)
    values["db3"]["cassandra_concurrent_reads"] = 128
    result = sections(values)
    lines = row(result["stock"], "cassandra_concurrent_reads")
    assert lines[0] == "  cassandra_concurrent_reads   64 on db1, db2 (2 nodes); 128 on db3 (1 node)"
    between = row(result["between"], "cassandra_concurrent_reads")
    assert between[0] == "  cassandra_concurrent_reads   64 on db1, db2 (2 nodes); 128 on db3 (1 node)"
    assert "--limit db3 --check --diff" in between[1]
    every_line = "\n".join(result["stock"] + result["between"] + result["fixes"] + result["look"])
    # (a fix of WORTH A LOOK names a variable to remove, "remove cassandra_x", never a file: "remove x from <file>")
    assert not re.search(r"group_vars|host_vars|set in |remove \S+ from |read by other clusters", every_line)


def test_between_nodes_says_values_and_nodes_never_an_edit():
    # (audit A5: its values and nodes, as Ansible gives them, and what changing them costs; a secret shown ****)
    values = {"db1": {"cassandra_server_keystore_password": "zorglubA", "cassandra_concurrent_reads": 64},
              "db2": {"cassandra_server_keystore_password": "zorglubB", "cassandra_concurrent_reads": 64},
              "db3": {"cassandra_server_keystore_password": "zorglubB"}}
    between = sections(values)["between"]
    reads = row(between, "cassandra_concurrent_reads")
    assert reads[0] == "  cassandra_concurrent_reads   64 on db1, db2 (2 nodes); 32 (default) on db3 (1 node)"
    assert reads[1].startswith("      1 node changes, then restart; check: ") and len(reads) == 2
    secret = row(between, "cassandra_server_keystore_password")
    assert secret[0] == u"  cassandra_server_keystore_password   **** (value 2) on db2, db3 (2 nodes); **** on db1 (1 node)"
    assert u"\u2192" not in "\n".join(between) and "zorglub" not in "\n".join(between)


def test_a_node_on_the_default_differs_from_the_others():
    values = {"db1": {"cassandra_data_dir": "/data/x"}, "db2": {}, "db3": {}}
    result = sections(values)
    assert row(result["between"], "cassandra_data_dir")[0] == \
        "  cassandra_data_dir   /var/lib/cassandra/data (default) on db2, db3 (2 nodes); /data/x on db1 (1 node)"
    # where the data is: moved by hand, not a config change
    assert "a data move" in " ".join(row(result["stock"], "cassandra_data_dir"))


def test_dict_settings_by_key():
    sysctl = {"vm.swappiness": 10, "vm.max_map_count": 1048575, "vm.zone_reclaim_mode": 0}
    stock = sections(every(cassandra_linux_sysctl=sysctl))["stock"]
    swappiness = row(stock, "cassandra_linux_sysctl.vm.swappiness")
    assert swappiness[0] == "  cassandra_linux_sysctl.vm.swappiness   10 on all (3 nodes)"
    assert swappiness[1] == "      default: 1"
    assert swappiness[3] == "      " + GREP % "cassandra_linux_sysctl"  # (the dict's variable)
    # a key of the default the inventory's dict lacks
    keepalive = row(stock, "cassandra_linux_sysctl.net.ipv4.tcp_keepalive_time")
    assert keepalive[0].startswith("  cassandra_linux_sysctl.net.ipv4.tcp_keepalive_time   not set on all")
    assert not any(line.startswith("  cassandra_linux_sysctl.vm.max_map_count ") for line in stock)
    extra = sections(every(cassandra_extra_settings={"a_b": 3}))
    assert row(extra["stock"], "cassandra_extra_settings")[0] == '  cassandra_extra_settings   {"a_b": 3} on all (3 nodes)'


def test_files_kept_by_hand_apart():
    values = every()
    values["db1"]["cassandra_config_keep_files"] = ["logback.xml", "cassandra-env.sh"]
    values["db2"]["cassandra_config_keep_files"] = ["logback.xml"]
    result = sections(values)
    stock = result["stock"]
    assert not any(line.startswith("  cassandra_config_keep_files") for line in stock)
    at = stock.index(u"  KEPT BY HAND \u2014 cassandra_config_keep_files: Ansible does not manage these files")
    assert stock[at + 1] == "  cassandra-env.sh   kept on db1 (1 node)"
    assert stock[at + 2] == "  logback.xml        kept on db1, db2 (2 nodes)"
    assert result["counts"] == {"stock": 0, "between": 0, "kept": 2}


def test_secrets_never_shown():
    values = every(cassandra_server_keystore_password=SECRET, cassandra_jmx_password=SECRET,
                   cassandra_extra_settings={"ldap_password": SECRET}, cassandra_version="41x")
    values["db3"]["cassandra_server_keystore_password"] = SECRET + "-other"
    result = sections(values)
    shown = text(result)
    assert SECRET not in shown
    # two values told apart, neither shown
    assert row(result["stock"], "cassandra_server_keystore_password")[0] == \
        "  cassandra_server_keystore_password   **** on db1, db2 (2 nodes); **** (value 2) on db3 (1 node)"
    # its default (a password too: cassandra before 5.0) masked as well
    assert row(result["stock"], "cassandra_server_keystore_password")[1] == "      default: ****"
    assert row(result["stock"], "cassandra_extra_settings")[0] == "  cassandra_extra_settings   **** on all (3 nodes)"
    # the playbooks' logins: not node settings, named only
    assert "Also set, not settings of the nodes (how the roles and playbooks work): cassandra_jmx_password" in shown


def test_per_node_settings_not_counted():
    values = every(cassandra_cluster_name="Prod", cassandra_dc="dc2")
    for n, address in (("db1", "192.0.2.11"), ("db2", "192.0.2.12"), ("db3", "192.0.2.13")):
        values[n]["cassandra_listen_address"] = address
    result = sections(values)
    assert result["counts"] == {"stock": 0, "between": 0, "kept": 0}
    assert "Not counted, per node by nature or kept by each node for life: cassandra_cluster_name, cassandra_dc," \
           " cassandra_listen_address" in text(result)


def test_screen_width_and_default_inventory():
    result = sections(every(cassandra_concurrent_reads=64), width=80, inventory=["/x/inv"], default_inventory="/x/inv")
    assert all(len(line) <= 80 or COMMAND_LINE.search(line) for line in result["stock"])
    assert "ansible-playbook community.cassandra.apply_config" in text(result)  # (no -i: ansible.cfg's)


# --- the values ---------------------------------------------------------------------------------------------------

class Broken(dict):
    def __getitem__(self, key):
        if key == "cassandra_concurrent_writes":
            raise ValueError("an undefined variable in its template")
        return dict.__getitem__(self, key)


def test_values_from_hostvars_the_roles_variables_only():
    hostvars = {"db1": {"cassandra_concurrent_reads": 64, "ansible_host": "192.0.2.11", "my_own": 1},
                "db2": Broken(cassandra_concurrent_writes="{{ nope }}", cassandra_concurrent_reads=32)}
    assert cassandra_drift_values(hostvars, ["db1", "db2"]) == {
        "db1": {"cassandra_concurrent_reads": 64},
        "db2": {"cassandra_concurrent_reads": 32, "cassandra_concurrent_writes": "(not resolved here)"}}
    # Medusa's switch, read for its settings check (audit rc3 r3 B3), never a setting off stock
    hostvars["db1"]["cassandra_medusa_enabled"] = True
    hostvars["db1"]["cassandra_medusa_config_mode"] = "0644"
    values = cassandra_drift_values(hostvars, ["db1"])
    assert values["db1"]["cassandra_medusa_enabled"] is True
    result = sections(values, nodes=NODES[:1])
    assert "medusa.ini is set to mode 0644" in " ".join(result["look"]) and \
        not any("cassandra_medusa_enabled" in line for line in result["stock"])


def test_the_grep_reads_the_sources_and_their_vars_dirs(tmp_path):
    # (audit rc3 r3 B2: a source file in the project's top dir: the whole dir was searched, every file of it)
    (tmp_path / "inv" / "group_vars").mkdir(parents=True)
    (tmp_path / "inv" / "prod.yml").write_text("")
    (tmp_path / "hosts.yml").write_text("")
    (tmp_path / "group_vars").mkdir()
    (tmp_path / "other").mkdir()
    assert _grep_paths(["inv/prod.yml", "inv/prod.yml", "hosts.yml", str(tmp_path / "other")], str(tmp_path)) == [
        str(tmp_path / "inv" / "prod.yml"), str(tmp_path / "inv" / "group_vars"), str(tmp_path / "hosts.yml"),
        str(tmp_path / "group_vars"), str(tmp_path / "other")]


# --- IN SYNC ----------------------------------------------------------------------------------------------------

def test_in_sync():
    lines = cassandra_drift_sync([{"name": "db1"}, {"name": "db2", "todo": False}], [], "ansible-playbook x")
    assert lines[1:] == ["  db1, db2  in sync", "  every node matches the inventory: apply_config has nothing to apply"]


def test_a_node_not_running_said_so():
    # h-n4 stopped, its files in sync: "h-n1..h-n5 in sync", no word of it not running
    lines = cassandra_drift_sync([{"name": "db1", "then": "none"}, {"name": "db2", "then": "write"},
                                  {"name": "db3", "then": "start"}], [], "x")
    assert lines[1:] == ["  db1  in sync", "  db2, db3  in sync, Cassandra not running",
                         "  every node matches the inventory: apply_config has nothing to apply"]


def test_the_nodes_that_differ_and_the_command():
    nodes = [{"name": "db1", "todo": True, "then": "restart", "files": ["cassandra.yaml", "jvm-server.options"]},
             {"name": "db2", "todo": True, "then": "restart", "files": ["cassandra.yaml", "jvm-server.options"]},
             {"name": "db3", "os": ["vm.swappiness"], "then": "none"},
             {"name": "db4", "todo": True, "then": "none", "medusa": [{"item": "medusa.ini"}],
              "extras": [{"section": "Packages"}], "restart": []},
             {"name": "db5"}]
    lines = cassandra_drift_sync(nodes, [], "ansible-playbook community.cassandra.apply_config --check --diff")
    assert lines == [u"NOT IN SYNC — the nodes against the inventory (apply_config's compare, nothing changed)",
                     "  db1, db2  cassandra.yaml, jvm-server.options; then restart",
                     "  db3  OS settings: vm.swappiness; no restart",
                     "  db4  Medusa; Packages; no restart",
                     "  db5  in sync",
                     "  the settings themselves, before and after (changing nothing):",
                     "    ansible-playbook community.cassandra.apply_config --check --diff"]


def test_a_unit_file_that_differs_is_named():
    # e2e: Environment=ZORGLUB=1 added to the unit by hand: "cassandra.yaml, jvm-server.options; then restart", the
    # unit not named
    nodes = [{"name": "db1", "todo": True, "then": "restart", "files": ["cassandra.yaml"], "unit": ["cassandra.service"],
              "restart": [{"section": "cassandra unit"}]},
             {"name": "db2", "todo": False, "then": "none", "unit": ["zz-ansible-environment.conf"]}]
    assert cassandra_drift_sync(nodes, [], "x")[1:3] == ["  db1  cassandra.yaml, cassandra.service; then restart",
                                                         "  db2  in sync; zz-ansible-environment.conf written at its next"
                                                         " restart (no effect now)"]
    # a unit apply_config writes only at the next restart: as apply_config says it, the node in sync
    nodes[1]["todo"] = True
    assert cassandra_drift_sync(nodes, [], "x")[2] == "  db2  zz-ansible-environment.conf; no restart"


def test_a_procedure_of_its_own_said_with_its_command():
    stops = ["db1: in the inventory, not in the ring: adding a node is topology's (it bootstraps it).",
             "ansible-playbook community.cassandra.topology --check"]
    lines = cassandra_drift_sync([{"name": "db2"}], stops, "x")
    assert lines[1:] == ["  db2  in sync", "  apply_config would stop before any change (a procedure of its own first):",
                         "    " + stops[0], "    " + stops[1]]


# --- the screen -----------------------------------------------------------------------------------------------------
# An inventory shaped as a large cluster's after its import: 24 nodes in 3 racks, about 120 settings off the default
# in every area, 3 that differ between two groups of nodes (set in 17 host_vars), a value in every node's host_vars,
# long JSON values. Invented values only.

SCREEN_NODES = [{"name": "db%02d" % i, "groups": ["uat", "uat_dc1"], "dc": "dc1", "rack": "r%d" % ((i - 1) // 8 + 1)}
                for i in range(1, 25)]
# (block of the import's main.yml, how many of its variables the fixture sets off their default)
SCREEN_QUOTAS = (("Other cassandra.yaml settings", 40), ("Medusa", 26), ("Logging (logback.xml)", 5),
                 ("Versions & packages", 5), ("JVM & heap (cassandra-env.sh, jvm*-server.options)", 4),
                 ("systemd unit & service", 6), ("Network & ports", 4), ("Other", 6),
                 ("OS tuning (found on the nodes, for the nodes added later)", 3), ("JMX", 2))
SCREEN_CLUSTER = {
    "cassandra_version": "50x", "cassandra_medusa_enabled": True, "cassandra_medusa_config_mode": "0644", "cassandra_batch_size_fail_threshold": "6400KiB",
    "cassandra_data_dir": "/srv/zorglub/data", "cassandra_commitlog_dir": "/srv/zorglub/commitlog",
    "cassandra_hints_dir": "/srv/zorglub/hints", "cassandra_saved_caches_dir": "/srv/zorglub/saved_caches",
    "cassandra_authenticator": "PasswordAuthenticator", "cassandra_authorizer": "CassandraAuthorizer",
    "cassandra_linux_sysctl": dict(("net.zorglub.knob_%02d" % i, 4096 * i) for i in range(1, 9)),
    "cassandra_jvm_extra_options": ["-XX:+ZorglubOption%d" % i for i in range(1, 9)]}
SCREEN_SPLIT = {"cassandra_log_audit_compress": False, "cassandra_log_jmx_configurator": True,
                "cassandra_log_pattern": "%-5level [%thread] %date{ISO8601} %F:%L - %msg%n zorglub"}


def owner_like():
    """-> the values of the large cluster: SCREEN_CLUSTER and SCREEN_QUOTAS' settings on every node, SCREEN_SPLIT on
    db01..db17, cassandra_concurrent_compactors on every node."""
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_apply_config_coverage import category
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_files import block_title
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_layout import roles_defaults

    cluster = dict(SCREEN_CLUSTER)
    left = dict(SCREEN_QUOTAS)
    for key, default in sorted(roles_defaults().items()):
        block = block_title(key)
        if not left.get(block) or key in cluster or key in SCREEN_SPLIT or (category(key) or "?")[0] in ("-", "D") \
                or isinstance(default, (list, dict)) or "{{" in str(default) or "_password" in key \
                or key == "cassandra_auto_bootstrap":  # (a finding of its own: not this cluster's)
            continue
        left[block] -= 1
        cluster[key] = (not default if isinstance(default, bool) else default * 2 + 1 if isinstance(default, int)
                        else "zorglub-%d" % left[block])
    values = {}
    for i, node in enumerate(SCREEN_NODES):
        mine = dict(cluster, cassandra_concurrent_compactors=4)
        own = {"cassandra_concurrent_compactors": 4}
        if i < 17:
            own.update(SCREEN_SPLIT)
        mine.update(own)
        values[node["name"]] = mine
    return values


# a command on its own line, whole whatever its length (ansible-playbook ..., the grep that finds a setting)
COMMAND_LINE = re.compile(r"ansible-playbook |grep -Rnw")
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
COMMAND = "ansible-playbook -i inventories/prod.yml community.cassandra.apply_config --check --diff"


def owner_screen(monkeypatch, sync=None, details=False, stops=None):
    """The large cluster's screen at a terminal of 120 columns (the screen takes SCREEN_WIDTH at most)."""
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    monkeypatch.setattr(cassandra_build, "label", lambda: "community.cassandra 3.0.0-rc3")
    values = owner_like()
    shown = cassandra_drift_sections(values, SCREEN_NODES, "uat", width="terminal", **CMD)
    sync = sync or [{"name": n["name"]} for n in SCREEN_NODES]
    return cassandra_drift_screen(shown, sync, stops or [], COMMAND, "uat", details=details)


def test_the_screen_of_a_large_cluster_in_sync(monkeypatch):
    # e2e (a 24-node cluster): ~750 lines, the verdict at the very end; now the verdict first, what deserves a look,
    # each section summed up, every line within 79 columns
    lines = owner_screen(monkeypatch)
    assert lines == golden_lines(os.path.join(FIXTURES, "drift_screen_in_sync.txt"), lines)
    assert len(lines) <= 50 and max(len(line) for line in lines if not COMMAND_LINE.search(line)) <= SCREEN_WIDTH
    assert lines[0] == "DRIFT  uat  24 nodes  in sync"


def test_the_screen_of_a_large_cluster_not_in_sync(monkeypatch):
    sync = [{"name": n["name"]} for n in SCREEN_NODES]
    for node in sync[:2]:
        node.update(todo=True, then="restart", files=["cassandra.yaml"])
    sync[2].update(os=["vm.swappiness"], then="none")
    sync[3].update(compared=False)
    lines = owner_screen(monkeypatch, sync)
    assert lines == golden_lines(os.path.join(FIXTURES, "drift_screen_not_in_sync.txt"), lines)
    assert lines[0] == "DRIFT  uat  24 nodes  not in sync"
    assert lines[3:5] == ["NOT IN SYNC  3 nodes differ from the inventory, 1 node not compared",
                          "  db01, db02  cassandra.yaml; then restart"]
    assert all(len(line) <= SCREEN_WIDTH for line in lines if not COMMAND_LINE.search(line))


def test_the_counts_add_up_to_the_settings_off_stock(monkeypatch):
    lines = owner_screen(monkeypatch)
    start = next(i for i, line in enumerate(lines) if line.startswith("OFF STOCK  "))
    total = int(lines[start].split()[2])
    area = lines.index("  by area:", start)
    costs = [int(re.match(r"^    \D+?\s+(\d+)\b", line).group(1)) for line in lines[start + 2:area]]
    areas = [int(n) for line in lines[area + 1:] if line.startswith("    ") for n in re.findall(r"\s(\d+)\b", line)]
    assert sum(costs) == total == sum(areas) and total > 100
    assert any(line.startswith("    move data by hand          4   commitlog_dir, data_dir, hints_dir") for line in lines)


def test_what_going_back_costs_is_the_convergence_s_kind():
    def cost(key, stock="x"):
        return _cost_of({"key": key, "stock": {"db1": stock}})

    assert cost("cassandra_data_dir") == "move" and cost("cassandra_authenticator") == "switch"
    assert cost("cassandra_concurrent_reads") == "restart" and cost("cassandra_java_version") == "restart"
    assert cost("cassandra_linux_swap_off") == "live" and cost("cassandra_seeds") == "live"
    assert cost("cassandra_version") == "other"  # (an upgrade)
    assert cost("cassandra_package_version", "") == "other"  # (the default keeps the release each node runs)
    assert cost("cassandra_firewall_enabled") == "live"  # (the coverage map's: live, no restart)
    assert cost("cassandra_zorglub_unknown") == "other"


def test_every_block_of_the_import_has_an_area():
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_files import BLOCKS

    other = [title for title, dummy in BLOCKS if title not in AREAS]
    assert all(t == "Other" or t.startswith(("Left as it is", "Kept by hand", "Template pieces", "Secrets of"))
               for t in other), other


def test_between_nodes_on_the_screen_a_line_each_beyond_a_few_groups():
    # 2 values on alternate nodes, 4 values on 4 groups: no table, each setting then its values; 12 settings: the
    # ones that fit in BETWEEN_LINES
    nodes = [{"name": "db%d" % i, "groups": ["prod"], "dc": "dc1", "rack": "r1"} for i in range(1, 9)]
    values = dict(("db%d" % i, dict(("cassandra_concurrent_%s" % k, 40 + i % 4) for k in (
        "reads", "writes", "counter_writes", "compactors", "materialized_view_writes"))) for i in range(1, 9))
    for i in range(1, 9):
        values["db%d" % i].update(cassandra_column_index_size=8 + i % 2, cassandra_memtable_flush_writers=2 + i % 2,
                                  cassandra_native_transport_max_threads=100 + i % 2,
                                  cassandra_hinted_handoff_throttle=1024 + i % 2,
                                  cassandra_max_hints_delivery_threads=2 + i % 2,
                                  cassandra_tombstone_warn_threshold=2000 + i % 2,
                                  cassandra_tombstone_failure_threshold=200000 + i % 2)
    screen = cassandra_drift_sections(values, nodes, "prod", width="terminal", **CMD)["screen"]["between"]
    assert screen[0] == "BETWEEN NODES  12 settings differ"
    assert screen[1:4] == ["  cassandra_column_index_size",  # (no table: each setting, then its values)
                           "    8  on db2, db4, db6, db8 (4 nodes)", "    9  on db1, db3, db5, db7 (4 nodes)"]
    assert "    2 (default)  on db2, db4, db6, db8 (4 nodes)" in screen
    # (BETWEEN_LINES at most: the settings that do not fit counted)
    assert screen[-1] == "  6 more: see BETWEEN NODES in the report"
    assert len(screen) <= BETWEEN_LINES + 2 and all(len(line) <= SCREEN_WIDTH for line in screen)
    four = dict((n, {"cassandra_concurrent_reads": v["cassandra_concurrent_reads"]}) for n, v in values.items())
    screen = cassandra_drift_sections(four, nodes, "prod", width="terminal", **CMD)["screen"]["between"]
    assert screen[1:6] == ["  cassandra_concurrent_reads", "    40  on db4, db8 (2 nodes)", "    41  on db1, db5 (2 nodes)",
                           "    42  on db2, db6 (2 nodes)", "    1 more: see the report"]


def test_between_nodes_with_one_node_in_each_group_lists_each_setting():
    # rig (3 nodes, each its own value): no "group 1..3" columns; each setting, then each value and its node
    nodes = [{"name": "db%d" % i, "groups": ["prod"], "dc": "dc1", "rack": "r1"} for i in range(1, 4)]
    values = dict(("db%d" % i, {"cassandra_concurrent_reads": 30 + i, "cassandra_concurrent_writes": 40 + i})
                  for i in range(1, 4))
    screen = cassandra_drift_sections(values, nodes, "prod", width="terminal", **CMD)["screen"]["between"]
    assert not any("group" in line for line in screen), screen
    assert screen[1] == "  cassandra_concurrent_reads" and sorted(screen[2:5]) == [
        "    31  on db1 (1 node)", "    32 (default)  on db2 (1 node)", "    33  on db3 (1 node)"]


def test_the_details_on_the_screen_cut_long_values(monkeypatch):
    lines = owner_screen(monkeypatch, details=True)
    assert not any(line.startswith("OFF STOCK  ") for line in lines)  # (the summary's)
    assert any(line.startswith(u"OFF STOCK \u2014 ") for line in lines)
    assert all(len(line) <= SCREEN_WIDTH for line in lines if not COMMAND_LINE.search(line)), \
        [line for line in lines if len(line) > SCREEN_WIDTH and not COMMAND_LINE.search(line)]
    assert any(line.startswith("  cassandra_jvm_extra_options   [\"-XX:+ZorglubOption1\", \"-XX:+ZorglubOp" + ELLIPSIS)
               for line in lines)
    assert not any("host_vars" in line or "group_vars" in line for line in lines)
    assert "      where: grep -Rnw cassandra_concurrent_compactors inventories/prod.yml" in lines


def test_the_report_keeps_every_value_whole():
    values = owner_like()
    report = cassandra_drift_sections(values, SCREEN_NODES, "uat", **CMD)
    text = "\n".join(report["stock"] + report["between"])
    assert ELLIPSIS not in text and '"-XX:+ZorglubOption8"]' in text
    assert "host_vars" not in text and "group_vars" not in text
    assert report["look"][0].startswith(u"WORTH A LOOK — ")
    assert any(line.startswith("  medusa.ini is set to mode 0644 in the inventory for all")
               for line in report["look"])
    assert report["counts"]["between"] == 3


def test_the_verdict_on_the_nodes():
    assert cassandra_drift_verdict([{"name": "db1"}, {"name": "db2", "todo": False}]) == "in sync"
    assert cassandra_drift_verdict([{"name": "db1", "todo": True}, {"name": "db2", "compared": False}]) == \
        "1 node differs from the inventory, 1 node not compared"
    assert cassandra_drift_verdict([{"name": "db1", "compared": False}], ["stop"]) == \
        "apply_config would stop before any change, 1 node not compared"
    # (audit rc3 r3 C3: every node compared, a stop: said once, what stops under it, never "NOT IN SYNC  not in sync")
    lines = cassandra_drift_sync([{"name": "db1"}], ["X stops: why"], "x", screen=True)
    assert lines[0] == "NOT IN SYNC  apply_config would stop before any change" and "    X stops: why" in lines
    assert cassandra_drift_verdict([{"name": "db1", "medusa": [{"item": "medusa.ini"}]}]) == \
        "1 node differs from the inventory"  # (Medusa alone too)
    assert cassandra_drift_verdict([], [], compared=False) == "nodes not checked"


def test_in_sync_on_the_screen():
    nodes = [{"name": "db%d" % i} for i in range(1, 4)]
    assert cassandra_drift_sync(nodes, [], "x", screen=True) == [
        "IN SYNC", "  all 3 nodes match the inventory: apply_config has nothing to apply"]
    nodes[2]["then"] = "start"  # (its files compared, Cassandra not running: said)
    assert cassandra_drift_sync(nodes, [], "x", screen=True) == [
        "IN SYNC", "  db3  in sync, Cassandra not running",
        "  all 3 nodes match the inventory: apply_config has nothing to apply"]
    assert cassandra_drift_sync(nodes[:1], [], "x", screen=True)[1] == \
        "  the node matches the inventory: apply_config has nothing to apply"


def test_a_long_file_list_fits_the_screen():
    # rig (rc3b import, every file rewritten as stock): "  ds-n1..ds-n3  cassandra-env.sh, ... ; no restart" took
    # 166 columns
    files = ["cassandra-env.sh", "jvm-server.options", "jvm17-server.options", "cassandra.yaml", "jvm11-server.options",
             "cassandra-rackdc.properties", "logback.xml"]
    nodes = [{"name": "ds-n%d" % i, "todo": True, "then": "none", "files": files} for i in range(1, 4)]
    lines = cassandra_drift_sync(nodes, [], "x", screen=True)
    assert lines[1].startswith("  ds-n1..ds-n3  cassandra-env.sh, ") and lines[2].startswith("    ")
    assert all(len(line) <= SCREEN_WIDTH for line in lines), lines
    assert " ".join(line.strip() for line in lines[1:]).count("no restart") == 1


def test_a_long_name_leaves_its_value_less_room_on_the_screen(monkeypatch):
    # e2e: a long dict key and its value took 92 columns with -e drift_details=true
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    name = "ZORGLUB_A_RATHER_LONG_ENVIRONMENT_VARIABLE_NAME"
    values = {"db1": {"cassandra_service_environment": {name: "0123456789abcdef" * 3}},
              "db2": {"cassandra_service_environment": {name: "fedcba9876543210" * 3}}}
    result = cassandra_drift_sections(values, NODES[:2], "prod", width="terminal", **CMD)
    lines = result["stock"] + result["between"]
    at = lines.index("  cassandra_service_environment.%s" % name)  # (its values under it)
    assert lines[at + 1].startswith("      0123456789abcdef0123456789abcdef0123456" + ELLIPSIS + " on db1 (1 node)"), lines
    assert all(len(line) <= SCREEN_WIDTH for line in lines if not COMMAND_LINE.search(line)), lines


# --- FIXES (the hand edits) and the hosts files of the dir --------------------------------------------------------

GUIDE_NODES = [{"name": "db%02d" % i, "groups": ["uat", "uat_r%d" % ((i - 1) // 3 + 1)], "dc": "dc1",
                "rack": "r%d" % ((i - 1) // 3 + 1)} for i in range(1, 7)]


def guide(values):
    return cassandra_drift_sections(values, GUIDE_NODES, "uat", width="terminal", **CMD)["screen"]["fixes"]


def test_fixes_says_the_values_and_how_to_find_where_they_are_set(monkeypatch):
    # (audit rc3: where a value comes from is Ansible's to say; FIXES gives its values and nodes, then the grep)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    values = dict((n["name"], {"cassandra_concurrent_reads": 96 if "uat_r2" in n["groups"] else 64})
                  for n in GUIDE_NODES)
    values["db01"]["cassandra_log_jmx_configurator"] = True
    lines = guide(values)
    assert lines[0] == "FIXES  by hand: to give every node one value, edit where the inventory sets it"
    at = lines.index("  cassandra_concurrent_reads")
    assert lines[at + 1:at + 3] == ["    64 on db01..db03", "    96 on db04..db06"], lines
    at = lines.index("  cassandra_log_jmx_configurator")
    assert lines[at + 1:at + 3] == ["    false (default) on db02..db06", "    true on db01"], lines
    assert lines[-4:] == ["  where: grep -RnwE 'cassandra_concurrent_reads|cassandra_log_jmx_configurator'"
                          " inventories/prod.yml",
                          "    (not seen: -e, vars plugins, inventory plugins or scripts, vaulted files)",
                          "  then: review the edits (git diff in a git repository), then",
                          "    ansible-playbook -i inventories/prod.yml community.cassandra.apply_config --check --diff"]
    assert all(len(line) <= SCREEN_WIDTH for line in lines if not COMMAND_LINE.search(line))


def test_fixes_takes_care_of_a_data_move_and_a_version(monkeypatch):
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    values = dict((n["name"], {}) for n in GUIDE_NODES)
    values["db06"] = {"cassandra_data_dir": "/data/cassandra"}
    for name in ("db01", "db02"):
        values[name]["cassandra_package_version"] = "5.0.8"
    for name in ("db03", "db04", "db05", "db06"):
        values[name]["cassandra_package_version"] = "5.0.6"
    lines = guide(values)
    at = lines.index("  cassandra_data_dir")
    assert lines[at + 3].startswith("    take care: moved by hand first, the node stopped"), lines
    # (a version: the newest first, whatever the count; never down)
    at = lines.index("  cassandra_package_version")
    assert lines[at + 1:at + 4] == ["    newest: 5.0.8 on db01, db02", "    5.0.6 on db03..db06",
                                    "    take care: a node never goes down a version"], lines


def test_fixes_names_five_settings_three_values_each_on_the_screen(monkeypatch):
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    keys = ["cassandra_concurrent_%s" % k for k in ("reads", "writes", "counter_writes", "compactors",
                                                    "materialized_view_writes")] + ["cassandra_memtable_flush_writers"]
    values = dict((n["name"], dict((k, 10 + i) for k in keys)) for i, n in enumerate(GUIDE_NODES))
    shown = cassandra_drift_sections(values, GUIDE_NODES, "uat", width="terminal", **CMD)
    lines = shown["screen"]["fixes"]
    assert sum(1 for line in lines if re.match(r"^  cassandra_", line)) == FIXES_SHOWN
    assert "  1 more: FIXES in the report" in lines and lines.count("    3 more values: FIXES in the report") == 5
    assert sum(1 for line in shown["fixes"] if re.match(r"^  cassandra_", line)) == 6  # (the report: all)


def test_a_boolean_is_said_as_yaml_writes_it():
    # (audit rc3 C: "True" in BETWEEN NODES, "true" on the screen's table)
    values = {"db1": {"cassandra_log_jmx_configurator": True}, "db2": {"cassandra_log_jmx_configurator": True},
              "db3": {}}
    between = row(sections(values)["between"], "cassandra_log_jmx_configurator")
    assert between[0].startswith("  cassandra_log_jmx_configurator   true on db1, db2 (2 nodes)"), between


def test_a_version_split_with_the_default_compares_its_real_value(monkeypatch):
    # (audit rc3 r2 A1: the default was "last", by its text: newest 11 over Java 17's default, a downgrade planned)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    nodes = GUIDE_NODES[:3]
    for key, older, newer in (("cassandra_java_version", "11", None), ("cassandra_medusa_version", "0.29.0", None),
                              ("cassandra_medusa_version", None, "0.99.0")):
        value = older or newer
        values = {"db01": {key: value}, "db02": {}, "db03": {}}
        result = cassandra_drift_sections(values, nodes, "uat", width="terminal", **CMD)
        lines = result["screen"]["fixes"]
        at = lines.index("  " + key)
        if older:  # (the default, newer, first; the node on the older one changes, up)
            assert lines[at + 1].startswith("    newest: ") and lines[at + 1].endswith("(default) on db02, db03")
            assert lines[at + 2] == "    %s on db01" % older
            assert " 1 node changes" in " ".join(row(result["between"], key))  # (db01, up)
        else:
            assert lines[at + 1:at + 3] == ["    newest: 0.99.0 on db01", "    0.30.1 (default) on db02, db03"]
            assert " 2 nodes change" in " ".join(row(result["between"], key))  # (db02, db03, up)
        assert lines[at + 3] == "    take care: a node never goes down a version"


def test_fixes_takes_care_of_machines_and_keystores(monkeypatch):
    # (audit rc3 r2 B5)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    values = dict((n["name"], {}) for n in GUIDE_NODES)
    values["db01"] = {"cassandra_heap_size": "8G", "cassandra_server_keystore": "/etc/zorglub.jks"}
    text = " ".join(line.strip() for line in guide(values))
    assert "take care: sized for the machine: one value only where the machines are alike" in text
    assert "take care: then restart, which fails unless the file on the node matches it" in text
    # the version kind: an upgrade, never down
    values = dict((n["name"], {"cassandra_version": "41x"}) for n in GUIDE_NODES)
    values["db01"] = {"cassandra_version": "50x"}
    assert "take care: an upgrade (upgrade, its preflight phase first), never down" in \
        " ".join(line.strip() for line in guide(values))


def test_the_grep_names_the_settings_shown_only(monkeypatch):
    # (audit rc3 r2 R15: the screen's grep: the settings it names; the report's: all)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    keys = ["cassandra_concurrent_%s" % k for k in ("reads", "writes", "counter_writes", "compactors",
                                                    "materialized_view_writes")] + ["cassandra_memtable_flush_writers"]
    values = dict((n["name"], dict((k, 10 + i) for k in keys)) for i, n in enumerate(GUIDE_NODES))
    shown = cassandra_drift_sections(values, GUIDE_NODES, "uat", width="terminal", **CMD)
    screen = next(line for line in shown["screen"]["fixes"] if "grep -R" in line)
    report = next(line for line in shown["fixes"] if "grep -R" in line)
    assert screen.count("cassandra_") == FIXES_SHOWN and report.count("cassandra_") == 6


def test_none_compared_and_them():
    # (audit rc3 r2 C1: every node without a result: "none compared", the way out for them)
    sync = [{"name": "db%d" % i, "compared": False} for i in (1, 2, 3)]
    lines = cassandra_drift_sync(sync, [], "CMD", screen=True)
    assert lines[0] == "NONE COMPARED  3 nodes not compared"
    assert lines[1] == "  db1..db3  not compared: the compare gave no result for them"
    assert lines[-1] == "  WAY OUT  run drift again, then look at the run's output above for them"


def effective(values):
    nodes = [{"name": n, "dc": "dc1", "rack": "rack1"} for n in values]
    return cassandra_drift_sections(values, nodes, "prod", {}, width="terminal", **CMD)


def test_a_setting_differs_only_where_the_nodes_get_different_values(monkeypatch):
    # (audit rc3 r3 A1: each node's own default, rendered for it: Java 17 on 41x set by hand and 50x's default 17
    # is one value; 41x's default 11 and 11 set on a 50x node too)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    same = effective({"a": {"cassandra_version": "41x", "cassandra_java_version": "17"},
                      "b": {"cassandra_version": "41x", "cassandra_java_version": "17"}, "c": {}, "d": {}})
    assert not any("cassandra_java_version" in line for line in same["between"] + same["fixes"])
    same = effective({"a": {"cassandra_version": "41x"}, "b": {"cassandra_version": "41x"},
                      "c": {"cassandra_java_version": "11"}})
    assert not any("cassandra_java_version" in line for line in same["between"] + same["fixes"])
    # a value some of its nodes have as their own default, the others set by hand to it
    mixed = effective({"a": {"cassandra_version": "41x", "cassandra_java_version": "17"}, "b": {},
                       "c": {"cassandra_java_version": "11"}})
    said = " ".join(line.strip() for line in row(mixed["between"], "cassandra_java_version"))
    assert said.startswith("cassandra_java_version   17 (default on 1 of them) on a, b (2 nodes); 11 on c (1 node)")


def test_the_default_release_is_each_node_s_running_one(monkeypatch):
    # (audit rc3 r3 B4: "" keeps the release each node runs: no value to rank, no node counted as changing)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    result = effective({"a": {"cassandra_package_version": "4.1.5-1"}, "b": {"cassandra_package_version": "4.1.10"},
                        "c": {}})
    lines = result["fixes"]
    at = lines.index("  cassandra_package_version")
    # (audit rc3 r4 B1: which one is the newest is not known while a node keeps its running release: never said)
    assert lines[at + 1:at + 4] == ["    4.1.10 on b", "    4.1.5-1 on a", "    (each node's running release) on c"]
    assert " 1 node changes" in " ".join(row(result["between"], "cassandra_package_version"))  # (a, up; c not)
    # one release pinned, the others on their running one: no node changes, no check claimed
    result = effective({"a": {"cassandra_package_version": ""}, "b": {"cassandra_package_version": "5.0.9"}, "c": {}})
    assert row(result["between"], "cassandra_package_version")[-1] == "      no node changes"
    lines = result["fixes"]
    at = lines.index("  cassandra_package_version")
    assert lines[at + 1:at + 3] == ["    5.0.9 on b", "    (each node's running release) on a, c"]


def test_a_value_as_the_roles_read_it(monkeypatch):
    # (audit rc3 r4 A1: True from a YAML file, "true" from an INI file: one value for the roles, two for drift)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    result = effective({"a": {"cassandra_log_console": True, "cassandra_concurrent_reads": "64"},
                        "b": {"cassandra_log_console": "true", "cassandra_concurrent_reads": 64},
                        "c": {"cassandra_log_console": "yes", "cassandra_concurrent_reads": 64}})
    assert result["counts"]["between"] == 0 and not result["fixes"]
    assert row(result["stock"], "cassandra_log_console")[0] == "  cassandra_log_console   true on all (3 nodes)"
    assert row(result["stock"], "cassandra_concurrent_reads")[0] == "  cassandra_concurrent_reads   64 on all (3 nodes)"


def test_a_host_list_has_no_file_to_grep(monkeypatch):
    # (audit rc3 r4 C2: -i db1,db2,)
    result = sections(every(cassandra_concurrent_reads=64), inventory=["db1,db2,db3,"])
    assert row(result["stock"], "cassandra_concurrent_reads")[3] == \
        "      where: none: the inventory is a host list, no file"


def test_a_long_file_list_wraps_under_its_start():
    # (audit rc3 r4 C4: the next lines under the files, after the node names)
    files = ["cassandra-env.sh", "jvm-server.options", "jvm17-server.options", "cassandra.yaml", "logback.xml",
             "cassandra-rackdc.properties"]
    lines = cassandra_drift_sync([{"name": "ds-n%d" % i, "todo": True, "then": "none", "files": files}
                                  for i in (1, 2, 3)], [], "x", screen=True)
    assert lines[1].startswith("  ds-n1..ds-n3  cassandra-env.sh") and lines[2].startswith(" " * 16 + "c")


def test_off_stock_says_a_cost_it_does_not_know(monkeypatch):
    # (audit rc3 r4 C8: the "other" row named what it is)
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    stock = sections(every(cassandra_version="41x"), width="terminal")["screen"]["stock"]
    assert any(line.startswith("    cost not known ") for line in stock), stock


def test_a_boolean_as_the_bool_filter_reads_it(monkeypatch):
    # (audit rc3 r5: Ansible's bool filter, for the settings the roles read with it: their sources say which, a
    # templated default too (cassandra_log_audit_file); 1 and 0 too; "y", "t", " true" are no boolean for it: shown
    # as they are; None as (empty))
    from ansible_collections.community.cassandra.plugins.plugin_utils.cassandra_import_common import (
        as_bool, bool_read_settings)
    assert [as_bool(v) for v in (1, 0, "TRUE", "Off", "y", "t", " true", None, 2)] == \
        [True, False, True, False, "y", "t", " true", None, 2]
    assert {"cassandra_log_audit_file", "cassandra_log_jmx_configurator", "cassandra_log_console"} <= \
        bool_read_settings()
    monkeypatch.setattr(cassandra_drift, "terminal_width", lambda: 120)
    one = effective({"a": {"cassandra_log_audit_file": 1}, "b": {"cassandra_log_audit_file": "true"},
                     "c": {"cassandra_log_audit_file": True}})
    assert not any("cassandra_log_audit_file" in line for line in one["between"])
    two = effective({"a": {"cassandra_log_console": "y"}, "b": {"cassandra_log_console": True}})
    said = " ".join(line.strip() for line in row(two["between"], "cassandra_log_console"))
    assert "y on a" in said and "true on b" in said
    empty = effective({"a": {"cassandra_log_dir": None}, "b": {}})
    assert row(empty["stock"], "cassandra_log_dir")[0] == "  cassandra_log_dir   (empty) on a (1 node)"
