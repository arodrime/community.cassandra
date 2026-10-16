from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# The operation playbooks check first, on localhost, that the group named by
# cassandra_hosts is in the inventory (a typo like -e cassandra_host=...
# otherwise ends in a raw template error). Rendered by Ansible.

import os

import pytest
import yaml

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

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


CHECK = next(t for t in load("roles", "cassandra_service", "tasks", "hosts_check.yml")
             if t["name"] == "The cluster's group is in the inventory")
SAY = next(t for t in load("roles", "cassandra_service", "tasks", "hosts_check.yml")
           if t["name"] == "Say which group is the cluster's")
GROUPS = {"all": ["n1", "n2"], "ungrouped": [], "prod": ["n1", "n2"], "empty": []}


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
                   "Its groups: empty, prod\n"
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


def test_the_refusal_is_a_verdict_and_the_group_line_is_the_default_callbacks():
    # ops callback: the refusal's message alone (marked), no "Cluster group ..." line on every run (not marked)
    assert CHECK["vars"]["cassandra_output"] is True and CHECK["run_once"] is True  # not "localhost: ..."
    assert "success_msg" not in CHECK["ansible.builtin.assert"]
    assert "cassandra_output" not in SAY.get("vars", {})


def test_default_is_the_cluster_group():
    # no -e cassandra_hosts: the inventory's only cluster group
    assert check()[0] is True
    assert render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=GROUPS) == "prod"


def test_shows_the_group_and_where_it_comes_from(monkeypatch):
    def shown(**variables):
        return render(SAY["ansible.builtin.debug"]["msg"], **variables)

    assert shown(groups=GROUPS) == "Cluster group prod (the inventory's cluster group)"
    assert shown(groups=GROUPS, cassandra_hosts="prod") == "Cluster group prod (cassandra_hosts)"
    monkeypatch.setenv("CASSANDRA_CLUSTER", "Prod")
    hostvars = {"n1": {"cassandra_cluster_name": "Prod"}, "n2": {"cassandra_cluster_name": "Prod"}}
    groups = dict(GROUPS, prod_dc1=["n1", "n2"])
    assert shown(groups=groups, hostvars=hostvars) == "Cluster group prod (CASSANDRA_CLUSTER=Prod)"


def test_typo_refused_by_the_lookup_too():
    """Under --limit this play does not run: the lookup in the plays' hosts refuses the group."""
    with pytest.raises(AnsibleError, match=r"REFUSED  cassandra_hosts=prd: no group of that name in the inventory\n\nIts groups: empty, prod\n"):
        render("{{ lookup('community.cassandra.cassandra_hosts') }}", groups=GROUPS, cassandra_hosts="prd")


def test_empty_group():
    passed, msg = check(cassandra_hosts="empty")
    assert passed is False and msg.startswith("REFUSED  cassandra_hosts=empty: that group has no host\n")
    assert msg.endswith("\nAdd its hosts to the inventory, or name the cluster's group: -e cassandra_hosts=<group>\n"
                        "Nothing was changed.")


@pytest.mark.parametrize("playbook", ["preflight.yml", "health_check.yml", "start_rack.yml", "status.yml"])
def test_first_play_checks_the_group(playbook):
    first = load("playbooks", playbook)[0]
    assert first["hosts"] == "localhost"
    assert first["tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml"


def test_every_playbook_on_the_group_checks_it():
    # each playbook that runs on cassandra_hosts starts with the check or imports preflight first
    for name in sorted(os.listdir(os.path.join(TOP, "playbooks"))):
        plays = load("playbooks", name)
        if "cassandra_hosts" not in str(plays) or name == "import_cluster.yml":
            continue
        first = plays[0]
        assert first.get("ansible.builtin.import_playbook") == "community.cassandra.preflight" \
            or first["tasks"][0]["ansible.builtin.include_role"]["tasks_from"] == "hosts_check.yml", name
