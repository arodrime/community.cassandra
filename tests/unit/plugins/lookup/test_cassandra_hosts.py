from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The group the operation playbooks run on: -e cassandra_hosts, or the one
# CASSANDRA_CLUSTER names (both set: the same one), else the inventory's only
# cluster group, as the import writes it or by hand; no group name by default.

import os
import re

import pytest

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.lookup.cassandra_hosts import (
    BY_ENV, BY_GIVEN, BY_ONLY, ENV, RUNTIME, UNKNOWN, banner, cluster_group, picked)

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
    assert cluster_group("prod_dc1", IMPORTED) == "prod_dc1"


def test_given_unknown_fails():
    """A typo matches no host: under --limit the localhost check is skipped, every play would be too."""
    with pytest.raises(ValueError, match=r"^REFUSED  cassandra_hosts=prdo: no group of that name in the inventory\n\n"
                       r"Its groups: prod, prod_dc1, prod_dc1_r1, prod_dc1_r2\n"
                       r"Name the cluster's group: -e cassandra_hosts=<group>\nNothing was changed\.$"):
        cluster_group("prdo", IMPORTED)
    with pytest.raises(ValueError, match="cassandra_hosts=ungrouped: that group has no host"):
        cluster_group("ungrouped", IMPORTED)
    with pytest.raises(ValueError, match="cassandra_hosts=prdo: no group of that name"):
        picked("prdo", TWO, "cluster_b", NAMES.get)  # before CASSANDRA_CLUSTER, like a good one


@pytest.mark.parametrize("groups", [
    dict(IMPORTED, cassandra=["n1"]),  # next to a cluster
    {"all": ["n1", "n2"], "cassandra": ["n1", "n2"], "dc1": ["n1"], "dc2": ["n2"]},  # hand-made, dc groups of other names
    {"all": ["n1", "n2"], "cassandra": ["n1", "n2"], "monitoring": ["n1", "n2", "m1"]},
])
def test_the_cassandra_group_is_not_picked(groups):
    """No group name is a default: a group cassandra is a group as any other."""
    with pytest.raises(ValueError, match="^REFUSED  no cluster named"):
        cluster_group(None, groups)


def test_the_cassandra_group_named():
    """The way to adapt: name it."""
    groups = {"all": ["n1", "n2"], "cassandra": ["n1", "n2"], "dc1": ["n1"], "dc2": ["n2"]}
    with pytest.raises(ValueError, match=r"holds no cluster laid out as the import writes it .*; groups: cassandra, dc1, dc2\)\n\n"
                                         r"Name the one to run on:\n(.*\n){2}A group cassandra is no longer taken by default: name it as"
                                         r" any other\.\nNothing was changed\.$"):
        cluster_group(None, groups)
    assert picked("cassandra", groups) == ("cassandra", BY_GIVEN)
    assert picked(None, groups, "cassandra") == ("cassandra", BY_ENV)


@pytest.mark.parametrize("name", ["cassandra", "linux", "prod"])
def test_a_lone_group_is_no_cluster(name):
    """The only cluster has a cluster's shape (<group> with its <group>_<dc> groups): a lone group, whatever its
    name, is not taken."""
    with pytest.raises(ValueError, match=r"holds no cluster laid out as the import writes it .*; groups: %s\)" % name):
        picked(None, {"all": ["n1", "n2"], "ungrouped": [], name: ["n1", "n2"]})


def test_the_cassandra_group_laid_out_as_a_cluster():
    """Named cassandra, laid out as the import writes it: the only cluster, as any other name."""
    assert picked(None, {"all": ["n1"], "cassandra": ["n1"], "cassandra_dc1": ["n1"]}) == ("cassandra", BY_ONLY)


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
    groups = {"all": ["n1", "n2"], "orders": ["n1", "n2"], "orders_dc1": ["n1", "n2"],
              "cassandra_seed_True": ["n1", "n2"], "cassandra_seed_False": ["n1"], "cassandra_create_start_order": ["n1", "n2"],
              "cassandra_target_rack_nodes": ["n1", "n2"]}
    assert cluster_group(None, groups) == "orders"


def test_same_hosts_without_a_common_name():
    groups = {"all": ["n1"], "orders": ["n1"], "eu": ["n1"]}
    with pytest.raises(ValueError, match="holds no cluster laid out .*; groups: eu, orders[)]"):
        cluster_group(None, groups)


@pytest.mark.parametrize("other", [
    {"monitoring": ["n1", "n2", "m1"]},             # every node and more: not a guess
    {"linux": ["n1", "n2", "b1"], "bastion": ["b1"]},
    {"seeds": ["n1"]},                              # a group of the user's, inside the cluster
    {"prod_extra": ["n1", "x9"]},                   # named like the cluster's, other hosts
])
def test_other_groups_refused(other):
    """No group named in the message either: monitoring, the top group, holds every node and more."""
    groups = dict({"all": ["n1", "n2", "m1", "b1", "x9"], "prod": ["n1", "n2"], "prod_dc1": ["n1", "n2"]}, **other)
    with pytest.raises(ValueError, match="^REFUSED  no cluster named: .*\n\nName the one to run on:\n"):
        cluster_group(None, groups)


def test_several_clusters():
    """The clusters found named (laid out as a cluster's: not their dc groups), the other groups apart."""
    groups = {"all": ["n1", "n2", "m1"], "orders": ["n1"], "orders_dc1": ["n1"], "users": ["n2"], "users_dc1": ["n2"],
              "monitoring": ["n1", "n2", "m1"]}
    with pytest.raises(ValueError, match=r"^REFUSED  no cluster named: this inventory has 2 clusters: orders, users"
                                         r" \(other groups: monitoring\)\n\nName the one to run on:\n  export CASSANDRA_CLUSTER=<cluster>\n"):
        cluster_group(None, groups)
    # a group of its own next to a cluster: not named a cluster
    groups = {"all": ["n1", "n2"], "orders": ["n1"], "orders_dc1": ["n1"], "users": ["n2"]}
    with pytest.raises(ValueError, match=r"is not the cluster orders alone \(other groups: users\)\n"):
        cluster_group(None, groups)


def test_clusters_named_like_each_other():
    """prod and prod_eu, two clusters of a shared dir: both named."""
    groups = {"all": ["a", "b"], "prod": ["a"], "prod_dc1": ["a"], "prod_eu": ["b"], "prod_eu_dc1": ["b"]}
    with pytest.raises(ValueError, match="2 clusters: prod, prod_eu\n"):
        cluster_group(None, groups)


def test_a_group_holding_every_node_is_not_named_a_cluster():
    """monitoring holds both clusters: listed apart, never as a cluster to pick."""
    with pytest.raises(ValueError, match=r"2 clusters: cluster_a, cluster_b \(other groups: monitoring\)"):
        cluster_group(None, dict(TWO, monitoring=["a1", "a2", "b1", "m1"]))


def test_no_group():
    with pytest.raises(ValueError, match="^REFUSED  no cluster: the inventory has no group with hosts\n"):
        cluster_group(None, {"all": ["n1"], "ungrouped": ["n1"], "empty": []})


def test_empty_given_is_not_set():
    assert cluster_group("", IMPORTED) == "prod"


def render(template, **variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


def test_lookup():
    assert render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=IMPORTED) == "prod"
    assert render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=IMPORTED,
                  cassandra_hosts="prod_dc1") == "prod_dc1"
    assert render("{{ lookup('community.cassandra.cassandra_hosts', explain=true) }}", groups=IMPORTED,
                  cassandra_hosts="prod") == "prod (-e cassandra_hosts)"


# two clusters in one inventory (a shared dir), and their cassandra_cluster_name
TWO = {"all": ["a1", "a2", "b1", "m1"], "ungrouped": [], "cluster_a": ["a1", "a2"], "cluster_a_dc1": ["a1", "a2"],
       "cluster_a_dc1_r1": ["a1", "a2"], "cluster_b": ["b1"], "cluster_b_dc1": ["b1"], "monitoring": ["a1", "b1", "m1"]}
NAMES = {"a1": "Cluster A", "a2": "Cluster A", "b1": "CLUSTER_B"}


def test_env_names_the_group():
    assert picked(None, TWO, "cluster_b", NAMES.get) == ("cluster_b", "CASSANDRA_CLUSTER (environment)")


def test_env_names_the_cluster():
    """Its cassandra_cluster_name, exact: the largest group of its hosts, not a dc or rack group of the same hosts."""
    assert cluster_group(None, TWO, "Cluster A", NAMES.get) == "cluster_a"
    assert cluster_group(None, TWO, "CLUSTER_B", NAMES.get) == "cluster_b"
    with pytest.raises(ValueError, match="CASSANDRA_CLUSTER=cluster a: no group of that name with hosts, and no host"):
        cluster_group(None, TWO, "cluster a", NAMES.get)


def test_given_and_env_that_agree():
    """The same group, or the env the cluster name of the group given."""
    assert picked("cluster_a", TWO, "cluster_a", NAMES.get) == ("cluster_a", BY_GIVEN)
    assert picked("cluster_a", TWO, "Cluster A", NAMES.get) == ("cluster_a", BY_GIVEN)


@pytest.mark.parametrize("env, said", [
    ("cluster_b", "it names the group cluster_b"),
    ("CLUSTER_B", "it is the cluster of the group cluster_b"),
    ("nope", "it names no cluster of the inventory (CASSANDRA_CLUSTER=nope: no group of that name"),
])
def test_given_and_env_that_disagree(env, said):
    """A shell left with another cluster's CASSANDRA_CLUSTER: refused, naming both and where each comes from."""
    with pytest.raises(ValueError) as err:
        picked("cluster_a", TWO, env, NAMES.get)
    msg = str(err.value)
    assert msg.startswith("REFUSED  -e cassandra_hosts=cluster_a and CASSANDRA_CLUSTER=%s (from your shell) disagree: %s" % (env, said))
    assert msg.endswith("Name one: unset CASSANDRA_CLUSTER, or run without -e cassandra_hosts.\nNothing was changed.")


def test_given_and_env_a_dc_group():
    """A datacenter group of the cluster the env names is another group: refused too."""
    with pytest.raises(ValueError, match="disagree: it names the group cluster_a"):
        picked("cluster_a_dc1", TWO, "cluster_a", NAMES.get)


def test_env_unknown():
    with pytest.raises(ValueError, match=r"^REFUSED  CASSANDRA_CLUSTER=nope: no group .*\n\nTop groups of this inventory: cluster_a, monitoring\.\n"):
        cluster_group(None, TWO, "nope", NAMES.get)


def test_env_cluster_name_of_several_groups():
    names = dict(NAMES, b1="Cluster A")  # two clusters with the same name
    with pytest.raises(ValueError, match=r"not one group's \(groups: cluster_a, cluster_b\)\n\nName the group instead:\n"):
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
    assert picked(None, IMPORTED, None, NAMES.get) == ("prod", "the only cluster in the inventory")
    assert picked(None, IMPORTED, "", NAMES.get) == ("prod", "the only cluster in the inventory")
    with pytest.raises(ValueError, match="2 clusters: cluster_a, cluster_b .other groups: monitoring"):
        cluster_group(None, TWO, None, NAMES.get)


def test_env_next_to_a_cassandra_group():
    assert cluster_group(None, dict(TWO, cassandra=["a1"]), "cluster_b", NAMES.get) == "cluster_b"


def test_env_cluster_name_of_a_cassandra_group_alone():
    """A cluster name stands for a group laid out as a cluster's (<group>_... groups): a group cassandra without any
    is no exception any more, CASSANDRA_CLUSTER=cassandra names it."""
    groups = {"all": ["n1", "n2", "m1"], "cassandra": ["n1", "n2"], "dc1": ["n1", "n2"], "monitoring": ["n1", "m1"]}
    with pytest.raises(ValueError, match="not one group's"):
        cluster_group(None, groups, "Prod", {"n1": "Prod", "n2": "Prod"}.get)
    assert cluster_group(None, groups, "cassandra", {"n1": "Prod", "n2": "Prod"}.get) == "cassandra"


def test_lookup_env(monkeypatch):
    hostvars = dict((h, {"cassandra_cluster_name": n}) for h, n in NAMES.items())
    hostvars["a2"] = {"cassandra_cluster_name": trust_as_template("{{ the_name }}")}
    monkeypatch.setenv(ENV, "Cluster A")
    lookup = "{{ lookup('community.cassandra.cassandra_hosts') }}"
    assert render(lookup, groups=TWO, hostvars=hostvars, the_name="Cluster A") == "cluster_a"
    assert render("{{ lookup('community.cassandra.cassandra_hosts', explain=true) }}", groups=TWO, hostvars=hostvars,
                  the_name="Cluster A") == "cluster_a (CASSANDRA_CLUSTER (environment))"
    assert render(lookup, groups=TWO, hostvars=hostvars, the_name="Cluster A", cassandra_hosts="cluster_a") == "cluster_a"
    with pytest.raises(AnsibleError, match="-e cassandra_hosts=cluster_b and CASSANDRA_CLUSTER=Cluster A .from your shell. disagree"):
        render(lookup, groups=TWO, hostvars=hostvars, the_name="Cluster A", cassandra_hosts="cluster_b")
    monkeypatch.setenv(ENV, "nope")
    with pytest.raises(AnsibleError, match="CASSANDRA_CLUSTER=nope: no group"):
        render(lookup, groups=TWO, hostvars=hostvars)
    monkeypatch.delenv(ENV)
    assert render("{{ lookup('community.cassandra.cassandra_hosts', explain=true) }}", groups=IMPORTED) == \
        "prod (the only cluster in the inventory)"


def test_lookup_error():
    with pytest.raises(AnsibleError, match="holds no cluster laid out .*; groups: a, b[)]"):
        render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups={"a": ["n1"], "b": ["n2"]})
    with pytest.raises(AnsibleError, match="cassandra_hosts=prdo: no group of that name in the inventory"):
        render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=IMPORTED, cassandra_hosts="prdo")


TWO_CLUSTERS = {"all": ["o1", "b1"], "ungrouped": [], "orders": ["o1"], "orders_dc1": ["o1"], "orders_dc1_r1": ["o1"],
                "billing": ["b1"], "billing_dc1": ["b1"]}
WAY_OUT = ("\nName the one to run on:\n  export CASSANDRA_CLUSTER=<cluster>\n"
           "or add -e cassandra_hosts=<cluster> to the command.\nNothing was changed.")


@pytest.mark.parametrize("variables,env,refusal", [
    ({}, None, "REFUSED  no cluster named: this inventory has 2 clusters: billing, orders\n" + WAY_OUT),
    ({"cassandra_hosts": ""}, None,
     "REFUSED  no cluster named (-e cassandra_hosts is empty): this inventory has 2 clusters: billing, orders\n" + WAY_OUT),
    ({"cassandra_hosts": "ordres"}, None,
     "REFUSED  cassandra_hosts=ordres: no group of that name in the inventory\n\n"
     "Its groups: billing, billing_dc1, orders, orders_dc1, orders_dc1_r1\n"
     "Name the cluster's group: -e cassandra_hosts=<group>\nNothing was changed."),
    ({"cassandra_hosts": ["orders"]}, None,
     "REFUSED  cassandra_hosts must be a group name, not ['orders']\n\nName the cluster's group: -e cassandra_hosts=<group>\n"
     "Nothing was changed."),
    ({}, "ordres",
     "REFUSED  CASSANDRA_CLUSTER=ordres: no group of that name with hosts, and no host with that cassandra_cluster_name\n"
     "\nThis inventory has 2 clusters: billing, orders." + WAY_OUT),
    ({"hostvars": {"o1": {"cassandra_cluster_name": "Shop"}, "b1": {"cassandra_cluster_name": "Shop"}}}, "Shop",
     "REFUSED  CASSANDRA_CLUSTER=Shop: the hosts with that cassandra_cluster_name are not one group's"
     " (groups: billing, orders)\n\nName the group instead:\n  export CASSANDRA_CLUSTER=<group>\n"
     "or add -e cassandra_hosts=<group> to the command.\nNothing was changed."),
])
def test_refusal_returned_not_raised(monkeypatch, variables, env, refusal):
    """refusal=true: the block the lookup fails with, as its value (the localhost check stops on it)."""
    if env:
        monkeypatch.setenv(ENV, env)
    variables = dict(variables, groups=TWO_CLUSTERS)
    assert render("{{ lookup('community.cassandra.cassandra_hosts', refusal=true) }}", **variables) == refusal
    with pytest.raises(AnsibleError, match=re.escape(refusal.split("\n")[0])):
        render("{{ lookup('community.cassandra.cassandra_hosts') }}", **variables)


def test_no_refusal_is_empty():
    assert render("{{ lookup('community.cassandra.cassandra_hosts', refusal=true) }}", groups=IMPORTED) == ""
    assert render("{{ lookup('community.cassandra.cassandra_hosts', refusal=true) }}", groups=TWO_CLUSTERS,
                  cassandra_hosts="orders") == ""


def test_runtime_groups_of_the_playbooks_are_known():
    """Every group a playbook or role makes while it runs is left out."""
    keys = []
    for base in ("playbooks", "roles"):
        for root, dummy, files in os.walk(os.path.join(TOP, base)):
            for name in files:
                if name.endswith(".yml"):
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


def test_lookup_refusal(monkeypatch):
    """refusal=true: the reason, '' when the lookup would not fail (hosts_check.yml's verdict)."""
    lookup = "{{ lookup('community.cassandra.cassandra_hosts', refusal=true) }}"
    assert render(lookup, groups=IMPORTED) == ""
    assert render(lookup, groups=TWO).startswith("REFUSED  no cluster named: this inventory has 2 clusters: cluster_a, cluster_b")
    assert render(lookup, groups=IMPORTED, cassandra_hosts="prdo").startswith("REFUSED  cassandra_hosts=prdo: no group of that name")
    monkeypatch.setenv(ENV, "cluster_b")
    assert render(lookup, groups=TWO, cassandra_hosts="cluster_b") == ""
    assert render(lookup, groups=TWO, cassandra_hosts="cluster_a").startswith(
        "REFUSED  -e cassandra_hosts=cluster_a and CASSANDRA_CLUSTER=cluster_b (from your shell) disagree")


def test_banner():
    assert banner("prod", BY_GIVEN, 5, ["dc1"] * 5) == "CLUSTER prod (5 nodes, dc1) | chosen by: -e cassandra_hosts"
    assert banner("prod", BY_ONLY, 1, ["dc2", "dc1"], absent=1) == \
        "CLUSTER prod (1 node + 1 marked absent, dc1, dc2) | chosen by: the only cluster in the inventory"
    assert banner("prod", BY_ENV, 3, []) == "CLUSTER prod (3 nodes) | chosen by: CASSANDRA_CLUSTER (environment)"


def test_lookup_banner(monkeypatch):
    """The group's nodes without the ones marked absent, their datacenters (the role's dc1 when the inventory sets
    none; a fact of the node is not shown), how the group was picked."""
    hostvars = {"n1": {"cassandra_dc": "dc1"}, "n2": {"cassandra_dc": trust_as_template("{{ the_dc }}")},
                "n3": {"cassandra_dc": "dc2", "cassandra_node_state": "absent"}}
    groups = {"all": ["n1", "n2", "n3"], "prod": ["n1", "n2", "n3"], "prod_dc1": ["n1", "n2"], "prod_dc2": ["n3"]}
    lookup = "{{ lookup('community.cassandra.cassandra_hosts', banner=true) }}"
    assert render(lookup, groups=groups, hostvars=hostvars, the_dc="eu") == \
        "CLUSTER prod (2 nodes + 1 marked absent, dc1, eu) | chosen by: the only cluster in the inventory"
    assert render(lookup, groups=groups, hostvars={}, cassandra_hosts="prod_dc1") == \
        "CLUSTER prod_dc1 (2 nodes, dc1) | chosen by: -e cassandra_hosts"
    hostvars["n2"]["cassandra_dc"] = trust_as_template("{{ ansible_facts.nope }}")  # only the node knows it
    assert render(lookup, groups=groups, hostvars=hostvars, ansible_facts={}) == \
        "CLUSTER prod (2 nodes + 1 marked absent, dc1, dc?) | chosen by: the only cluster in the inventory"
    monkeypatch.setenv(ENV, "prod")
    assert render(lookup, groups=groups, hostvars={}).endswith("| chosen by: CASSANDRA_CLUSTER (environment)")


def test_env_cluster_name_only_the_nodes_know():
    """Every host's name a fact of the node: said so, and to give the group."""
    with pytest.raises(ValueError, match=r"no host with that cassandra_cluster_name \(4 hosts have one only the node knows:"
                                         r" give the group instead\)"):
        cluster_group(None, TWO, "Cluster A", lambda h: UNKNOWN)


class _Loader(object):
    """What load_extra_vars reads the -e variables with (memoized by ansible-core, here a stub)."""


@pytest.mark.parametrize("extra, given, inventory, refused", [
    ({"cassandra_hosts": "prod"}, "prod", False, None),
    ({"cassandra_hosts": "prod"}, "prod", True, None),  # -e beats the inventory's: the plays see it
    ({}, None, False, None),
    ({}, "prod", False, None),  # a playbook's vars (import_playbook ... vars:): the plays' hosts see them
    ({}, "prod", True, "cassandra_hosts is set in the inventory (prod, group_vars or host_vars): the plays' hosts do"
                       " not see it. Give it on the command line, -e cassandra_hosts=prod, or CASSANDRA_CLUSTER=prod in"
                       " your shell."),
    ({"cassandra_hosts": ""}, "", False, "Give the cluster's group, -e cassandra_hosts=<group>, or leave -e cassandra_hosts out"),
])
def test_cassandra_hosts_not_from_the_inventory(monkeypatch, extra, given, inventory, refused):
    """Under ansible-playbook: a cassandra_hosts of the inventory (group_vars/all) would name the cluster to the tasks,
    not to the plays' hosts (they never see it); an empty -e one would leave the cluster unsaid."""
    from ansible import context
    from ansible_collections.community.cassandra.plugins.lookup import cassandra_hosts
    monkeypatch.setattr(context, "CLIARGS", {"args": ["site.yml"]})
    monkeypatch.setattr(cassandra_hosts, "load_extra_vars", lambda loader: extra)
    assert cassandra_hosts.not_given(given, _Loader(), inventory) == refused
    monkeypatch.setattr(context, "CLIARGS", {})  # no command line (the Python API, these tests): not checked
    assert cassandra_hosts.not_given(given, _Loader(), inventory) is None
