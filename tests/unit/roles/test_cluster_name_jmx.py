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


def message(found):
    with open(os.path.join(TASKS, "cluster_name.yml"), encoding="utf-8") as f:
        task = next(t for t in yaml.safe_load(f) if t["name"] == "The running node belongs to this cluster")
    assert task["vars"]["cassandra_output"] is True
    variables = dict(task["vars"], ansible_play_hosts=["n1"], cassandra_cluster_name="Demo Cluster",
                     hostvars={"n1": {"_cassandra_cluster_found": found}})
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
