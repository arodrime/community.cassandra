from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The drift playbook's filters: FROM STOCK (each setting off the collection's default: its values, the default
# resolved per node, where it is set, the edit back, its cost), BETWEEN NODES (import_cluster's convergence over the
# values off the default), IN SYNC (apply_config's compare summed up). Neutral data: zorglub, 192.0.2.x.

from ansible_collections.community.cassandra.plugins.filter.cassandra_drift import (
    cassandra_drift_dirs, cassandra_drift_paths, cassandra_drift_sections, cassandra_drift_shared, cassandra_drift_sync,
    cassandra_drift_values)

SECRET = "zorglub-s3cr3t"
NODES = [{"name": n, "groups": ["prod", "prod_dc1"], "dc": "dc1", "rack": "rack1"} for n in ("db1", "db2", "db3")]
CMD = {"inventory": ["inventories/prod.yml"], "cwd": "/x"}


def sections(values, files=None, nodes=None, **kwargs):
    args = dict(CMD, width=400)
    args.update(kwargs)
    return cassandra_drift_sections(values, nodes or NODES, files or {}, "prod", **args)


def text(result):
    return "\n".join(result["stock"] + result["between"])


def row(lines, setting):
    """The lines of a setting's row: its first line and the ones under it (indented further)."""
    start = next(i for i, line in enumerate(lines) if line.startswith("  %s " % setting))
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith("      ")), len(lines))
    return lines[start:end]


def every(**values):
    return dict((n, dict(values)) for n in ("db1", "db2", "db3"))


# --- FROM STOCK -----------------------------------------------------------------------------------------------------

def test_a_setting_off_its_default_with_where_the_edit_and_the_cost():
    result = sections(every(cassandra_concurrent_reads=64),
                      {"group_vars/prod/main.yml": "cassandra_concurrent_reads: 64\n"})
    lines = row(result["stock"], "cassandra_concurrent_reads")
    assert lines[0] == "  cassandra_concurrent_reads   64 on all (3 nodes)"
    assert lines[1] == "      default: 32; set in group_vars/prod/main.yml"
    assert lines[2] == u"      → remove cassandra_concurrent_reads from group_vars/prod/main.yml"
    assert lines[3].startswith("      3 nodes change, then restart; check: ansible-playbook -i inventories/prod.yml"
                               " community.cassandra.apply_config --limit db1,db2,db3 --check --diff")
    assert result["counts"] == {"stock": 1, "between": 0, "kept": 0}
    assert "every node has the same value of every setting" in result["between"][1]


def test_a_release_and_a_java_cost_what_apply_config_does():
    # e2e: drift said "an upgrade (upgrade, update_java)" for both; apply_config updates a release of the series and
    # moves the Java in the node's restart, and the default release ("": any of the series) changes no node
    result = sections(every(cassandra_package_version="4.1.12", cassandra_java_version=8, cassandra_version="41x"),
                      {"group_vars/prod/main.yml": "cassandra_package_version: 4.1.12\ncassandra_java_version: 8\n"
                                                   "cassandra_version: 41x\n"})
    lines = row(result["stock"], "cassandra_package_version")
    assert lines[3] == ("      no node changes: the default keeps the release each node runs (a node added later gets"
                        " the repository's newest of the series)")
    lines = row(result["stock"], "cassandra_java_version")
    assert lines[3].startswith("      3 nodes change, then restart (apply_config moves the Java in it, as update_java);"
                               " check: ansible-playbook -i inventories/prod.yml community.cassandra.apply_config")
    lines = row(result["stock"], "cassandra_version")
    assert lines[3] == ("      3 nodes change, an upgrade (upgrade, its preflight phase first), never down; check: none,"
                        " apply_config does not show it")
    assert "update_java)," not in text(result)


def test_every_cost_line_ends_with_its_check_the_same_words_in_both_sections():
    # e2e: FROM STOCK said "apply_config: applied in the node's one restart" for a unit limit, BETWEEN NODES
    # "restart unknown (apply_config does not write it); check: none" for the same variable; some lines had no check
    values = {"db1": {"cassandra_service_limit_nofile": 500000, "cassandra_linux_timesync": "chrony"},
              "db2": {"cassandra_service_limit_nofile": 524288, "cassandra_linux_timesync": "chrony"},
              "db3": {"cassandra_service_limit_nofile": 524288, "cassandra_linux_timesync": "chrony"}}
    result = sections(values, {"group_vars/prod/main.yml": "cassandra_service_limit_nofile: 524288\n"
                                                           "cassandra_linux_timesync: chrony\n",
                               "host_vars/db1.yml": "cassandra_service_limit_nofile: 500000\n"})
    stock, between = row(result["stock"], "cassandra_service_limit_nofile"), row(result["between"],
                                                                                  "cassandra_service_limit_nofile")
    assert stock[-1].startswith("      3 nodes change, apply_config: ") and "; check: ansible-playbook" in stock[-1]
    assert between[-1].startswith("      1 node changes, apply_config: ") and "; check: ansible-playbook" in between[-1]
    assert stock[-1].split(", apply_config: ")[1].split(";")[0] == between[-1].split(", apply_config: ")[1].split(";")[0]
    assert "restart unknown" not in text(result)
    # (a restart for the time sync: its reason)
    timesync = row(result["stock"], "cassandra_linux_timesync")
    assert "then restart (apply_config starts time sync in the node's restart window, Cassandra stopped" in timesync[-1]
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
    result = sections(values)
    stock = result["stock"]
    assert not any(line.startswith("  cassandra_data_file_directories ") for line in stock)
    assert row(stock, "cassandra_data_dir")[1].startswith("      default: /var/lib/cassandra/data;")
    period = row(stock, "cassandra_key_cache_save_period")
    assert period[0] == "  cassandra_key_cache_save_period   14400 on db3 (1 node)"
    assert period[1].startswith("      default: 4h;")


def test_a_default_made_of_facts_is_known_once_the_nodes_are_read():
    values = every(cassandra_conf_dir="/etc/cassandra")
    unknown = sections(values)
    assert row(unknown["stock"], "cassandra_conf_dir")[1].startswith(
        "      default: not known here (made of the node's facts, or of a filter);")
    facts = dict((n, {"os_family": "Debian", "hostname": n}) for n in ("db1", "db2", "db3"))
    assert sections(values, facts=facts)["counts"]["stock"] == 0
    redhat = dict((n, {"os_family": "RedHat", "hostname": n}) for n in ("db1", "db2", "db3"))
    assert row(sections(values, facts=redhat)["stock"], "cassandra_conf_dir")[1].startswith(
        "      default: /etc/cassandra/conf;")


def test_host_vars_exception_named_with_its_file():
    values = every(cassandra_concurrent_reads=64)
    values["db3"]["cassandra_concurrent_reads"] = 128
    files = {"group_vars/prod/main.yml": "cassandra_concurrent_reads: 64\n",
             "host_vars/db3/main.yml": "cassandra_concurrent_reads: 128\n"}
    result = sections(values, files)
    lines = row(result["stock"], "cassandra_concurrent_reads")
    assert lines[0] == "  cassandra_concurrent_reads   64 on db1, db2 (2 nodes); 128 on db3 (1 node)"
    assert lines[1] == "      default: 32; set in group_vars/prod/main.yml, host_vars/db3/main.yml"
    assert "remove cassandra_concurrent_reads from group_vars/prod/main.yml, host_vars/db3/main.yml" in lines[2]
    between = row(result["between"], "cassandra_concurrent_reads")
    assert between[0] == "  cassandra_concurrent_reads   64 on db1, db2 (2 nodes); 128 on db3 (1 node)"
    assert "remove cassandra_concurrent_reads from host_vars/db3/main.yml" in between[1]
    assert "--limit db3 --check --diff" in between[2]
    assert result["counts"]["between"] == 1


def test_between_nodes_names_the_inventory_s_own_files():
    # (a hand-made inventory, group_vars/prod.yml and host_vars/db1.yml: the edits name them, never the import's
    # group_vars/prod/secrets.yml or host_vars/db1/; a secret shown ****, as FROM STOCK shows it)
    values = {"db1": {"cassandra_server_keystore_password": "zorglubA", "cassandra_concurrent_reads": 64},
              "db2": {"cassandra_server_keystore_password": "zorglubB", "cassandra_concurrent_reads": 64},
              "db3": {"cassandra_server_keystore_password": "zorglubB",
                      "cassandra_service_environment": {"JVM_EXTRA_OPTS": "-Djavax.net.ssl.keyStorePassword=zz"}}}
    files = {"group_vars/prod.yml": "cassandra_server_keystore_password: zorglubB\n",
             "host_vars/db1.yml": "cassandra_server_keystore_password: zorglubA\ncassandra_concurrent_reads: 64\n",
             "host_vars/db2.yml": "cassandra_concurrent_reads: 64\n",
             "host_vars/db3.yml": "cassandra_service_environment:\n  JVM_EXTRA_OPTS: -Djavax.net.ssl.keyStorePassword=zz\n"}
    between = sections(values, files)["between"]
    assert row(between, "cassandra_concurrent_reads")[1] == (
        u"      \u2192 64 for all: cassandra_concurrent_reads: 64 in group_vars/prod.yml, removed from host_vars/db1.yml,"
        u" host_vars/db2.yml")
    secret = row(between, "cassandra_server_keystore_password")
    assert secret[0] == u"  cassandra_server_keystore_password   **** (value 2) on db2, db3 (2 nodes); **** on db1 (1 node)"
    assert secret[1] == (u"      \u2192 **** (value 2) for all: already in group_vars/prod.yml: remove"
                         u" cassandra_server_keystore_password from host_vars/db1.yml")
    assert row(between, "cassandra_service_environment.JVM_EXTRA_OPTS")[1] == (
        u"      \u2192 default for all: remove JVM_EXTRA_OPTS from cassandra_service_environment in host_vars/db3.yml")
    assert "secrets.yml" not in "\n".join(between) and "zorglub" not in "\n".join(between)
    # no file of the cluster's group: where to put it named as a new file
    del files["group_vars/prod.yml"]
    values["db3"]["cassandra_server_keystore_password"] = values["db2"]["cassandra_server_keystore_password"] = "zorglubA"
    files["host_vars/db2.yml"] += "cassandra_server_keystore_password: zorglubA\n"
    files["host_vars/db3.yml"] += "cassandra_server_keystore_password: zorglubA\n"
    values["db3"]["cassandra_concurrent_reads"] = 64
    files["host_vars/db3.yml"] += "cassandra_concurrent_reads: 64\n"
    values["db1"]["cassandra_concurrent_reads"] = 128
    files["host_vars/db1.yml"] = files["host_vars/db1.yml"].replace("64", "128")
    reads = row(sections(values, files)["between"], "cassandra_concurrent_reads")
    assert "in group_vars/prod/main.yml (a new file)" in reads[1]


def test_a_node_on_the_default_differs_from_the_others():
    values = {"db1": {"cassandra_data_dir": "/data/x"}, "db2": {}, "db3": {}}
    result = sections(values, {"host_vars/db1.yml": "cassandra_data_dir: /data/x\n"})
    assert row(result["between"], "cassandra_data_dir")[0] == \
        "  cassandra_data_dir   default on db2, db3 (2 nodes); /data/x on db1 (1 node)"
    # where the data is: moved by hand, not a config change
    assert "a data move" in " ".join(row(result["stock"], "cassandra_data_dir"))


def test_a_file_other_clusters_read_says_how_to_leave_them_as_they_are():
    result = sections(every(cassandra_linux_swap_off=False), {"group_vars/all.yml": "cassandra_linux_swap_off: false\n"})
    lines = " ".join(row(result["stock"], "cassandra_linux_swap_off"))
    assert "remove cassandra_linux_swap_off from group_vars/all.yml (group_vars/all.yml is read by other clusters" \
           " too: to leave them as they are, set cassandra_linux_swap_off to true in group_vars/prod/ instead)" in lines
    assert "no restart (cassandra_linux, applied live)" in lines
    shared = sections(every(cassandra_linux_swap_off=False), {"group_vars/prod_dc1.yml": "cassandra_linux_swap_off: false\n"},
                      shared=["prod_dc1"])
    assert "group_vars/prod_dc1.yml is read by other clusters too" in text(shared)
    # (not shared: no such note)
    alone = sections(every(cassandra_linux_swap_off=False), {"group_vars/prod_dc1.yml": "cassandra_linux_swap_off: false\n"})
    assert "other clusters" not in text(alone)


def test_a_value_in_the_hosts_file_and_one_from_nowhere():
    hosts = "all:\n  children:\n    prod:\n      hosts:\n        db1: {cassandra_concurrent_reads: 64}\n        db2:\n"
    values = {"db1": {"cassandra_concurrent_reads": 64}, "db2": {"cassandra_concurrent_writes": 64}, "db3": {}}
    result = sections(values, {"prod.yml": hosts})
    assert "set in prod.yml" in row(result["stock"], "cassandra_concurrent_reads")[1]
    writes = row(result["stock"], "cassandra_concurrent_writes")
    assert "set in no vars file of the inventory (-e?)" in writes[1]
    assert "set outside the inventory's vars files (-e, or a vars plugin)" in writes[2]


def test_dict_settings_by_key_and_their_edits():
    sysctl = {"vm.swappiness": 10, "vm.max_map_count": 1048575, "vm.zone_reclaim_mode": 0}
    result = sections(every(cassandra_linux_sysctl=sysctl), {"group_vars/prod/main.yml": "cassandra_linux_sysctl: {}\n"})
    stock = result["stock"]
    swappiness = row(stock, "cassandra_linux_sysctl.vm.swappiness")
    assert swappiness[0] == "  cassandra_linux_sysctl.vm.swappiness   10 on all (3 nodes)"
    assert swappiness[1].startswith("      default: 1;")
    assert "vm.swappiness: 1 in cassandra_linux_sysctl in group_vars/prod/main.yml" in swappiness[2]
    # a key of the default the inventory's dict lacks: added back, or the whole dict removed
    keepalive = row(stock, "cassandra_linux_sysctl.net.ipv4.tcp_keepalive_time")
    assert keepalive[0].startswith("  cassandra_linux_sysctl.net.ipv4.tcp_keepalive_time   not set on all")
    assert "add net.ipv4.tcp_keepalive_time: 60 to cassandra_linux_sysctl in group_vars/prod/main.yml, or remove" \
           " cassandra_linux_sysctl there (its whole default back)" in keepalive[2]
    assert not any(line.startswith("  cassandra_linux_sysctl.vm.max_map_count ") for line in stock)
    extra = sections(every(cassandra_extra_settings={"a_b": 3}))
    assert row(extra["stock"], "cassandra_extra_settings")[0] == '  cassandra_extra_settings   {"a_b": 3} on all (3 nodes)'


def test_files_kept_by_hand_apart():
    values = every()
    values["db1"]["cassandra_config_keep_files"] = ["logback.xml", "cassandra-env.sh"]
    values["db2"]["cassandra_config_keep_files"] = ["logback.xml"]
    result = sections(values, {"host_vars/db1.yml": "cassandra_config_keep_files: [logback.xml]\n"})
    stock = result["stock"]
    assert not any(line.startswith("  cassandra_config_keep_files") for line in stock)
    at = stock.index(u"  KEPT BY HAND — cassandra_config_keep_files: Ansible does not manage these files")
    assert stock[at + 1] == "  cassandra-env.sh   kept on db1 (1 node)"
    assert stock[at + 2] == "  logback.xml        kept on db1, db2 (2 nodes)"
    assert result["counts"] == {"stock": 0, "between": 0, "kept": 2}


def test_secrets_never_shown():
    values = every(cassandra_server_keystore_password=SECRET, cassandra_jmx_password=SECRET,
                   cassandra_extra_settings={"ldap_password": SECRET}, cassandra_version="41x")
    values["db3"]["cassandra_server_keystore_password"] = SECRET + "-other"
    files = {"group_vars/prod/secrets.yml": "cassandra_server_keystore_password: %s\n" % SECRET}
    result = sections(values, files)
    shown = text(result)
    assert SECRET not in shown
    # two values told apart, neither shown
    assert row(result["stock"], "cassandra_server_keystore_password")[0] == \
        "  cassandra_server_keystore_password   **** on db1, db2 (2 nodes); **** (value 2) on db3 (1 node)"
    # its default (a password too: cassandra before 5.0) masked as well
    assert row(result["stock"], "cassandra_server_keystore_password")[1].startswith("      default: ****;")
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


def test_unread_vaulted_file_named():
    result = sections(every(cassandra_concurrent_reads=64), {"group_vars/prod/secrets.yml": None})
    assert "Not read (vaulted, no vault password for them): group_vars/prod/secrets.yml" in text(result)


def test_screen_width_and_default_inventory():
    result = sections(every(cassandra_concurrent_reads=64), width=80, inventory=["/x/inv"], default_inventory="/x/inv")
    assert all(len(line) <= 80 or "ansible-playbook" in line for line in result["stock"])
    assert "ansible-playbook community.cassandra.apply_config" in text(result)  # (no -i: ansible.cfg's)


# --- the values, the files ------------------------------------------------------------------------------------------

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


def test_paths_read_for_the_cluster_only():
    found = ["/i/group_vars/all.yml", "/i/group_vars/prod/main.yml", "/i/group_vars/other/main.yml",
             "/i/host_vars/db1.yml", "/i/host_vars/zz9/main.yml", "/i/prod.yml", "/i/reports/x.yml", "/j/hosts.yml",
             "/i/group_vars/prod/notes.txt"]
    assert cassandra_drift_paths(found, "/i", ["prod"], ["db1"]) == [
        "group_vars/all.yml", "group_vars/prod/main.yml", "host_vars/db1.yml", "prod.yml"]


def test_dirs_and_shared_groups(tmp_path):
    (tmp_path / "inv").mkdir()
    (tmp_path / "inv" / "prod.yml").write_text("")
    assert cassandra_drift_dirs(["inv/prod.yml", str(tmp_path / "inv")], cwd=str(tmp_path)) == [str(tmp_path / "inv")]
    groups = {"all": ["db1", "db2", "x1"], "prod": ["db1", "db2"], "dc1": ["db1", "x1"], "ungrouped": []}
    assert cassandra_drift_shared(groups, ["prod", "dc1"], ["db1", "db2"]) == ["all", "dc1"]


# --- IN SYNC ----------------------------------------------------------------------------------------------------

def test_in_sync():
    lines = cassandra_drift_sync([{"name": "db1"}, {"name": "db2", "todo": False}], [], "ansible-playbook x")
    assert lines[1:] == ["  db1, db2  in sync", "  every node matches the inventory: apply_config has nothing to apply"]


def test_the_nodes_that_differ_and_the_command():
    nodes = [{"name": "db1", "todo": True, "then": "restart", "files": ["cassandra.yaml", "jvm-server.options"]},
             {"name": "db2", "todo": True, "then": "restart", "files": ["cassandra.yaml", "jvm-server.options"]},
             {"name": "db3", "os": ["vm.swappiness"], "then": "none"},
             {"name": "db4", "todo": True, "then": "none", "medusa": [{"item": "medusa.ini"}],
              "extras": [{"section": "Packages"}], "restart": []},
             {"name": "db5"}]
    lines = cassandra_drift_sync(nodes, [], "ansible-playbook community.cassandra.apply_config --check --diff")
    assert lines == [u"IN SYNC — the nodes against the inventory (apply_config's compare, nothing changed)",
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
    # (audit) a unit apply_config writes only at the next restart: as apply_config says it, the node in sync
    nodes[1]["todo"] = True
    assert cassandra_drift_sync(nodes, [], "x")[2] == "  db2  zz-ansible-environment.conf; no restart"


def test_a_procedure_of_its_own_said_with_its_command():
    stops = ["db1: in the inventory, not in the ring: adding a node is topology's (it bootstraps it).",
             "ansible-playbook community.cassandra.topology --check"]
    lines = cassandra_drift_sync([{"name": "db2"}], stops, "x")
    assert lines[1:] == ["  db2  in sync", "  apply_config would stop before any change (a procedure of its own first):",
                         "    " + stops[0], "    " + stops[1]]
