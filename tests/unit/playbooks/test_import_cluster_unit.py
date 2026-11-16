from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# import_cluster: what the node's systemd unit sets (Environment=) is kept in
# cassandra_service's unit, and the JMX login goes to the inventory.

import os

import pytest
import yaml

import warnings

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

with warnings.catch_warnings():  # already done under ansible-test
    warnings.simplefilter("ignore")
    init_plugin_loader()  # the file lookup, under plain pytest too

from ansible_collections.community.cassandra.tests.unit.import_nodes import node

PLAYBOOK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "playbooks", "import_cluster.yml")

with open(PLAYBOOK, encoding="utf-8") as f:
    PLAYS = yaml.safe_load(f)


def task(name):
    todo = [t for play in PLAYS for t in play.get("tasks", [])]
    while todo:
        t = todo.pop(0)
        if t.get("name") == name:
            return t
        todo += t.get("block", []) + t.get("rescue", [])
    raise KeyError(name)


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def unit_env(env, log_dir="/var/log/cassandra", read=True):
    hv = {"import_cluster_unit": {"env": env}, "import_cluster_log_dir": log_dir}
    return node(hv, read=read)["vars"].get("cassandra_service_environment", {})


def test_unit_environment_kept():
    env = {"LOCAL_JMX": "no", "MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "2G", "CASSANDRA_LOG_DIR": "/data/log"}
    # the log dir as cassandra_log_dir: the rest stays in the unit, its heap too (cassandra-env.sh
    # stays as it is: moving the heap there would rewrite it and the unit)
    assert unit_env(env, "/data/log") == {"LOCAL_JMX": "no", "MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "2G"}


def test_unit_environment_of_an_unread_node():
    assert unit_env({"LOCAL_JMX": "no"}, read=False) == {}


WRITE = next(play for play in PLAYS if play["name"] == "Write the inventory")["vars"]


@pytest.mark.parametrize("given_vars, jmx", [
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/jmx.pw"},
     {"cassandra_jmx_username": "ops", "cassandra_jmx_password_file": "/etc/cassandra/jmx.pw"}),
    ({"cassandra_jmx_username": "ops", "cassandra_jmx_password": "p", "cassandra_jmx_password_file": ""},
     {"cassandra_jmx_username": "ops", "cassandra_jmx_password": "p"}),
    ({}, {}),
])
def test_jmx_login_from_the_given_node(given_vars, jmx):
    # -e or the given node's own inventory: read on it, not on localhost
    hostvars = {"node1": given_vars}
    assert render(WRITE["_jmx"], _given=["node1"], hostvars=hostvars) == jmx


WORK_OUT = task("Work out the series and the conf dir")["ansible.builtin.set_fact"]
WORK_OUT_VARS = task("Work out the series and the conf dir")["vars"]


@pytest.mark.parametrize("marks", [
    {"own_repo": "yes", "own_os": "yes", "own_cqlsh": "yes", "own_unit": "yes"}, {}, {"own_os": "yes", "own_repo": "yes"}])
def test_what_the_roles_leave_as_it_is(marks):
    # rc10: nothing left aside by the import itself (the parts are imported, the self-check stops on what cannot be)
    assert render(str(WORK_OUT["import_cluster_keep"]), _kv=marks) in ({}, "{}")
    # whether cassandra_linux set it up (its limits file's mark): how its OS settings are imported
    assert render(WORK_OUT["import_cluster_own_os"], _kv=marks) in (("own_os" in marks), str("own_os" in marks))
    assert render(WORK_OUT["import_cluster_own_cqlsh"], _kv=marks) in (("own_cqlsh" in marks), str("own_cqlsh" in marks))


def test_where_the_config_really_is():
    assert render(WORK_OUT["import_cluster_conf_target"], _kv={"conftarget": "/etc/cassandra/default.conf"}) \
        == "/etc/cassandra/default.conf"
    assert render(WORK_OUT["import_cluster_conf_target"], _kv={}) == ""


KEEP_UNIT = {"cassandra_service_unit_manage": False}


CONF = ("cassandra_conf_dir", "cassandra_log_dir", "cassandra_service_restart", "cassandra_service_enabled",
        "cassandra_service_environment")


def conf(boot, read=True, conf_dir="/etc/cassandra/conf", os_family="RedHat"):
    hv = {"import_cluster_conf_dir": conf_dir, "import_cluster_log_dir": "",
          "import_cluster_unit": {"restart": "", "boot": boot}, "ansible_facts": {"os_family": os_family}}
    return dict((k, v) for k, v in node(hv, read=read)["vars"].items() if k in CONF)


def test_boot_setting_imported():
    assert conf("disabled") == {"cassandra_service_enabled": False}
    assert conf("enabled") == {}


@pytest.mark.parametrize("conf_dir, os_family, imported", [
    ("/etc/cassandra/conf", "RedHat", False),
    ("/etc/cassandra", "Debian", False),
    # the other family's dir: not where cassandra_config writes by default
    ("/etc/cassandra/conf", "Debian", True),
    ("/etc/cassandra", "RedHat", True),
    ("/etc/cassandra", "", False),  # no facts: either
])
def test_conf_dir_imported_when_not_the_role_default(conf_dir, os_family, imported):
    assert ("cassandra_conf_dir" in conf("enabled", conf_dir=conf_dir, os_family=os_family)) == imported


def test_node_not_read_is_left_as_it_is():
    keep = node({}, read=False)["keep"]
    assert keep == {"cassandra_repository_manage": False, "cassandra_linux_manage": False,
                    "cassandra_cqlsh_python_manage": False, "cassandra_service_unit_manage": False,
                    "cassandra_java_set_default": False, "cassandra_firewall_manage": False,
                    "cassandra_install_tools": False, "cassandra_install_jemalloc": False, "cassandra_package_hold": False,
                    "cassandra_dsbulk_install": False}


@pytest.mark.parametrize("keep, env, config_vars, expected", [
    # the kept unit sets the log dir: this node's cassandra-env.sh keeps its stock fallback
    (KEEP_UNIT, {"CASSANDRA_LOG_DIR": "/data/log"}, {}, {"cassandra_log_dir": "/var/log/cassandra"}),
    (KEEP_UNIT, {"CASSANDRA_LOG_DIR": "/data/log"}, {"cassandra_log_dir": "/data/log"}, {}),
    # the role's unit: cassandra-env.sh gets the log dir
    ({}, {"CASSANDRA_LOG_DIR": "/data/log"}, {}, {}),
    (KEEP_UNIT, {}, {}, {}),
])
def test_log_dir_of_a_kept_unit(keep, env, config_vars, expected):
    hv = {"import_cluster_keep": keep, "import_cluster_unit": {"env": env}, "import_cluster_config": {"vars": config_vars},
          "import_cluster_log_dir": "/data/log"}
    kept = dict((k, v) for k, v in node(hv)["keep"].items() if k == "cassandra_log_dir")
    assert kept == expected


@pytest.mark.parametrize("keep_hand_edits, expected", [
    # the unit kept by name: its CASSANDRA_LOG_DIR still wins, cassandra-env.sh keeps its stock fallback
    # (hostile/import-noop: the role's cassandra-env.sh would get the unit's directory, a self-check stop)
    ("unit", {"cassandra_log_dir": "/var/log/cassandra"}),
    ("cqlsh,unit", {"cassandra_log_dir": "/var/log/cassandra"}),
    ("cqlsh", {}),
    (False, {}),
])
def test_log_dir_of_a_unit_kept_by_name(keep_hand_edits, expected):
    hv = {"import_cluster_unit": {"env": {"CASSANDRA_LOG_DIR": "/data/log"}}, "import_cluster_log_dir": "/data/log"}
    kept = dict((k, v) for k, v in node(hv, keep_hand_edits=keep_hand_edits)["keep"].items()
                if k == "cassandra_log_dir")
    assert kept == expected


@pytest.mark.parametrize("kv, boot", [
    ({"unit_UnitFileState": "enabled", "unit_IsEnabled": "enabled"}, "enabled"),
    # the package's init script: systemd only has a generated unit, the init system answers
    ({"unit_UnitFileState": "generated", "unit_IsEnabled": "disabled"}, "disabled"),
    ({"unit_UnitFileState": "", "unit_IsEnabled": ""}, ""),
])
def test_boot_setting_read(kv, boot):
    assert render(WORK_OUT["import_cluster_unit"]["boot"], _kv=kv) == boot


def test_medusa_kept_on_the_node():
    hv = {"import_cluster_keep": {}, "import_cluster_medusa": {"keep": {"cassandra_medusa_link_dir": ""}}}
    assert node(hv)["keep"] == {"cassandra_medusa_link_dir": "", "cassandra_dsbulk_install": False}


def test_disk_tuning_kept_on_the_node():
    hv = {"import_cluster_keep": {}, "import_cluster_os": {"keep": {"cassandra_data_disk_tuning": False}}}
    assert node(hv)["keep"]["cassandra_data_disk_tuning"] is False


def test_os_baseline_is_the_roles_defaults_not_the_inventory():
    """The OS tuning is compared with the roles' own defaults (read from their files): a value an
    inventory already sets must still be carried when it is not the default."""
    load = task("Load the cassandra_linux and cassandra_service defaults (what they would set)")
    wanted = task("Compare it with what the roles would set")["vars"]["_wanted"]
    used = [v for v in yaml.safe_dump(wanted).split() if v.startswith("_d.")]
    roles = os.path.join(os.path.dirname(PLAYBOOK), "..", "roles")
    defaults = {}
    for role in ("cassandra_linux", "cassandra_service"):
        with open(os.path.join(roles, role, "defaults", "main.yml"), encoding="utf-8") as f:
            defaults.update(yaml.safe_load(f))
    templar = Templar(loader=DataLoader(), variables={"playbook_dir": os.path.dirname(PLAYBOOK),
                                                      "cassandra_linux_timesync": False})
    loaded = templar.template(trust_as_template(load["ansible.builtin.set_fact"]["import_cluster_os_defaults"]))
    assert len(used) == 13 and all(v[3:] in loaded for v in used)
    assert loaded == dict((k, v) for k, v in defaults.items() if k in loaded)
    assert loaded["cassandra_linux_timesync"] is True  # the role's, not the play's
    assert "{{" not in str(wanted).replace("{{ _d.", "").replace("{{ _cfg.", "").replace(
        "{{ 'cassandra_", "").replace("{{ ansible_facts['os_family']", "").replace(
        "{{ import_cluster_own_os", "").replace("{{ import_cluster_own_unit", "")  # nothing else read from the play's vars (the node's own facts aside)


@pytest.mark.parametrize("kv, from_file", [
    ({"installed_cassandra": "5.0.7", "pkg_query": "ok"}, True),  # no configured repository offers it
    ({"installed_cassandra": "5.0.7", "pkg_query": "ok", "pkg_repo": "yes"}, False),
    ({"installed_cassandra": "5.0.7"}, False),  # the query failed (e.g. timed out): no conclusion
    ({"pkg_query": "ok"}, False),  # not a package install
])
def test_package_installed_from_a_file(kv, from_file):
    installed = render(WORK_OUT_VARS["_installed"], _kv=kv, _pairs=render(WORK_OUT_VARS["_pairs"], _kv=kv))
    assert render(WORK_OUT["import_cluster_package_from_file"], _kv=kv, _installed=installed) == from_file
    assert render(WORK_OUT["import_cluster_package"], _installed=installed) == kv.get("installed_cassandra", "")


def test_packages_read_as_package_facts_gives_them():
    # the version of the installed ones (rpm's VERSION, dpkg's Version), each name the import looks at
    kv = {"installed_cassandra": "5.0.7", "installed_libjemalloc2": "5.3.0-2", "pkg_query": "ok", "version": "5.0.7"}
    pairs = render(WORK_OUT_VARS["_pairs"], _kv=kv)
    assert render(WORK_OUT_VARS["_installed"], _pairs=pairs) == {"cassandra": "5.0.7", "libjemalloc2": "5.3.0-2"}
    script = task("Read the running Cassandra (conf dir, Cassandra and Java versions)")
    assert "rpm -q --qf '%{VERSION}\\n'" in script["ansible.builtin.shell"]
    assert "${db:Status-Status} ${Version}" in script["ansible.builtin.shell"]
    for name in ("cassandra", "cassandra-tools", "jemalloc", "libjemalloc2"):
        assert name in script["environment"]["IMPORT_CLUSTER_PACKAGES"]
    assert "debian_packages" in script["environment"]["IMPORT_CLUSTER_PACKAGES"]


@pytest.mark.parametrize("from_file, read, pkg", [
    (True, True, {"cassandra_package_version": "5.0.7", "cassandra_install_method": "packages"}),
    (False, True, {"cassandra_package_version": "5.0.7"}),
    (True, False, {}),
])
def test_install_method_of_a_package_installed_from_a_file(from_file, read, pkg):
    hv = {"import_cluster_package": "5.0.7-1", "import_cluster_package_from_file": from_file}
    out = node(hv, read=read)["vars"]
    assert dict((k, v) for k, v in out.items() if k in ("cassandra_package_version", "cassandra_install_method")) == pkg


# --- rc10: the package's init script as installed is a start method (cassandra_service_start) ---

SYSV = {"file": "/etc/rc.d/init.d/cassandra", "script": "/etc/rc.d/init.d/cassandra", "restart": "no", "boot": "enabled"}


def test_package_script_as_installed_is_imported_as_the_start_method():
    n = node({"import_cluster_unit": dict(SYSV, pkg_script="cassandra")})
    assert n["vars"]["cassandra_service_start"] == "package_script"
    assert "cassandra_service_unit_manage" not in n["keep"]
    assert ("the init script of package cassandra, as installed: cassandra_service_start: package_script, new nodes"
            " too") in n["notes"][1]
    # modified since installed, or a script of no package: no start method imported, nothing kept by itself (the
    # self-check compares, and stops with the way out)
    for state in ("modified", ""):
        n = node({"import_cluster_unit": dict(SYSV, pkg_script=state)})
        assert "cassandra_service_start" not in n["vars"] and "cassandra_service_unit_manage" not in n["keep"]


@pytest.mark.parametrize("hv", [
    {"import_cluster_unit": dict(SYSV, pkg_script="cassandra")},  # the package's script, the unit kept by name
    {"import_cluster_unit": {"file": "/etc/systemd/system/cassandra.service", "restart": "always", "boot": "enabled"}},
])
def test_started_note_of_a_unit_kept_by_name(hv):
    # no start method imported (cassandra_service_unit_manage: false): new nodes get the role's unit
    n = node(hv, keep_hand_edits="unit")
    assert "cassandra_service_start" not in n["vars"] and n["keep"]["cassandra_service_unit_manage"] is False
    assert "package_script" not in n["notes"][1]
    assert "(kept on this node; new nodes get the unit of cassandra_service" in n["notes"][1]


@pytest.mark.parametrize("hv, keep_hand_edits", [
    ({"import_cluster_unit": {"env": {"CASSANDRA_LOG_DIR": "/data/log", "LOCAL_JMX": "no"}}}, "unit"),
    ({"import_cluster_unit": dict(SYSV, pkg_script="cassandra",
                                  env={"CASSANDRA_LOG_DIR": "/data/log", "LOCAL_JMX": "no"})}, False),
])
def test_log_dir_of_a_kept_unit_carried_for_the_role_unit(hv, keep_hand_edits):
    # cassandra-env.sh keeps its fallback (host_vars): the role's unit (a node replaced once the unit lines are removed,
    # or started by unit) gets the unit's CASSANDRA_LOG_DIR and logs where its peers do
    n = node(dict(hv, import_cluster_log_dir="/data/log"), keep_hand_edits=keep_hand_edits)
    assert n["keep"]["cassandra_log_dir"] == "/var/log/cassandra"
    assert n["vars"]["cassandra_service_environment"] == {"CASSANDRA_LOG_DIR": "/data/log", "LOCAL_JMX": "no"}
    assert "its Environment CASSANDRA_LOG_DIR, LOCAL_JMX as cassandra_service_environment" in n["notes"][1]


@pytest.mark.parametrize("dropins, imported", [
    # a drop-in of its own: what it sets would not hold for a node added later, the self-check compares instead
    ("/etc/systemd/system/cassandra.service.d/override.conf", False),
    ("/run/systemd/generator/cassandra.service.d/x.conf /etc/systemd/system/cassandra.service.d/java.conf", False),
    # the distribution's drop-ins for every service are not the node's own
    ("/usr/lib/systemd/system/service.d/10-timeout-abort.conf", True),
    ("", True)])
def test_package_script_with_drop_ins_of_its_own_is_not_imported(dropins, imported):
    n = node({"import_cluster_unit": dict(SYSV, pkg_script="cassandra", dropins=dropins)})
    assert ("cassandra_service_start" in n["vars"]) is imported


def test_unit_facts_read_the_script_and_its_package():
    unit = WORK_OUT["import_cluster_unit"]
    kv = {"unit_SourcePath": "/etc/rc.d/init.d/cassandra", "unit_FragmentPath": "/run/systemd/generator.late/cassandra.service",
          "pkg_script": "cassandra"}
    assert render(str(unit["script"]), _kv=kv) == "/etc/rc.d/init.d/cassandra"
    assert render(str(unit["pkg_script"]), _kv=kv) == "cassandra"
    assert render(str(unit["pkg_script"]), _kv={}) == ""


def test_read_script_verifies_the_init_script():
    script = task("Read the running Cassandra (conf dir, Cassandra and Java versions)")["ansible.builtin.shell"]
    assert 'rpm -V' in script and 'dpkg --verify' in script and 'echo "pkg_script=modified"' in script


def test_cqlsh_of_the_inventory_is_external():
    assert node({"import_cluster_cqlsh_command": "/opt/cqlsh/bin/cqlsh"})["vars"]["cassandra_cqlsh_source"] == "external"
    assert "cassandra_cqlsh_source" not in node({"import_cluster_cqlsh_command": "cqlsh"})["vars"]
    # set up by the roles already: the package's
    assert "cassandra_cqlsh_source" not in node({"import_cluster_cqlsh_command": "/opt/c", "import_cluster_own_cqlsh": True})["vars"]


def test_named_cqlsh_written_with_its_source():
    # (given by -e or the inventory: written with it, so the next runs do not stop on a source without its command)
    assert node({"import_cluster_cqlsh_command": "/opt/cqlsh/bin/cqlsh"})["vars"]["cassandra_cqlsh_command"] == \
        "/opt/cqlsh/bin/cqlsh"
