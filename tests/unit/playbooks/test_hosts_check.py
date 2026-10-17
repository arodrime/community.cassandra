from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The operation playbooks check first, on localhost, that the cluster is named
# (-e cassandra_hosts, CASSANDRA_CLUSTER: the same one) or the inventory's only
# one, the group in the inventory (a typo like -e cassandra_host=... otherwise
# ends in a raw template error), then say which it is. Rendered by Ansible.

import os
import re

import pytest
import yaml

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ansible_collections.community.cassandra.plugins.lookup.cassandra_hosts import RUNTIME

try:  # ansible-core 2.19+ renders trusted templates only
    from ansible.template import trust_as_template
except ImportError:
    def trust_as_template(template):
        return template

TOP = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def load(*path):
    with open(os.path.join(TOP, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template, **variables):
    variables = dict((k, trust_as_template(v) if isinstance(v, str) else v) for k, v in variables.items())
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(template))


CHECK = next(t for t in load("roles", "cassandra_operation", "tasks", "hosts_check.yml")
             if t["name"] == "The cluster is named, or the inventory holds one")
SAY = next(t for t in load("roles", "cassandra_operation", "tasks", "cluster_banner.yml")
           if t["name"] == "Say which cluster the run is on")
GROUPS = {"all": ["n1", "n2"], "ungrouped": [], "prod": ["n1", "n2"], "prod_dc1": ["n1", "n2"], "empty": []}


@pytest.fixture(autouse=True)
def no_cluster_env(monkeypatch):
    monkeypatch.delenv("CASSANDRA_CLUSTER", raising=False)


def check(**variables):
    variables = dict(CHECK["vars"], **dict(dict(groups=GROUPS), **variables))
    passed = render("{{ %s }}" % CHECK["ansible.builtin.assert"]["that"], **variables)
    return passed, render(CHECK["ansible.builtin.assert"]["fail_msg"], **variables)


def test_existing_group_passes():
    assert check(cassandra_hosts="prod")[0] is True


def test_missing_group_named_with_the_groups():
    passed, msg = check(cassandra_hosts="prd")
    assert passed is False
    assert msg == ("REFUSED  cassandra_hosts=prd: no group of that name in the inventory\n\n"
                   "Its groups: empty, prod, prod_dc1\n"
                   "Name the cluster's group: -e cassandra_hosts=<group>\n"
                   "Nothing was changed.")


def test_several_clusters_refused_with_the_way_out():
    """The lookup's refusal is the message: a play's hosts calling it would fail with a raw error."""
    groups = {"all": ["o1", "b1"], "ungrouped": [], "orders": ["o1"], "orders_dc1": ["o1"],
              "billing": ["b1"], "billing_dc1": ["b1"]}
    passed, msg = check(groups=groups)
    assert passed is False
    assert msg == ("REFUSED  no cluster named: this inventory has 2 clusters: billing, orders\n\n"
                   "Name the one to run on:\n"
                   "  export CASSANDRA_CLUSTER=<cluster>\n"
                   "or add -e cassandra_hosts=<cluster> to the command.\n"
                   "Nothing was changed.")
    assert check(groups=groups, cassandra_hosts="")[1].startswith(
        "REFUSED  no cluster named (-e cassandra_hosts is empty): this inventory has 2 clusters")
    assert check(groups=groups, cassandra_hosts="orders")[0] is True


def test_the_check_runs_under_tags():
    assert "always" in CHECK["tags"]


def test_the_refusal_and_the_cluster_line_are_operator_messages():
    # ops callback: the refusal's message alone, the cluster line on every run, once, under --tags too
    assert CHECK["vars"]["cassandra_output"] is True and CHECK["run_once"] is True  # not "localhost: ..."
    assert "success_msg" not in CHECK["ansible.builtin.assert"]
    assert SAY["vars"]["cassandra_output"] is True and SAY["run_once"] is True
    assert "always" in CHECK["tags"] and "always" in SAY["tags"]
    tasks = load("roles", "cassandra_operation", "tasks", "hosts_check.yml")
    names = [t["name"] for t in tasks]
    # the variables checked first (a -e cassandra_host=... typo is pointed out before the refusal it leads to)
    assert names.index("Check cassandra_target_nodes") < names.index("Variables close to the playbooks' own") \
        < names.index(CHECK["name"]) < names.index("Say which cluster the run is on")


def test_default_is_the_cluster_group():
    # no -e cassandra_hosts: the inventory's only cluster group
    assert check()[0] is True
    assert render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=GROUPS) == "prod"


def test_several_clusters_refused():
    passed, msg = check(groups=dict(GROUPS, other=["n3"], other_dc1=["n3"]))
    assert passed is False
    assert msg == ("REFUSED  no cluster named: this inventory has 2 clusters: other, prod\n\n"
                   "Name the one to run on:\n  export CASSANDRA_CLUSTER=<cluster>\n"
                   "or add -e cassandra_hosts=<cluster> to the command.\nNothing was changed.")


def test_given_and_env_disagree_refused(monkeypatch):
    monkeypatch.setenv("CASSANDRA_CLUSTER", "other")
    passed, msg = check(groups=dict(GROUPS, other=["n3"]), cassandra_hosts="prod")
    assert passed is False
    assert msg.startswith("REFUSED  -e cassandra_hosts=prod and CASSANDRA_CLUSTER=other (from your shell) disagree")
    assert msg.endswith("Nothing was changed.")


def test_shows_the_cluster_and_how_it_was_picked(monkeypatch):
    def shown(**variables):
        return render(SAY["ansible.builtin.debug"]["msg"], **variables)

    assert shown(groups=GROUPS) == "CLUSTER prod (2 nodes, dc1) | chosen by: the only cluster in the inventory"
    assert shown(groups=GROUPS, cassandra_hosts="prod") == "CLUSTER prod (2 nodes, dc1) | chosen by: -e cassandra_hosts"
    monkeypatch.setenv("CASSANDRA_CLUSTER", "Prod")
    hostvars = {"n1": {"cassandra_cluster_name": "Prod"}, "n2": {"cassandra_cluster_name": "Prod"}}
    groups = dict(GROUPS, prod_dc1=["n1", "n2"])
    assert shown(groups=groups, hostvars=hostvars) == "CLUSTER prod (2 nodes, dc1) | chosen by: CASSANDRA_CLUSTER (environment)"


def test_the_cluster_line_once_per_run():
    """A playbook importing another (topology, its add_node and decommission_node) says it once."""
    when = SAY["when"]
    assert render("{{ %s }}" % when, _group="prod", hostvars={"localhost": {}}) is True
    assert render("{{ %s }}" % when, _group="prod", hostvars={"localhost": {"_cassandra_cluster_shown": "prod"}}) is False
    assert render("{{ %s }}" % when, _group="dev", hostvars={"localhost": {"_cassandra_cluster_shown": "prod"}}) is True
    mark = next(t for t in load("roles", "cassandra_operation", "tasks", "cluster_banner.yml") if t["name"] == "Say it once per run")
    assert mark["ansible.builtin.set_fact"] == {"_cassandra_cluster_shown": "{{ _group }}"}
    assert mark["vars"]["_group"] == SAY["vars"]["_group"]
    assert mark["delegate_to"] == "localhost" and mark["delegate_facts"] is True and mark["run_once"] is True


def test_typo_refused_by_the_lookup_too():
    """Under --limit this play does not run: the lookup in the plays' hosts refuses the group."""
    with pytest.raises(AnsibleError, match=r"REFUSED  cassandra_hosts=prd: no group of that name in the inventory\n\nIts groups: empty, prod, prod_dc1\n"):
        render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=GROUPS, cassandra_hosts="prd")


def test_empty_group():
    passed, msg = check(cassandra_hosts="empty")
    assert passed is False and msg.startswith("REFUSED  cassandra_hosts=empty: that group has no host\n")
    assert msg.endswith("\nAdd its hosts to the inventory, or name the cluster's group: -e cassandra_hosts=<group>\n"
                        "Nothing was changed.")


@pytest.mark.parametrize("playbook", ["preflight.yml", "health_check.yml", "status.yml"])
def test_first_play_checks_the_group(playbook):
    first = load("playbooks", playbook)[0]
    assert first["hosts"] == "localhost"
    assert first["tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml"


def test_every_playbook_on_the_group_checks_it():
    """Every operation playbook, read-only ones included, picks its cluster by the same rule: its first play is the
    check (or preflight's, which starts with it); none names a group of its own (no cassandra_hosts default)."""
    names = sorted(n for n in os.listdir(os.path.join(TOP, "playbooks")) if n.endswith(".yml"))
    assert len(names) >= 3
    for name in names:
        plays = load("playbooks", name)
        text = str(plays)
        assert not re.search(r"cassandra_hosts\s*\|\s*default\(\s*['\"]cassandra['\"]", text), name
        assert all(p.get("hosts") not in ("cassandra", "all") for p in plays), name
        first = plays[0]
        assert first.get("ansible.builtin.import_playbook") == "community.cassandra.preflight" \
            or first["tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml", name
        # the nodes' plays take their hosts from the same lookups (or what the run gives)
        for play in plays[1:]:
            hosts = str(play.get("hosts", ""))
            if hosts and "import_playbook" not in str(play.keys()):
                # a lookup, cassandra_target_nodes, a group the playbooks make: never a group of a fixed name
                assert re.search(r"community\.cassandra\.cassandra_(hosts|nodes)|cassandra_target_nodes", hosts) \
                    or any(RUNTIME.match(group) for group in re.findall(r"cassandra_\w+", hosts)), (name, play["name"], hosts)
                assert not re.search(r"['\"](cassandra|all|ungrouped)['\"]", hosts), (name, play["name"], hosts)


def _checking():
    """The playbooks whose first play is the check itself (the others import preflight, one of them)."""
    names = sorted(n for n in os.listdir(os.path.join(TOP, "playbooks")) if n.endswith(".yml"))
    return [n for n in names if load("playbooks", n)[0].get("hosts") == "localhost"
            and "hosts_check.yml" in str(load("playbooks", n)[0].get("tasks"))]


def test_the_checking_playbooks():
    assert _checking() == ["health_check.yml", "preflight.yml", "status.yml"]


@pytest.mark.parametrize("playbook", _checking())
def test_the_nodes_first_play_says_the_cluster_under_limit(playbook):
    """--limit skips the localhost play: the first play on the nodes says the cluster (once per run)."""
    second = load("playbooks", playbook)[1]
    includes = [t.get("ansible.builtin.include_role", {}).get("tasks_from") for t in second["tasks"]]
    assert "cluster_banner.yml" in includes
    # after the variables checks, before anything is read on the nodes
    assert includes.index("cluster_banner.yml") <= 3
