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


MATCH = task("Match the ring with the hosts")["vars"]


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def unit_env(env, unit_heap, log_dir="/var/log/cassandra", read=True):
    hv = {"import_cluster_unit": {"env": env}, "import_cluster_log_dir": log_dir}
    return render(MATCH["_unit_env"], _hv=hv, _unit_heap=unit_heap, _read=read)


def test_unit_environment_kept():
    env = {"LOCAL_JMX": "no", "MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "2G", "CASSANDRA_LOG_DIR": "/data/log"}
    # heap imported as variables, the log dir as cassandra_log_dir: the rest stays in the unit
    assert unit_env(env, {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "2G"}, "/data/log") == {"LOCAL_JMX": "no"}


def test_heap_not_imported_stays_in_the_unit():
    # e.g. 5.0 with CMS: MAX_HEAP_SIZE and HEAP_NEWSIZE only work as a pair, kept as they are
    env = {"MAX_HEAP_SIZE": "8G", "HEAP_NEWSIZE": "2G"}
    assert unit_env(env, {}) == env


def test_unit_environment_of_an_unread_node():
    assert unit_env({"LOCAL_JMX": "no"}, {}, read=False) == {}


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


@pytest.mark.parametrize("marks, keep", [
    # set up by the roles (a cluster they built): nothing left aside
    ({"own_repo": "yes", "own_os": "yes", "own_cqlsh": "yes", "own_unit": "yes"}, {}),
    # set up by hand: the roles leave each part as it is on this node
    ({}, {"cassandra_repository_manage": False, "cassandra_linux_manage": False,
          "cassandra_cqlsh_python_manage": False, "cassandra_service_unit_manage": False}),
    ({"own_os": "yes", "own_repo": "yes"}, {"cassandra_cqlsh_python_manage": False, "cassandra_service_unit_manage": False}),
])
def test_what_the_roles_leave_as_it_is(marks, keep):
    assert render(WORK_OUT["import_cluster_keep"], _kv=marks) == keep


def test_where_the_config_really_is():
    assert render(WORK_OUT["import_cluster_conf_target"], _kv={"conftarget": "/etc/cassandra/default.conf"}) \
        == "/etc/cassandra/default.conf"
    assert render(WORK_OUT["import_cluster_conf_target"], _kv={}) == ""


KEEP_UNIT = {"cassandra_service_unit_manage": False}


def unit_heap(keep):
    hv = {"import_cluster_unit": {"heap": "8G", "newsize": "2G", "cms": False}, "import_cluster_series": "41x",
          "import_cluster_config": {"vars": {}}, "import_cluster_keep": keep}
    return render(MATCH["_unit_heap"], _hv=hv, _read=True)


def test_heap_of_a_kept_unit_stays_in_it():
    # cassandra-env.sh left as it is; new nodes get it through cassandra_service_environment
    assert unit_heap(KEEP_UNIT) == {}
    assert unit_heap({}) == {"cassandra_heap_size": "8G", "cassandra_heap_newsize": "2G"}


def conf(boot, read=True, conf_dir="/etc/cassandra/conf", os_family="RedHat"):
    hv = {"import_cluster_conf_dir": conf_dir, "import_cluster_log_dir": "",
          "import_cluster_unit": {"restart": "", "boot": boot}, "ansible_facts": {"os_family": os_family}}
    defaults = render(MATCH["_default_conf_dirs"], _hv=hv)
    return render(MATCH["_conf"], _hv=hv, _read=read, _unit_heap={}, _unit_env={}, _default_conf_dirs=defaults)


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
    keep = render(MATCH["_node"]["keep"], _hv={}, _read=False)
    assert keep == {"cassandra_repository_manage": False, "cassandra_linux_manage": False,
                    "cassandra_cqlsh_python_manage": False, "cassandra_service_unit_manage": False}


@pytest.mark.parametrize("keep, env, config_vars, expected", [
    # the kept unit sets the log dir: this node's cassandra-env.sh keeps its stock fallback
    (KEEP_UNIT, {"CASSANDRA_LOG_DIR": "/data/log"}, {}, {"cassandra_log_dir": "/var/log/cassandra"}),
    (KEEP_UNIT, {"CASSANDRA_LOG_DIR": "/data/log"}, {"cassandra_log_dir": "/data/log"}, {}),
    # the role's unit: cassandra-env.sh gets the log dir
    ({}, {"CASSANDRA_LOG_DIR": "/data/log"}, {}, {}),
    (KEEP_UNIT, {}, {}, {}),
])
def test_log_dir_of_a_kept_unit(keep, env, config_vars, expected):
    hv = {"import_cluster_keep": keep, "import_cluster_unit": {"env": env}, "import_cluster_config": {"vars": config_vars}}
    assert render(MATCH["_env_log_dir"], _hv=hv, _read=True, _conf={"cassandra_log_dir": "/data/log"}) == expected


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
    assert render(MATCH["_node"]["keep"], _hv=hv, _read=True, _env_log_dir={}) == {"cassandra_medusa_link_dir": ""}


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
    assert len(used) == 9 and all(v[3:] in loaded for v in used)
    assert loaded == dict((k, v) for k, v in defaults.items() if k in loaded)
    assert loaded["cassandra_linux_timesync"] is True  # the role's, not the play's
    assert "{{" not in str(wanted).replace("{{ _d.", "").replace("{{ _cfg.", "").replace(
        "{{ 'cassandra_", "")  # nothing else read from the play's vars


@pytest.mark.parametrize("installed, kv, from_file", [
    ({"cassandra": [{"version": "5.0.7"}]}, {"pkg_query": "ok"}, True),  # no configured repository offers it
    ({"cassandra": [{"version": "5.0.7"}]}, {"pkg_query": "ok", "pkg_repo": "yes"}, False),
    ({"cassandra": [{"version": "5.0.7"}]}, {}, False),  # the query failed (e.g. timed out): no conclusion
    ({}, {}, False),  # not a package install
])
def test_package_installed_from_a_file(installed, kv, from_file):
    facts = {"packages": installed}
    assert render(WORK_OUT["import_cluster_package_from_file"], _kv=kv, ansible_facts=facts) == from_file


@pytest.mark.parametrize("from_file, read, pkg", [
    (True, True, {"cassandra_package_version": "5.0.7", "cassandra_install_method": "packages"}),
    (False, True, {"cassandra_package_version": "5.0.7"}),
    (True, False, {}),
])
def test_install_method_of_a_package_installed_from_a_file(from_file, read, pkg):
    hv = {"import_cluster_package": "5.0.7-1", "import_cluster_package_from_file": from_file}
    assert render(MATCH["_pkg"], _hv=hv, _read=read) == pkg
