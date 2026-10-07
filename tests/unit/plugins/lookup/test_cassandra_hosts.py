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

from ansible_collections.community.cassandra.plugins.lookup.cassandra_hosts import ENV, RUNTIME, cluster_group, picked

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template


@pytest.fixture(autouse=True)
def no_cluster_env(monkeypatch):
    monkeypatch.delenv("CASSANDRA_CLUSTER", raising=False)


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


# two clusters in one inventory (a shared dir), and their cassandra_cluster_name
TWO = {"all": ["a1", "a2", "b1", "m1"], "ungrouped": [], "cluster_a": ["a1", "a2"], "cluster_a_dc1": ["a1", "a2"],
       "cluster_a_dc1_r1": ["a1", "a2"], "cluster_b": ["b1"], "cluster_b_dc1": ["b1"], "monitoring": ["a1", "b1", "m1"]}
NAMES = {"a1": "Cluster A", "a2": "Cluster A", "b1": "CLUSTER_B"}


def test_env_names_the_group():
    assert picked(None, TWO, "cluster_b", NAMES.get) == ("cluster_b", "CASSANDRA_CLUSTER=cluster_b")


def test_env_names_the_cluster():
    """Its cassandra_cluster_name, exact: the largest group of its hosts, not a dc or rack group of the same hosts."""
    assert cluster_group(None, TWO, "Cluster A", NAMES.get) == "cluster_a"
    assert cluster_group(None, TWO, "CLUSTER_B", NAMES.get) == "cluster_b"
    with pytest.raises(ValueError, match="CASSANDRA_CLUSTER=cluster a: no group of that name with hosts, and no host"):
        cluster_group(None, TWO, "cluster a", NAMES.get)


def test_given_wins_over_env():
    assert picked("cluster_a", TWO, "cluster_b", NAMES.get) == ("cluster_a", "cassandra_hosts")


def test_env_unknown():
    with pytest.raises(ValueError, match=r"CASSANDRA_CLUSTER=nope: no group .* \(top groups: cluster_a, monitoring\)"):
        cluster_group(None, TWO, "nope", NAMES.get)


def test_env_cluster_name_of_several_groups():
    names = dict(NAMES, b1="Cluster A")  # two clusters with the same name
    with pytest.raises(ValueError, match=r"not one group's \(groups: cluster_a, cluster_b\): give the group instead"):
        cluster_group(None, TWO, "Cluster A", names.get)
    # its hosts in no group of their own
    with pytest.raises(ValueError, match=r"not one group's \(groups: cluster_b\)"):
        cluster_group(None, TWO, "Cluster M", {"a1": "Cluster M", "b1": "Cluster M"}.get)


def test_env_name_set_for_every_host():
    """cassandra_cluster_name in group_vars/all: the group holding every host (linux) is not a cluster's."""
    groups = {"all": ["n1", "n2", "m1"], "linux": ["n1", "n2", "m1"], "prod": ["n1", "n2"], "prod_dc1": ["n1", "n2"],
              "monitoring": ["m1"]}
    with pytest.raises(ValueError, match=r"not one group's \(groups: prod\)"):
        cluster_group(None, groups, "Prod", lambda h: "Prod")


def test_env_name_not_known_for_some_hosts():
    """A host whose name the inventory does not give may belong to it: no smaller group picked instead."""
    from ansible_collections.community.cassandra.plugins.lookup.cassandra_hosts import UNKNOWN
    groups = {"all": ["n1", "n2", "n3"], "prod": ["n1", "n2", "n3"], "prod_dc1": ["n1", "n2"],
              "prod_dc1_r1": ["n1", "n2"], "prod_dc2": ["n3"], "prod_dc2_r1": ["n3"]}
    names = {"n1": "Prod", "n2": "Prod", "n3": UNKNOWN}
    with pytest.raises(ValueError, match="not one group's"):
        cluster_group(None, groups, "Prod", names.get)
    assert cluster_group(None, groups, "Prod", dict(names, n3="Prod").get) == "prod"


def test_env_unset_or_empty():
    assert picked(None, IMPORTED, None, NAMES.get) == ("prod", "the inventory's cluster group")
    assert picked(None, IMPORTED, "", NAMES.get) == ("prod", "the inventory's cluster group")
    with pytest.raises(ValueError, match="top groups: cluster_a, monitoring"):
        cluster_group(None, TWO, None, NAMES.get)


def test_env_before_the_cassandra_group():
    assert cluster_group(None, dict(TWO, cassandra=["a1"]), "cluster_b", NAMES.get) == "cluster_b"


def test_lookup_env(monkeypatch):
    hostvars = dict((h, {"cassandra_cluster_name": n}) for h, n in NAMES.items())
    hostvars["a2"] = {"cassandra_cluster_name": trust_as_template("{{ the_name }}")}
    monkeypatch.setenv(ENV, "Cluster A")
    lookup = "{{ lookup('community.cassandra.cassandra_hosts') }}"
    assert render(lookup, groups=TWO, hostvars=hostvars, the_name="Cluster A") == "cluster_a"
    assert render("{{ lookup('community.cassandra.cassandra_hosts', explain=true) }}", groups=TWO, hostvars=hostvars,
                  the_name="Cluster A") == "cluster_a (CASSANDRA_CLUSTER=Cluster A)"
    assert render(lookup, groups=TWO, hostvars=hostvars, cassandra_hosts="cluster_b") == "cluster_b"
    monkeypatch.setenv(ENV, "nope")
    with pytest.raises(AnsibleError, match="CASSANDRA_CLUSTER=nope: no group"):
        render(lookup, groups=TWO, hostvars=hostvars)
    monkeypatch.delenv(ENV)
    assert render("{{ lookup('community.cassandra.cassandra_hosts', explain=true) }}", groups=IMPORTED) == \
        "prod (the inventory's cluster group)"


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


def test_a_cluster_named_like_a_runtime_group():
    groups = {"all": ["n1"], "cassandra_seed_cluster": ["n1"], "cassandra_seed_cluster_dc1": ["n1"]}
    assert cluster_group(None, groups) == "cassandra_seed_cluster"
