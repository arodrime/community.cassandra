from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The cluster name check is the first nodetool call of the operations: a JMX
# login refused there is said as such, naming the variables, one plain verdict.

import os
import warnings

import yaml

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
    init_plugin_loader()

TASKS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_service", "tasks")


def tasks():
    with open(os.path.join(TASKS, "cluster_name.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def message(found, n1=None):
    task = next(t for t in tasks() if t["name"] == "The running node belongs to this cluster")
    assert task["vars"]["cassandra_output"] is True
    variables = dict(task["vars"], ansible_play_hosts=["n1"], cassandra_cluster_name="Demo Cluster",
                     hostvars={"n1": dict(n1 or {}, _cassandra_cluster_found=found)})
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    templar = Templar(loader=DataLoader(), variables=variables)
    return " ".join(templar.template(trust_as_template(task["ansible.builtin.assert"]["fail_msg"])).split())


def test_a_jmx_login_refused():
    found = {"from": "n1", "rc": 2, "stdout": "", "stderr": "error: Invalid username or password\n-- StackTrace --\n"
             "javax.security.auth.login.FailedLoginException: Invalid username or password\n\tat java.base/x.run(X.java:840)\n"}
    assert message(found) == (
        "JMX login refused on n1 (Invalid username or password): check cassandra_jmx_username and"
        " cassandra_jmx_password_file or cassandra_jmx_password; the cluster name is not checked against"
        " 'Demo Cluster' (cassandra_cluster_name). Nothing was changed.")


def test_another_failure_keeps_its_error_line():
    found = {"from": "n1", "rc": 1, "stdout": "", "stderr": "nodetool: Failed to connect to '127.0.0.1:7199'\n"}
    assert message(found) == (
        "nodetool describecluster failed on n1 (nodetool: Failed to connect to '127.0.0.1:7199'): the cluster name is"
        " not checked against 'Demo Cluster' (cassandra_cluster_name). Nothing was changed. Check the inventory (-i)"
        " and cassandra_cluster_name.")


REFUSED = {"from": "n1", "rc": 2, "stdout": "", "stderr": "error: Authentication failed! Error: unable to load the password file\n"}


def test_a_jmx_login_refused_with_the_password_file_missing():
    # the JVM reads it at each login: the cause said
    n1 = {"cassandra_jmx_users": [{"name": "ops"}], "cassandra_jmx_remote_password_file": "/etc/cassandra/jmx/jmxremote.password",
          "cassandra_cluster_jmx_password_file": {"changed": False, "stat": {"exists": False}}}
    assert message(REFUSED, n1).endswith(
        "Nothing was changed. /etc/cassandra/jmx/jmxremote.password is missing on n1: Cassandra reads it at each JMX login."
        " No running node of this run has it: restore it on one (or run without --limit, so that one that has it is read),"
        " then apply_config writes it back on the others, no restart.")
    # there (another password), or not looked at: as before
    for stat in ({"stat": {"exists": True}}, {"skipped": True}):
        assert message(REFUSED, dict(n1, cassandra_cluster_jmx_password_file=stat)).endswith("Nothing was changed.")


def test_the_password_file_is_looked_at_only_for_a_refused_login_of_jmx_users():
    task = next(t for t in tasks() if t["name"] == "Look for the JMX password file on the node that refused the login")
    # (delegate_to is templated before the when: never empty)
    assert task["delegate_to"] == "{{ _from or inventory_hostname }}" and task["check_mode"] is False

    def runs(found, users):
        variables = dict(task["vars"], inventory_hostname="n1", ansible_play_hosts=["n1", "n2"], _cassandra_cluster_found=found,
                         hostvars={"n1": {"cassandra_jmx_users": users}})
        variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
        templar = Templar(loader=DataLoader(), variables=variables)
        return all(templar.template(trust_as_template("{{ %s }}" % w)) in (True, "True") for w in task["when"])
    assert runs(REFUSED, [{"name": "ops"}])
    assert not runs(REFUSED, [])
    assert not runs({}, [{"name": "ops"}])
    assert not runs(dict(REFUSED, rc=0), [{"name": "ops"}])
    assert not runs(dict(REFUSED, stderr="nodetool: Failed to connect to '127.0.0.1:7199'"), [{"name": "ops"}])
