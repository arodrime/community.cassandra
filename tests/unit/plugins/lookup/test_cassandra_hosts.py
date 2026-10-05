from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The group the operation playbooks run on without -e cassandra_hosts: the
# inventory's only cluster group, as the import writes it or by hand.

import os
import re

import pytest

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.lookup.cassandra_hosts import RUNTIME, cluster_group

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")

# all > prod > prod_dc1 > prod_dc1_r1/_r2, as the import writes it
IMPORTED = {"all": ["n1", "n2", "n3"], "ungrouped": [], "prod": ["n1", "n2", "n3"], "prod_dc1": ["n1", "n2", "n3"],
            "prod_dc1_r1": ["n1", "n2"], "prod_dc1_r2": ["n3"]}


def test_given_wins():
    assert cluster_group("other", IMPORTED) == "other"


def test_the_cassandra_group_first():
    assert cluster_group(None, dict(IMPORTED, cassandra=["n1"])) == "cassandra"


def test_imported_one_dc_one_rack():
    # cluster, dc and rack groups hold the same hosts: the cluster's name starts the others'
    groups = {"all": ["n1", "n2"], "ungrouped": [], "prod": ["n1", "n2"], "prod_dc1": ["n1", "n2"],
              "prod_dc1_r1": ["n1", "n2"]}
    assert cluster_group(None, groups) == "prod"


def test_imported_one_dc():
    assert cluster_group(None, IMPORTED) == "prod"


def test_two_dcs():
    groups = {"all": ["n1", "n2"], "prod": ["n1", "n2"], "prod_dc1": ["n1"], "prod_dc2": ["n2"]}
    assert cluster_group(None, groups) == "prod"


def test_runtime_groups_ignored():
    # group_by groups made by the playbooks hold every host at times
    groups = {"all": ["n1", "n2"], "orders": ["n1", "n2"],
              "cassandra_apply_config_True": ["n1", "n2"], "cassandra_update_java_True": ["n1", "n2"],
              "cassandra_upgrade_nodes": ["n1", "n2"], "cassandra_create_start_order": ["n1", "n2"],
              "cassandra_seed_True": ["n1", "n2"], "cassandra_target_rack_nodes": ["n1", "n2"],
              "cassandra_move_order": ["n1", "n2"], "cassandra_leaving_dc": ["n1", "n2"],
              "cassandra_move_left_going": ["n1"]}
    assert cluster_group(None, groups) == "orders"


def test_same_hosts_without_a_common_name():
    groups = {"all": ["n1"], "orders": ["n1"], "eu": ["n1"]}
    with pytest.raises(ValueError, match="top groups: eu, orders[)]"):
        cluster_group(None, groups)


@pytest.mark.parametrize("other", [
    {"monitoring": ["n1", "n2", "m1"]},             # every node and more: not a guess
    {"linux": ["n1", "n2", "b1"], "bastion": ["b1"]},
    {"seeds": ["n1"]},                              # a group of the user's, inside the cluster
    {"prod_extra": ["n1", "x9"]},                   # named like the cluster's, other hosts
])
def test_other_groups_refused(other):
    groups = dict({"all": ["n1", "n2", "m1", "b1", "x9"], "prod": ["n1", "n2"], "prod_dc1": ["n1", "n2"]}, **other)
    with pytest.raises(ValueError, match="-e cassandra_hosts=<the cluster's group>"):
        cluster_group(None, groups)


def test_several_clusters():
    groups = {"all": ["n1", "n2"], "orders": ["n1"], "orders_dc1": ["n1"], "users": ["n2"]}
    with pytest.raises(ValueError, match=r"top groups: orders, users\)"):
        cluster_group(None, groups)


def test_no_group():
    with pytest.raises(ValueError, match="no group with hosts"):
        cluster_group(None, {"all": ["n1"], "ungrouped": ["n1"], "empty": []})


def test_empty_given_is_not_set():
    assert cluster_group("", IMPORTED) == "prod"


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_lookup():
    assert render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=IMPORTED) == "prod"
    assert render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=IMPORTED,
                  cassandra_hosts="prod_dc1") == "prod_dc1"


def test_lookup_error():
    with pytest.raises(AnsibleError, match="not one cluster"):
        render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups={"a": ["n1"], "b": ["n2"]})


def test_runtime_groups_of_the_playbooks_are_known():
    """Every group a playbook or role makes while it runs is left out."""
    keys = []
    for base in ("playbooks", "roles"):
        for root, dummy, files in os.walk(os.path.join(TOP, base)):
            for name in files:
                if name.endswith(".yml") and name != "import_cluster.yml":  # runs on the given nodes
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        text = f.read()
                    keys += re.findall(r"group_by:\s*\n\s*key:\s*\"?([^\"\n]+)", text)
                    keys += re.findall(r"add_host:\s*\n(?:\s+\w+:.*\n)*?\s+groups:\s*\"?([^\"\n]+)", text)
    assert keys
    for key in keys:
        name = re.sub(r"\{\{.*?\}\}", "True", key.strip())
        assert RUNTIME.match(name), key
